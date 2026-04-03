"""
C-5 BV50/R50 Paper Trading 월간 리포트
- sleeve별 기여도 (BV / Rotation)
- 종목별 기여도
- all-days Sharpe / position-only Sharpe
- 경고/중단 규칙 자동 판정

사용법:
  python scripts/c5_paper_monthly_report.py [start_date] [end_date]
  예: python scripts/c5_paper_monthly_report.py 2025-01-01 2025-12-31
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
BV_CAPITAL = 5_000_000
ROT_CAPITAL = 5_000_000

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}

# 경고/중단 규칙
WARN_MONTHLY_RETURN = -3.0       # 월간 return <= -3% → 경고
WARN_MDD = -5.0                  # MDD <= -5% → 경고
WARN_CONSECUTIVE_LOSS = 3        # N개월 연속 음수 → 경고
HALT_MDD = -8.0                  # MDD <= -8% → 중단
DOWNGRADE_CONSECUTIVE = 3        # 3개월 연속 음수 → BV75/R25로 축소 검토

# Cash 수익 가정
CMA_ANNUAL_RATE = 0.025          # CMA/MMF 가정 연수익률 2.5%


def run_sleeve(start, end):
    """Run BV + Rotation sleeve and return individual + combined results.

    지표 lookback(Rotation 120일 등)을 위해 데이터 수집은 start보다 8개월 앞에서
    시작하되, equity curve와 trades는 원래 start~end 구간만 반환한다.
    """
    config = Config.get()

    # 지표 warmup용 데이터 수집 시작일 (8개월 전)
    fetch_start = (pd.Timestamp(start) - pd.DateOffset(months=8)).strftime("%Y-%m-%d")
    report_start = pd.Timestamp(start)

    # BV sleeve
    pbt = PortfolioBacktester(config)
    r_bv = pbt.run(symbols=SYMBOLS, strategy_name="breakout_volume",
                    initial_capital=BV_CAPITAL, start_date=fetch_start, end_date=end)

    # Rotation sleeve
    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in [
        "max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio",
    ]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt2 = PortfolioBacktester(config)
        r_rot = pbt2.run(symbols=SYMBOLS, strategy_name="relative_strength_rotation",
                          initial_capital=ROT_CAPITAL, start_date=fetch_start, end_date=end)
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v

    # equity curve / trades를 리포트 구간(start~end)으로 트리밍
    def _trim(result, report_start_ts):
        eq = result.get("equity_curve")
        if eq is None or eq.empty:
            return result
        if "date" in eq.columns:
            mask = pd.to_datetime(eq["date"]) >= report_start_ts
        else:
            mask = eq.index >= report_start_ts
        eq_trimmed = eq[mask].copy()
        trades_trimmed = [
            t for t in result.get("trades", [])
            if pd.Timestamp(t["date"]) >= report_start_ts
        ]
        trimmed = dict(result)
        trimmed["equity_curve"] = eq_trimmed
        trimmed["trades"] = trades_trimmed
        return trimmed

    r_bv = _trim(r_bv, report_start)
    r_rot = _trim(r_rot, report_start)

    # Combine
    eq_bv = r_bv["equity_curve"].set_index("date") if "date" in r_bv["equity_curve"].columns else r_bv["equity_curve"]
    eq_rot = r_rot["equity_curve"].set_index("date") if "date" in r_rot["equity_curve"].columns else r_rot["equity_curve"]
    common = sorted(set(eq_bv.index) & set(eq_rot.index))
    rows = [{"date": d,
             "value": float(eq_bv.loc[d, "value"]) + float(eq_rot.loc[d, "value"]),
             "cash": float(eq_bv.loc[d, "cash"]) + float(eq_rot.loc[d, "cash"]),
             "n_positions": int(eq_bv.loc[d, "n_positions"]) + int(eq_rot.loc[d, "n_positions"])}
            for d in common]

    return {
        "bv": r_bv, "rot": r_rot,
        "combined": {"equity_curve": pd.DataFrame(rows),
                     "trades": r_bv["trades"] + r_rot["trades"]},
    }


def compute_metrics(equity_df, trades, initial_capital):
    if equity_df is None or equity_df.empty:
        return {}

    final = float(equity_df["value"].iloc[-1])
    total_return = (final / initial_capital - 1) * 100
    years = len(equity_df) / 252
    cagr = ((final / initial_capital) ** (1 / max(years, 0.01)) - 1) * 100

    dr = equity_df["value"].pct_change().dropna()
    daily_mean = float(dr.mean()) if len(dr) > 0 else 0
    daily_std = float(dr.std()) if len(dr) > 1 else 0

    # all-days Sharpe
    ann_ret = daily_mean * 252
    ann_std = daily_std * np.sqrt(252)
    sharpe_all = (ann_ret - 0.03) / ann_std if ann_std > 0 else 0

    # position-only Sharpe
    n_pos_col = equity_df["n_positions"] if "n_positions" in equity_df else pd.Series(0, index=equity_df.index)
    pos_mask = n_pos_col.iloc[1:].values > 0
    dr_pos = dr[pos_mask] if len(pos_mask) == len(dr) else dr
    if len(dr_pos) > 1 and dr_pos.std() > 0:
        pos_ann_ret = float(dr_pos.mean()) * 252
        pos_ann_std = float(dr_pos.std()) * np.sqrt(252)
        sharpe_pos = (pos_ann_ret - 0.03) / pos_ann_std
    else:
        sharpe_pos = 0
        pos_ann_ret = 0

    peak = equity_df["value"].cummax()
    mdd = ((equity_df["value"] - peak) / peak).min() * 100

    avg_pos = float(n_pos_col.mean())
    dim = int((n_pos_col > 0).sum())
    total_days = len(equity_df)
    cash_days = total_days - dim

    sell = [t for t in trades if t.get("action") != "BUY"]
    total_trades = len(sell)
    wins = sum(1 for t in sell if t.get("pnl", 0) > 0)
    win_rate = wins / total_trades * 100 if total_trades else 0

    per_sym = {}
    for t in sell:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + t.get("pnl", 0)
    total_pnl = sum(per_sym.values())
    top_share = max(abs(v) for v in per_sym.values()) / abs(total_pnl) * 100 if total_pnl != 0 and per_sym else 0

    # cash 수익 가정 포함 Sharpe
    cma_daily = CMA_ANNUAL_RATE / 252
    dr_adj = dr.copy()
    if len(pos_mask) == len(dr):
        cash_mask = ~pos_mask
        dr_adj.iloc[np.where(cash_mask)[0]] = cma_daily
    adj_ann_ret = float(dr_adj.mean()) * 252
    adj_ann_std = float(dr_adj.std()) * np.sqrt(252)
    sharpe_adj = (adj_ann_ret - 0.03) / adj_ann_std if adj_ann_std > 0 else 0

    # signal density (투자비중)
    signal_density = dim / max(total_days, 1) * 100
    # turnover (연간 왕복)
    turnover = total_trades / max(years, 0.01)
    # profit factor
    gp = sum(t.get("pnl", 0) for t in sell if t.get("pnl", 0) > 0)
    gl = abs(sum(t.get("pnl", 0) for t in sell if t.get("pnl", 0) < 0))
    profit_factor = gp / gl if gl > 0 else (99 if gp > 0 else 0)
    # cash-adjusted return
    cash_frac = cash_days / max(total_days, 1)
    cash_adj_return = total_return + CMA_ANNUAL_RATE * cash_frac * 100 * years

    return {
        "total_return": round(total_return, 2),
        "cagr": round(cagr, 2),
        "mdd": round(mdd, 2),
        "sharpe_all": round(sharpe_all, 2),
        "sharpe_pos": round(sharpe_pos, 2),
        "sharpe_adj": round(sharpe_adj, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "avg_positions": round(avg_pos, 2),
        "days_in_market": dim,
        "cash_days": cash_days,
        "total_days": total_days,
        "signal_density": round(signal_density, 1),
        "turnover": round(turnover, 1),
        "cash_adj_return": round(cash_adj_return, 2),
        "top_sym_share": round(top_share, 1),
        "per_sym_pnl": per_sym,
        "ann_return": round(ann_ret * 100, 2),
        "ann_std": round(ann_std * 100, 2),
        "pos_ann_return": round(pos_ann_ret * 100, 2) if pos_ann_ret else 0,
    }


def monthly_breakdown(equity_df, initial_capital):
    """월별 수익률 분해."""
    if equity_df is None or equity_df.empty:
        return []

    eq = equity_df.copy()
    if "date" in eq.columns:
        eq["date"] = pd.to_datetime(eq["date"])
        eq = eq.set_index("date")

    months = eq.index.to_period("M").unique()
    results = []
    for m in months:
        mask = eq.index.to_period("M") == m
        mdata = eq[mask]
        if mdata.empty:
            continue
        start_val = float(mdata["value"].iloc[0])
        end_val = float(mdata["value"].iloc[-1])
        ret = (end_val / start_val - 1) * 100 if start_val > 0 else 0
        peak = mdata["value"].cummax()
        mdd = ((mdata["value"] - peak) / peak).min() * 100
        results.append({
            "month": str(m),
            "return": round(ret, 2),
            "mdd": round(mdd, 2),
            "end_value": round(end_val, 0),
        })
    return results


def print_report(start, end):
    print("=" * 90)
    print(f"  C-5 BV50/R50 Paper Monthly Report")
    print(f"  기간: {start} ~ {end}")
    print(f"  설정: BV(TP=8%, TS=5%) + Rotation(TP=7%, TS=OFF)")
    print("=" * 90)

    result = run_sleeve(start, end)

    # Combined metrics
    m_comb = compute_metrics(result["combined"]["equity_curve"],
                              result["combined"]["trades"], INITIAL_CAPITAL)
    m_bv = compute_metrics(result["bv"]["equity_curve"],
                            result["bv"]["trades"], BV_CAPITAL)
    m_rot = compute_metrics(result["rot"]["equity_curve"],
                             result["rot"]["trades"], ROT_CAPITAL)

    # ── 전체 성과 ──
    print(f"\n  전체 성과 (BV50/R50 합산)")
    print(f"  {'지표':<28} {'값':>12}")
    print(f"  {'-'*28} {'-'*12}")
    items = [
        ("total_return (%)", m_comb["total_return"]),
        ("CAGR (%)", m_comb["cagr"]),
        ("MDD (%)", m_comb["mdd"]),
        ("all-days Sharpe", m_comb["sharpe_all"]),
        ("position-only Sharpe", m_comb["sharpe_pos"]),
        ("CMA-adjusted Sharpe", m_comb["sharpe_adj"]),
        ("total_trades", m_comb["total_trades"]),
        ("win_rate (%)", m_comb["win_rate"]),
        ("avg_positions", m_comb["avg_positions"]),
        ("days_in_market", m_comb["days_in_market"]),
        ("cash_days", m_comb["cash_days"]),
        ("top_sym_share (%)", m_comb["top_sym_share"]),
    ]
    for lbl, v in items:
        fmt = f"{v:>12.2f}" if isinstance(v, float) else f"{v:>12}"
        print(f"  {lbl:<28} {fmt}")

    # ── Sleeve별 기여도 ──
    print(f"\n  Sleeve별 기여도")
    print(f"  {'지표':<28} {'BV':>12} {'Rotation':>12}")
    print(f"  {'-'*28} {'-'*12} {'-'*12}")
    for lbl, kb, kr in [
        ("total_return (%)", m_bv["total_return"], m_rot["total_return"]),
        ("MDD (%)", m_bv["mdd"], m_rot["mdd"]),
        ("total_trades", m_bv["total_trades"], m_rot["total_trades"]),
        ("win_rate (%)", m_bv["win_rate"], m_rot["win_rate"]),
        ("days_in_market", m_bv["days_in_market"], m_rot["days_in_market"]),
    ]:
        fb = f"{kb:>12.2f}" if isinstance(kb, float) else f"{kb:>12}"
        fr = f"{kr:>12.2f}" if isinstance(kr, float) else f"{kr:>12}"
        print(f"  {lbl:<28} {fb} {fr}")

    # ── Sleeve별 신호/체결/스킵 ──
    print(f"\n  Sleeve별 신호/체결/스킵")
    print(f"  {'지표':<28} {'BV':>12} {'Rotation':>12}")
    print(f"  {'-'*28} {'-'*12} {'-'*12}")

    bv_sig_buy = result["bv"].get("signal_buy_count", 0)
    bv_sig_sell = result["bv"].get("signal_sell_count", 0)
    bv_exec_buy = result["bv"].get("executed_buy_count", 0)
    bv_exec_sell = result["bv"].get("executed_sell_count", 0)
    bv_skipped = result["bv"].get("skipped_reasons", {})

    rot_sig_buy = result["rot"].get("signal_buy_count", 0)
    rot_sig_sell = result["rot"].get("signal_sell_count", 0)
    rot_exec_buy = result["rot"].get("executed_buy_count", 0)
    rot_exec_sell = result["rot"].get("executed_sell_count", 0)
    rot_skipped = result["rot"].get("skipped_reasons", {})

    for lbl, vb, vr in [
        ("BUY signal_count", bv_sig_buy, rot_sig_buy),
        ("BUY executed_count", bv_exec_buy, rot_exec_buy),
        ("BUY skipped_count", bv_sig_buy - bv_exec_buy, rot_sig_buy - rot_exec_buy),
        ("SELL signal_count", bv_sig_sell, rot_sig_sell),
        ("SELL executed_count", bv_exec_sell, rot_exec_sell),
    ]:
        print(f"  {lbl:<28} {vb:>12} {vr:>12}")

    # skipped reason breakdown
    all_skip_keys = sorted(set(list(bv_skipped.keys()) + list(rot_skipped.keys())))
    if all_skip_keys:
        print(f"\n  스킵 사유 breakdown")
        print(f"  {'사유':<28} {'BV':>12} {'Rotation':>12}")
        print(f"  {'-'*28} {'-'*12} {'-'*12}")
        for reason in all_skip_keys:
            vb = bv_skipped.get(reason, 0)
            vr = rot_skipped.get(reason, 0)
            print(f"  {reason:<28} {vb:>12} {vr:>12}")

    # ── 종목별 기여도 ──
    print(f"\n  종목별 PnL")
    print(f"  {'종목':<8} {'BV':>12} {'Rotation':>12} {'합산':>12}")
    print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*12}")
    for sym in SYMBOLS:
        bv_p = m_bv.get("per_sym_pnl", {}).get(sym, 0)
        rot_p = m_rot.get("per_sym_pnl", {}).get(sym, 0)
        print(f"  {sym:<8} {bv_p:>12,.0f} {rot_p:>12,.0f} {bv_p+rot_p:>12,.0f}")

    # ── 월별 추이 ──
    monthly = monthly_breakdown(result["combined"]["equity_curve"], INITIAL_CAPITAL)
    if monthly:
        print(f"\n  월별 추이")
        print(f"  {'월':<10} {'return%':>10} {'MDD%':>10} {'end_value':>14}")
        print(f"  {'-'*10} {'-'*10} {'-'*10} {'-'*14}")
        for m in monthly:
            print(f"  {m['month']:<10} {m['return']:>10.2f} {m['mdd']:>10.2f} {m['end_value']:>14,.0f}")

    # ── Sharpe 해설 ──
    print(f"\n  Sharpe 해설")
    print(f"    all-days Sharpe ({m_comb['sharpe_all']:.2f}): "
          f"cash days 포함. ann_return={m_comb['ann_return']:.2f}%, ann_std={m_comb['ann_std']:.2f}%")
    print(f"    position-only Sharpe ({m_comb['sharpe_pos']:.2f}): "
          f"포지션 보유일만. pos_ann_return={m_comb['pos_ann_return']:.2f}%")
    print(f"    CMA-adjusted Sharpe ({m_comb['sharpe_adj']:.2f}): "
          f"cash days에 CMA {CMA_ANNUAL_RATE:.1%} 가정 적용")
    print(f"    → 의사결정 기준: CMA-adjusted Sharpe (현실에 가장 근접)")

    # ── 경고/중단 규칙 판정 ──
    print(f"\n{'='*90}")
    print(f"  경고/중단 규칙 판정")
    print(f"{'='*90}")

    alerts = []

    # 월간 return 경고
    for m in monthly:
        if m["return"] <= WARN_MONTHLY_RETURN:
            alerts.append(f"WARNING: {m['month']} return {m['return']:.2f}% <= {WARN_MONTHLY_RETURN}%")

    # MDD 경고
    if m_comb["mdd"] <= WARN_MDD:
        alerts.append(f"WARNING: MDD {m_comb['mdd']:.2f}% <= {WARN_MDD}%")
    if m_comb["mdd"] <= HALT_MDD:
        alerts.append(f"HALT: MDD {m_comb['mdd']:.2f}% <= {HALT_MDD}% → paper 즉시 중단")

    # 연속 음수
    if monthly:
        consec = 0
        max_consec = 0
        for m in monthly:
            if m["return"] < 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0
        if max_consec >= WARN_CONSECUTIVE_LOSS:
            alerts.append(
                f"WARNING: {max_consec}개월 연속 음수 → "
                f"{'BV75/R25로 축소 검토' if max_consec >= DOWNGRADE_CONSECUTIVE else '모니터링 강화'}")

    if alerts:
        for a in alerts:
            print(f"  {a}")
    else:
        print(f"  → 경고 없음. 정상 운용 범위.")

    # 종합
    has_halt = any("HALT" in a for a in alerts)
    has_warn = any("WARNING" in a for a in alerts)

    if has_halt:
        status = "HALT — paper 중단 필요"
    elif has_warn:
        status = "MONITOR — 경고 발생, 모니터링 강화"
    else:
        status = "NORMAL — 정상 운용"

    print(f"\n  종합 상태: {status}")
    print(f"\n{'='*90}")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        start, end = "2025-01-01", "2025-12-31"

    print_report(start, end)
