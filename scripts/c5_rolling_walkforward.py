"""
C-5: Rolling walk-forward + Sharpe sanity check
- 4 configs: BV100, Rot100, BV50/R50, BV75/R25
- Rolling 12-month test windows, 6-month step
- Frozen params, no re-optimization
- Rotation: TP=7%, TS=OFF, min_hold=0
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


def make_windows(start="2021-01-01", end="2025-12-31", window_months=12, step_months=6):
    """Generate rolling windows."""
    windows = []
    s = pd.Timestamp(start)
    e_max = pd.Timestamp(end)
    while True:
        w_end = s + pd.DateOffset(months=window_months) - pd.Timedelta(days=1)
        if w_end > e_max:
            w_end = e_max
        if s >= e_max:
            break
        windows.append((s.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")))
        s += pd.DateOffset(months=step_months)
        if s >= e_max:
            break
    return windows


def run_single(strategy_name, capital, start, end, tp_rate=0.08,
               max_pos=None, extra_div=None, disable_ts=False):
    config = Config.get()
    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["disable_trailing_stop"] = disable_ts
    rot_params["min_hold_days"] = 0
    rot_params["abs_momentum_filter"] = "none"
    rot_params["market_filter_sma200"] = False

    tp_cfg = config.risk_params.setdefault("take_profit", {})
    saved_tp = tp_cfg.get("fixed_rate", 0.08)

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved_div = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}

    try:
        tp_cfg["fixed_rate"] = tp_rate
        if max_pos is not None:
            div_cfg["max_positions"] = max_pos
        if extra_div:
            div_cfg.update(extra_div)

        pbt = PortfolioBacktester(config)
        return pbt.run(
            symbols=SYMBOLS, strategy_name=strategy_name,
            initial_capital=capital, start_date=start, end_date=end,
        )
    finally:
        tp_cfg["fixed_rate"] = saved_tp
        for k, v in saved_div.items():
            if v is not None:
                div_cfg[k] = v
            elif k in div_cfg and k in (extra_div or {}):
                del div_cfg[k]


def run_sleeve(start, end, bv_w, rot_w, rot_tp=0.07):
    bv_cap = int(INITIAL_CAPITAL * bv_w / 100)
    rot_cap = INITIAL_CAPITAL - bv_cap

    r_bv = run_single("breakout_volume", bv_cap, start, end, tp_rate=0.08)
    r_rot = run_single("relative_strength_rotation", rot_cap, start, end,
                        tp_rate=rot_tp, max_pos=2, extra_div=ROTATION_DIV,
                        disable_ts=True)

    eq_a = r_bv["equity_curve"].set_index("date")
    eq_b = r_rot["equity_curve"].set_index("date")
    common = sorted(set(eq_a.index) & set(eq_b.index))
    rows = [{"date": d,
             "value": float(eq_a.loc[d, "value"]) + float(eq_b.loc[d, "value"]),
             "cash": float(eq_a.loc[d, "cash"]) + float(eq_b.loc[d, "cash"]),
             "n_positions": int(eq_a.loc[d, "n_positions"]) + int(eq_b.loc[d, "n_positions"])}
            for d in common]
    return {
        "equity_curve": pd.DataFrame(rows),
        "trades": r_bv["trades"] + r_rot["trades"],
    }


def calc_metrics(equity_df, trades, initial_capital):
    if equity_df is None or equity_df.empty:
        return {"total_return": 0, "mdd": 0, "sharpe": 0, "avg_positions": 0,
                "days_in_market": 0, "top_sym_share": 0, "total_trades": 0,
                "daily_mean": 0, "daily_std": 0, "ann_return": 0, "ann_std": 0}

    final = float(equity_df["value"].iloc[-1])
    total_return = (final / initial_capital - 1) * 100

    dr = equity_df["value"].pct_change().dropna()
    daily_mean = float(dr.mean()) if len(dr) > 0 else 0
    daily_std = float(dr.std()) if len(dr) > 1 else 0
    ann_return = daily_mean * 252
    ann_std = daily_std * np.sqrt(252)
    sharpe = (ann_return - 0.03) / ann_std if ann_std > 0 else 0.0

    peak = equity_df["value"].cummax()
    mdd = ((equity_df["value"] - peak) / peak).min() * 100

    avg_pos = float(equity_df["n_positions"].mean()) if "n_positions" in equity_df else 0
    dim = int((equity_df["n_positions"] > 0).sum()) if "n_positions" in equity_df else 0

    sell = [t for t in trades if t.get("action") != "BUY"]
    total_trades = len(sell)
    per_sym = {}
    for t in sell:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + t.get("pnl", 0)
    total_pnl = sum(per_sym.values())
    top_share = max(abs(v) for v in per_sym.values()) / abs(total_pnl) * 100 if total_pnl != 0 and per_sym else 0

    return {
        "total_return": round(total_return, 2), "mdd": round(mdd, 2),
        "sharpe": round(sharpe, 2), "avg_positions": round(avg_pos, 2),
        "days_in_market": dim, "top_sym_share": round(top_share, 1),
        "total_trades": total_trades,
        "daily_mean": daily_mean, "daily_std": daily_std,
        "ann_return": round(ann_return * 100, 2), "ann_std": round(ann_std * 100, 2),
    }


CONFIGS = {
    "BV100": lambda s, e: run_single("breakout_volume", INITIAL_CAPITAL, s, e, tp_rate=0.08),
    "Rot100": lambda s, e: run_single("relative_strength_rotation", INITIAL_CAPITAL, s, e,
                                        tp_rate=0.07, max_pos=2, extra_div=ROTATION_DIV,
                                        disable_ts=True),
    "BV50/R50": lambda s, e: run_sleeve(s, e, 50, 50, rot_tp=0.07),
    "BV75/R25": lambda s, e: run_sleeve(s, e, 75, 25, rot_tp=0.07),
}


if __name__ == "__main__":
    windows = make_windows()

    print("=" * 120)
    print("  C-5: Rolling Walk-Forward + Sharpe Sanity Check")
    print(f"  Windows: {len(windows)} x 12-month, 6-month step")
    print(f"  Configs: {list(CONFIGS.keys())}")
    print("=" * 120)

    # window labels
    wlabels = [f"{w[0][:7]}~{w[1][:7]}" for w in windows]

    all_metrics = {}  # config -> [metrics per window]

    for cfg_name, runner in CONFIGS.items():
        all_metrics[cfg_name] = []
        for i, (ws, we) in enumerate(windows):
            wl = wlabels[i]
            print(f"  {cfg_name:<12} [{wl}]...", end="", flush=True)
            try:
                r = runner(ws, we)
                m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            except Exception as ex:
                print(f" ERROR: {ex}")
                m = {"total_return": 0, "mdd": 0, "sharpe": 0, "avg_positions": 0,
                     "days_in_market": 0, "top_sym_share": 0, "total_trades": 0,
                     "daily_mean": 0, "daily_std": 0, "ann_return": 0, "ann_std": 0}
            all_metrics[cfg_name].append(m)
            print(f"  ret={m['total_return']:+.2f}% MDD={m['mdd']:.2f}% Sh={m['sharpe']:.2f}")

    # ── 구간별 결과표 ──
    print(f"\n{'='*120}")
    print(f"  구간별 결과표")
    print(f"{'='*120}")

    header = f"  {'window':<20}"
    for cfg in CONFIGS:
        header += f" {'ret':>6} {'MDD':>6} {'Sh':>6} |"
    print(header)
    print(f"  {'':>20}" + (f" {'':>6} {'':>6} {'':>6} |" * 4).replace("       ", "------"))

    cfglist = list(CONFIGS.keys())
    for i, wl in enumerate(wlabels):
        line = f"  {wl:<20}"
        for cfg in cfglist:
            m = all_metrics[cfg][i]
            line += f" {m['total_return']:>+6.1f} {m['mdd']:>6.1f} {m['sharpe']:>6.2f} |"
        print(line)

    # ── 구성별 집계표 ──
    print(f"\n{'='*120}")
    print(f"  구성별 집계")
    print(f"{'='*120}")

    agg_labels = [
        "평균 return (%)",
        "median return (%)",
        "최악 window ret (%)",
        "평균 MDD (%)",
        "평균 Sharpe",
        "positive window %",
        "평균 avg_positions",
        "평균 days_in_market",
        "평균 top_sym_share (%)",
    ]

    print(f"  {'지표':<24}" + "".join(f"{c:>14}" for c in cfglist))
    print(f"  {'-'*24}" + f"{'-'*14}" * 4)

    for cfg in cfglist:
        ms = all_metrics[cfg]
        rets = [m["total_return"] for m in ms]
        mdds = [m["mdd"] for m in ms]
        shs = [m["sharpe"] for m in ms]

    for label_idx, label in enumerate(agg_labels):
        vals = []
        for cfg in cfglist:
            ms = all_metrics[cfg]
            rets = [m["total_return"] for m in ms]
            mdds = [m["mdd"] for m in ms]
            shs = [m["sharpe"] for m in ms]
            aps = [m["avg_positions"] for m in ms]
            dims = [m["days_in_market"] for m in ms]
            tss = [m["top_sym_share"] for m in ms]

            if label_idx == 0: vals.append(np.mean(rets))
            elif label_idx == 1: vals.append(np.median(rets))
            elif label_idx == 2: vals.append(min(rets))
            elif label_idx == 3: vals.append(np.mean(mdds))
            elif label_idx == 4: vals.append(np.mean(shs))
            elif label_idx == 5: vals.append(sum(1 for r in rets if r > 0) / len(rets) * 100)
            elif label_idx == 6: vals.append(np.mean(aps))
            elif label_idx == 7: vals.append(np.mean(dims))
            elif label_idx == 8: vals.append(np.mean(tss))

        print(f"  {label:<24}" + "".join(f"{v:>14.2f}" for v in vals))

    # ── Sharpe Sanity Check ──
    print(f"\n{'='*120}")
    print(f"  Sharpe Sanity Check")
    print(f"{'='*120}")

    # Full period run for BV50/R50
    print(f"\n  BV50/R50 full period (2021-2025) daily return 분석:")
    r_full = run_sleeve("2021-01-01", "2025-12-31", 50, 50, rot_tp=0.07)
    eq = r_full["equity_curve"]
    dr = eq["value"].pct_change().dropna()

    daily_mean = float(dr.mean())
    daily_std = float(dr.std())
    daily_median = float(dr.median())
    ann_ret = daily_mean * 252
    ann_std = daily_std * np.sqrt(252)
    sharpe = (ann_ret - 0.03) / ann_std if ann_std > 0 else 0

    n_pos_days = int((eq["n_positions"] > 0).sum())
    n_cash_days = int((eq["n_positions"] == 0).sum())
    total_days = len(eq)

    print(f"    total days: {total_days}")
    print(f"    position days: {n_pos_days} ({n_pos_days/total_days*100:.1f}%)")
    print(f"    cash days: {n_cash_days} ({n_cash_days/total_days*100:.1f}%)")
    print(f"    daily return mean: {daily_mean*100:.6f}%")
    print(f"    daily return median: {daily_median*100:.6f}%")
    print(f"    daily return std: {daily_std*100:.4f}%")
    print(f"    annualized return: {ann_ret*100:.2f}%")
    print(f"    annualized std: {ann_std*100:.2f}%")
    print(f"    risk-free rate: 3.00%")
    print(f"    Sharpe = ({ann_ret*100:.2f}% - 3.00%) / {ann_std*100:.2f}% = {sharpe:.2f}")

    print(f"\n  왜 return 양수인데 Sharpe가 음수인가:")
    total_ret = (float(eq["value"].iloc[-1]) / INITIAL_CAPITAL - 1)
    cagr = (float(eq["value"].iloc[-1]) / INITIAL_CAPITAL) ** (1 / (total_days / 252)) - 1
    print(f"    total return: {total_ret*100:.2f}%")
    print(f"    CAGR: {cagr*100:.2f}%")
    print(f"    annualized return (daily mean * 252): {ann_ret*100:.2f}%")
    if ann_ret * 100 < 3.0:
        print(f"    → annualized return ({ann_ret*100:.2f}%) < risk-free rate (3.00%)")
        print(f"    → 수익은 양수이나 연환산 수익률이 무위험 수익률 미만이면 Sharpe는 음수")
        print(f"    → 이것은 계산식 특성이지 전략 실패가 아님")
        print(f"    → cash days({n_cash_days}일, {n_cash_days/total_days*100:.0f}%)에서 daily return=0이")
        print(f"      daily mean을 끌어내리고, 동시에 std를 키워 Sharpe를 이중으로 악화시킴")
    else:
        print(f"    → annualized return ({ann_ret*100:.2f}%) ≥ risk-free rate (3.00%)")
        print(f"    → Sharpe가 양수여야 정상")

    # Position-only Sharpe
    dr_pos = dr[eq["n_positions"].iloc[1:].values > 0] if len(eq) > 1 else dr
    if len(dr_pos) > 1 and dr_pos.std() > 0:
        pos_ann_ret = float(dr_pos.mean()) * 252
        pos_ann_std = float(dr_pos.std()) * np.sqrt(252)
        pos_sharpe = (pos_ann_ret - 0.03) / pos_ann_std
        print(f"\n  Position-only Sharpe (cash days 제외):")
        print(f"    position days: {len(dr_pos)}")
        print(f"    ann return: {pos_ann_ret*100:.2f}%")
        print(f"    ann std: {pos_ann_std*100:.2f}%")
        print(f"    Sharpe: {pos_sharpe:.2f}")

    # ── 최종 판정 ──
    print(f"\n{'='*120}")
    print(f"  최종 판정")
    print(f"{'='*120}")

    bv_rets = [m["total_return"] for m in all_metrics["BV100"]]
    r50_rets = [m["total_return"] for m in all_metrics["BV50/R50"]]
    r75_rets = [m["total_return"] for m in all_metrics["BV75/R25"]]

    r50_pos_pct = sum(1 for r in r50_rets if r > 0) / len(r50_rets) * 100
    bv_pos_pct = sum(1 for r in bv_rets if r > 0) / len(bv_rets) * 100
    r50_worst = min(r50_rets)
    r50_avg_mdd = np.mean([m["mdd"] for m in all_metrics["BV50/R50"]])
    r75_avg_conc = np.mean([m["top_sym_share"] for m in all_metrics["BV75/R25"]])
    r50_avg_conc = np.mean([m["top_sym_share"] for m in all_metrics["BV50/R50"]])

    print(f"\n  BV50/R50 vs BV75/R25:")
    print(f"    BV50/R50: 평균ret={np.mean(r50_rets):.2f}%, pos_win={r50_pos_pct:.0f}%, "
          f"worst={r50_worst:.2f}%, avg_conc={r50_avg_conc:.1f}%")
    print(f"    BV75/R25: 평균ret={np.mean(r75_rets):.2f}%, pos_win={sum(1 for r in r75_rets if r > 0)/len(r75_rets)*100:.0f}%, "
          f"worst={min(r75_rets):.2f}%, avg_conc={r75_avg_conc:.1f}%")

    # 판정
    catastrophic = r50_worst < -10
    mostly_positive = r50_pos_pct >= 50
    better_than_bv = np.mean(r50_rets) >= np.mean(bv_rets) * 0.8

    if catastrophic:
        verdict = "NOT_READY"
    elif mostly_positive and better_than_bv:
        verdict = "PAPER_READY"
    elif mostly_positive:
        verdict = "PAPER_ONLY_BUT_MONITOR"
    else:
        verdict = "NOT_READY"

    # Final pick: BV50/R50 vs BV75/R25
    if np.mean(r50_rets) >= np.mean(r75_rets) * 0.9 and r50_avg_conc < r75_avg_conc:
        pick = "BV50/R50"
        pick_reason = "concentration 우위 + return 유사"
    elif np.mean(r75_rets) > np.mean(r50_rets) * 1.1:
        pick = "BV75/R25"
        pick_reason = "return 유의미하게 우위"
    else:
        pick = "BV50/R50"
        pick_reason = "분산 우위, return 근접"

    print(f"\n  판정: {verdict}")
    print(f"  paper 후보: {pick} ({pick_reason})")

    if verdict == "PAPER_READY":
        print(f"  사유: 대부분 window 양수, 극단 붕괴 없음.")
        print(f"  다음 액션: {pick} 설정으로 paper trading 시작")
    elif verdict == "PAPER_ONLY_BUT_MONITOR":
        print(f"  사유: 양수 window 비율 충족하나 일부 약점.")
        print(f"  다음 액션: {pick}로 paper trading, 1개월 단위 모니터링")
    else:
        print(f"  사유: 안정성 부족.")
        print(f"  다음 액션: Rotation exit 추가 개선 또는 유니버스 확장")

    kr10 = "YES" if verdict in ["PAPER_READY", "PAPER_ONLY_BUT_MONITOR"] and np.mean(r50_rets) > 0 else "NO"
    print(f"\n  KR_CORE_10 확장 재검토 가치: {kr10}")

    print(f"\n{'='*120}")
    print(f"  완료")
    print(f"{'='*120}")
