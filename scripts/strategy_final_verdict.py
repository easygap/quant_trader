"""
전략 최종 판정 — 수정 코드 기준 재평가
- 확장 유니버스: KOSPI 시총 상위 10종목 (유동성 필터 통과 대형주)
- 비교 기준: traded universe 동일비중 B&H (raw KOSPI가 아님)
- 수정된 WF 경로, 벤치마크 거래비용 반영
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="ERROR")

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester
from backtest.strategy_validator import StrategyValidator
from core.data_collector import DataCollector

# ── 확장 유니버스: KOSPI 시총 상위 10 (우선주 제외) ──
UNIVERSE_10 = [
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "005380",  # 현대차
    "207940",  # 삼성바이오로직스
    "012450",  # 한화에어로스페이스
    "000270",  # 기아
    "105560",  # KB금융
    "068270",  # 셀트리온
    "055550",  # 신한지주
    "006400",  # 삼성SDI
]

# Paper 실험 유니버스 (기존 4종목)
UNIVERSE_4 = ["005930", "000660", "035720", "051910"]

INITIAL_CAPITAL = 10_000_000
PERIOD_START = "2021-01-01"
PERIOD_END = "2025-12-31"
REPORT_START = "2023-01-01"

BV_CAPITAL = 5_000_000
ROT_CAPITAL = 5_000_000

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}

STRATEGIES = ["scoring", "breakout_volume", "relative_strength_rotation",
              "mean_reversion", "trend_following"]


def run_portfolio(strategy_name, symbols, capital=INITIAL_CAPITAL, div_override=None):
    config = Config.get()
    if div_override:
        div_cfg = config.risk_params.setdefault("diversification", {})
        saved = {k: div_cfg.get(k) for k in div_override}
        div_cfg.update(div_override)
    else:
        saved = None
    try:
        pbt = PortfolioBacktester(config)
        result = pbt.run(symbols=symbols, strategy_name=strategy_name,
                         initial_capital=capital, start_date=PERIOD_START, end_date=PERIOD_END)
    finally:
        if saved:
            div_cfg = config.risk_params.setdefault("diversification", {})
            for k, v in saved.items():
                if v is not None:
                    div_cfg[k] = v
    eq = result.get("equity_curve")
    if eq is not None and not eq.empty and "date" in eq.columns:
        result["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(REPORT_START)].copy()
    trades = result.get("trades", [])
    result["trades"] = [t for t in trades
                        if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(REPORT_START)]
    return result


def metrics(result, capital):
    eq = result.get("equity_curve")
    trades = result.get("trades", [])
    if eq is None or eq.empty:
        return dict.fromkeys(["total_return","sharpe","pf","mdd","win_rate","trades_n",
                              "turnover","sig_density","signal_buy","exec_buy","skipped"], 0)
    if "date" in eq.columns:
        eq = eq.set_index("date")
    final = float(eq["value"].iloc[-1])
    total_return = (final / capital - 1) * 100
    n_days = len(eq)
    years = n_days / 252
    dr = eq["value"].pct_change().dropna()
    d_mean = float(dr.mean()) if len(dr) > 0 else 0
    d_std = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (d_mean * 252 - 0.03) / (d_std * np.sqrt(252)) if d_std > 0 else 0
    peak = eq["value"].cummax()
    mdd = float(((eq["value"] - peak) / peak).min() * 100)
    sells = [t for t in trades if t.get("action") != "BUY"]
    n_trades = len(sells)
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
    wr = (wins / n_trades * 100) if n_trades else 0
    gp = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) > 0)
    gl = abs(sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) < 0))
    pf = gp / gl if gl > 0 else (99 if gp > 0 else 0)
    turnover = n_trades / max(years, 0.01)
    npos = eq.get("n_positions", pd.Series(0, index=eq.index))
    sig_d = float((npos > 0).sum()) / max(n_days, 1) * 100
    return {
        "total_return": round(total_return, 2), "sharpe": round(sharpe, 2),
        "pf": round(pf, 2), "mdd": round(mdd, 2),
        "win_rate": round(wr, 1), "trades_n": n_trades,
        "turnover": round(turnover, 1), "sig_density": round(sig_d, 1),
        "signal_buy": result.get("signal_buy_count", 0),
        "exec_buy": result.get("executed_buy_count", 0),
        "skipped": result.get("skipped_reasons", {}),
    }


def equal_weight_bh(symbols, capital, start, end, report_start):
    """traded universe 동일비중 B&H 벤치마크."""
    dc = DataCollector()
    per_sym = capital / len(symbols)
    equity_parts = []
    for sym in symbols:
        df = dc.fetch_korean_stock(sym, start, end)
        if df is None or df.empty:
            continue
        if "date" in df.columns:
            df = df.set_index("date")
        df = df[df.index >= pd.Timestamp(report_start)]
        if len(df) < 2:
            continue
        shares = per_sym / float(df["close"].iloc[0])
        eq = shares * df["close"].astype(float)
        equity_parts.append(eq)
    if not equity_parts:
        return 0.0, 0.0
    combined = pd.concat(equity_parts, axis=1).sum(axis=1).dropna()
    total_ret = (float(combined.iloc[-1]) / capital - 1) * 100
    # 거래비용 적용 (진입+청산)
    cost = 0.00015 + 0.0005 + 0.00015 + 0.0020 + 0.0005  # commission*2 + slippage*2 + tax
    total_ret_net = total_ret * (1 - cost)
    dr = combined.pct_change().dropna()
    d_std = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (float(dr.mean()) * 252 - 0.03) / (d_std * np.sqrt(252)) if d_std > 0 else 0
    return round(total_ret_net, 2), round(sharpe, 2)


def run_wf(strategy, symbol="005930"):
    try:
        config = Config.get()
        v = StrategyValidator(config)
        r = v.run_walk_forward(symbol=symbol, strategy_name=strategy,
                                start_date=PERIOD_START, end_date=PERIOD_END,
                                validation_years=5, train_days=504, test_days=252, step_days=252)
        return {
            "n": r.get("n_total", 0), "passed": r.get("n_passed", 0),
            "rate": round(r.get("pass_rate", 0) * 100, 1),
            "avg_sharpe": round(r.get("avg_oos_sharpe", 0), 2),
        }
    except Exception as e:
        return {"n": 0, "passed": 0, "rate": 0, "avg_sharpe": 0, "err": str(e)[:60]}


def run_bv50r50(symbols, bv_cap, rot_cap):
    config = Config.get()
    pbt = PortfolioBacktester(config)
    r_bv = pbt.run(symbols=symbols, strategy_name="breakout_volume",
                    initial_capital=bv_cap, start_date=PERIOD_START, end_date=PERIOD_END)

    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in ["max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio"]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt2 = PortfolioBacktester(config)
        r_rot = pbt2.run(symbols=symbols, strategy_name="relative_strength_rotation",
                          initial_capital=rot_cap, start_date=PERIOD_START, end_date=PERIOD_END)
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v

    def trim(r):
        eq = r.get("equity_curve")
        if eq is not None and not eq.empty and "date" in eq.columns:
            r["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(REPORT_START)].copy()
        r["trades"] = [t for t in r.get("trades", [])
                       if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(REPORT_START)]
        return r

    r_bv, r_rot = trim(r_bv), trim(r_rot)
    eq_bv = r_bv["equity_curve"].set_index("date") if "date" in r_bv["equity_curve"].columns else r_bv["equity_curve"]
    eq_rot = r_rot["equity_curve"].set_index("date") if "date" in r_rot["equity_curve"].columns else r_rot["equity_curve"]
    common = sorted(set(eq_bv.index) & set(eq_rot.index))
    rows = [{"date": d,
             "value": float(eq_bv.loc[d, "value"]) + float(eq_rot.loc[d, "value"]),
             "cash": float(eq_bv.loc[d, "cash"]) + float(eq_rot.loc[d, "cash"]),
             "n_positions": int(eq_bv.loc[d, "n_positions"]) + int(eq_rot.loc[d, "n_positions"])}
            for d in common]
    combined = {"equity_curve": pd.DataFrame(rows),
                "trades": r_bv.get("trades", []) + r_rot.get("trades", [])}
    return metrics(r_bv, bv_cap), metrics(r_rot, rot_cap), metrics(combined, bv_cap + rot_cap)


def main():
    print("=" * 110)
    print("  전략 최종 판정 — 수정 코드 기준 (벤치마크 거래비용 반영, WF 수정, --force-live 제거)")
    print("=" * 110)

    # ── A. 벤치마크 ──
    print("\n[A] 벤치마크 계산...")
    bh4_ret, bh4_sharpe = equal_weight_bh(UNIVERSE_4, INITIAL_CAPITAL, PERIOD_START, PERIOD_END, REPORT_START)
    bh10_ret, bh10_sharpe = equal_weight_bh(UNIVERSE_10, INITIAL_CAPITAL, PERIOD_START, PERIOD_END, REPORT_START)
    print(f"  4종목 동일비중 B&H (비용 반영): {bh4_ret}% (Sharpe {bh4_sharpe})")
    print(f"  10종목 동일비중 B&H (비용 반영): {bh10_ret}% (Sharpe {bh10_sharpe})")

    # ── B. 4종목 유니버스 (Paper 실험 세트) ──
    print("\n[B] 4종목 유니버스 (Paper 실험 세트)...")
    r4 = {}
    for s in STRATEGIES:
        print(f"  → {s}...", end=" ", flush=True)
        div = {"max_positions": 2, **ROTATION_DIV} if s == "relative_strength_rotation" else None
        r = run_portfolio(s, UNIVERSE_4, INITIAL_CAPITAL, div)
        m = metrics(r, INITIAL_CAPITAL)
        m["excess"] = round(m["total_return"] - bh4_ret, 2)
        r4[s] = m
        print(f"ret={m['total_return']}%")

    # BV50/R50
    print("  → BV50/R50...", end=" ", flush=True)
    bv4, rot4, comb4 = run_bv50r50(UNIVERSE_4, BV_CAPITAL, ROT_CAPITAL)
    comb4["excess"] = round(comb4["total_return"] - bh4_ret, 2)
    r4["BV50/R50"] = comb4
    r4["BV단독"] = bv4
    r4["Rot단독"] = rot4
    print(f"ret={comb4['total_return']}%")

    # ── C. 10종목 유니버스 ──
    print("\n[C] 10종목 유니버스 (시총 상위 대형주)...")
    r10 = {}
    for s in STRATEGIES:
        print(f"  → {s}...", end=" ", flush=True)
        div = {"max_positions": 2, **ROTATION_DIV} if s == "relative_strength_rotation" else None
        r = run_portfolio(s, UNIVERSE_10, INITIAL_CAPITAL, div)
        m = metrics(r, INITIAL_CAPITAL)
        m["excess"] = round(m["total_return"] - bh10_ret, 2)
        r10[s] = m
        print(f"ret={m['total_return']}%")

    bv10, rot10, comb10 = run_bv50r50(UNIVERSE_10, BV_CAPITAL, ROT_CAPITAL)
    comb10["excess"] = round(comb10["total_return"] - bh10_ret, 2)
    r10["BV50/R50"] = comb10
    print(f"  → BV50/R50 10종목: ret={comb10['total_return']}%")

    # ── D. Walk-Forward ──
    print("\n[D] Walk-Forward (005930, 수정 경로)...")
    wf = {}
    for s in STRATEGIES:
        print(f"  → {s}...", end=" ", flush=True)
        w = run_wf(s)
        wf[s] = w
        print(f"windows={w['n']}, passed={w['passed']}, rate={w['rate']}%")

    # ── E. 결과 출력 ──
    print("\n" + "=" * 110)
    print("  [E] 4종목 유니버스 비교표")
    print(f"  벤치마크: 4종목 동일비중 B&H {bh4_ret}% (Sharpe {bh4_sharpe})")
    print("=" * 110)
    hdr = f"  {'전략':<28} {'Ret%':>7} {'Shrp':>6} {'PF':>6} {'MDD%':>7} {'WR%':>5} {'Trd':>5} {'Turn':>5} {'SgD%':>5} {'Excs':>7} {'WF%':>5}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in STRATEGIES + ["BV단독", "Rot단독", "BV50/R50"]:
        m = r4.get(s, {})
        w = wf.get(s, {})
        wr = f"{w.get('rate', '--'):>4}" if isinstance(w.get('rate'), (int, float)) else "  --"
        print(f"  {s:<28} {m.get('total_return',0):>7.2f} {m.get('sharpe',0):>6.2f} "
              f"{m.get('pf',0):>6.2f} {m.get('mdd',0):>7.2f} {m.get('win_rate',0):>5.1f} "
              f"{m.get('trades_n',0):>5} {m.get('turnover',0):>5.1f} {m.get('sig_density',0):>5.1f} "
              f"{m.get('excess',0):>7.2f} {wr}%")

    print(f"\n  [E-2] 10종목 유니버스 비교표")
    print(f"  벤치마크: 10종목 동일비중 B&H {bh10_ret}% (Sharpe {bh10_sharpe})")
    print("  " + "-" * (len(hdr) - 2))
    for s in STRATEGIES + ["BV50/R50"]:
        m = r10.get(s, {})
        print(f"  {s:<28} {m.get('total_return',0):>7.2f} {m.get('sharpe',0):>6.2f} "
              f"{m.get('pf',0):>6.2f} {m.get('mdd',0):>7.2f} {m.get('win_rate',0):>5.1f} "
              f"{m.get('trades_n',0):>5} {m.get('turnover',0):>5.1f} {m.get('sig_density',0):>5.1f} "
              f"{m.get('excess',0):>7.2f}   --")

    # ── F. 신호 분석 ──
    print(f"\n  [F] 신호/체결/스킵 (4종목)")
    print(f"  {'전략':<28} {'BUY신호':>8} {'BUY체결':>8} {'Fill%':>7} {'스킵 상세'}")
    print("  " + "-" * 80)
    for s in STRATEGIES:
        m = r4.get(s, {})
        sig, exe = m.get("signal_buy", 0), m.get("exec_buy", 0)
        fill = (exe / sig * 100) if sig > 0 else 0
        sk = m.get("skipped", {})
        sk_str = ", ".join(f"{k}({v})" for k, v in sorted(sk.items(), key=lambda x: -x[1])[:3]) if sk else "-"
        print(f"  {s:<28} {sig:>8} {exe:>8} {fill:>6.1f}% {sk_str}")

    # ── G. WF 상세 ──
    print(f"\n  [G] Walk-Forward 상세")
    print(f"  {'전략':<28} {'Windows':>8} {'Passed':>8} {'Rate%':>7} {'AvgShrp':>8}")
    print("  " + "-" * 62)
    for s in STRATEGIES:
        w = wf.get(s, {})
        print(f"  {s:<28} {w.get('n',0):>8} {w.get('passed',0):>8} "
              f"{w.get('rate',0):>6.1f}% {w.get('avg_sharpe',0):>8.2f}")

    print("\n" + "=" * 110)


if __name__ == "__main__":
    main()
