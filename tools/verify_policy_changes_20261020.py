"""2026-10-20 규칙 변경을 운영 장부 사본과 고정 시세로 실제 주문 경로에 태워 확인한다.

확인하는 것
1. 대형주 계좌: 시장 대비 손절은 지금 보유 종목을 팔지 않는다. 계좌 낙폭 18%대에서도
   선언 비중(60%)까지 보충 매수가 체결되고, 체결 뒤 주식 비중은 60% + 밴드 3%p 안이다.
   재매수 차단 중인 종목(손절 후 60일)은 사지 않는다. 평가의 진입 상태가 낙폭으로 막히지 않는다.
2. 적립 계좌: 지금(추세 필터로 주식 목표 23.75%)은 주문이 없고 이유가 남는다. 추세가 복귀해
   목표가 47.5%로 돌아가면 CD를 팔아 주식 ETF를 산다. 적립금 10만 원은 매도 없이 산다.

원본 DB는 읽기 전용으로 복사만 하고 해시로 불변을 확인한다. 시세는 처음 한 번 받아
--out-dir/prices에 저장하고, 주문 경로는 네트워크를 막은 채 그 파일만 쓴다.

실행: python tools/verify_policy_changes_20261020.py --source-db data/quant_trader.db --out-dir output/policy_20261020
      (이미 받은 시세로 다시 돌리려면 --prices-dir <이전 out-dir>/prices)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

HOLD = "kr_diversified_hold"
POCKET = "kr_pocket"


def _download_prices(symbols, start, end, out_dir: Path):
    import FinanceDataReader as fdr

    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        frame = fdr.DataReader(symbol, start, end)
        frame = frame.rename(columns={c: c.lower() for c in frame.columns})
        frame.index.name = "date"
        frame.reset_index().to_csv(out_dir / f"{symbol}.csv", index=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--prices-dir", type=Path)
    parser.add_argument("--as-of", default="2026-10-20", help="시세 기준일(이 날짜 종가까지 사용)")
    args = parser.parse_args()

    source = args.source_db.resolve(strict=True)
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    target = output / "verification.db"
    digest_before = hashlib.sha256(source.read_bytes()).hexdigest()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(target) as dest:
        src.backup(dest)

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import yaml

    baskets = (yaml.safe_load((root / "config" / "baskets.yaml").read_text(encoding="utf-8")) or {})["baskets"]
    symbols = sorted(set(baskets[HOLD]["holdings"]) | set(baskets[POCKET]["holdings"]) | {"069500"})
    prices_dir = args.prices_dir.resolve() if args.prices_dir else output / "prices"
    if not args.prices_dir:
        _download_prices(symbols, "2026-05-01", args.as_of, prices_dir)

    # 업종 비중 검사는 운영과 같은 업종 맵 사본으로 한다(없으면 fail-closed로 매수가 막힌다).
    sector_map = output / "sector_map.json"
    sector_map.write_bytes((root / "reports" / "sector_map_cache.json").read_bytes())
    os.environ.update(
        QUANT_DB_PATH=str(target), QUANT_BACKUP_PATH=str(output / "backups"),
        QUANT_OVERLAY_STATE_DIR=str(output / "overlays"),
        QUANT_DASHBOARD_STATE_PATH=str(output / "dashboard.json"),
        QUANT_RUNTIME_LOCK_DIR=str(output / "locks"),
    )
    import pandas as pd
    from loguru import logger
    from config.config_loader import Config
    from core.basket_evaluation import collect_basket_paper_evaluation
    from core.basket_rebalancer import BasketRebalancer
    from core.data_collector import DataCollector
    from core.risk_overlays import OverlayDecision
    from database.models import TradeHistory, get_engine, get_session, init_database
    from database.repositories import get_all_positions, record_cash_flow

    logger.remove()
    logger.add(output / "execution.log", encoding="utf-8")
    cfg = Config.get()
    cfg.settings["trading"]["mode"] = "paper"
    init_database()
    assert Path(get_engine().url.database).resolve() == target

    frames = {}
    for path in prices_dir.glob("*.csv"):
        frame = pd.read_csv(path)
        frame.columns = [str(c).lower() for c in frame.columns]
        frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None)
        frames[path.stem] = frame[frame["date"] <= pd.Timestamp(args.as_of)].reset_index(drop=True)
    as_of = pd.Timestamp(args.as_of)
    session_now = datetime(as_of.year, as_of.month, as_of.day, 10, 10)

    def fetch(self, symbol, start_date=None, end_date=None):
        frame = frames[symbol].copy()
        days = frame["date"].dt.strftime("%Y-%m-%d")
        lower = str(start_date)[:10] if start_date else "0001-01-01"
        upper = str(end_date)[:10] if end_date else "9999-12-31"
        return frame.loc[(days >= lower) & (days <= upper)].copy()

    def make(name, scale=None):
        rb = BasketRebalancer(name, config=cfg)
        if scale is not None:
            rb.overlay_decision = lambda: OverlayDecision(scale=scale)
        snapshot = {
            s: {"price": float(frames[s].iloc[-1]["close"]), "avg_volume": float(frames[s]["volume"].tail(20).mean()),
                "quote_date": str(frames[s].iloc[-1]["date"].date()), "source": "frozen_csv",
                "observed_at": session_now.isoformat()}
            for s in rb.holdings
        }
        rb._market_snapshot = snapshot
        rb._fetch_market_snapshot = lambda: snapshot
        return rb

    def fills():
        with get_session() as s:
            return s.query(TradeHistory).count()

    def exposure(rb):
        prices = {s: q["price"] for s, q in rb._market_snapshot.items()}
        summary = rb.portfolio_mgr.get_portfolio_summary(current_prices=prices)
        held = sum(prices.get(p.symbol, p.avg_price) * p.quantity
                   for p in get_all_positions(rb.account_key, mode="paper") if p.symbol in rb.holdings)
        return held / float(summary["total_value"]), float(summary["total_value"])

    results = {"as_of": args.as_of, "prices": {s: float(f.iloc[-1]["close"]) for s, f in frames.items()}}
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        stack.enter_context(patch("core.notifier.Notifier"))
        stack.enter_context(patch.object(DataCollector, "fetch_korean_stock", fetch))
        stack.enter_context(patch.object(DataCollector, "get_last_source_info", return_value={"source": "frozen_csv"}))
        stack.enter_context(patch("core.data_collector.SECTOR_MAP_CACHE_PATH", output / "sector_map.json"))
        stack.enter_context(patch("core.trading_hours._now_kst", return_value=session_now))
        # 실적일 확인은 yfinance 조회가 필요해 네트워크를 막은 이 검증에서는 '근처 아님'으로 둔다.
        # 실제 운영에서는 그대로 검사한다(10월은 실적 시즌이라 일부 보충 매수가 며칠 보류될 수 있다).
        stack.enter_context(patch("core.earnings_filter.is_near_earnings", return_value=(False, "")))
        results["note"] = "실적일 필터는 이 검증에서 제외(네트워크 차단). 운영에서는 그대로 적용된다."

        # 1) 대형주 계좌
        hold = make(HOLD)
        exits = hold.plan_risk_exits()
        assert exits == [], f"시장 대비 기준으로는 손절 대상이 없어야 한다: {exits}"
        before_w, total = exposure(hold)
        orders = hold.plan_rebalance()
        cooldown = {"005930", "005380", "012330"}
        assert orders and all(o.action == "BUY" for o in orders), orders
        assert not cooldown & {o.symbol for o in orders}, "재매수 차단 종목을 샀다"
        n0 = fills()
        outcome = hold.execute(orders)
        after_w, _ = exposure(hold)
        assert outcome["executed"] == len(orders) and outcome["failed"] == 0, outcome
        assert fills() - n0 == len(orders)
        assert after_w <= 0.60 + 0.03 + 1e-9, after_w
        evaluation, _ = collect_basket_paper_evaluation(config=cfg, basket_name=HOLD, include_benchmark=False)
        assert evaluation["entry_status"]["status"] != "BLOCKED", evaluation["entry_status"]
        results["hold"] = {
            "risk_exits": [], "stock_weight_before": round(before_w, 4), "stock_weight_after": round(after_w, 4),
            "orders": [repr(o) for o in orders], "execution": outcome,
            "entry_status": evaluation["entry_status"], "verdict": evaluation.get("verdict"),
        }

        # 2) 적립 계좌 — 지금 상태(추세 필터 배수 0.5)
        pocket = make(POCKET, scale=0.5)
        assert pocket.plan_rebalance() == []
        held_reasons = [d for d in pocket._plan_diagnostics if d["code"] in ("INTEGER_HOLD", "MIN_LOT")]
        results["pocket_now"] = {"orders": [], "diagnostics": pocket._plan_diagnostics,
                                 "allocation": pocket.diagnose_deployment()["allocation"]}
        assert held_reasons, "주문이 없을 때 이유가 남아야 한다"

        # 추세가 복귀했다고 가정(배수 1.0) — 실행하지 않고 계획만 본다
        restored = make(POCKET, scale=1.0)
        plan_restored = restored.plan_rebalance()
        assert [(o.action, o.symbol) for o in plan_restored] == [("SELL", "357870"), ("BUY", "069500")], plan_restored
        results["pocket_trend_restored_plan"] = [repr(o) for o in plan_restored]

        # 사본에만 적립금 10만 원을 넣고 실제 실행 경로까지
        record_cash_flow(100_000, account_key=pocket.account_key, note="COPY ONLY: verification 2026-10-20")
        deposit = make(POCKET, scale=0.5)
        orders = deposit.plan_rebalance()
        assert orders and all(o.action == "BUY" for o in orders), orders
        n0 = fills()
        outcome = deposit.execute(orders)
        assert outcome["executed"] == len(orders) and outcome["failed"] == 0, outcome
        results["pocket_deposit_copy_only"] = {
            "orders": [repr(o) for o in orders], "execution": outcome, "new_fills": fills() - n0,
            "allocation_after": deposit.diagnose_deployment()["allocation"],
        }

    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest_before
    results["source_unchanged"] = True
    (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verified": [k for k in results if k not in ("as_of", "prices")],
                      "report": str(output / "result.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
