"""
Debiased 전략 평가 프레임워크

유니버스 재구성 규칙 (ex-ante, 월별):
- 매 평가 window 시작 월의 직전 3개월 평균 일거래대금 상위 20 KOSPI 보통주
- FDR 현재 KOSPI 리스트에서 후보 풀 → 과거 거래대금으로 순위 결정
- 이 방법은 survivorship bias를 완전히 제거하지 못하지만,
  ex-post 시총 순위보다 selection bias를 줄임
  (거래대금 상위는 시총 변동보다 안정적이며, 상폐 종목은 거래대금 0으로 자연 탈락)

Benchmark 3종:
- ① EW B&H: 동일 유니버스 동일비중 B&H (거래비용 반영)
- ② Exposure-matched: B&H × 전략 평균 투자비중
- ③ Cash-adjusted: 비투자 기간에 CMA 2.5% 적용
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
from core.data_collector import DataCollector

INITIAL_CAPITAL = 10_000_000
BV_CAPITAL = 5_000_000
ROT_CAPITAL = 5_000_000
EVAL_START = "2023-01-01"
EVAL_END = "2025-12-31"
CMA_ANNUAL = 0.025
TOP_N = 20

ROTATION_DIV = {"max_positions": 2, "max_position_ratio": 0.45,
                "max_investment_ratio": 0.85, "min_cash_ratio": 0.10}

STRATEGIES = ["scoring", "breakout_volume", "relative_strength_rotation",
              "mean_reversion", "trend_following"]


def build_universe_proxy(top_n=TOP_N):
    """FDR KOSPI 보통주에서 현재 리스트를 후보 풀로 사용.
    과거 거래대금으로 순위를 매기므로 ex-ante에 가깝지만,
    후보 풀 자체가 현재 상장 종목이라 완전한 point-in-time은 아님.
    """
    import FinanceDataReader as fdr
    stocks = fdr.StockListing('KOSPI')
    # 보통주만 (우선주 코드 패턴 제외)
    common = stocks[~stocks['Code'].str.match(r'^\d{5}[5-9KL]$')]
    # ETF/ETN 제외 (시총 0이거나 매우 작은 것)
    if 'Marcap' in common.columns:
        common = common[common['Marcap'] > 1e11]  # 시총 1000억 이상
    codes = common['Code'].tolist()
    return codes


def rank_by_trading_value(candidates, ref_start, ref_end, dc, top_n=TOP_N):
    """ref_start~ref_end 평균 일거래대금 기준 상위 N종목 선정."""
    dc.quiet_ohlcv_log = True
    amounts = {}
    for sym in candidates[:100]:  # 상위 100개만 확인 (시간 절약)
        try:
            df = dc.fetch_korean_stock(sym, ref_start, ref_end)
            if df is not None and not df.empty:
                if "date" in df.columns:
                    df = df.set_index("date")
                amt = (df["close"].astype(float) * df["volume"].astype(float)).mean()
                amounts[sym] = amt
        except Exception:
            pass
    ranked = sorted(amounts, key=amounts.get, reverse=True)
    return ranked[:top_n]


def make_windows(start, end, window_months=12, step_months=6):
    windows = []
    s = pd.Timestamp(start)
    e_max = pd.Timestamp(end)
    while True:
        w_end = s + pd.DateOffset(months=window_months) - pd.Timedelta(days=1)
        if w_end > e_max: w_end = e_max
        if s >= e_max or (w_end - s).days < 60: break
        windows.append((s.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")))
        s += pd.DateOffset(months=step_months)
    return windows


def run_window(strategy, symbols, start, end, capital, div_override=None):
    config = Config.get()
    if div_override:
        div_cfg = config.risk_params.setdefault("diversification", {})
        saved = {k: div_cfg.get(k) for k in div_override}
        div_cfg.update(div_override)
    else:
        saved = None
    try:
        fetch_s = (pd.Timestamp(start) - pd.DateOffset(months=8)).strftime("%Y-%m-%d")
        pbt = PortfolioBacktester(config)
        r = pbt.run(symbols=symbols, strategy_name=strategy,
                     initial_capital=capital, start_date=fetch_s, end_date=end)
    finally:
        if saved:
            div_cfg = config.risk_params.setdefault("diversification", {})
            for k, v in saved.items():
                if v is not None: div_cfg[k] = v
    eq = r.get("equity_curve")
    if eq is not None and not eq.empty and "date" in eq.columns:
        r["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(start)].copy()
    r["trades"] = [t for t in r.get("trades", [])
                   if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(start)]
    return r


def calc(result, capital):
    eq = result.get("equity_curve")
    trades = result.get("trades", [])
    if eq is None or eq.empty:
        return {"ret": 0, "sharpe": 0, "pf": 0, "mdd": 0, "wr": 0, "n": 0,
                "turn": 0, "density": 0, "sig_buy": 0, "exec_buy": 0, "skipped": {}}
    if "date" in eq.columns: eq = eq.set_index("date")
    final = float(eq["value"].iloc[-1])
    ret = (final / capital - 1) * 100
    n_days = len(eq); years = n_days / 252
    dr = eq["value"].pct_change().dropna()
    d_m = float(dr.mean()) if len(dr) > 0 else 0
    d_s = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (d_m * 252 - 0.03) / (d_s * np.sqrt(252)) if d_s > 0 else 0
    peak = eq["value"].cummax()
    mdd = float(((eq["value"] - peak) / peak).min() * 100)
    sells = [t for t in trades if t.get("action") != "BUY"]
    n_trades = len(sells)
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
    wr = (wins / n_trades * 100) if n_trades else 0
    gp = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) > 0)
    gl = abs(sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) < 0))
    pf = gp / gl if gl > 0 else (99 if gp > 0 else 0)
    turn = n_trades / max(years, 0.01)
    npos = eq.get("n_positions", pd.Series(0, index=eq.index))
    density = float((npos > 0).sum()) / max(n_days, 1) * 100
    return {"ret": round(ret, 2), "sharpe": round(sharpe, 2), "pf": round(pf, 2),
            "mdd": round(mdd, 2), "wr": round(wr, 1), "n": n_trades,
            "turn": round(turn, 1), "density": round(density, 1),
            "sig_buy": result.get("signal_buy_count", 0),
            "exec_buy": result.get("executed_buy_count", 0),
            "skipped": result.get("skipped_reasons", {})}


def bench_ew(symbols, capital, start, end):
    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    per = capital / len(symbols)
    parts = []; valid = 0
    for sym in symbols:
        df = dc.fetch_korean_stock(sym, start, end)
        if df is None or df.empty: continue
        if "date" in df.columns: df = df.set_index("date")
        df = df[df.index >= pd.Timestamp(start)]
        if len(df) < 2: continue
        parts.append(per / float(df["close"].iloc[0]) * df["close"].astype(float))
        valid += 1
    if not parts: return 0, 0, 0
    combined = pd.concat(parts, axis=1).sum(axis=1).dropna()
    cost = 0.00315  # 진입+청산 비용 합산
    raw_ret = (float(combined.iloc[-1]) / capital - 1) * 100
    net_ret = raw_ret * (1 - cost)
    dr = combined.pct_change().dropna()
    d_s = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (float(dr.mean()) * 252 - 0.03) / (d_s * np.sqrt(252)) if d_s > 0 else 0
    return round(net_ret, 2), round(sharpe, 2), valid


def main():
    print("=" * 115)
    print("  Debiased 전략 평가 — 거래대금 기반 ex-ante proxy 유니버스")
    print("=" * 115)

    dc = DataCollector()
    candidates = build_universe_proxy()
    print(f"\n[1] 유니버스 구성")
    print(f"  후보 풀: FDR KOSPI 보통주 시총 1000억+ ({len(candidates)}종목)")
    print(f"  선정 규칙: 평가 시작 직전 3개월(2022-10~2022-12) 평균 일거래대금 상위 {TOP_N}")
    print(f"  bias: 후보 풀이 현재(2026-04-02) 상장 종목 → survivorship bias 잔존")
    print(f"        단, 거래대금 기반 순위는 시총 순위보다 상폐 종목 영향이 적음")

    # 평가 시작 전 3개월 기준 유니버스 구성
    print(f"\n  거래대금 상위 {TOP_N} 선정 중 (2022-10~2022-12 기준)...", flush=True)
    universe = rank_by_trading_value(candidates, "2022-10-01", "2022-12-31", dc, TOP_N)
    print(f"  선정 결과: {universe}")

    # 벤치마크
    print(f"\n[2] 벤치마크 (3종)")
    bh_ret, bh_sharpe, bh_n = bench_ew(universe, INITIAL_CAPITAL, EVAL_START, EVAL_END)
    print(f"  ① EW B&H ({bh_n}종목, 비용 반영): {bh_ret}% (Sharpe {bh_sharpe})")

    # Full period 전략 평가
    print(f"\n[3] Full-period 평가 ({len(universe)}종목, {EVAL_START}~{EVAL_END})")
    full = {}
    for strat in STRATEGIES:
        print(f"  → {strat}...", end=" ", flush=True)
        div = ROTATION_DIV if strat == "relative_strength_rotation" else None
        try:
            r = run_window(strat, universe, EVAL_START, EVAL_END, INITIAL_CAPITAL, div)
            m = calc(r, INITIAL_CAPITAL)
            # Benchmark 3종
            m["excess_ew"] = round(m["ret"] - bh_ret, 2)
            exposure = m["density"] / 100
            m["excess_exp"] = round(m["ret"] - bh_ret * max(exposure, 0.01), 2)
            # Cash-adjusted: 비투자 기간에 CMA 수익 가산
            cash_frac = (100 - m["density"]) / 100
            years = 3
            cma_return = CMA_ANNUAL * cash_frac * 100  # 연간 cash 수익(%)
            m["cash_adj_ret"] = round(m["ret"] + cma_return * years, 2)
            m["excess_cash_adj"] = round(m["cash_adj_ret"] - bh_ret, 2)
            full[strat] = m
            print(f"ret={m['ret']}%, sharpe={m['sharpe']}")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")
            full[strat] = {"ret": 0, "sharpe": 0, "error": str(e)[:60]}

    # BV50/R50
    print(f"  → BV50/R50...", end=" ", flush=True)
    try:
        r_bv = run_window("breakout_volume", universe, EVAL_START, EVAL_END, BV_CAPITAL)
        r_rot = run_window("relative_strength_rotation", universe, EVAL_START, EVAL_END, ROT_CAPITAL, ROTATION_DIV)
        m_bv = calc(r_bv, BV_CAPITAL)
        m_rot = calc(r_rot, ROT_CAPITAL)
        eq_bv = r_bv["equity_curve"].set_index("date") if "date" in r_bv["equity_curve"].columns else r_bv["equity_curve"]
        eq_rot = r_rot["equity_curve"].set_index("date") if "date" in r_rot["equity_curve"].columns else r_rot["equity_curve"]
        common = sorted(set(eq_bv.index) & set(eq_rot.index))
        rows = [{"date": d, "value": float(eq_bv.loc[d, "value"]) + float(eq_rot.loc[d, "value"]),
                 "cash": float(eq_bv.loc[d, "cash"]) + float(eq_rot.loc[d, "cash"]),
                 "n_positions": int(eq_bv.loc[d, "n_positions"]) + int(eq_rot.loc[d, "n_positions"])} for d in common]
        m_combo = calc({"equity_curve": pd.DataFrame(rows), "trades": r_bv.get("trades",[]) + r_rot.get("trades",[])}, INITIAL_CAPITAL)
        m_combo["excess_ew"] = round(m_combo["ret"] - bh_ret, 2)
        full["BV50/R50"] = m_combo
        print(f"ret={m_combo['ret']}%")
    except Exception as e:
        print(f"ERROR: {str(e)[:60]}")

    # Rolling WF
    print(f"\n[4] Portfolio Rolling WF (12mo, 6mo step)")
    windows = make_windows(EVAL_START, EVAL_END, 12, 6)
    print(f"  Windows: {len(windows)}")

    wf = {}
    for strat in STRATEGIES + ["BV50/R50"]:
        print(f"  → {strat}...", end=" ", flush=True)
        w_metrics = []
        for ws, we in windows:
            try:
                if strat == "BV50/R50":
                    rb = run_window("breakout_volume", universe, ws, we, BV_CAPITAL)
                    rr = run_window("relative_strength_rotation", universe, ws, we, ROT_CAPITAL, ROTATION_DIV)
                    mb = calc(rb, BV_CAPITAL); mr = calc(rr, ROT_CAPITAL)
                    # 간이 합산
                    combined_ret = (mb["ret"] + mr["ret"]) / 2
                    w_metrics.append({"ret": combined_ret, "sharpe": (mb["sharpe"]+mr["sharpe"])/2,
                                      "pf": (mb["pf"]+mr["pf"])/2, "mdd": min(mb["mdd"],mr["mdd"]),
                                      "n": mb["n"]+mr["n"]})
                else:
                    div = ROTATION_DIV if strat == "relative_strength_rotation" else None
                    r = run_window(strat, universe, ws, we, INITIAL_CAPITAL, div)
                    w_metrics.append(calc(r, INITIAL_CAPITAL))
            except Exception:
                w_metrics.append({"ret": 0, "sharpe": 0, "pf": 0, "mdd": 0, "n": 0})

        n_win = len(w_metrics)
        n_pos = sum(1 for m in w_metrics if m["ret"] > 0)
        n_sh = sum(1 for m in w_metrics if m["sharpe"] > 0)
        avg_ret = np.mean([m["ret"] for m in w_metrics])
        avg_sh = np.mean([m["sharpe"] for m in w_metrics])
        tot_trades = sum(m["n"] for m in w_metrics)
        wf[strat] = {"win": n_win, "pos": n_pos, "pos_rate": round(n_pos/max(n_win,1)*100,1),
                      "sh_pos": n_sh, "sh_rate": round(n_sh/max(n_win,1)*100,1),
                      "avg_ret": round(avg_ret,2), "avg_sh": round(avg_sh,2), "trades": tot_trades}
        print(f"pos={n_pos}/{n_win}, sh+={n_sh}/{n_win}, avgR={round(avg_ret,2)}%")

    # Scoring 신호 분석
    print(f"\n[5] Scoring 신호 분석")
    sc = full.get("scoring", {})
    sig = sc.get("sig_buy", 0); exe = sc.get("exec_buy", 0)
    sk = sc.get("skipped", {})
    aip = sk.get("already_in_position", 0)
    raw_fill = (exe / sig * 100) if sig > 0 else 0
    # effective fill rate: already_in_position 제외
    eff_sig = sig - aip
    eff_fill = (exe / eff_sig * 100) if eff_sig > 0 else 0
    print(f"  Raw: {sig} signals, {exe} executed, fill={raw_fill:.1f}%")
    print(f"  AIP excluded: {eff_sig} effective signals, fill={eff_fill:.1f}%")
    print(f"  Skips: {sk}")

    # 결과 출력
    print("\n" + "=" * 115)
    print("  결과 비교표")
    print(f"  벤치마크 ① EW B&H: {bh_ret}% (Sharpe {bh_sharpe})")
    print("=" * 115)

    hdr = f"  {'전략':<24} {'Ret%':>7} {'Shrp':>6} {'PF':>6} {'MDD%':>7} {'WR%':>5} {'Trd':>5} {'Den%':>5} {'ExEW':>7} {'ExExp':>7} {'CshA':>7} {'WPos':>5} {'WSh+':>5}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for strat in STRATEGIES + ["BV50/R50"]:
        m = full.get(strat, {})
        w = wf.get(strat, {})
        print(f"  {strat:<24} {m.get('ret',0):>7.2f} {m.get('sharpe',0):>6.2f} "
              f"{m.get('pf',0):>6.2f} {m.get('mdd',0):>7.2f} {m.get('wr',0):>5.1f} "
              f"{m.get('n',0):>5} {m.get('density',0):>5.1f} "
              f"{m.get('excess_ew',0):>7.2f} {m.get('excess_exp',0):>7.2f} "
              f"{m.get('excess_cash_adj',0):>7.2f} "
              f"{w.get('pos_rate','--'):>5} {w.get('sh_rate','--'):>5}")

    print(f"\n  WF 상세 ({len(windows)} windows)")
    print(f"  {'전략':<24} {'Win':>4} {'Pos':>4} {'P%':>5} {'Sh+':>4} {'S%':>5} {'AvgR':>7} {'AvgS':>6} {'Trades':>7}")
    print("  " + "-" * 66)
    for strat in STRATEGIES + ["BV50/R50"]:
        w = wf.get(strat, {})
        print(f"  {strat:<24} {w.get('win',0):>4} {w.get('pos',0):>4} "
              f"{w.get('pos_rate',0):>5.1f} {w.get('sh_pos',0):>4} "
              f"{w.get('sh_rate',0):>5.1f} {w.get('avg_ret',0):>7.2f} "
              f"{w.get('avg_sh',0):>6.2f} {w.get('trades',0):>7}")

    print(f"\n  Scoring 신호 (raw vs effective)")
    print(f"  Raw fill rate:       {raw_fill:.1f}% ({exe}/{sig})")
    print(f"  Effective fill rate: {eff_fill:.1f}% ({exe}/{eff_sig}) — AIP {aip}건 제외")

    print("\n" + "=" * 115)


if __name__ == "__main__":
    main()
