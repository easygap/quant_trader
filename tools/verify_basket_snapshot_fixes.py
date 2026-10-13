"""Offline execution verification using a COPY of the 2026-10-12 paper ledger.

Usage: python tools/verify_basket_snapshot_fixes.py --source-db <snapshot.db>
       --prices-dir <dated CSVs> --out-dir <new verification directory>
Never uses the source DB as the runtime ledger, and prohibits network access.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--prices-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    source = args.source_db.resolve(strict=True)
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    target = output / "verification.db"
    if source == target:
        raise ValueError("Source must not be the runtime DB")
    digest_before = hashlib.sha256(source.read_bytes()).hexdigest()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dest:
        src.backup(dest)
    os.environ.update(QUANT_DB_PATH=str(target), QUANT_BACKUP_PATH=str(output / "backups"),
                      QUANT_OVERLAY_STATE_DIR=str(output / "overlays"),
                      QUANT_DASHBOARD_STATE_PATH=str(output / "dashboard.json"),
                      QUANT_RUNTIME_LOCK_DIR=str(output / "locks"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pandas as pd
    from loguru import logger
    from config.config_loader import Config
    from core.basket_close import finalize_basket_closes
    from core.basket_evaluation import collect_basket_paper_evaluation
    from core.basket_rebalancer import BasketRebalancer, RebalanceOrder
    from core.basket_observability import read_basket_diagnostics
    from core.data_collector import DataCollector
    from core.risk_overlays import OverlayDecision
    from database.models import init_database, get_engine, get_session, TradeHistory
    from database.repositories import get_all_positions, record_cash_flow, get_portfolio_valuations

    logger.remove()
    logger.add(output / "execution.log", encoding="utf-8")
    cfg = Config.get()
    cfg.settings["trading"]["mode"] = "paper"
    init_database()
    assert Path(get_engine().url.database).resolve() == target
    frames = {}
    for path in args.prices_dir.glob("*.csv"):
        frame = pd.read_csv(path)
        frame.columns = [str(c).lower() for c in frame.columns]
        frames[path.stem] = frame

    def fetch(self, symbol, start_date=None, end_date=None):
        frame = frames[symbol].copy()
        days = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        lower = str(start_date)[:10] if start_date else "0001-01-01"
        upper = str(end_date)[:10] if end_date else "9999-12-31"
        return frame.loc[(days >= lower) & (days <= upper)].copy()

    def make(name):
        rb = BasketRebalancer(name, config=cfg)
        rb.overlay_decision = lambda: OverlayDecision(scale=1.0) if name == "kr_pocket" else None
        snapshot = {s: {"price": float(frames[s].iloc[-1]["close"]), "avg_volume": float(frames[s].volume.tail(20).mean()),
                        "quote_date": "2026-10-12", "source": "frozen_csv", "observed_at": "2026-10-12T10:10:00"}
                    for s in rb.holdings if s in frames}
        rb._market_snapshot = snapshot
        rb._fetch_market_snapshot = lambda: snapshot
        return rb

    def count():
        with get_session() as s:
            return s.query(TradeHistory).count()

    results = {}
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Offline verification: network forbidden")))
        stack.enter_context(patch("core.notifier.Notifier"))
        stack.enter_context(patch.object(DataCollector, "fetch_korean_stock", fetch))
        stack.enter_context(patch.object(DataCollector, "get_last_source_info", return_value={"source": "frozen_csv"}))
        stack.enter_context(patch("core.data_collector.SECTOR_MAP_CACHE_PATH", output / "sector_map.json"))
        pocket = make("kr_pocket")
        orders = pocket.plan_rebalance()
        assert orders == []
        assert {d["code"] for d in pocket._plan_diagnostics} == {"MIN_LOT"}
        results["pocket_before_deposit"] = {"plan": pocket._plan_diagnostics, "allocation": pocket.diagnose_deployment()["allocation"]}
        before = count()
        for hour, minute in [(8, 49), (17, 43)]:
            with patch("core.trading_hours._now_kst", return_value=datetime(2026, 10, 13, hour, minute)):
                for action in ("BUY", "SELL"):
                    outcome = pocket.execute([RebalanceOrder("357870", action, 1, 58195)])
                    assert outcome["blocked"] and not outcome["executed"]
        assert count() == before
        results["off_session"] = {"blocked_orders": 4, "new_fills": count() - before}

        hold = make("kr_diversified_hold")
        with patch("core.trading_hours._now_kst", return_value=datetime(2026, 10, 13, 10, 10)):
            outcome = hold.execute([RebalanceOrder("005380", "BUY", 1, hold._market_snapshot["005380"]["price"])])
        assert outcome["executed"] == 0 and outcome["failed"] == 1
        assert "MDD" in outcome["details"][0]["reason"]
        result, _ = collect_basket_paper_evaluation(config=cfg, basket_name="kr_diversified_hold", include_benchmark=False)
        assert result["entry_status"]["status"] == "BLOCKED" and result["verdict"] != "PASS_CANDIDATE"
        results["mdd_guard"] = {"execution": outcome, "entry_status": result["entry_status"], "verdict": result["verdict"]}

        # Replay dated marks through the real snapshot -> close ledger path.
        # These are controlled fixture prices, not recovered intraday quotes.
        class ReplayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 12, 10, 20, tzinfo=tz)
        with patch("core.basket_rebalancer.datetime", ReplayClock):
            assert pocket.save_daily_nav_snapshot()
        original_nav = pocket.portfolio_mgr.get_portfolio_summary(
            current_prices={s: q["price"] for s, q in pocket._market_snapshot.items()})["total_value"]
        assert finalize_basket_closes(pocket, now=datetime(2026, 10, 13, 10)) == ["2026-10-12"]
        close = get_portfolio_valuations(pocket.account_key, kind="close")[-1]
        assert close["total_value"] == original_nav == 380092
        results["close_valuation_copy_only"] = {"date": close["date"], "total_value": close["total_value"],
                                                "source": "controlled frozen CSV replay"}

        # Hypothetical deposit is written to this disposable copy only. The full
        # planner and paper executor must be capable of buying one equity ETF.
        record_cash_flow(100000, account_key=pocket.account_key, note="COPY ONLY: verification")
        orders = pocket.plan_rebalance()
        assert [(o.symbol, o.action, o.quantity) for o in orders] == [("069500", "BUY", 1)]
        with patch("core.trading_hours._now_kst", return_value=datetime(2026, 10, 13, 10, 10)):
            outcome = pocket.execute(orders)
        assert outcome["executed"] == 1, outcome
        assert next(p.quantity for p in get_all_positions(pocket.account_key, mode="paper") if p.symbol == "069500") == 2
        assert pocket.save_daily_nav_snapshot()
        diag = read_basket_diagnostics(pocket.basket, pocket.account_key, cfg.risk_params)
        assert diag["allocation"]["status"] == "OK"
        assert diag["measurement"]["kind"] == "unverified"  # Oct12 marks on Oct13
        results["deposit_copy_only"] = {"execution": outcome, "allocation": diag["allocation"]}
        results["valuation_rows"] = len(get_portfolio_valuations(pocket.account_key))
        assert finalize_basket_closes(pocket, now=datetime(2026, 10, 13, 16)) == []
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest_before
    results["source_unchanged"] = True
    (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verified": list(results), "report": str(output / "result.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
