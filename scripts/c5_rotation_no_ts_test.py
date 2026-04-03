"""
C-5 Rotation trailing stop 제거 전/후 비교
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
- Rotation 단독 (10M, max_pos=2)
- disable_trailing_stop OFF/ON 비교
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from loguru import logger

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester
from core.data_collector import DataCollector

logger.remove()
logger.add(sys.stderr, level="WARNING")

SYMBOLS = ["005930", "000660", "035720", "051910"]
INITIAL_CAPITAL = 10_000_000

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}

PERIODS = {
    "DEV": ("2021-01-01", "2023-12-31"),
    "OOS": ("2024-01-01", "2025-12-31"),
}


def run_rotation(period_name, disable_ts=False):
    start, end = PERIODS[period_name]
    config = Config.get()

    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["disable_trailing_stop"] = disable_ts
    rot_params["min_hold_days"] = 0
    rot_params["abs_momentum_filter"] = "none"
    rot_params["market_filter_sma200"] = False

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt = PortfolioBacktester(config)
        return pbt.run(
            symbols=SYMBOLS,
            strategy_name="relative_strength_rotation",
            initial_capital=INITIAL_CAPITAL,
            start_date=start,
            end_date=end,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v


def calc_metrics(equity_df, trades, initial_capital):
    if equity_df is None or equity_df.empty:
        return {"total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0,
                "total_trades": 0, "win_rate": 0, "avg_positions": 0, "days_in_market": 0}
    final = float(equity_df["value"].iloc[-1])
    total_return = (final / initial_capital - 1) * 100
    years = len(equity_df) / 252
    cagr = ((final / initial_capital) ** (1 / max(years, 0.01)) - 1) * 100
    dr = equity_df["value"].pct_change().dropna()
    sharpe = (dr.mean() * 252 - 0.03) / (dr.std() * np.sqrt(252)) if len(dr) > 1 and dr.std() > 0 else 0.0
    peak = equity_df["value"].cummax()
    mdd = ((equity_df["value"] - peak) / peak).min() * 100
    sell = [t for t in trades if t.get("action") != "BUY"]
    total_trades = len(sell)
    wins = sum(1 for t in sell if t.get("pnl", 0) > 0)
    win_rate = wins / total_trades * 100 if total_trades else 0
    avg_pos = float(equity_df["n_positions"].mean()) if "n_positions" in equity_df else 0
    dim = int((equity_df["n_positions"] > 0).sum()) if "n_positions" in equity_df else 0
    return {
        "total_return": round(total_return, 2), "cagr": round(cagr, 2),
        "mdd": round(mdd, 2), "sharpe": round(sharpe, 2),
        "total_trades": total_trades, "win_rate": round(win_rate, 1),
        "avg_positions": round(avg_pos, 2), "days_in_market": dim,
    }


def pair_trades(trades):
    buys = {}
    paired = []
    for t in trades:
        if t["action"] == "BUY":
            buys[t["symbol"]] = t
        else:
            buy = buys.pop(t["symbol"], None)
            if buy:
                paired.append({
                    "symbol": t["symbol"],
                    "entry_date": buy["date"], "exit_date": t["date"],
                    "entry_price": buy["price"], "exit_price": t["price"],
                    "quantity": buy["quantity"],
                    "pnl": t["pnl"], "pnl_rate": t["pnl_rate"],
                    "holding_days": t.get("holding_days", 0),
                    "exit_reason": t["action"],
                    "notional": buy["price"] * buy["quantity"],
                })
    return paired


def compute_mfe_mae(paired, price_data):
    for t in paired:
        df = price_data.get(t["symbol"])
        if df is None or t["entry_price"] <= 0:
            t["mfe"] = t["mae"] = 0
            continue
        mask = (df.index >= t["entry_date"]) & (df.index <= t["exit_date"])
        w = df.loc[mask]
        if w.empty:
            t["mfe"] = t["mae"] = 0
            continue
        highs = w["high"].astype(float) if "high" in w else w["close"].astype(float)
        lows = w["low"].astype(float) if "low" in w else w["close"].astype(float)
        t["mfe"] = round((highs.max() / t["entry_price"] - 1) * 100, 2)
        t["mae"] = round((lows.min() / t["entry_price"] - 1) * 100, 2)


def print_exit_reasons(paired, label):
    reasons = {}
    for t in paired:
        reasons.setdefault(t["exit_reason"], []).append(t)
    print(f"\n  [{label}] Exit Reason")
    print(f"  {'reason':<16} {'trades':>7} {'win%':>7} {'avg_pnl%':>10} {'total_pnl':>12} {'avg_hold':>9}")
    print(f"  {'-'*16} {'-'*7} {'-'*7} {'-'*10} {'-'*12} {'-'*9}")
    for r in sorted(reasons.keys()):
        b = reasons[r]
        n = len(b)
        w = sum(1 for t in b if t["pnl"] > 0)
        print(f"  {r:<16} {n:>7} {w/n*100:>7.1f} {sum(t['pnl_rate'] for t in b)/n:>10.2f} "
              f"{sum(t['pnl'] for t in b):>12,.0f} {sum(t['holding_days'] for t in b)/n:>9.1f}")


def print_bucket(paired, label):
    buckets = [("1~5일", 1, 5), ("6~10일", 6, 10), ("11~20일", 11, 20), ("21~30일", 21, 30), ("31일+", 31, 9999)]
    print(f"\n  [{label}] Holding Bucket")
    print(f"  {'bucket':<10} {'trades':>7} {'win%':>7} {'avg_pnl%':>10} {'total_pnl':>12}")
    print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*10} {'-'*12}")
    for name, lo, hi in buckets:
        b = [t for t in paired if lo <= t["holding_days"] <= hi]
        if not b:
            print(f"  {name:<10} {'0':>7}")
            continue
        n = len(b)
        w = sum(1 for t in b if t["pnl"] > 0)
        print(f"  {name:<10} {n:>7} {w/n*100:>7.1f} {sum(t['pnl_rate'] for t in b)/n:>10.2f} "
              f"{sum(t['pnl'] for t in b):>12,.0f}")


def print_mfe_mae(paired, label):
    losses = [t for t in paired if t["pnl"] <= 0]
    wins = [t for t in paired if t["pnl"] > 0]
    print(f"\n  [{label}] MFE/MAE")
    if losses:
        avg_mae = sum(t["mae"] for t in losses) / len(losses)
        avg_mfe = sum(t["mfe"] for t in losses) / len(losses)
        avg_r = sum(t["pnl_rate"] for t in losses) / len(losses)
        print(f"    손실({len(losses)}건): avg_MAE={avg_mae:.2f}% avg_MFE={avg_mfe:.2f}% avg_realized={avg_r:.2f}%")
    if wins:
        avg_mfe = sum(t["mfe"] for t in wins) / len(wins)
        avg_r = sum(t["pnl_rate"] for t in wins) / len(wins)
        cap = avg_r / avg_mfe * 100 if avg_mfe > 0 else 0
        print(f"    수익({len(wins)}건): avg_MFE={avg_mfe:.2f}% avg_realized={avg_r:.2f}% capture={cap:.0f}%")


if __name__ == "__main__":
    print("=" * 100)
    print("  C-5 Rotation Trailing Stop 제거 전/후 비교")
    print("=" * 100)

    collector = DataCollector()
    price_data = {}
    for sym in SYMBOLS:
        df = collector.fetch_stock(sym, "2020-06-01", "2025-12-31")
        if df is not None and not df.empty:
            price_data[sym] = df

    all_results = {}

    for period in ["DEV", "OOS"]:
        for dts in [False, True]:
            label = f"{period}_{'noTS' if dts else 'base'}"
            tag = "TS제거" if dts else "TS유지"
            print(f"\n  [{label}] {period} {tag} 실행 중...")
            r = run_rotation(period, disable_ts=dts)
            m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            paired = pair_trades(r["trades"])
            compute_mfe_mae(paired, price_data)
            all_results[label] = {"metrics": m, "paired": paired}

            sell_trades = [t for t in r["trades"] if t["action"] != "BUY"]
            buy_trades = [t for t in r["trades"] if t["action"] == "BUY"]
            print(f"    BUY: {len(buy_trades)}건, SELL: {len(sell_trades)}건")

    # ── 메트릭 비교 ──
    print(f"\n{'='*100}")
    print(f"  메트릭 비교")
    print(f"{'='*100}")

    metric_labels = [
        ("total_return (%)", "total_return"), ("CAGR (%)", "cagr"),
        ("MDD (%)", "mdd"), ("Sharpe", "sharpe"),
        ("total_trades", "total_trades"), ("win_rate (%)", "win_rate"),
        ("avg_positions", "avg_positions"), ("days_in_market", "days_in_market"),
    ]

    for period in ["DEV", "OOS"]:
        base = all_results[f"{period}_base"]["metrics"]
        nots = all_results[f"{period}_noTS"]["metrics"]
        print(f"\n  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"  {'지표':<22} {'TS유지':>10} {'TS제거':>10} {'변화':>10}")
        print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*10}")
        for lbl, key in metric_labels:
            vb, vc = base.get(key, 0), nots.get(key, 0)
            d = vc - vb
            def fmt(v): return f"{v:>10.2f}" if isinstance(v, float) else f"{v:>10}"
            def fmtd(v): return f"{v:>+10.2f}" if isinstance(v, float) else f"{v:>+10}"
            print(f"  {lbl:<22} {fmt(vb)} {fmt(vc)} {fmtd(d)}")

    # ── 상세 진단 ──
    for period in ["DEV", "OOS"]:
        print(f"\n{'='*100}")
        print(f"  [{period}] 상세 진단")
        print(f"{'='*100}")
        for dts in [False, True]:
            label = f"{period}_{'noTS' if dts else 'base'}"
            tag = "TS유지" if not dts else "TS제거"
            paired = all_results[label]["paired"]
            print_exit_reasons(paired, f"{period} {tag}")
            print_bucket(paired, f"{period} {tag}")
            print_mfe_mae(paired, f"{period} {tag}")

    # ── 판정 ──
    dev_b = all_results["DEV_base"]["metrics"]
    dev_n = all_results["DEV_noTS"]["metrics"]
    oos_b = all_results["OOS_base"]["metrics"]
    oos_n = all_results["OOS_noTS"]["metrics"]

    dev_ret_ok = dev_n["total_return"] > dev_b["total_return"]
    dev_mdd_ok = dev_n["mdd"] > dev_b["mdd"]
    oos_ret_ok = oos_n["total_return"] >= oos_b["total_return"] - 2.0
    oos_sharpe_ok = oos_n["sharpe"] >= oos_b["sharpe"] - 0.15

    # TS exit → 어디로 이동했는지
    base_ts = [t for t in all_results["DEV_base"]["paired"] if t["exit_reason"] == "TRAILING_STOP"]
    nots_exits = {}
    for t in all_results["DEV_noTS"]["paired"]:
        nots_exits.setdefault(t["exit_reason"], []).append(t)

    print(f"\n{'='*100}")
    print(f"  성공 기준 점검")
    print(f"{'='*100}")
    print(f"    DEV return: {dev_b['total_return']:.2f}% → {dev_n['total_return']:.2f}% → "
          f"{'PASS' if dev_ret_ok else 'FAIL'}")
    print(f"    DEV MDD: {dev_b['mdd']:.2f}% → {dev_n['mdd']:.2f}% → "
          f"{'PASS' if dev_mdd_ok else 'FAIL'}")
    print(f"    OOS return(±2%p): {oos_b['total_return']:.2f}% → {oos_n['total_return']:.2f}% → "
          f"{'PASS' if oos_ret_ok else 'FAIL'}")
    print(f"    OOS Sharpe(±0.15): {oos_b['sharpe']:.2f} → {oos_n['sharpe']:.2f} → "
          f"{'PASS' if oos_sharpe_ok else 'FAIL'}")

    # 손실거래 MFE/MAE 비교
    dev_b_loss = [t for t in all_results["DEV_base"]["paired"] if t["pnl"] <= 0]
    dev_n_loss = [t for t in all_results["DEV_noTS"]["paired"] if t["pnl"] <= 0]
    b_mae = sum(t["mae"] for t in dev_b_loss) / len(dev_b_loss) if dev_b_loss else 0
    n_mae = sum(t["mae"] for t in dev_n_loss) / len(dev_n_loss) if dev_n_loss else 0
    b_r = sum(t["pnl_rate"] for t in dev_b_loss) / len(dev_b_loss) if dev_b_loss else 0
    n_r = sum(t["pnl_rate"] for t in dev_n_loss) / len(dev_n_loss) if dev_n_loss else 0
    print(f"    DEV 손실거래 avg_realized: {b_r:.2f}% → {n_r:.2f}%")
    print(f"    DEV 손실거래 avg_MAE: {b_mae:.2f}% → {n_mae:.2f}%")

    if dev_ret_ok and dev_mdd_ok and oos_ret_ok:
        verdict = "REMOVE_TS_IMPROVES"
    elif dev_ret_ok and oos_ret_ok:
        verdict = "REMOVE_TS_IMPROVES"
    elif not dev_ret_ok and not dev_mdd_ok:
        verdict = "TS_WAS_NOT_THE_PROBLEM"
    else:
        verdict = "MIXED_RESULT"

    print(f"\n{'='*100}")
    print(f"  판정: {verdict}")
    print(f"{'='*100}")

    if verdict == "REMOVE_TS_IMPROVES":
        print(f"  사유: TS 제거로 DEV return/MDD 개선, OOS 유지.")
        print(f"  BV25/Rot75 재검토 가치: YES")
        print(f"  다음 액션: take_profit rate 6% 실험 또는 BV/Rotation 비중 스윕 재수행")
    elif verdict == "TS_WAS_NOT_THE_PROBLEM":
        print(f"  사유: TS 제거에도 DEV return/MDD 미개선. 병목은 TS가 아님.")
        print(f"  BV25/Rot75 재검토 가치: NO")
        print(f"  다음 액션: sizing 강화(volatility-target) 또는 Rotation 전략 보류 검토")
    else:
        print(f"  사유: 일부만 개선.")
        bv_review = "YES" if dev_ret_ok else "NO"
        print(f"  BV25/Rot75 재검토 가치: {bv_review}")
        if dev_ret_ok:
            print(f"  다음 액션: take_profit rate 6% 추가 실험")
        else:
            print(f"  다음 액션: sizing 강화 또는 Rotation 보류")

    print(f"\n{'='*100}")
    print(f"  비교 완료")
    print(f"{'='*100}")
