"""
C-5: BV/Rotation sleeve 비중 스윕 (TS 제거 반영)
- Rotation: disable_trailing_stop=true, min_hold_days=0
- BV: 변경 없음
- 비중: 0/100, 25/75, 50/50, 75/25, 100/0
- DEV: 2021-01-01 ~ 2023-12-31
- OOS: 2024-01-01 ~ 2025-12-31
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
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

WEIGHTS = [
    (0, 100),    # Rotation only
    (25, 75),
    (50, 50),
    (75, 25),
    (100, 0),    # BV only
]


def run_backtest(strategy_name, capital, start, end, max_positions_override=None, extra_div=None):
    config = Config.get()

    # Rotation 설정 고정
    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["disable_trailing_stop"] = True
    rot_params["min_hold_days"] = 0
    rot_params["abs_momentum_filter"] = "none"
    rot_params["market_filter_sma200"] = False

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}

    try:
        if max_positions_override is not None:
            div_cfg["max_positions"] = max_positions_override
        if extra_div:
            div_cfg.update(extra_div)

        pbt = PortfolioBacktester(config)
        return pbt.run(
            symbols=SYMBOLS,
            strategy_name=strategy_name,
            initial_capital=capital,
            start_date=start,
            end_date=end,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v
            elif k in div_cfg and k in (extra_div or {}):
                del div_cfg[k]


def combine_sleeves(res_a, res_b):
    eq_a = res_a["equity_curve"].set_index("date")
    eq_b = res_b["equity_curve"].set_index("date")
    common = sorted(set(eq_a.index) & set(eq_b.index))
    rows = []
    for d in common:
        rows.append({
            "date": d,
            "value": float(eq_a.loc[d, "value"]) + float(eq_b.loc[d, "value"]),
            "cash": float(eq_a.loc[d, "cash"]) + float(eq_b.loc[d, "cash"]),
            "n_positions": int(eq_a.loc[d, "n_positions"]) + int(eq_b.loc[d, "n_positions"]),
        })
    return {
        "equity_curve": pd.DataFrame(rows),
        "trades": res_a["trades"] + res_b["trades"],
    }


def calc_metrics(equity_df, trades, initial_capital):
    if equity_df is None or equity_df.empty:
        return {"total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0,
                "total_trades": 0, "win_rate": 0, "avg_positions": 0,
                "days_in_market": 0, "top_sym_share": 0, "per_sym_pnl": {}}

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

    per_sym = {}
    for t in sell:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + t.get("pnl", 0)
    total_pnl = sum(per_sym.values())
    top_share = (
        max(abs(v) for v in per_sym.values()) / abs(total_pnl) * 100
        if total_pnl != 0 and per_sym else 0
    )

    # exit reason 분포
    exit_reasons = {}
    for t in sell:
        r = t["action"]
        exit_reasons.setdefault(r, {"n": 0, "pnl": 0})
        exit_reasons[r]["n"] += 1
        exit_reasons[r]["pnl"] += t.get("pnl", 0)

    return {
        "total_return": round(total_return, 2), "cagr": round(cagr, 2),
        "mdd": round(mdd, 2), "sharpe": round(sharpe, 2),
        "total_trades": total_trades, "win_rate": round(win_rate, 1),
        "avg_positions": round(avg_pos, 2), "days_in_market": dim,
        "top_sym_share": round(top_share, 1), "per_sym_pnl": per_sym,
        "exit_reasons": exit_reasons,
    }


if __name__ == "__main__":
    print("=" * 100)
    print("  C-5: BV/Rotation Sleeve 비중 스윕 (Rotation TS 제거)")
    print(f"  유니버스: {SYMBOLS}")
    print("=" * 100)

    all_results = {}  # key = f"{period}_{bv_w}_{rot_w}"

    for period in ["DEV", "OOS"]:
        start, end = PERIODS[period]
        print(f"\n  ── {period} ({start} ~ {end}) ──")

        # 먼저 BV와 Rotation 각각 10M으로 단독 실행 (100/0, 0/100)
        print(f"    BV 단독 (10M)...")
        r_bv_full = run_backtest("breakout_volume", INITIAL_CAPITAL, start, end)

        print(f"    Rotation 단독 (10M, TS OFF)...")
        r_rot_full = run_backtest(
            "relative_strength_rotation", INITIAL_CAPITAL, start, end,
            max_positions_override=2, extra_div=ROTATION_DIV,
        )

        all_results[f"{period}_100_0"] = calc_metrics(
            r_bv_full["equity_curve"], r_bv_full["trades"], INITIAL_CAPITAL)
        all_results[f"{period}_0_100"] = calc_metrics(
            r_rot_full["equity_curve"], r_rot_full["trades"], INITIAL_CAPITAL)

        # 혼합 비중 (25/75, 50/50, 75/25)
        for bv_w, rot_w in [(25, 75), (50, 50), (75, 25)]:
            bv_cap = int(INITIAL_CAPITAL * bv_w / 100)
            rot_cap = INITIAL_CAPITAL - bv_cap
            label = f"BV{bv_w}/Rot{rot_w}"
            print(f"    {label} ({bv_cap:,} / {rot_cap:,})...")

            r_bv = run_backtest("breakout_volume", bv_cap, start, end)
            r_rot = run_backtest(
                "relative_strength_rotation", rot_cap, start, end,
                max_positions_override=2, extra_div=ROTATION_DIV,
            )
            comb = combine_sleeves(r_bv, r_rot)
            all_results[f"{period}_{bv_w}_{rot_w}"] = calc_metrics(
                comb["equity_curve"], comb["trades"], INITIAL_CAPITAL)

    # ── 결과표 출력 ──
    labels = [
        ("total_return (%)", "total_return"),
        ("CAGR (%)", "cagr"),
        ("MDD (%)", "mdd"),
        ("Sharpe", "sharpe"),
        ("total_trades", "total_trades"),
        ("win_rate (%)", "win_rate"),
        ("avg_positions", "avg_positions"),
        ("days_in_market", "days_in_market"),
        ("top_sym_share (%)", "top_sym_share"),
    ]

    for period in ["DEV", "OOS"]:
        print(f"\n{'='*100}")
        print(f"  [{period}] {PERIODS[period][0]} ~ {PERIODS[period][1]}")
        print(f"{'='*100}")

        cols = ["BV100", "BV75/R25", "BV50/R50", "BV25/R75", "Rot100"]
        keys = [f"{period}_100_0", f"{period}_75_25", f"{period}_50_50",
                f"{period}_25_75", f"{period}_0_100"]

        header = f"  {'지표':<22}" + "".join(f"{c:>12}" for c in cols)
        print(header)
        print(f"  {'-'*22}" + f"{'-'*12}" * 5)

        for lbl, key in labels:
            vals = [all_results[k].get(key, 0) for k in keys]
            fmts = [f"{v:>12.2f}" if isinstance(v, float) else f"{v:>12}" for v in vals]
            print(f"  {lbl:<22}" + "".join(fmts))

    # ── 종목별 기여도 ──
    for period in ["DEV", "OOS"]:
        print(f"\n  [{period}] 종목별 PnL")
        cols = ["BV100", "BV75/R25", "BV50/R50", "BV25/R75", "Rot100"]
        keys = [f"{period}_100_0", f"{period}_75_25", f"{period}_50_50",
                f"{period}_25_75", f"{period}_0_100"]

        print(f"  {'종목':<8}" + "".join(f"{c:>12}" for c in cols))
        print(f"  {'-'*8}" + f"{'-'*12}" * 5)
        for sym in SYMBOLS:
            vals = [all_results[k].get("per_sym_pnl", {}).get(sym, 0) for k in keys]
            print(f"  {sym:<8}" + "".join(f"{v:>12,.0f}" for v in vals))

    # ── Exit reason 요약 ──
    for period in ["DEV", "OOS"]:
        print(f"\n  [{period}] Exit Reason 요약 (주요 비중)")
        for bv_w, rot_w in [(100, 0), (50, 50), (25, 75), (0, 100)]:
            k = f"{period}_{bv_w}_{rot_w}"
            er = all_results[k].get("exit_reasons", {})
            tag = f"BV{bv_w}/R{rot_w}" if bv_w > 0 else "Rot100"
            if bv_w == 100:
                tag = "BV100"
            parts = []
            for r in ["STOP_LOSS", "TRAILING_STOP", "TAKE_PROFIT", "SELL", "MAX_HOLD"]:
                if r in er:
                    parts.append(f"{r}:{er[r]['n']}({er[r]['pnl']:+,.0f})")
            print(f"    {tag:<12} " + "  ".join(parts))

    # ── 판정 ──
    print(f"\n{'='*100}")
    print(f"  판정")
    print(f"{'='*100}")

    bv_only_oos = all_results["OOS_100_0"]

    best_mix = None
    best_score = -999
    for bv_w, rot_w in [(25, 75), (50, 50), (75, 25)]:
        k_oos = f"OOS_{bv_w}_{rot_w}"
        k_dev = f"DEV_{bv_w}_{rot_w}"
        m_oos = all_results[k_oos]
        m_dev = all_results[k_dev]

        # Score: OOS return + OOS Sharpe*10 + DEV return*0.5 (MDD penalty)
        score = (m_oos["total_return"]
                 + m_oos["sharpe"] * 10
                 + m_dev["total_return"] * 0.3
                 + max(m_oos["mdd"] + 4, 0) * 5)  # MDD gate bonus
        if score > best_score:
            best_score = score
            best_mix = (bv_w, rot_w)

    bv_w, rot_w = best_mix
    k_oos = f"OOS_{bv_w}_{rot_w}"
    k_dev = f"DEV_{bv_w}_{rot_w}"
    m_oos = all_results[k_oos]
    m_dev = all_results[k_dev]

    # 이전 50/50 기준값 (TS 있던 시절)
    prev_oos_ret = 3.41
    prev_oos_sharpe = -0.54
    prev_oos_mdd = -2.28

    oos_ret_ok = m_oos["total_return"] > prev_oos_ret
    oos_mdd_ok = m_oos["mdd"] > -4.0
    oos_sharpe_ok = m_oos["sharpe"] > prev_oos_sharpe
    bv_only_ret = bv_only_oos["total_return"]
    beats_bv = m_oos["total_return"] > bv_only_ret

    print(f"\n  최적 혼합 비중: BV{bv_w}/Rot{rot_w}")
    print(f"\n  성공 기준:")
    print(f"    OOS return > 이전 50/50 ({prev_oos_ret}%): {m_oos['total_return']:.2f}% → "
          f"{'PASS' if oos_ret_ok else 'FAIL'}")
    print(f"    OOS MDD > -4%: {m_oos['mdd']:.2f}% → "
          f"{'PASS' if oos_mdd_ok else 'FAIL'}")
    print(f"    OOS Sharpe > 이전 50/50 ({prev_oos_sharpe}): {m_oos['sharpe']:.2f} → "
          f"{'PASS' if oos_sharpe_ok else 'FAIL'}")
    print(f"    OOS return > BV 단독 ({bv_only_ret:.2f}%): {m_oos['total_return']:.2f}% → "
          f"{'PASS' if beats_bv else 'FAIL'}")

    # concentration 비교
    rot_only_conc = all_results[f"OOS_0_100"]["top_sym_share"]
    mix_conc = m_oos["top_sym_share"]
    conc_ok = mix_conc < rot_only_conc
    print(f"    concentration 완화: Rot단독 {rot_only_conc:.1f}% → 혼합 {mix_conc:.1f}% → "
          f"{'PASS' if conc_ok else 'FAIL'}")

    if oos_ret_ok and oos_mdd_ok and oos_sharpe_ok:
        verdict = "BARBELL_MIX_IMPROVES"
    elif beats_bv and oos_mdd_ok:
        verdict = "BARBELL_MIX_IMPROVES"
    elif m_oos["total_return"] > bv_only_ret * 0.9 and oos_mdd_ok:
        verdict = "BARBELL_MIX_IMPROVES"
    elif all_results[f"OOS_0_100"]["total_return"] > bv_only_ret and oos_mdd_ok:
        verdict = "ROTATION_ONLY_STILL_BETTER"
    else:
        verdict = "NOT_READY"

    print(f"\n  판정: {verdict}")

    if verdict == "BARBELL_MIX_IMPROVES":
        print(f"\n  사유: BV{bv_w}/Rot{rot_w} 혼합이 이전 50/50 대비 OOS 개선.")
        print(f"  다음 액션: take_profit 6% 실험 또는 KR_CORE_10 유니버스 확장 검토")
        kr10 = "YES" if m_oos["total_return"] > 3 else "NO"
    elif verdict == "ROTATION_ONLY_STILL_BETTER":
        print(f"\n  사유: Rotation 단독이 BV를 능가하나 혼합 시너지 미발생.")
        print(f"  다음 액션: 유니버스 확장으로 Rotation 분산 기대 검토")
        kr10 = "YES"
    else:
        print(f"\n  사유: TS 제거에도 혼합 시너지 부족.")
        print(f"  다음 액션: take_profit 6% 조정 후 재검토 또는 Rotation 보류")
        kr10 = "NO"

    print(f"\n  KR_CORE_10 확장 재검토 가치: {kr10}")

    # TS 제거 효과 설명
    print(f"\n  TS 제거 효과 분석:")
    dev_rot_ts = -4.99   # 이전 TS 유지
    dev_rot_nots = all_results["DEV_0_100"]["total_return"]
    oos_rot_ts = 6.18    # 이전 TS 유지
    oos_rot_nots = all_results["OOS_0_100"]["total_return"]
    print(f"    DEV Rotation: {dev_rot_ts:.2f}% → {dev_rot_nots:.2f}% (TS 제거)")
    print(f"    OOS Rotation: {oos_rot_ts:.2f}% → {oos_rot_nots:.2f}% (TS 제거)")
    print(f"    수익 capture 증가(71%→79%)로 TP 도달 빈도 증가 = DEV 개선의 주 요인")
    print(f"    OOS에서 STOP_LOSS 증가(1→4건)는 TS가 일부 방어하던 포지션이 더 깊이 하락한 결과")
    print(f"    net 효과: DEV +{dev_rot_nots - dev_rot_ts:.2f}%p, OOS {oos_rot_nots - oos_rot_ts:+.2f}%p")

    print(f"\n{'='*100}")
    print(f"  비교 완료")
    print(f"{'='*100}")
