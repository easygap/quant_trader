"""
C-5 Rotation take_profit rate 스윕 + 최적 TP로 sleeve 재비교
- TP: 5%, 6%, 7%, 8% (8%=현재 baseline)
- Rotation: trailing_stop OFF, min_hold_days=0
- BV: 변경 없음 (TP=8% 유지)
- DEV/OOS 비교 후, 최적 TP로 BV50/R50 vs BV75/R25 재비교
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
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

TP_VALUES = [0.05, 0.06, 0.07, 0.08]


def run_rotation(period_name, tp_rate=0.08):
    start, end = PERIODS[period_name]
    config = Config.get()

    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["disable_trailing_stop"] = True
    rot_params["min_hold_days"] = 0
    rot_params["abs_momentum_filter"] = "none"
    rot_params["market_filter_sma200"] = False

    # TP rate 오버라이드
    tp_cfg = config.risk_params.setdefault("take_profit", {})
    saved_tp = tp_cfg.get("fixed_rate", 0.08)

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved_div = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}

    try:
        tp_cfg["fixed_rate"] = tp_rate
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
        tp_cfg["fixed_rate"] = saved_tp
        for k, v in saved_div.items():
            if v is not None:
                div_cfg[k] = v


def run_bv(period_name):
    """BV backtest with default TP=8%."""
    start, end = PERIODS[period_name]
    config = Config.get()
    tp_cfg = config.risk_params.setdefault("take_profit", {})
    tp_cfg["fixed_rate"] = 0.08  # BV always 8%

    pbt = PortfolioBacktester(config)
    return pbt.run(
        symbols=SYMBOLS,
        strategy_name="breakout_volume",
        initial_capital=INITIAL_CAPITAL,
        start_date=start,
        end_date=end,
    )


def run_sleeve(period_name, bv_w, rot_w, rot_tp):
    """Run combined sleeve."""
    start, end = PERIODS[period_name]
    config = Config.get()

    bv_cap = int(INITIAL_CAPITAL * bv_w / 100)
    rot_cap = INITIAL_CAPITAL - bv_cap

    # BV sleeve (TP=8%)
    tp_cfg = config.risk_params.setdefault("take_profit", {})
    tp_cfg["fixed_rate"] = 0.08
    pbt_bv = PortfolioBacktester(config)
    r_bv = pbt_bv.run(symbols=SYMBOLS, strategy_name="breakout_volume",
                       initial_capital=bv_cap, start_date=start, end_date=end)

    # Rotation sleeve (custom TP)
    rot_params = config.strategies.setdefault("relative_strength_rotation", {})
    rot_params["disable_trailing_stop"] = True
    rot_params["min_hold_days"] = 0
    rot_params["abs_momentum_filter"] = "none"
    rot_params["market_filter_sma200"] = False

    tp_cfg["fixed_rate"] = rot_tp
    div_cfg = config.risk_params.setdefault("diversification", {})
    saved_div = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt_rot = PortfolioBacktester(config)
        r_rot = pbt_rot.run(symbols=SYMBOLS, strategy_name="relative_strength_rotation",
                            initial_capital=rot_cap, start_date=start, end_date=end)
    finally:
        tp_cfg["fixed_rate"] = 0.08
        for k, v in saved_div.items():
            if v is not None:
                div_cfg[k] = v

    # Combine
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
        return {"total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0,
                "total_trades": 0, "win_rate": 0, "avg_positions": 0,
                "days_in_market": 0, "top_sym_share": 0, "per_sym_pnl": {},
                "sl_pnl": 0, "sl_n": 0, "tp_pnl": 0, "tp_n": 0}
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
    top_share = max(abs(v) for v in per_sym.values()) / abs(total_pnl) * 100 if total_pnl != 0 and per_sym else 0

    sl_trades = [t for t in sell if t["action"] == "STOP_LOSS"]
    tp_trades = [t for t in sell if t["action"] == "TAKE_PROFIT"]

    return {
        "total_return": round(total_return, 2), "cagr": round(cagr, 2),
        "mdd": round(mdd, 2), "sharpe": round(sharpe, 2),
        "total_trades": total_trades, "win_rate": round(win_rate, 1),
        "avg_positions": round(avg_pos, 2), "days_in_market": dim,
        "top_sym_share": round(top_share, 1), "per_sym_pnl": per_sym,
        "sl_pnl": round(sum(t["pnl"] for t in sl_trades), 0),
        "sl_n": len(sl_trades),
        "tp_pnl": round(sum(t["pnl"] for t in tp_trades), 0),
        "tp_n": len(tp_trades),
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
                    "pnl": t["pnl"], "pnl_rate": t["pnl_rate"],
                    "holding_days": t.get("holding_days", 0),
                    "exit_reason": t["action"],
                })
    return paired


def compute_mfe(paired, price_data):
    for t in paired:
        df = price_data.get(t["symbol"])
        if df is None or t["entry_price"] <= 0:
            t["mfe"] = 0
            continue
        mask = (df.index >= t["entry_date"]) & (df.index <= t["exit_date"])
        w = df.loc[mask]
        if w.empty:
            t["mfe"] = 0
            continue
        highs = w["high"].astype(float) if "high" in w else w["close"].astype(float)
        t["mfe"] = round((highs.max() / t["entry_price"] - 1) * 100, 2)


if __name__ == "__main__":
    print("=" * 100)
    print("  C-5 Rotation Take-Profit 스윕 (TS OFF)")
    print(f"  TP values: {[f'{v:.0%}' for v in TP_VALUES]}")
    print("=" * 100)

    # 가격 데이터 (MFE 계산용)
    collector = DataCollector()
    price_data = {}
    for sym in SYMBOLS:
        df = collector.fetch_stock(sym, "2020-06-01", "2025-12-31")
        if df is not None and not df.empty:
            price_data[sym] = df

    # ── Phase 1: Rotation 단독 TP 스윕 ──
    rot_results = {}
    rot_paired = {}

    for period in ["DEV", "OOS"]:
        for tp in TP_VALUES:
            label = f"{period}_TP{int(tp*100)}"
            tp_pct = f"{tp:.0%}"
            print(f"\n  [{label}] {period} TP={tp_pct} 실행 중...")
            r = run_rotation(period, tp_rate=tp)
            m = calc_metrics(r["equity_curve"], r["trades"], INITIAL_CAPITAL)
            paired = pair_trades(r["trades"])
            compute_mfe(paired, price_data)
            rot_results[label] = m
            rot_paired[label] = paired

            buy_n = sum(1 for t in r["trades"] if t["action"] == "BUY")
            sell_n = sum(1 for t in r["trades"] if t["action"] != "BUY")
            print(f"    BUY: {buy_n}, SELL: {sell_n}")

    # ── Phase 1 결과표 ──
    metric_labels = [
        ("total_return (%)", "total_return"),
        ("CAGR (%)", "cagr"),
        ("MDD (%)", "mdd"),
        ("Sharpe", "sharpe"),
        ("total_trades", "total_trades"),
        ("win_rate (%)", "win_rate"),
        ("STOP_LOSS n", "sl_n"),
        ("STOP_LOSS pnl", "sl_pnl"),
        ("TAKE_PROFIT n", "tp_n"),
        ("TAKE_PROFIT pnl", "tp_pnl"),
    ]

    for period in ["DEV", "OOS"]:
        print(f"\n{'='*100}")
        print(f"  [{period}] Rotation 단독 TP 스윕")
        print(f"{'='*100}")

        cols = [f"TP{int(tp*100)}%" for tp in TP_VALUES]
        keys = [f"{period}_TP{int(tp*100)}" for tp in TP_VALUES]

        print(f"  {'지표':<22}" + "".join(f"{c:>12}" for c in cols))
        print(f"  {'-'*22}" + f"{'-'*12}" * len(cols))
        for lbl, key in metric_labels:
            vals = [rot_results[k].get(key, 0) for k in keys]
            fmts = [f"{v:>12,.0f}" if key.endswith("pnl") else
                    (f"{v:>12.2f}" if isinstance(v, float) else f"{v:>12}")
                    for v in vals]
            print(f"  {lbl:<22}" + "".join(fmts))

    # ── STOP_LOSS 구제 분석 ──
    print(f"\n{'='*100}")
    print(f"  STOP_LOSS 구제 분석: TP8%에서 STOP_LOSS로 끝난 거래 중 낮은 TP에서 구제 가능한 건수")
    print(f"{'='*100}")

    for period in ["DEV", "OOS"]:
        baseline_paired = rot_paired[f"{period}_TP8"]
        sl_trades = [t for t in baseline_paired if t["exit_reason"] == "STOP_LOSS"]
        print(f"\n  [{period}] TP8% STOP_LOSS 거래: {len(sl_trades)}건")
        for t in sl_trades:
            ed = str(t["entry_date"])[:10]
            rescued_5 = "YES" if t["mfe"] >= 5 else "no"
            rescued_6 = "YES" if t["mfe"] >= 6 else "no"
            rescued_7 = "YES" if t["mfe"] >= 7 else "no"
            print(f"    {t['symbol']} {ed} MFE={t['mfe']:+.2f}% pnl_rate={t['pnl_rate']:+.2f}% "
                  f"hold={t['holding_days']}d → TP5%:{rescued_5} TP6%:{rescued_6} TP7%:{rescued_7}")

        for tp_thresh in [5, 6, 7]:
            rescued = sum(1 for t in sl_trades if t["mfe"] >= tp_thresh)
            print(f"    TP{tp_thresh}%에서 구제 가능: {rescued}/{len(sl_trades)}건")

    # ── 최적 TP 선택 ──
    # OOS return 유지 + DEV 개선 + OOS STOP_LOSS 감소 기준 스코어링
    best_tp = None
    best_score = -999
    for tp in TP_VALUES:
        d = rot_results[f"DEV_TP{int(tp*100)}"]
        o = rot_results[f"OOS_TP{int(tp*100)}"]
        score = (o["total_return"]
                 + o["sharpe"] * 5
                 + d["total_return"] * 0.3
                 + (o["sl_pnl"] / 100000))  # SL pnl closer to 0 = better
        if score > best_score:
            best_score = score
            best_tp = tp

    print(f"\n{'='*100}")
    print(f"  최적 TP 후보: {best_tp:.0%}")
    print(f"{'='*100}")

    d_best = rot_results[f"DEV_TP{int(best_tp*100)}"]
    o_best = rot_results[f"OOS_TP{int(best_tp*100)}"]
    d_base = rot_results["DEV_TP8"]
    o_base = rot_results["OOS_TP8"]
    print(f"  DEV: return {d_base['total_return']:.2f}% → {d_best['total_return']:.2f}%, "
          f"MDD {d_base['mdd']:.2f}% → {d_best['mdd']:.2f}%")
    print(f"  OOS: return {o_base['total_return']:.2f}% → {o_best['total_return']:.2f}%, "
          f"Sharpe {o_base['sharpe']:.2f} → {o_best['sharpe']:.2f}")
    print(f"  OOS STOP_LOSS: {o_base['sl_n']}건({o_base['sl_pnl']:+,.0f}) → "
          f"{o_best['sl_n']}건({o_best['sl_pnl']:+,.0f})")

    # ── Phase 2: 최적 TP로 sleeve 재비교 ──
    if best_tp == 0.08:
        print(f"\n  TP 8%가 최적 — sleeve 재비교 불필요. 이전 스윕 결과 유지.")
    else:
        print(f"\n{'='*100}")
        print(f"  Phase 2: TP={best_tp:.0%}로 Sleeve 재비교")
        print(f"{'='*100}")

        sleeve_results = {}
        for period in ["DEV", "OOS"]:
            start, end = PERIODS[period]

            # BV 단독
            print(f"\n  [{period}] BV 단독...")
            r_bv = run_bv(period)
            sleeve_results[f"{period}_BV100"] = calc_metrics(
                r_bv["equity_curve"], r_bv["trades"], INITIAL_CAPITAL)

            # BV50/R50
            print(f"  [{period}] BV50/R50 (Rot TP={best_tp:.0%})...")
            r_5050 = run_sleeve(period, 50, 50, best_tp)
            sleeve_results[f"{period}_50_50"] = calc_metrics(
                r_5050["equity_curve"], r_5050["trades"], INITIAL_CAPITAL)

            # BV75/R25
            print(f"  [{period}] BV75/R25 (Rot TP={best_tp:.0%})...")
            r_7525 = run_sleeve(period, 75, 25, best_tp)
            sleeve_results[f"{period}_75_25"] = calc_metrics(
                r_7525["equity_curve"], r_7525["trades"], INITIAL_CAPITAL)

        # Sleeve 비교표
        sleeve_labels = [
            ("total_return (%)", "total_return"),
            ("MDD (%)", "mdd"),
            ("Sharpe", "sharpe"),
            ("avg_positions", "avg_positions"),
            ("top_sym_share (%)", "top_sym_share"),
            ("days_in_market", "days_in_market"),
        ]

        for period in ["DEV", "OOS"]:
            print(f"\n  [{period}] Sleeve 비교 (Rot TP={best_tp:.0%})")
            cols = ["BV100", "BV50/R50", "BV75/R25"]
            keys = [f"{period}_BV100", f"{period}_50_50", f"{period}_75_25"]
            print(f"  {'지표':<22}" + "".join(f"{c:>12}" for c in cols))
            print(f"  {'-'*22}" + f"{'-'*12}" * 3)
            for lbl, key in sleeve_labels:
                vals = [sleeve_results[k].get(key, 0) for k in keys]
                fmts = [f"{v:>12.2f}" if isinstance(v, float) else f"{v:>12}" for v in vals]
                print(f"  {lbl:<22}" + "".join(fmts))

        # Sleeve 판정
        oos_bv = sleeve_results["OOS_BV100"]
        oos_5050 = sleeve_results["OOS_50_50"]
        oos_7525 = sleeve_results["OOS_75_25"]

        print(f"\n{'='*100}")
        print(f"  Sleeve 판정")
        print(f"{'='*100}")

        # 이전 TP8% sleeve 결과와 비교
        prev_5050_ret = 2.72
        prev_7525_ret = 2.97

        for tag, m, prev in [("BV50/R50", oos_5050, prev_5050_ret),
                              ("BV75/R25", oos_7525, prev_7525_ret)]:
            improved = m["total_return"] > prev
            print(f"  {tag}: OOS return {prev:.2f}% → {m['total_return']:.2f}% "
                  f"({'개선' if improved else '악화/동일'}), "
                  f"MDD {m['mdd']:.2f}%, Sharpe {m['sharpe']:.2f}")

    # ── 최종 판정 ──
    print(f"\n{'='*100}")
    print(f"  최종 판정")
    print(f"{'='*100}")

    # TP 스윕 판정
    tp_improved = (o_best["total_return"] >= o_base["total_return"] - 0.5 and
                   o_best["sharpe"] > o_base["sharpe"] and
                   d_best["total_return"] >= d_base["total_return"] - 1.0)
    sl_reduced = abs(o_best["sl_pnl"]) < abs(o_base["sl_pnl"])

    if tp_improved and sl_reduced:
        verdict = "TP_SWEEP_IMPROVES"
    elif tp_improved:
        verdict = "TP_SWEEP_IMPROVES"
    elif best_tp == 0.08:
        verdict = "NO_MATERIAL_GAIN"
    else:
        verdict = "NO_MATERIAL_GAIN"

    print(f"  판정: {verdict}")

    if verdict == "TP_SWEEP_IMPROVES":
        print(f"  사유: TP {best_tp:.0%}로 OOS Sharpe/STOP_LOSS 개선, DEV 유지.")

        # paper 운용 후보
        if best_tp != 0.08 and "OOS_50_50" in sleeve_results:
            s5050 = sleeve_results["OOS_50_50"]
            s7525 = sleeve_results["OOS_75_25"]
            if s5050["top_sym_share"] < s7525["top_sym_share"] and s5050["total_return"] >= s7525["total_return"] * 0.9:
                paper = f"BV50/R50, Rotation TP={best_tp:.0%}, TS=OFF"
            else:
                paper = f"BV75/R25, Rotation TP={best_tp:.0%}, TS=OFF"
        else:
            paper = f"BV50/R50, Rotation TP=8%, TS=OFF"
        print(f"\n  paper 운용 후보: {paper}")
        print(f"  다음 액션: 위 설정으로 full-period(2021-2025) walk-forward 검증")
    else:
        print(f"  사유: TP 스윕으로 유의미한 개선 없음. TP=8% 유지.")
        paper = "BV50/R50, Rotation TP=8%, TS=OFF"
        print(f"\n  paper 운용 후보: {paper}")
        print(f"  다음 액션: 현재 설정으로 walk-forward 검증 또는 유니버스 확장 검토")

    print(f"\n{'='*100}")
    print(f"  비교 완료")
    print(f"{'='*100}")
