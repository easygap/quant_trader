"""
C-5: breakout_volume + relative_strength_rotation 2-sleeve 포트폴리오 비교
- breakout_volume 단독 (10M)
- rotation 단독 (10M, max_positions=2)
- 50/50 sleeve 결합 (각 5M)
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
START = "2024-01-01"
END = "2025-12-31"
INITIAL_CAPITAL = 10_000_000
HALF_CAPITAL = 5_000_000


def run_backtest(strategy_name, capital, max_positions_override=None, extra_div=None):
    """Portfolio backtest with optional diversification config overrides."""
    config = Config.get()
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
            start_date=START,
            end_date=END,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v
            elif k in div_cfg and k in (extra_div or {}):
                del div_cfg[k]


ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}


def calc_metrics(equity_df, trades, initial_capital):
    """Calculate standard metrics from equity curve and trades."""
    if equity_df is None or equity_df.empty:
        return {"total_return": 0, "cagr": 0, "mdd": 0, "sharpe": 0}

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

    avg_pos = float(equity_df["n_positions"].mean()) if "n_positions" in equity_df else 0
    dim = int((equity_df["n_positions"] > 0).sum()) if "n_positions" in equity_df else 0
    max_conc = int(equity_df["n_positions"].max()) if "n_positions" in equity_df else 0
    total_days = len(equity_df)

    per_sym = {}
    for t in sell:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + t.get("pnl", 0)
    total_pnl = sum(per_sym.values())
    top_share = (
        max(abs(v) for v in per_sym.values()) / abs(total_pnl) * 100
        if total_pnl != 0 and per_sym else 0
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
        "max_concurrent": max_conc,
        "total_days": total_days,
        "per_sym_pnl": per_sym,
        "top_sym_share": round(top_share, 1),
        "final_value": round(final, 0),
    }


def combine_sleeves(res_a, res_b):
    """Merge two sleeve equity curves into one combined portfolio."""
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


def print_signal_density(strategy_name, result):
    """Print signal density diagnostics."""
    trades = result.get("trades", [])
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] != "BUY"]
    buy_dates = set(str(t["date"])[:10] for t in buys)
    print(f"    BUY 발생 일수: {len(buy_dates)}")
    print(f"    BUY 건수: {len(buys)}, SELL 건수: {len(sells)}")
    per_sym = {}
    for t in buys:
        per_sym[t["symbol"]] = per_sym.get(t["symbol"], 0) + 1
    for sym in SYMBOLS:
        print(f"      {sym}: BUY {per_sym.get(sym, 0)}건")


if __name__ == "__main__":
    print("=" * 80)
    print("  C-5: 2-Sleeve 포트폴리오 비교")
    print("  breakout_volume + relative_strength_rotation")
    print(f"  유니버스: {SYMBOLS}")
    print(f"  기간: {START} ~ {END}")
    print("=" * 80)

    # ── 1. breakout_volume 단독 ──
    print("\n[1/5] breakout_volume 단독 (10M)...")
    r_bv = run_backtest("breakout_volume", INITIAL_CAPITAL)
    m_bv = calc_metrics(r_bv["equity_curve"], r_bv["trades"], INITIAL_CAPITAL)
    print_signal_density("breakout_volume", r_bv)

    # ── 2. rotation 단독 ──
    print("\n[2/5] relative_strength_rotation 단독 (10M, max_pos=2)...")
    r_rot = run_backtest(
        "relative_strength_rotation", INITIAL_CAPITAL,
        max_positions_override=2, extra_div=ROTATION_DIV,
    )
    m_rot = calc_metrics(r_rot["equity_curve"], r_rot["trades"], INITIAL_CAPITAL)
    print_signal_density("rotation", r_rot)

    # ── 3. BV sleeve (5M) ──
    print("\n[3/5] breakout_volume sleeve (5M)...")
    r_bv_half = run_backtest("breakout_volume", HALF_CAPITAL)

    # ── 4. Rotation sleeve (5M) ──
    print("\n[4/5] rotation sleeve (5M, max_pos=2)...")
    r_rot_half = run_backtest(
        "relative_strength_rotation", HALF_CAPITAL,
        max_positions_override=2, extra_div=ROTATION_DIV,
    )

    # ── 5. Combine ──
    print("\n[5/5] 50/50 결합 계산 중...")
    comb = combine_sleeves(r_bv_half, r_rot_half)
    m_comb = calc_metrics(comb["equity_curve"], comb["trades"], INITIAL_CAPITAL)

    # ============================================================
    # Results
    # ============================================================
    print("\n" + "=" * 80)
    print("  OOS 결과 비교표")
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
        ("max_concurrent", "max_concurrent"),
        ("top_sym_share (%)", "top_sym_share"),
    ]

    print(f"\n  {'지표':<22} {'BV단독':>12} {'Rotation':>12} {'50/50':>12}")
    print(f"  {'-' * 22} {'-' * 12} {'-' * 12} {'-' * 12}")
    for label, key in labels:
        vals = [m_bv.get(key, 0), m_rot.get(key, 0), m_comb.get(key, 0)]
        fmt = [f"{v:>12.2f}" if isinstance(v, float) else f"{v:>12}" for v in vals]
        print(f"  {label:<22} {''.join(fmt)}")

    # ── Per-symbol PnL ──
    print(f"\n  종목별 PnL 기여도:")
    print(f"  {'종목':<8} {'BV단독':>12} {'Rotation':>12} {'50/50':>12}")
    print(f"  {'-' * 8} {'-' * 12} {'-' * 12} {'-' * 12}")
    for sym in SYMBOLS:
        bv_p = m_bv.get("per_sym_pnl", {}).get(sym, 0)
        rot_p = m_rot.get("per_sym_pnl", {}).get(sym, 0)
        co_p = m_comb.get("per_sym_pnl", {}).get(sym, 0)
        print(f"  {sym:<8} {bv_p:>12,.0f} {rot_p:>12,.0f} {co_p:>12,.0f}")

    # ── Judgment ──
    bv_ret = m_bv["total_return"]
    rot_ret = m_rot["total_return"]
    co_ret = m_comb["total_return"]
    bv_sh = m_bv["sharpe"]
    co_sh = m_comb["sharpe"]
    co_mdd = m_comb["mdd"]
    bv_ap = m_bv["avg_positions"]
    co_ap = m_comb["avg_positions"]
    bv_dim = m_bv["days_in_market"]
    co_dim = m_comb["days_in_market"]

    if rot_ret < -2 and co_ret <= bv_ret:
        verdict = "ROTATION_WEAK"
    elif co_ret > bv_ret and co_sh > bv_sh and co_mdd > -4:
        verdict = "MULTI_STRATEGY_IMPROVES"
    elif co_ret > bv_ret and co_mdd > -4:
        verdict = "MULTI_STRATEGY_MARGINAL"
    else:
        verdict = "NO_BENEFIT_OVER_BREAKOUT"

    print(f"\n{'=' * 80}")
    print(f"  판정: {verdict}")
    print(f"{'=' * 80}")
    print(f"\n  판정 근거:")
    print(f"    50/50 return ({co_ret:.2f}%) vs BV ({bv_ret:.2f}%): "
          f"{'개선' if co_ret > bv_ret else '악화/동일'}")
    print(f"    50/50 Sharpe ({co_sh:.2f}) vs BV ({bv_sh:.2f}): "
          f"{'개선' if co_sh > bv_sh else '악화/동일'}")
    print(f"    50/50 MDD ({co_mdd:.2f}%) vs -4% gate: "
          f"{'통과' if co_mdd > -4 else '실패'}")
    print(f"    avg_positions ({co_ap:.2f}) vs BV ({bv_ap:.2f}): "
          f"{'증가' if co_ap > bv_ap else '동일/감소'}")
    print(f"    days_in_market ({co_dim}) vs BV ({bv_dim}): "
          f"{'증가' if co_dim > bv_dim else '동일/감소'}")

    print(f"\n  Rotation 단독 평가:")
    rot_dim = m_rot["days_in_market"]
    rot_ap = m_rot["avg_positions"]
    print(f"    return: {rot_ret:.2f}%")
    print(f"    days_in_market: {rot_dim} / {m_rot['total_days']}")
    print(f"    avg_positions: {rot_ap:.2f}")
    if rot_ret < 0:
        print(f"    → Rotation 단독 음수 수익률: 합산 시 BV 성과를 희석.")

    kr10 = "YES" if verdict == "MULTI_STRATEGY_IMPROVES" and rot_ret > 0 else "NO"
    print(f"\n  KR_CORE_10 확장 가치: {kr10}")
    if kr10 == "YES":
        print(f"    → Rotation 양호 — 넓은 유니버스에서 상대강도 분산이 개선될 여지 있음")
    else:
        print(f"    → 현재 4종목에서도 개선 미미 — 유니버스 확장보다 전략 자체 개선 우선")

    print("\n" + "=" * 80)
    print("  비교 완료")
    print("=" * 80)
