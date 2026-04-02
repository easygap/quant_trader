"""
Canonical 전략 평가 프레임워크
- FDR 현재 시총 상위 20 보통주 (KOSPI) 유니버스
- Portfolio-level rolling walk-forward (12mo window, 6mo step)
- Benchmark 3종: equal-weight B&H, cash-adjusted, exposure-matched
- survivorship/selection bias 감사
- scoring 신호 품질 분석
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

# ── 유니버스 정의 ──
# FDR 현재(2026-04-02) KOSPI 시총 상위 20 보통주 (우선주 제외)
# 이는 ex-post(결과 시점) 선정이므로 survivorship bias가 존재함
UNIVERSE_20 = [
    "005930", "000660", "373220", "005380", "207940",
    "012450", "402340", "034020", "000270", "105560",
    "329180", "068270", "055550", "032830", "028260",
    "042660", "006400", "012330", "006800", "267260",
]

# Paper 실험 세트 (hand-picked, 참고용)
UNIVERSE_4 = ["005930", "000660", "035720", "051910"]

INITIAL_CAPITAL = 10_000_000
FETCH_START = "2021-06-01"  # warmup 포함
EVAL_START = "2023-01-01"
EVAL_END = "2025-12-31"

STRATEGIES = ["scoring", "breakout_volume", "relative_strength_rotation",
              "mean_reversion", "trend_following"]

ROTATION_DIV = {"max_positions": 2, "max_position_ratio": 0.45,
                "max_investment_ratio": 0.85, "min_cash_ratio": 0.10}


def make_windows(start, end, window_months=12, step_months=6):
    """Rolling windows 생성."""
    windows = []
    s = pd.Timestamp(start)
    e_max = pd.Timestamp(end)
    while True:
        w_end = s + pd.DateOffset(months=window_months) - pd.Timedelta(days=1)
        if w_end > e_max:
            w_end = e_max
        if s >= e_max or (w_end - s).days < 60:
            break
        windows.append((s.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")))
        s += pd.DateOffset(months=step_months)
    return windows


def run_single_window(strategy, symbols, start, end, capital, div_override=None):
    """단일 구간 포트폴리오 백테스트."""
    config = Config.get()
    if div_override:
        div_cfg = config.risk_params.setdefault("diversification", {})
        saved = {k: div_cfg.get(k) for k in div_override}
        div_cfg.update(div_override)
    else:
        saved = None
    try:
        # warmup을 위해 8개월 앞에서 시작
        fetch_s = (pd.Timestamp(start) - pd.DateOffset(months=8)).strftime("%Y-%m-%d")
        pbt = PortfolioBacktester(config)
        r = pbt.run(symbols=symbols, strategy_name=strategy,
                     initial_capital=capital, start_date=fetch_s, end_date=end)
    finally:
        if saved:
            div_cfg = config.risk_params.setdefault("diversification", {})
            for k, v in saved.items():
                if v is not None: div_cfg[k] = v

    # 평가 구간으로 트리밍
    eq = r.get("equity_curve")
    if eq is not None and not eq.empty and "date" in eq.columns:
        r["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(start)].copy()
    r["trades"] = [t for t in r.get("trades", [])
                   if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(start)]
    return r


def calc_metrics(result, capital):
    eq = result.get("equity_curve")
    trades = result.get("trades", [])
    if eq is None or eq.empty:
        return {"ret": 0, "sharpe": 0, "pf": 0, "mdd": 0, "wr": 0, "n": 0, "turn": 0, "density": 0}
    if "date" in eq.columns: eq = eq.set_index("date")
    final = float(eq["value"].iloc[-1])
    ret = (final / capital - 1) * 100
    n_days = len(eq)
    years = n_days / 252
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


def bench_equal_weight(symbols, capital, start, end):
    """동일비중 B&H 벤치마크 (거래비용 반영)."""
    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    per = capital / len(symbols)
    parts = []
    valid_syms = 0
    for sym in symbols:
        df = dc.fetch_korean_stock(sym, start, end)
        if df is None or df.empty: continue
        if "date" in df.columns: df = df.set_index("date")
        df = df[df.index >= pd.Timestamp(start)]
        if len(df) < 2: continue
        shares = per / float(df["close"].iloc[0])
        parts.append(shares * df["close"].astype(float))
        valid_syms += 1
    if not parts: return 0, 0, 0
    combined = pd.concat(parts, axis=1).sum(axis=1).dropna()
    cost = 0.00215 + 0.0005  # entry commission+slippage + exit commission+tax+slippage
    raw_ret = (float(combined.iloc[-1]) / capital - 1) * 100
    net_ret = raw_ret * (1 - cost)
    dr = combined.pct_change().dropna()
    d_s = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (float(dr.mean()) * 252 - 0.03) / (d_s * np.sqrt(252)) if d_s > 0 else 0
    return round(net_ret, 2), round(sharpe, 2), valid_syms


def main():
    print("=" * 110)
    print("  Canonical 전략 평가 프레임워크")
    print("=" * 110)

    # ── 1. 유니버스 & bias 감사 ──
    print("\n[1] 유니버스 정의 & bias 감사")
    print(f"  20종목 유니버스: FDR 현재(2026-04-02) KOSPI 시총 상위 20 보통주")
    print(f"  선정 방식: ex-post (현재 시점 시총 순위)")
    print(f"  survivorship bias: 존재 — 2023~2025 중 상폐/시총 하락 종목이 현재 Top20에서 제외됨")
    print(f"  영향도: 대형주(Top20) 상폐 확률 낮으나, 시총 순위 변동 있음 (ex: 삼성SDI 하락, 한화에어로 상승)")
    print(f"  한계: pykrx historical constituent 조회가 현재 환경에서 비호환 → point-in-time 재구성 불가")
    print(f"  대안: 현재 시총 Top20을 proxy로 사용하되 결과 해석 시 bias 감안")

    # ── 2. 벤치마크 3종 ──
    print("\n[2] 벤치마크 계산 (20종목)...")
    bh_ret, bh_sharpe, bh_n = bench_equal_weight(UNIVERSE_20, INITIAL_CAPITAL, EVAL_START, EVAL_END)
    print(f"  ① Equal-weight B&H (20종목, 비용 반영): {bh_ret}% (Sharpe {bh_sharpe}, {bh_n}종목 유효)")

    # ── 3. 전략별 full-period 평가 ──
    print("\n[3] Full-period 포트폴리오 백테스트 (20종목, 2023-01~2025-12)...")
    full_results = {}
    for strat in STRATEGIES:
        print(f"  → {strat}...", end=" ", flush=True)
        div = ROTATION_DIV if strat == "relative_strength_rotation" else None
        try:
            r = run_single_window(strat, UNIVERSE_20, EVAL_START, EVAL_END, INITIAL_CAPITAL, div)
            m = calc_metrics(r, INITIAL_CAPITAL)
            m["excess_ew"] = round(m["ret"] - bh_ret, 2)
            full_results[strat] = m
            print(f"ret={m['ret']}%, sharpe={m['sharpe']}")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")
            full_results[strat] = {"ret": 0, "sharpe": 0, "error": str(e)[:60]}

    # BV50/R50 20종목
    print("  → BV50/R50...", end=" ", flush=True)
    try:
        r_bv = run_single_window("breakout_volume", UNIVERSE_20, EVAL_START, EVAL_END, BV_CAPITAL)
        r_rot = run_single_window("relative_strength_rotation", UNIVERSE_20, EVAL_START, EVAL_END, ROT_CAPITAL, ROTATION_DIV)
        m_bv = calc_metrics(r_bv, BV_CAPITAL)
        m_rot = calc_metrics(r_rot, ROT_CAPITAL)

        eq_bv = r_bv["equity_curve"].set_index("date") if "date" in r_bv["equity_curve"].columns else r_bv["equity_curve"]
        eq_rot = r_rot["equity_curve"].set_index("date") if "date" in r_rot["equity_curve"].columns else r_rot["equity_curve"]
        common = sorted(set(eq_bv.index) & set(eq_rot.index))
        rows = [{"date": d, "value": float(eq_bv.loc[d, "value"]) + float(eq_rot.loc[d, "value"]),
                 "cash": float(eq_bv.loc[d, "cash"]) + float(eq_rot.loc[d, "cash"]),
                 "n_positions": int(eq_bv.loc[d, "n_positions"]) + int(eq_rot.loc[d, "n_positions"])} for d in common]
        m_combo = calc_metrics({"equity_curve": pd.DataFrame(rows),
                                "trades": r_bv.get("trades", []) + r_rot.get("trades", [])}, INITIAL_CAPITAL)
        m_combo["excess_ew"] = round(m_combo["ret"] - bh_ret, 2)
        full_results["BV50/R50"] = m_combo
        print(f"ret={m_combo['ret']}%")
    except Exception as e:
        print(f"ERROR: {str(e)[:60]}")

    # ── 4. Portfolio-level rolling walk-forward ──
    print("\n[4] Portfolio-level Rolling Walk-Forward (20종목, 12mo window, 6mo step)...")
    windows = make_windows(EVAL_START, EVAL_END, window_months=12, step_months=6)
    print(f"  Windows: {len(windows)}")
    for i, (ws, we) in enumerate(windows):
        print(f"    W{i+1}: {ws} ~ {we}")

    wf_results = {}
    for strat in STRATEGIES:
        print(f"  → {strat} WF...", end=" ", flush=True)
        div = ROTATION_DIV if strat == "relative_strength_rotation" else None
        w_metrics = []
        for ws, we in windows:
            try:
                r = run_single_window(strat, UNIVERSE_20, ws, we, INITIAL_CAPITAL, div)
                m = calc_metrics(r, INITIAL_CAPITAL)
                w_metrics.append(m)
            except Exception:
                w_metrics.append({"ret": 0, "sharpe": 0, "pf": 0, "mdd": 0, "n": 0})

        n_win = len(w_metrics)
        n_positive = sum(1 for m in w_metrics if m["ret"] > 0)
        n_sharpe_pos = sum(1 for m in w_metrics if m["sharpe"] > 0)
        avg_ret = np.mean([m["ret"] for m in w_metrics]) if w_metrics else 0
        avg_sharpe = np.mean([m["sharpe"] for m in w_metrics]) if w_metrics else 0
        total_trades = sum(m["n"] for m in w_metrics)

        wf_results[strat] = {
            "windows": n_win, "positive": n_positive, "pos_rate": round(n_positive/max(n_win,1)*100, 1),
            "sharpe_pos": n_sharpe_pos, "sharpe_rate": round(n_sharpe_pos/max(n_win,1)*100, 1),
            "avg_ret": round(avg_ret, 2), "avg_sharpe": round(avg_sharpe, 2),
            "total_trades": total_trades, "details": w_metrics,
        }
        print(f"positive={n_positive}/{n_win} ({round(n_positive/max(n_win,1)*100)}%), "
              f"avg_ret={round(avg_ret,2)}%, trades={total_trades}")

    # ── 5. Scoring 신호 품질 분석 ──
    print("\n[5] Scoring 신호 품질 분석 (20종목)")
    sc = full_results.get("scoring", {})
    sig = sc.get("sig_buy", 0)
    exe = sc.get("exec_buy", 0)
    sk = sc.get("skipped", {})
    fill = (exe / sig * 100) if sig > 0 else 0
    print(f"  BUY 신호: {sig}건, 체결: {exe}건, fill rate: {fill:.1f}%")
    print(f"  스킵 사유: {sk}")
    aip = sk.get("already_in_position", 0)
    if sig > 0:
        print(f"  already_in_position 비율: {aip}/{sig} = {aip/sig*100:.1f}%")
        print(f"  해석: scoring은 보유 중인 종목에도 매일 BUY 신호를 발생시킴.")
        print(f"        이는 신호 과잉(signal flooding)이며, 실질 진입 기회는 {exe}건뿐.")
        print(f"        신호 생성 단계에서 보유 여부를 체크하면 해소 가능하나,")
        print(f"        backtester가 이미 올바르게 차단하고 있으므로 성과에는 영향 없음.")
        print(f"        다만 fill rate 지표가 왜곡되어 전략 품질 오판 가능.")

    # ── 6. 벤치마크 3종 비교 ──
    print(f"\n[6] 벤치마크 3종 비교")
    print(f"  ① Equal-weight B&H: {bh_ret}% (Sharpe {bh_sharpe})")
    # ② Cash-adjusted: 전략의 cash holding 기간에 CMA 2.5% 가정
    print(f"  ② Cash-adjusted: 전략의 비투자 기간에 CMA 2.5% 연수익 가정 (별도 계산 필요)")
    # ③ Exposure-matched: 전략의 평균 투자비중만큼 벤치마크 스케일링
    print(f"  ③ Exposure-matched: 전략 평균 투자비중에 맞춰 벤치마크 스케일링")

    for strat in STRATEGIES + ["BV50/R50"]:
        m = full_results.get(strat, {})
        density = m.get("density", 0)
        exposure = density / 100
        # exposure-matched: B&H 수익률 × 평균 투자비중
        exp_bh = round(bh_ret * max(exposure, 0.01), 2)
        exp_excess = round(m.get("ret", 0) - exp_bh, 2)
        # cash-adjusted: 비투자 기간에 CMA 수익 가산
        cash_pct = (100 - density) / 100
        years = 3
        cma_add = round(0.025 * cash_pct * years * 100 / years, 2)  # 연환산 cash 수익
        adj_ret = round(m.get("ret", 0) + cma_add * years / 100 * 100, 2)  # 근사

        m["exp_matched_excess"] = exp_excess
        m["cash_adj_ret"] = adj_ret

    # ── 결과 출력 ──
    print("\n" + "=" * 110)
    print("  [결과] 20종목 유니버스 — Full Period + Rolling WF")
    print(f"  벤치마크: EW B&H {bh_ret}% (Sharpe {bh_sharpe})")
    print("=" * 110)

    hdr = f"  {'전략':<24} {'Ret%':>7} {'Shrp':>6} {'PF':>6} {'MDD%':>7} {'WR%':>5} {'Trd':>5} {'Den%':>5} {'ExcEW':>7} {'ExcExp':>7} {'WF+%':>5} {'WFSh+':>5} {'WfTrd':>6}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for strat in STRATEGIES + ["BV50/R50"]:
        m = full_results.get(strat, {})
        w = wf_results.get(strat, {})
        print(f"  {strat:<24} {m.get('ret',0):>7.2f} {m.get('sharpe',0):>6.2f} "
              f"{m.get('pf',0):>6.2f} {m.get('mdd',0):>7.2f} {m.get('wr',0):>5.1f} "
              f"{m.get('n',0):>5} {m.get('density',0):>5.1f} "
              f"{m.get('excess_ew',0):>7.2f} {m.get('exp_matched_excess',0):>7.2f} "
              f"{w.get('pos_rate','--'):>5} {w.get('sharpe_rate','--'):>5} "
              f"{w.get('total_trades',0):>6}")

    # WF 상세
    print(f"\n  [WF 상세] Rolling 12mo window, 6mo step, {len(windows)} windows")
    print(f"  {'전략':<24} {'Win':>4} {'Pos':>4} {'Pos%':>5} {'Sh+':>4} {'Sh+%':>5} {'AvgRet':>7} {'AvgSh':>7} {'Trades':>7}")
    print("  " + "-" * 72)
    for strat in STRATEGIES:
        w = wf_results.get(strat, {})
        print(f"  {strat:<24} {w.get('windows',0):>4} {w.get('positive',0):>4} "
              f"{w.get('pos_rate',0):>5.1f} {w.get('sharpe_pos',0):>4} "
              f"{w.get('sharpe_rate',0):>5.1f} {w.get('avg_ret',0):>7.2f} "
              f"{w.get('avg_sharpe',0):>7.2f} {w.get('total_trades',0):>7}")

    # 신호 분석
    print(f"\n  [신호 분석] 20종목 Full Period")
    print(f"  {'전략':<24} {'BUY신호':>8} {'BUY체결':>8} {'Fill%':>7} {'주요 스킵':>30}")
    print("  " + "-" * 80)
    for strat in STRATEGIES:
        m = full_results.get(strat, {})
        sig = m.get("sig_buy", 0); exe = m.get("exec_buy", 0)
        fill = (exe/sig*100) if sig > 0 else 0
        sk = m.get("skipped", {})
        sk_str = ", ".join(f"{k}({v})" for k, v in sorted(sk.items(), key=lambda x: -x[1])[:2]) if sk else "-"
        print(f"  {strat:<24} {sig:>8} {exe:>8} {fill:>6.1f}% {sk_str:>30}")

    print("\n" + "=" * 110)


if __name__ == "__main__":
    main()
