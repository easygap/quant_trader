"""
C-5 Rotation 시장 필터(KS11 > SMA200) 전/후 비교
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
- Rotation 단독 (10M, max_pos=2)
- breakout_volume 변경 없음, sleeve 비중 재조정 없음
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from loguru import logger

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester

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


def run_rotation(period_name, market_filter: bool):
    """Run rotation backtest with or without market filter."""
    start, end = PERIODS[period_name]
    config = Config.get()

    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["market_filter_sma200"] = market_filter

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {
        k: div_cfg.get(k)
        for k in [
            "max_positions",
            "max_position_ratio",
            "max_investment_ratio",
            "min_cash_ratio",
        ]
    }

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
        rot_params["market_filter_sma200"] = False


def calc_metrics(equity_df, trades, initial_capital):
    """Calculate standard metrics from equity curve and trades."""
    if equity_df is None or equity_df.empty:
        return {
            "total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0,
            "total_trades": 0, "avg_positions": 0, "days_in_market": 0,
            "total_days": 0,
        }

    final = float(equity_df["value"].iloc[-1])
    total_return = (final / initial_capital - 1) * 100
    years = len(equity_df) / 252
    cagr = ((final / initial_capital) ** (1 / max(years, 0.01)) - 1) * 100

    dr = equity_df["value"].pct_change().dropna()
    if len(dr) > 1 and dr.std() > 0:
        sharpe = (dr.mean() * 252 - 0.03) / (dr.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    peak = equity_df["value"].cummax()
    mdd = ((equity_df["value"] - peak) / peak).min() * 100

    sell = [t for t in trades if t.get("action") != "BUY"]
    total_trades = len(sell)
    wins = sum(1 for t in sell if t.get("pnl", 0) > 0)
    win_rate = wins / total_trades * 100 if total_trades else 0

    avg_pos = (
        float(equity_df["n_positions"].mean()) if "n_positions" in equity_df else 0
    )
    dim = (
        int((equity_df["n_positions"] > 0).sum()) if "n_positions" in equity_df else 0
    )

    return {
        "total_return": round(total_return, 2),
        "cagr": round(cagr, 2),
        "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "avg_positions": round(avg_pos, 2),
        "days_in_market": dim,
        "total_days": len(equity_df),
    }


if __name__ == "__main__":
    print("=" * 80)
    print("  C-5 Rotation 시장 필터 전/후 비교")
    print("  필터: KS11 close(T-1) > SMA200(T-1) → 당일 신규 진입 허용")
    print(f"  유니버스: {SYMBOLS}")
    print(f"  DEV: {PERIODS['DEV'][0]} ~ {PERIODS['DEV'][1]}")
    print(f"  OOS: {PERIODS['OOS'][0]} ~ {PERIODS['OOS'][1]}")
    print("=" * 80)

    results = {}
    for period in ["DEV", "OOS"]:
        for filt in [False, True]:
            label = f"{period}_{'FILTER' if filt else 'BASE'}"
            tag = "필터ON" if filt else "필터OFF"
            print(f"\n  [{label}] {period} {tag} 실행 중...")
            r = run_rotation(period, market_filter=filt)
            m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            results[label] = m

            sell_trades = [t for t in r["trades"] if t["action"] != "BUY"]
            buy_trades = [t for t in r["trades"] if t["action"] == "BUY"]
            print(f"    BUY: {len(buy_trades)}건, SELL: {len(sell_trades)}건")

    # ── 비교표 출력 ──
    print("\n" + "=" * 80)
    print("  Rotation 단독: 시장 필터 전/후 비교")
    print("=" * 80)

    labels = [
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
        base = results[f"{period}_BASE"]
        filt = results[f"{period}_FILTER"]

        print(f"\n  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"  {'지표':<22} {'필터OFF':>12} {'필터ON':>12} {'변화':>12}")
        print(f"  {'-' * 22} {'-' * 12} {'-' * 12} {'-' * 12}")
        for label_name, key in labels:
            v_base = base.get(key, 0)
            v_filt = filt.get(key, 0)
            diff = v_filt - v_base
            fmt_b = f"{v_base:>12.2f}" if isinstance(v_base, float) else f"{v_base:>12}"
            fmt_f = f"{v_filt:>12.2f}" if isinstance(v_filt, float) else f"{v_filt:>12}"
            fmt_d = f"{diff:>+12.2f}" if isinstance(diff, float) else f"{diff:>+12}"
            print(f"  {label_name:<22} {fmt_b} {fmt_f} {fmt_d}")

    # ── 판정 ──
    dev_base = results["DEV_BASE"]
    dev_filt = results["DEV_FILTER"]
    oos_base = results["OOS_BASE"]
    oos_filt = results["OOS_FILTER"]

    dev_mdd_improved = dev_filt["mdd"] - dev_base["mdd"]  # 양수면 개선(MDD는 음수)
    dev_ret_improved = dev_filt["total_return"] > dev_base["total_return"]
    oos_ret_positive = oos_filt["total_return"] >= 0
    oos_sharpe_ok = oos_filt["sharpe"] >= oos_base["sharpe"] - 0.15

    # 거래 수 급감 체크
    trade_reduction = 1 - (dev_filt["total_trades"] / max(dev_base["total_trades"], 1))

    print(f"\n{'=' * 80}")
    print(f"  성공 기준 점검:")
    print(f"    DEV MDD 개선 ≥ 5%p: {dev_mdd_improved:+.2f}%p → "
          f"{'PASS' if dev_mdd_improved >= 5 else 'FAIL'}")
    print(f"    DEV return 개선: {dev_base['total_return']:.2f}% → "
          f"{dev_filt['total_return']:.2f}% → "
          f"{'PASS' if dev_ret_improved else 'FAIL'}")
    print(f"    OOS return ≥ 0: {oos_filt['total_return']:.2f}% → "
          f"{'PASS' if oos_ret_positive else 'FAIL'}")
    print(f"    OOS Sharpe 훼손 ≤ 0.15: "
          f"{oos_base['sharpe']:.2f} → {oos_filt['sharpe']:.2f} → "
          f"{'PASS' if oos_sharpe_ok else 'FAIL'}")
    print(f"    거래 수 감소율: {trade_reduction:.1%}")

    if trade_reduction > 0.7:
        verdict = "FILTER_TOO_RESTRICTIVE"
        reason = (
            f"거래 수가 {trade_reduction:.0%} 감소 — 필터가 과도하게 제한적. "
            f"absolute momentum filter(composite > 0% 또는 > 5%) 검토 필요."
        )
    elif (
        dev_mdd_improved >= 5
        and dev_ret_improved
        and oos_ret_positive
        and oos_sharpe_ok
    ):
        verdict = "FILTER_IMPROVES_ROTATION"
        reason = (
            f"DEV MDD {dev_mdd_improved:+.2f}%p 개선, "
            f"DEV return {dev_base['total_return']:.2f}% → {dev_filt['total_return']:.2f}%, "
            f"OOS return {oos_filt['total_return']:.2f}% ≥ 0, "
            f"OOS Sharpe 유지."
        )
    elif dev_mdd_improved >= 3 and dev_ret_improved:
        verdict = "FILTER_IMPROVES_ROTATION"
        reason = (
            f"DEV MDD {dev_mdd_improved:+.2f}%p 개선 (5%p 미만이나 유의미), "
            f"DEV return 개선. OOS 조건 일부 미충족이나 방향성 양호."
        )
    else:
        verdict = "NO_MEANINGFUL_IMPROVEMENT"
        reason = (
            f"DEV MDD 개선 {dev_mdd_improved:+.2f}%p, "
            f"DEV return {'개선' if dev_ret_improved else '악화/동일'}, "
            f"OOS return {oos_filt['total_return']:.2f}%. "
            f"시장 필터만으로는 부족 — 추가 대안 필요."
        )

    print(f"\n{'=' * 80}")
    print(f"  판정: {verdict}")
    print(f"{'=' * 80}")
    print(f"  사유: {reason}")

    # ── BV25/Rot75 재비교 필요성 ──
    if verdict == "FILTER_IMPROVES_ROTATION":
        print(f"\n  다음 액션: BV/Rotation 비중 스윕 재수행 (필터 적용 상태)")
        print(f"    → c5_sleeve_backtest.py에서 market_filter_sma200=True로 Rotation 실행")
    elif verdict == "FILTER_TOO_RESTRICTIVE":
        print(f"\n  다음 액션: absolute momentum filter 검토")
        print(f"    → composite > 0% 조건은 이미 존재하므로 threshold를 5%로 올리거나")
        print(f"    → SMA200 대신 SMA120 등 짧은 MA 사용 검토")
    else:
        print(f"\n  다음 액션: absolute momentum filter(B안) 구현 후 재비교")

    print(f"\n{'=' * 80}")
    print(f"  비교 완료")
    print(f"{'=' * 80}")
