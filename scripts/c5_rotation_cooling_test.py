"""
C-5 Rotation 냉각기(min_hold_days) 전/후 비교
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
- Rotation 단독 (10M, max_pos=2)
- 냉각기 OFF(0) vs ON(5) 비교
- holding bucket / exit reason / MFE·MAE 진단 포함
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


def run_rotation(period_name, min_hold_days=0):
    start, end = PERIODS[period_name]
    config = Config.get()

    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["min_hold_days"] = min_hold_days
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
        rot_params["min_hold_days"] = 0


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
                    "entry_score": buy.get("entry_score", 0),
                    "notional": buy["price"] * buy["quantity"],
                })
    return paired


def compute_mfe_mae(paired, price_data):
    import pandas as pd
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


def print_bucket(paired, label):
    buckets = [("1~5일", 1, 5), ("6~10일", 6, 10), ("11~20일", 11, 20), ("21~30일", 21, 30), ("31일+", 31, 9999)]
    print(f"\n  [{label}] Holding Bucket")
    print(f"  {'bucket':<10} {'trades':>7} {'win%':>7} {'avg_pnl%':>10} {'total_pnl':>12} {'avg_MAE%':>9} {'avg_MFE%':>9}")
    print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*10} {'-'*12} {'-'*9} {'-'*9}")
    for name, lo, hi in buckets:
        b = [t for t in paired if lo <= t["holding_days"] <= hi]
        if not b:
            print(f"  {name:<10} {'0':>7}")
            continue
        n = len(b)
        w = sum(1 for t in b if t["pnl"] > 0)
        print(f"  {name:<10} {n:>7} {w/n*100:>7.1f} {sum(t['pnl_rate'] for t in b)/n:>10.2f} "
              f"{sum(t['pnl'] for t in b):>12,.0f} {sum(t['mae'] for t in b)/n:>9.2f} {sum(t['mfe'] for t in b)/n:>9.2f}")


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


def print_mfe_mae(paired, label):
    losses = [t for t in paired if t["pnl"] <= 0]
    wins = [t for t in paired if t["pnl"] > 0]
    print(f"\n  [{label}] MFE/MAE")
    if losses:
        print(f"    손실({len(losses)}건): avg_MAE={sum(t['mae'] for t in losses)/len(losses):.2f}% "
              f"avg_MFE={sum(t['mfe'] for t in losses)/len(losses):.2f}% "
              f"avg_realized={sum(t['pnl_rate'] for t in losses)/len(losses):.2f}%")
    if wins:
        print(f"    수익({len(wins)}건): avg_MFE={sum(t['mfe'] for t in wins)/len(wins):.2f}% "
              f"avg_realized={sum(t['pnl_rate'] for t in wins)/len(wins):.2f}% "
              f"capture={sum(t['pnl_rate'] for t in wins)/sum(t['mfe'] for t in wins)*100:.0f}%")


if __name__ == "__main__":
    print("=" * 100)
    print("  C-5 Rotation 냉각기(min_hold_days=5) 전/후 비교")
    print("=" * 100)

    # 가격 데이터 로드 (MFE/MAE용)
    collector = DataCollector()
    price_data = {}
    for sym in SYMBOLS:
        df = collector.fetch_stock(sym, "2020-06-01", "2025-12-31")
        if df is not None and not df.empty:
            price_data[sym] = df

    all_results = {}

    for period in ["DEV", "OOS"]:
        for mhd in [0, 5]:
            label = f"{period}_MHD{mhd}"
            tag = "냉각기OFF" if mhd == 0 else "냉각기ON(5d)"
            print(f"\n  [{label}] {period} {tag} 실행 중...")
            r = run_rotation(period, min_hold_days=mhd)
            m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            paired = pair_trades(r["trades"])
            compute_mfe_mae(paired, price_data)
            all_results[label] = {"metrics": m, "paired": paired, "result": r}

            sell_trades = [t for t in r["trades"] if t["action"] != "BUY"]
            buy_trades = [t for t in r["trades"] if t["action"] == "BUY"]
            print(f"    BUY: {len(buy_trades)}건, SELL: {len(sell_trades)}건")

    # ── 메트릭 비교표 ──
    print(f"\n{'='*100}")
    print(f"  메트릭 비교")
    print(f"{'='*100}")

    metric_labels = [
        ("total_return (%)", "total_return"),
        ("CAGR (%)", "cagr"),
        ("MDD (%)", "mdd"),
        ("Sharpe", "sharpe"),
        ("total_trades", "total_trades"),
        ("win_rate (%)", "win_rate"),
        ("avg_positions", "avg_positions"),
        ("days_in_market", "days_in_market"),
    ]

    for period in ["DEV", "OOS"]:
        base = all_results[f"{period}_MHD0"]["metrics"]
        cool = all_results[f"{period}_MHD5"]["metrics"]
        print(f"\n  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"  {'지표':<22} {'냉각OFF':>10} {'냉각ON':>10} {'변화':>10}")
        print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*10}")
        for lbl, key in metric_labels:
            vb = base.get(key, 0)
            vc = cool.get(key, 0)
            d = vc - vb
            def fmt(v):
                return f"{v:>10.2f}" if isinstance(v, float) else f"{v:>10}"
            def fmtd(v):
                return f"{v:>+10.2f}" if isinstance(v, float) else f"{v:>+10}"
            print(f"  {lbl:<22} {fmt(vb)} {fmt(vc)} {fmtd(d)}")

    # ── Bucket / Exit / MFE·MAE 상세 ──
    for period in ["DEV", "OOS"]:
        print(f"\n{'='*100}")
        print(f"  [{period}] 상세 진단")
        print(f"{'='*100}")

        for mhd in [0, 5]:
            label = f"{period}_MHD{mhd}"
            tag = "냉각OFF" if mhd == 0 else "냉각ON"
            paired = all_results[label]["paired"]
            print_bucket(paired, f"{period} {tag}")
            print_exit_reasons(paired, f"{period} {tag}")
            print_mfe_mae(paired, f"{period} {tag}")

    # ── 판정 ──
    dev_b = all_results["DEV_MHD0"]["metrics"]
    dev_c = all_results["DEV_MHD5"]["metrics"]
    oos_b = all_results["OOS_MHD0"]["metrics"]
    oos_c = all_results["OOS_MHD5"]["metrics"]

    dev_ret_ok = dev_c["total_return"] > dev_b["total_return"]
    dev_mdd_ok = dev_c["mdd"] > dev_b["mdd"]  # MDD is negative, higher = better
    oos_ret_ok = oos_c["total_return"] >= oos_b["total_return"] - 2.0  # 2%p 이내 허용
    oos_mdd_ok = oos_c["mdd"] >= oos_b["mdd"] - 2.0

    # 1~5일 bucket 개선 체크
    dev_base_short = [t for t in all_results["DEV_MHD0"]["paired"] if 1 <= t["holding_days"] <= 5]
    dev_cool_short = [t for t in all_results["DEV_MHD5"]["paired"] if 1 <= t["holding_days"] <= 5]
    base_short_pnl = sum(t["pnl"] for t in dev_base_short)
    cool_short_pnl = sum(t["pnl"] for t in dev_cool_short)
    bucket_improved = cool_short_pnl > base_short_pnl

    print(f"\n{'='*100}")
    print(f"  성공 기준 점검")
    print(f"{'='*100}")
    print(f"    DEV return 개선: {dev_b['total_return']:.2f}% → {dev_c['total_return']:.2f}% → "
          f"{'PASS' if dev_ret_ok else 'FAIL'}")
    print(f"    DEV MDD 개선: {dev_b['mdd']:.2f}% → {dev_c['mdd']:.2f}% → "
          f"{'PASS' if dev_mdd_ok else 'FAIL'}")
    print(f"    DEV 1~5일 bucket PnL: {base_short_pnl:,.0f} → {cool_short_pnl:,.0f} → "
          f"{'PASS' if bucket_improved else 'FAIL'}")
    print(f"    OOS return 유지(±2%p): {oos_b['total_return']:.2f}% → {oos_c['total_return']:.2f}% → "
          f"{'PASS' if oos_ret_ok else 'FAIL'}")
    print(f"    OOS MDD 유지(±2%p): {oos_b['mdd']:.2f}% → {oos_c['mdd']:.2f}% → "
          f"{'PASS' if oos_mdd_ok else 'FAIL'}")

    if dev_ret_ok and dev_mdd_ok and oos_ret_ok and bucket_improved:
        verdict = "COOLING_OFF_IMPROVES"
    elif dev_ret_ok and bucket_improved:
        verdict = "COOLING_OFF_IMPROVES"
    elif not dev_ret_ok and not bucket_improved:
        verdict = "NO_MEANINGFUL_IMPROVEMENT"
    else:
        verdict = "MIXED_RESULTS"

    # 손실 거래 MFE 비교
    dev_base_loss = [t for t in all_results["DEV_MHD0"]["paired"] if t["pnl"] <= 0]
    dev_cool_loss = [t for t in all_results["DEV_MHD5"]["paired"] if t["pnl"] <= 0]
    base_loss_mfe = sum(t["mfe"] for t in dev_base_loss) / len(dev_base_loss) if dev_base_loss else 0
    cool_loss_mfe = sum(t["mfe"] for t in dev_cool_loss) / len(dev_cool_loss) if dev_cool_loss else 0
    print(f"    DEV 손실거래 avg_MFE: {base_loss_mfe:.2f}% → {cool_loss_mfe:.2f}%")

    print(f"\n{'='*100}")
    print(f"  판정: {verdict}")
    print(f"{'='*100}")

    if verdict == "COOLING_OFF_IMPROVES":
        print(f"  사유: DEV return/MDD 개선, 1~5일 bucket 손실 해소.")
        print(f"  다음 액션: BV/Rotation 비중 스윕 재수행 (냉각기 적용 상태)")
    elif verdict == "MIXED_RESULTS":
        print(f"  사유: 일부 개선이나 모든 기준 충족 못함.")
        print(f"  다음 액션: min_hold_days 값 조정(3~7일) 또는 sizing 강화 검토")
    else:
        print(f"  사유: 냉각기로도 손실 구조 미개선.")
        loss_pnl = sum(t["pnl"] for t in dev_cool_loss)
        ts_pnl = sum(t["pnl"] for t in all_results["DEV_MHD5"]["paired"] if t["exit_reason"] == "TRAILING_STOP")
        if abs(ts_pnl) > abs(loss_pnl) * 0.5:
            print(f"  → TRAILING_STOP 여전히 손실 주도({ts_pnl:,.0f}) — trailing stop 파라미터 조정 또는 비활성화 검토")
        else:
            print(f"  → exit가 아닌 sizing 강화(volatility-target sizing)로 전환 검토")

    print(f"\n{'='*100}")
    print(f"  비교 완료")
    print(f"{'='*100}")
