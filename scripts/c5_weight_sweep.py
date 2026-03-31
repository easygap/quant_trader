"""
C-5 비중 스윕 + 강건성 검증
- BV/Rotation = 0/100, 25/75, 50/50, 75/25, 100/0
- 구간 1: 2024-01-01 ~ 2025-12-31  (주 OOS)
- 구간 2: 2021-01-01 ~ 2023-12-31  (강건성)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester

logger.remove()
logger.add(sys.stderr, level="WARNING")

SYMBOLS = ["005930", "000660", "035720", "051910"]
TOTAL_CAPITAL = 10_000_000

PERIODS = [
    ("OOS_2024", "2024-01-01", "2025-12-31"),
    ("DEV_2021", "2021-01-01", "2023-12-31"),
]

# BV weight → Rotation weight = 1 - BV weight
WEIGHTS = [0, 25, 50, 75, 100]  # BV %

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}


def run_bt(strategy_name, capital, start, end, max_pos=None, extra_div=None):
    config = Config.get()
    div_cfg = config.risk_params.setdefault("diversification", {})

    saved = {}
    keys = ["max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio"]
    for k in keys:
        saved[k] = div_cfg.get(k)

    try:
        if max_pos is not None:
            div_cfg["max_positions"] = max_pos
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
        for k in keys:
            if saved[k] is not None:
                div_cfg[k] = saved[k]
            elif k in div_cfg and extra_div and k in extra_div:
                del div_cfg[k]


def metrics(eq_df, trades, capital):
    if eq_df is None or eq_df.empty:
        return {k: 0 for k in [
            "total_return", "cagr", "mdd", "sharpe", "total_trades",
            "win_rate", "avg_positions", "days_in_market", "max_concurrent",
            "top_sym_share", "total_days", "final_value",
        ]} | {"per_sym_pnl": {}}

    final = float(eq_df["value"].iloc[-1])
    ret = (final / capital - 1) * 100
    yrs = len(eq_df) / 252
    cagr = ((final / capital) ** (1 / max(yrs, 0.01)) - 1) * 100

    dr = eq_df["value"].pct_change().dropna()
    if len(dr) > 1 and dr.std() > 0:
        sharpe = (dr.mean() * 252 - 0.03) / (dr.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    pk = eq_df["value"].cummax()
    mdd = ((eq_df["value"] - pk) / pk).min() * 100

    sell = [t for t in trades if t.get("action") != "BUY"]
    n_sell = len(sell)
    wins = sum(1 for t in sell if t.get("pnl", 0) > 0)

    has_npos = "n_positions" in eq_df.columns
    avg_pos = float(eq_df["n_positions"].mean()) if has_npos else 0
    dim = int((eq_df["n_positions"] > 0).sum()) if has_npos else 0
    maxc = int(eq_df["n_positions"].max()) if has_npos else 0

    per_sym = {}
    for t in sell:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + t.get("pnl", 0)
    tpnl = sum(per_sym.values())
    top_sh = (max(abs(v) for v in per_sym.values()) / abs(tpnl) * 100
              if tpnl != 0 and per_sym else 0)

    return {
        "total_return": round(ret, 2),
        "cagr": round(cagr, 2),
        "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "total_trades": n_sell,
        "win_rate": round(wins / n_sell * 100, 1) if n_sell else 0,
        "avg_positions": round(avg_pos, 2),
        "days_in_market": dim,
        "max_concurrent": maxc,
        "total_days": len(eq_df),
        "top_sym_share": round(top_sh, 1),
        "final_value": round(final, 0),
        "per_sym_pnl": per_sym,
    }


def combine(res_a, res_b):
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
    return {"equity_curve": pd.DataFrame(rows),
            "trades": res_a["trades"] + res_b["trades"]}


def run_weight(bv_pct, start, end):
    """Run a single BV/Rot weight combination. Returns metrics dict."""
    rot_pct = 100 - bv_pct
    bv_cap = int(TOTAL_CAPITAL * bv_pct / 100)
    rot_cap = TOTAL_CAPITAL - bv_cap  # int 보정

    if bv_pct == 100:
        r = run_bt("breakout_volume", TOTAL_CAPITAL, start, end)
        return metrics(r["equity_curve"], r["trades"], TOTAL_CAPITAL)

    if bv_pct == 0:
        r = run_bt("relative_strength_rotation", TOTAL_CAPITAL, start, end,
                    max_pos=2, extra_div=ROTATION_DIV)
        return metrics(r["equity_curve"], r["trades"], TOTAL_CAPITAL)

    # Mixed
    r_bv = run_bt("breakout_volume", bv_cap, start, end)
    r_rot = run_bt("relative_strength_rotation", rot_cap, start, end,
                   max_pos=2, extra_div=ROTATION_DIV)
    c = combine(r_bv, r_rot)
    return metrics(c["equity_curve"], c["trades"], TOTAL_CAPITAL)


if __name__ == "__main__":
    print("=" * 90)
    print("  C-5 비중 스윕 + 강건성 검증")
    print(f"  유니버스: {SYMBOLS}")
    print(f"  비중: BV = {WEIGHTS}%")
    print("=" * 90)

    all_results = {}  # (period_name, bv_pct) -> metrics

    for pname, pstart, pend in PERIODS:
        print(f"\n{'─' * 90}")
        print(f"  구간: {pname} ({pstart} ~ {pend})")
        print(f"{'─' * 90}")

        for bv_pct in WEIGHTS:
            rot_pct = 100 - bv_pct
            label = f"BV{bv_pct}/Rot{rot_pct}"
            print(f"    {label} ... ", end="", flush=True)
            m = run_weight(bv_pct, pstart, pend)
            all_results[(pname, bv_pct)] = m
            print(f"ret={m['total_return']:>6.2f}%  sharpe={m['sharpe']:>6.2f}  "
                  f"mdd={m['mdd']:>6.2f}%  dim={m['days_in_market']}")

    # ════════════════════════════════════════════════════════════
    # 결과표 출력
    # ════════════════════════════════════════════════════════════
    LABELS = [
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

    for pname, _, _ in PERIODS:
        print(f"\n{'=' * 90}")
        print(f"  비중별 결과표 — {pname}")
        print(f"{'=' * 90}")
        header = f"  {'지표':<22}"
        for bv_pct in WEIGHTS:
            header += f" {'BV'+str(bv_pct)+'/R'+str(100-bv_pct):>11}"
        print(header)
        print(f"  {'-'*22}" + f" {'-'*11}" * len(WEIGHTS))

        for label, key in LABELS:
            row = f"  {label:<22}"
            for bv_pct in WEIGHTS:
                v = all_results[(pname, bv_pct)].get(key, 0)
                if isinstance(v, float):
                    row += f" {v:>11.2f}"
                else:
                    row += f" {v:>11}"

            print(row)

        # Per-symbol PnL
        print(f"\n  종목별 PnL:")
        header2 = f"  {'종목':<8}"
        for bv_pct in WEIGHTS:
            header2 += f" {'BV'+str(bv_pct)+'/R'+str(100-bv_pct):>11}"
        print(header2)
        print(f"  {'-'*8}" + f" {'-'*11}" * len(WEIGHTS))
        for sym in SYMBOLS:
            row = f"  {sym:<8}"
            for bv_pct in WEIGHTS:
                pnl = all_results[(pname, bv_pct)].get("per_sym_pnl", {}).get(sym, 0)
                row += f" {pnl:>11,.0f}"
            print(row)

    # ════════════════════════════════════════════════════════════
    # 두 구간 합산 요약
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print(f"  두 구간 합산 요약 (단순 평균)")
    print(f"{'=' * 90}")
    header3 = f"  {'지표':<22}"
    for bv_pct in WEIGHTS:
        header3 += f" {'BV'+str(bv_pct)+'/R'+str(100-bv_pct):>11}"
    print(header3)
    print(f"  {'-'*22}" + f" {'-'*11}" * len(WEIGHTS))

    avg_keys = ["total_return", "cagr", "mdd", "sharpe", "avg_positions", "days_in_market", "top_sym_share"]
    avg_labels = {
        "total_return": "avg return (%)",
        "cagr": "avg CAGR (%)",
        "mdd": "avg MDD (%)",
        "sharpe": "avg Sharpe",
        "avg_positions": "avg positions",
        "days_in_market": "avg days_in_mkt",
        "top_sym_share": "avg top_share(%)",
    }
    for key in avg_keys:
        row = f"  {avg_labels[key]:<22}"
        for bv_pct in WEIGHTS:
            vals = [all_results[(pn, bv_pct)].get(key, 0) for pn, _, _ in PERIODS]
            avg = sum(vals) / len(vals)
            row += f" {avg:>11.2f}"
        print(row)

    # worst MDD across periods
    row_wmdd = f"  {'worst MDD (%)':<22}"
    for bv_pct in WEIGHTS:
        worst = min(all_results[(pn, bv_pct)].get("mdd", 0) for pn, _, _ in PERIODS)
        row_wmdd += f" {worst:>11.2f}"
    print(row_wmdd)

    # ════════════════════════════════════════════════════════════
    # BV sleeve hedge 분석
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print(f"  BV sleeve hedge 분석")
    print(f"{'=' * 90}")
    for pname, _, _ in PERIODS:
        m0 = all_results[(pname, 0)]    # rotation only
        m25 = all_results[(pname, 25)]   # 25/75
        m50 = all_results[(pname, 50)]   # 50/50
        m100 = all_results[(pname, 100)] # bv only
        print(f"\n  {pname}:")
        print(f"    Rot100: ret={m0['total_return']:>6.2f}% mdd={m0['mdd']:>6.2f}% sharpe={m0['sharpe']:>6.2f}")
        print(f"    BV25:   ret={m25['total_return']:>6.2f}% mdd={m25['mdd']:>6.2f}% sharpe={m25['sharpe']:>6.2f}")
        print(f"    BV50:   ret={m50['total_return']:>6.2f}% mdd={m50['mdd']:>6.2f}% sharpe={m50['sharpe']:>6.2f}")
        print(f"    BV100:  ret={m100['total_return']:>6.2f}% mdd={m100['mdd']:>6.2f}% sharpe={m100['sharpe']:>6.2f}")

        ret_loss_25 = m0['total_return'] - m25['total_return']
        mdd_gain_25 = m25['mdd'] - m0['mdd']  # less negative = improvement
        ret_loss_50 = m0['total_return'] - m50['total_return']
        mdd_gain_50 = m50['mdd'] - m0['mdd']
        print(f"    BV25% 혼합: return 희생={ret_loss_25:>+.2f}%p, MDD 변화={mdd_gain_25:>+.2f}%p "
              f"({'hedge' if mdd_gain_25 > 0 else '희석'})")
        print(f"    BV50% 혼합: return 희생={ret_loss_50:>+.2f}%p, MDD 변화={mdd_gain_50:>+.2f}%p "
              f"({'hedge' if mdd_gain_50 > 0 else '희석'})")

    # ════════════════════════════════════════════════════════════
    # 판정
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * 90}")
    print(f"  최종 판정")
    print(f"{'=' * 90}")

    # Collect key stats per weight across both periods
    scores = {}
    for bv_pct in WEIGHTS:
        avg_ret = np.mean([all_results[(pn, bv_pct)]["total_return"] for pn, _, _ in PERIODS])
        avg_sharpe = np.mean([all_results[(pn, bv_pct)]["sharpe"] for pn, _, _ in PERIODS])
        worst_mdd = min(all_results[(pn, bv_pct)]["mdd"] for pn, _, _ in PERIODS)
        avg_dim = np.mean([all_results[(pn, bv_pct)]["days_in_market"] for pn, _, _ in PERIODS])
        avg_top = np.mean([all_results[(pn, bv_pct)]["top_sym_share"] for pn, _, _ in PERIODS])

        # Check stability: both periods positive return?
        both_positive = all(all_results[(pn, bv_pct)]["total_return"] > 0 for pn, _, _ in PERIODS)
        # Sharpe flip: one period positive, other very negative?
        sharpes = [all_results[(pn, bv_pct)]["sharpe"] for pn, _, _ in PERIODS]
        sharpe_flip = (max(sharpes) > 0 and min(sharpes) < -1.5)

        scores[bv_pct] = {
            "avg_ret": avg_ret, "avg_sharpe": avg_sharpe,
            "worst_mdd": worst_mdd, "avg_dim": avg_dim, "avg_top": avg_top,
            "both_positive": both_positive, "sharpe_flip": sharpe_flip,
        }

    # Check for instability
    any_flip = any(s["sharpe_flip"] for s in scores.values())
    no_stable = not any(s["both_positive"] for s in scores.values())

    if any_flip or no_stable:
        verdict = "NOT_READY_TO_EXPAND"
        best_bv = None
    else:
        # Compare rotation-only (bv=0) vs best mix
        rot_only = scores[0]
        # Find best mix (bv > 0) by avg_sharpe
        mix_candidates = {k: v for k, v in scores.items() if k > 0}
        best_mix_bv = max(mix_candidates, key=lambda k: mix_candidates[k]["avg_sharpe"])
        best_mix = scores[best_mix_bv]

        # Is rotation-only dominant?
        rot_better_ret = rot_only["avg_ret"] > best_mix["avg_ret"] + 0.3
        rot_better_sharpe = rot_only["avg_sharpe"] > best_mix["avg_sharpe"] + 0.1
        mix_mdd_gain = best_mix["worst_mdd"] - rot_only["worst_mdd"]  # positive = mix less negative

        if rot_better_ret and rot_better_sharpe and mix_mdd_gain < 0.5:
            verdict = "ROTATION_ONLY"
            best_bv = 0
        elif mix_mdd_gain > 0.3 or best_mix["avg_dim"] > rot_only["avg_dim"] * 1.15:
            verdict = "BARBELL_MIX"
            best_bv = best_mix_bv
        else:
            verdict = "ROTATION_ONLY"
            best_bv = 0

    print(f"\n  판정: {verdict}")
    if best_bv is not None:
        print(f"  최적 비중: BV {best_bv}% / Rotation {100 - best_bv}%")
    print()

    for bv_pct in WEIGHTS:
        s = scores[bv_pct]
        marker = " ◀ 최적" if bv_pct == best_bv else ""
        print(f"    BV{bv_pct:>3}%: avg_ret={s['avg_ret']:>6.2f}%  avg_sharpe={s['avg_sharpe']:>6.2f}  "
              f"worst_mdd={s['worst_mdd']:>6.2f}%  both_pos={'Y' if s['both_positive'] else 'N'}  "
              f"flip={'Y' if s['sharpe_flip'] else 'N'}{marker}")

    print(f"\n  KR_CORE_10 확장: {'YES' if verdict != 'NOT_READY_TO_EXPAND' else 'NO'}")
    print(f"\n{'=' * 90}")
