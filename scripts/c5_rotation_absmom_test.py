"""
C-5 Rotation 종목 절대모멘텀 필터 전/후 비교
- 필터 A: ret_120d(T-1) > 0
- 필터 B: ret_60d(T-1) > 0 AND ret_120d(T-1) > 0
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
- Rotation 단독 (10M, max_pos=2)
- market_filter_sma200 = OFF
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


def run_rotation(period_name, abs_momentum_filter="none"):
    """Run rotation backtest with abs momentum filter variant."""
    start, end = PERIODS[period_name]
    config = Config.get()

    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["abs_momentum_filter"] = abs_momentum_filter
    rot_params["market_filter_sma200"] = False  # 시장 필터 OFF

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
        rot_params["abs_momentum_filter"] = "none"


def calc_metrics(equity_df, trades, initial_capital):
    """Calculate standard metrics from equity curve and trades."""
    if equity_df is None or equity_df.empty:
        return {
            "total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0,
            "total_trades": 0, "win_rate": 0, "avg_positions": 0,
            "days_in_market": 0, "total_days": 0, "avg_pnl_rate": 0,
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
    avg_pnl_rate = (
        sum(t.get("pnl_rate", 0) for t in sell) / total_trades if total_trades else 0
    )

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
        "avg_pnl_rate": round(avg_pnl_rate, 2),
    }


if __name__ == "__main__":
    print("=" * 80)
    print("  C-5 Rotation 종목 절대모멘텀 필터 비교")
    print("  A: ret_120d(T-1) > 0")
    print("  B: ret_60d(T-1) > 0 AND ret_120d(T-1) > 0")
    print(f"  유니버스: {SYMBOLS}")
    print(f"  DEV: {PERIODS['DEV'][0]} ~ {PERIODS['DEV'][1]}")
    print(f"  OOS: {PERIODS['OOS'][0]} ~ {PERIODS['OOS'][1]}")
    print("=" * 80)

    results = {}
    for period in ["DEV", "OOS"]:
        for filt in ["none", "A", "B"]:
            label = f"{period}_{filt}"
            tag = {"none": "필터OFF", "A": "필터A", "B": "필터B"}[filt]
            print(f"\n  [{label}] {period} {tag} 실행 중...")
            r = run_rotation(period, abs_momentum_filter=filt)
            m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            results[label] = m

            sell_trades = [t for t in r["trades"] if t["action"] != "BUY"]
            buy_trades = [t for t in r["trades"] if t["action"] == "BUY"]
            print(f"    BUY: {len(buy_trades)}건, SELL: {len(sell_trades)}건")
            if sell_trades:
                wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
                avg_pnl = sum(t.get("pnl_rate", 0) for t in sell_trades) / len(sell_trades)
                print(f"    승률: {wins}/{len(sell_trades)} ({wins/len(sell_trades)*100:.1f}%), "
                      f"평균 수익률: {avg_pnl:.2f}%")

    # ── 비교표 출력 ──
    print("\n" + "=" * 80)
    print("  Rotation 단독: 절대모멘텀 필터 비교")
    print("=" * 80)

    labels = [
        ("total_return (%)", "total_return"),
        ("CAGR (%)", "cagr"),
        ("MDD (%)", "mdd"),
        ("Sharpe", "sharpe"),
        ("total_trades", "total_trades"),
        ("win_rate (%)", "win_rate"),
        ("avg_pnl_rate (%)", "avg_pnl_rate"),
        ("avg_positions", "avg_positions"),
        ("days_in_market", "days_in_market"),
    ]

    for period in ["DEV", "OOS"]:
        base = results[f"{period}_none"]
        fa = results[f"{period}_A"]
        fb = results[f"{period}_B"]

        print(f"\n  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"  {'지표':<22} {'필터OFF':>10} {'A':>10} {'B':>10} {'A변화':>10} {'B변화':>10}")
        print(f"  {'-' * 22} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}")
        for label_name, key in labels:
            vb = base.get(key, 0)
            va = fa.get(key, 0)
            vbb = fb.get(key, 0)
            da = va - vb
            db = vbb - vb
            def fmt(v):
                return f"{v:>10.2f}" if isinstance(v, float) else f"{v:>10}"
            def fmtd(v):
                return f"{v:>+10.2f}" if isinstance(v, float) else f"{v:>+10}"
            print(f"  {label_name:<22} {fmt(vb)} {fmt(va)} {fmt(vbb)} {fmtd(da)} {fmtd(db)}")

    # ── 판정 ──
    dev_base = results["DEV_none"]
    dev_a = results["DEV_A"]
    dev_b = results["DEV_B"]
    oos_base = results["OOS_none"]
    oos_a = results["OOS_A"]
    oos_b = results["OOS_B"]

    def judge(tag, dev, oos, dev_base_m, oos_base_m):
        dev_ret_ok = dev["total_return"] > dev_base_m["total_return"]
        dev_mdd_ok = dev["mdd"] > -6.0  # MDD > -6%
        dev_mdd_imp = dev["mdd"] - dev_base_m["mdd"]  # 양수면 개선
        oos_ret_ok = oos["total_return"] >= 0
        oos_sharpe_ok = oos["sharpe"] >= oos_base_m["sharpe"] - 0.15
        trade_reduction = 1 - (dev["total_trades"] / max(dev_base_m["total_trades"], 1))
        quality_ok = dev["win_rate"] > dev_base_m["win_rate"] or dev["avg_pnl_rate"] > dev_base_m["avg_pnl_rate"]

        print(f"\n  [{tag}] 성공 기준 점검:")
        print(f"    DEV return 개선: {dev_base_m['total_return']:.2f}% → {dev['total_return']:.2f}% → "
              f"{'PASS' if dev_ret_ok else 'FAIL'}")
        print(f"    DEV MDD > -6%: {dev['mdd']:.2f}% → "
              f"{'PASS' if dev_mdd_ok else 'FAIL'}")
        print(f"    DEV MDD 개선: {dev_mdd_imp:+.2f}%p")
        print(f"    OOS return ≥ 0: {oos['total_return']:.2f}% → "
              f"{'PASS' if oos_ret_ok else 'FAIL'}")
        print(f"    OOS Sharpe 훼손 ≤ 0.15: {oos_base_m['sharpe']:.2f} → {oos['sharpe']:.2f} → "
              f"{'PASS' if oos_sharpe_ok else 'FAIL'}")
        print(f"    거래 수 감소: {trade_reduction:.1%}")
        print(f"    거래 질 개선 (승률/평균수익률): {'YES' if quality_ok else 'NO'}")

        if trade_reduction > 0.7:
            return "FILTER_TOO_RESTRICTIVE"
        elif dev_ret_ok and dev_mdd_ok and oos_ret_ok and quality_ok:
            return "ABS_MOM_FILTER_IMPROVES"
        elif dev_ret_ok and dev_mdd_imp >= 1.0:
            return "ABS_MOM_FILTER_IMPROVES"
        else:
            return "NO_MEANINGFUL_IMPROVEMENT"

    print(f"\n{'=' * 80}")
    verdict_a = judge("A", dev_a, oos_a, dev_base, oos_base)
    verdict_b = judge("B", dev_b, oos_b, dev_base, oos_base)

    # 최종 판정
    if verdict_a == "ABS_MOM_FILTER_IMPROVES" or verdict_b == "ABS_MOM_FILTER_IMPROVES":
        if verdict_a == "ABS_MOM_FILTER_IMPROVES" and verdict_b == "ABS_MOM_FILTER_IMPROVES":
            # 둘 다 통과 시 더 나은 쪽 선택
            a_score = dev_a["total_return"] + oos_a["total_return"] + dev_a["mdd"]
            b_score = dev_b["total_return"] + oos_b["total_return"] + dev_b["mdd"]
            best = "A" if a_score >= b_score else "B"
            final_verdict = "ABS_MOM_FILTER_IMPROVES"
        elif verdict_a == "ABS_MOM_FILTER_IMPROVES":
            best = "A"
            final_verdict = "ABS_MOM_FILTER_IMPROVES"
        else:
            best = "B"
            final_verdict = "ABS_MOM_FILTER_IMPROVES"
    elif verdict_a == "FILTER_TOO_RESTRICTIVE" and verdict_b == "FILTER_TOO_RESTRICTIVE":
        best = "none"
        final_verdict = "FILTER_TOO_RESTRICTIVE"
    else:
        best = "none"
        final_verdict = "NO_MEANINGFUL_IMPROVEMENT"

    print(f"\n{'=' * 80}")
    print(f"  최종 판정: {final_verdict}")
    if best != "none":
        print(f"  채택 필터: {best}")
    print(f"{'=' * 80}")

    if final_verdict == "ABS_MOM_FILTER_IMPROVES":
        dev_best = results[f"DEV_{best}"]
        oos_best = results[f"OOS_{best}"]
        print(f"\n  판정 사유:")
        print(f"    필터 {best}: DEV return {dev_base['total_return']:.2f}% → {dev_best['total_return']:.2f}%")
        print(f"    필터 {best}: DEV MDD {dev_base['mdd']:.2f}% → {dev_best['mdd']:.2f}%")
        print(f"    필터 {best}: OOS return {oos_best['total_return']:.2f}%")
        print(f"    필터 {best}: OOS Sharpe {oos_best['sharpe']:.2f}")
        print(f"\n  다음 액션: BV/Rotation 비중 스윕 재수행 (필터 {best} 적용 상태)")
    elif final_verdict == "FILTER_TOO_RESTRICTIVE":
        print(f"\n  판정 사유: 두 필터 모두 거래 수를 70% 이상 줄여 평가 불가.")
        print(f"\n  다음 액션: Rotation 전략 보류, BV 단독 유지 검토")
    else:
        # 둘 다 실패
        print(f"\n  판정 사유:")
        print(f"    A: {verdict_a} — DEV ret {dev_a['total_return']:.2f}%, MDD {dev_a['mdd']:.2f}%")
        print(f"    B: {verdict_b} — DEV ret {dev_b['total_return']:.2f}%, MDD {dev_b['mdd']:.2f}%")
        print(f"\n  Rotation 전략 보류 판정:")
        # OOS에서 양수 return이면 아직 가치가 있음
        if oos_base["total_return"] > 0:
            print(f"    OOS 기간 Rotation 단독 return {oos_base['total_return']:.2f}% > 0")
            print(f"    → 상승장에서는 기여. 하락장 방어가 핵심 과제.")
            print(f"    → composite threshold(C안) 또는 exit 규칙 강화 검토 후 최종 판단")
            print(f"\n  다음 액션: composite threshold 필터(C안) 구현 — composite > 0.03 ~ 0.05 스윕")
        else:
            print(f"    OOS 기간에서도 음수 — Rotation 전략 보류, BV 단독 유지 권고")
            print(f"\n  다음 액션: Rotation 전략 보류. BV 단독 체제로 sleeve 제거.")

    print(f"\n{'=' * 80}")
    print(f"  비교 완료")
    print(f"{'=' * 80}")
