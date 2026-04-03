"""
전략 수익성 재평가 스크립트
- 동일 유니버스 / 동일 기간 / 동일 비용 가정
- 6개 전략 + BV50/R50 조합 비교
- OOS Sharpe, Profit Factor, MDD, excess return, turnover, signal density, WF pass ratio
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from config.config_loader import Config
from backtest.portfolio_backtester import PortfolioBacktester
from backtest.strategy_validator import StrategyValidator
from core.data_collector import DataCollector

# ── 공통 설정 ──
SYMBOLS = ["005930", "000660", "035720", "051910"]
SYMBOL_SINGLE = "005930"  # 단일 종목 validate/WF용
INITIAL_CAPITAL = 10_000_000
PERIOD_START = "2021-01-01"  # 5년 데이터 (warmup 포함)
PERIOD_END = "2025-12-31"
REPORT_START = "2023-01-01"  # 실제 평가 구간 3년
BV_CAPITAL = 5_000_000
ROT_CAPITAL = 5_000_000

STRATEGIES = [
    "scoring",
    "breakout_volume",
    "relative_strength_rotation",
    "mean_reversion",
    "trend_following",
]

ROTATION_DIV = {
    "max_position_ratio": 0.45,
    "max_investment_ratio": 0.85,
    "min_cash_ratio": 0.10,
}


def run_portfolio_backtest(strategy_name, capital=INITIAL_CAPITAL, div_override=None):
    """포트폴리오 백테스트 실행."""
    config = Config.get()
    if div_override:
        div_cfg = config.risk_params.setdefault("diversification", {})
        saved = {k: div_cfg.get(k) for k in div_override}
        div_cfg.update(div_override)
    else:
        saved = None

    try:
        pbt = PortfolioBacktester(config)
        result = pbt.run(
            symbols=SYMBOLS,
            strategy_name=strategy_name,
            initial_capital=capital,
            start_date=PERIOD_START,
            end_date=PERIOD_END,
        )
    finally:
        if saved:
            div_cfg = config.risk_params.setdefault("diversification", {})
            for k, v in saved.items():
                if v is not None:
                    div_cfg[k] = v

    # equity curve를 평가 구간으로 트리밍
    eq = result.get("equity_curve")
    if eq is not None and not eq.empty and "date" in eq.columns:
        mask = pd.to_datetime(eq["date"]) >= pd.Timestamp(REPORT_START)
        eq = eq[mask].copy()
        result["equity_curve"] = eq

    trades = result.get("trades", [])
    trades = [t for t in trades if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(REPORT_START)]
    result["trades"] = trades

    return result


def compute_metrics(result, capital):
    """equity_curve + trades에서 핵심 지표 계산."""
    eq = result.get("equity_curve")
    trades = result.get("trades", [])

    if eq is None or eq.empty:
        return {
            "total_return": 0, "oos_sharpe": 0, "profit_factor": 0,
            "mdd": 0, "win_rate": 0, "total_trades": 0,
            "turnover": 0, "signal_density": 0, "excess_return": 0,
        }

    if "date" in eq.columns:
        eq = eq.set_index("date")

    final_val = float(eq["value"].iloc[-1])
    total_return = (final_val / capital - 1) * 100
    n_days = len(eq)
    years = n_days / 252

    # daily returns
    dr = eq["value"].pct_change().dropna()
    daily_mean = float(dr.mean()) if len(dr) > 0 else 0
    daily_std = float(dr.std()) if len(dr) > 1 else 0
    ann_ret = daily_mean * 252
    ann_std = daily_std * np.sqrt(252)
    sharpe = (ann_ret - 0.03) / ann_std if ann_std > 0 else 0

    # MDD
    peak = eq["value"].cummax()
    mdd = float(((eq["value"] - peak) / peak).min() * 100)

    # trades analysis
    sell_trades = [t for t in trades if t.get("action") != "BUY"]
    total_trades = len(sell_trades)
    wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
    win_rate = (wins / total_trades * 100) if total_trades else 0

    gross_profit = sum(t.get("pnl", 0) for t in sell_trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in sell_trades if t.get("pnl", 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 0)

    # turnover: 연간 왕복 수
    turnover = total_trades / max(years, 0.01)

    # signal density: 전체 거래일 대비 포지션 보유일 비율
    n_pos_col = eq.get("n_positions", pd.Series(0, index=eq.index))
    signal_density = float((n_pos_col > 0).sum()) / max(n_days, 1) * 100

    # excess return vs KOSPI (간단 추정)
    excess_return = total_return  # 벤치마크는 별도 계산

    return {
        "total_return": round(total_return, 2),
        "oos_sharpe": round(sharpe, 2),
        "profit_factor": round(profit_factor, 2),
        "mdd": round(mdd, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": total_trades,
        "turnover": round(turnover, 1),
        "signal_density": round(signal_density, 1),
        "excess_return": round(excess_return, 2),
    }


def run_benchmark():
    """KOSPI B&H 벤치마크 수익률 계산."""
    dc = DataCollector()
    df = dc.fetch_korean_stock("KS11", PERIOD_START, PERIOD_END)
    if df is None or df.empty:
        return 0.0
    if "date" in df.columns:
        df = df.set_index("date")
    mask = df.index >= pd.Timestamp(REPORT_START)
    df = df[mask]
    if len(df) < 2:
        return 0.0
    ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100
    return round(ret, 2)


def run_walk_forward(strategy_name, symbol=SYMBOL_SINGLE):
    """단일 종목 walk-forward 검증."""
    try:
        config = Config.get()
        validator = StrategyValidator(config)
        result = validator.run_walk_forward(
            symbol=symbol,
            strategy_name=strategy_name,
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            validation_years=5,
            train_days=504,
            test_days=252,
            step_days=252,
        )
        # validator는 flat 구조 반환 (summary 서브키 없음)
        return {
            "n_windows": result.get("n_total", 0),
            "n_passed": result.get("n_passed", 0),
            "pass_rate": round(result.get("pass_rate", 0) * 100, 1),
            "avg_oos_sharpe": round(result.get("avg_oos_sharpe", 0), 2),
        }
    except Exception as e:
        return {"n_windows": 0, "n_passed": 0, "pass_rate": 0, "avg_oos_sharpe": 0, "error": str(e)[:80]}


def run_bv50_r50():
    """BV50/R50 조합 백테스트."""
    config = Config.get()

    # BV sleeve
    pbt = PortfolioBacktester(config)
    r_bv = pbt.run(symbols=SYMBOLS, strategy_name="breakout_volume",
                    initial_capital=BV_CAPITAL, start_date=PERIOD_START, end_date=PERIOD_END)

    # Rotation sleeve
    div_cfg = config.risk_params.setdefault("diversification", {})
    saved = {k: div_cfg.get(k) for k in ["max_positions", "max_position_ratio", "max_investment_ratio", "min_cash_ratio"]}
    try:
        div_cfg["max_positions"] = 2
        div_cfg.update(ROTATION_DIV)
        pbt2 = PortfolioBacktester(config)
        r_rot = pbt2.run(symbols=SYMBOLS, strategy_name="relative_strength_rotation",
                          initial_capital=ROT_CAPITAL, start_date=PERIOD_START, end_date=PERIOD_END)
    finally:
        for k, v in saved.items():
            if v is not None:
                div_cfg[k] = v

    # 트리밍
    def _trim(result):
        eq = result.get("equity_curve")
        if eq is None or eq.empty:
            return result
        if "date" in eq.columns:
            mask = pd.to_datetime(eq["date"]) >= pd.Timestamp(REPORT_START)
        else:
            mask = eq.index >= pd.Timestamp(REPORT_START)
        result["equity_curve"] = eq[mask].copy()
        result["trades"] = [t for t in result.get("trades", [])
                            if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(REPORT_START)]
        return result

    r_bv = _trim(r_bv)
    r_rot = _trim(r_rot)

    # Combine
    eq_bv = r_bv["equity_curve"].set_index("date") if "date" in r_bv["equity_curve"].columns else r_bv["equity_curve"]
    eq_rot = r_rot["equity_curve"].set_index("date") if "date" in r_rot["equity_curve"].columns else r_rot["equity_curve"]
    common = sorted(set(eq_bv.index) & set(eq_rot.index))
    rows = [{"date": d,
             "value": float(eq_bv.loc[d, "value"]) + float(eq_rot.loc[d, "value"]),
             "cash": float(eq_bv.loc[d, "cash"]) + float(eq_rot.loc[d, "cash"]),
             "n_positions": int(eq_bv.loc[d, "n_positions"]) + int(eq_rot.loc[d, "n_positions"])}
            for d in common]

    combined = {
        "equity_curve": pd.DataFrame(rows),
        "trades": r_bv.get("trades", []) + r_rot.get("trades", []),
    }

    return {
        "bv": compute_metrics(r_bv, BV_CAPITAL),
        "rot": compute_metrics(r_rot, ROT_CAPITAL),
        "combined": compute_metrics(combined, INITIAL_CAPITAL),

        "bv_signals": {
            "signal_buy": r_bv.get("signal_buy_count", 0),
            "executed_buy": r_bv.get("executed_buy_count", 0),
            "skipped": r_bv.get("skipped_reasons", {}),
        },
        "rot_signals": {
            "signal_buy": r_rot.get("signal_buy_count", 0),
            "executed_buy": r_rot.get("executed_buy_count", 0),
            "skipped": r_rot.get("skipped_reasons", {}),
        },
    }


def main():
    print("=" * 100)
    print("  전략 수익성 재평가 — 동일 유니버스·기간·비용 기준")
    print(f"  유니버스: {SYMBOLS}")
    print(f"  데이터 기간: {PERIOD_START} ~ {PERIOD_END} (warmup 포함)")
    print(f"  평가 기간: {REPORT_START} ~ {PERIOD_END} (3년)")
    print(f"  초기 자본: {INITIAL_CAPITAL:,}원")
    print("=" * 100)

    # 벤치마크
    print("\n[1/4] 벤치마크 계산 중...")
    bench_ret = run_benchmark()
    print(f"  KOSPI B&H 수익률: {bench_ret}%")

    # 전략별 포트폴리오 백테스트
    print("\n[2/4] 전략별 포트폴리오 백테스트...")
    results = {}
    for strat in STRATEGIES:
        print(f"  → {strat}...", end=" ", flush=True)
        try:
            div_override = None
            if strat == "relative_strength_rotation":
                div_override = {"max_positions": 2, **ROTATION_DIV}
            r = run_portfolio_backtest(strat, INITIAL_CAPITAL, div_override)
            m = compute_metrics(r, INITIAL_CAPITAL)
            m["excess_return"] = round(m["total_return"] - bench_ret, 2)

            # signal/executed counts
            m["signal_buy"] = r.get("signal_buy_count", 0)
            m["executed_buy"] = r.get("executed_buy_count", 0)
            m["skipped"] = r.get("skipped_reasons", {})

            results[strat] = m
            print(f"return={m['total_return']}%, sharpe={m['oos_sharpe']}")
        except Exception as e:
            print(f"ERROR: {e}")
            results[strat] = {"error": str(e)[:80]}

    # BV50/R50 조합
    print("\n  → BV50/R50 조합...", end=" ", flush=True)
    try:
        bv50r50 = run_bv50_r50()
        bv50r50["combined"]["excess_return"] = round(bv50r50["combined"]["total_return"] - bench_ret, 2)
        results["BV50/R50"] = bv50r50["combined"]
        results["BV단독"] = bv50r50["bv"]
        results["BV단독"]["excess_return"] = round(bv50r50["bv"]["total_return"] - bench_ret, 2)
        results["Rot단독"] = bv50r50["rot"]
        results["Rot단독"]["excess_return"] = round(bv50r50["rot"]["total_return"] - bench_ret, 2)
        print(f"combined={bv50r50['combined']['total_return']}%")
    except Exception as e:
        print(f"ERROR: {e}")

    # Walk-Forward 검증
    print("\n[3/4] Walk-Forward 검증 (단일 종목 005930)...")
    wf_results = {}
    for strat in STRATEGIES:
        print(f"  → {strat} WF...", end=" ", flush=True)
        wf = run_walk_forward(strat)
        wf_results[strat] = wf
        if "error" in wf:
            print(f"ERROR: {wf['error']}")
        else:
            print(f"pass_rate={wf['pass_rate']}%, avg_sharpe={wf['avg_oos_sharpe']}")

    # 결과 출력
    print("\n" + "=" * 100)
    print("[4/4] 결과 요약")
    print("=" * 100)

    print(f"\n  KOSPI B&H 벤치마크 수익률: {bench_ret}%\n")

    # 메인 비교 테이블
    header = f"  {'전략':<28} {'Return%':>8} {'Sharpe':>7} {'PF':>6} {'MDD%':>7} {'WinR%':>6} {'Trades':>7} {'Turn':>6} {'SigD%':>6} {'Excess':>7} {'WF%':>5}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    all_strats = STRATEGIES + ["BV단독", "Rot단독", "BV50/R50"]
    for strat in all_strats:
        m = results.get(strat, {})
        if "error" in m:
            print(f"  {strat:<28} ERROR: {m['error']}")
            continue
        wf = wf_results.get(strat, {})
        wf_rate = wf.get("pass_rate", "-")
        if isinstance(wf_rate, (int, float)):
            wf_str = f"{wf_rate:>4.0f}%"
        else:
            wf_str = f"{'--':>5}"

        print(f"  {strat:<28} {m.get('total_return', 0):>8.2f} {m.get('oos_sharpe', 0):>7.2f} "
              f"{m.get('profit_factor', 0):>6.2f} {m.get('mdd', 0):>7.2f} "
              f"{m.get('win_rate', 0):>6.1f} {m.get('total_trades', 0):>7} "
              f"{m.get('turnover', 0):>6.1f} {m.get('signal_density', 0):>6.1f} "
              f"{m.get('excess_return', 0):>7.2f} {wf_str}")

    # WF 상세
    print(f"\n  Walk-Forward 상세 (005930, train=2y, test=1y, step=1y)")
    print(f"  {'전략':<28} {'Windows':>8} {'Passed':>8} {'PassRate':>9} {'AvgSharpe':>10}")
    print("  " + "-" * 66)
    for strat in STRATEGIES:
        wf = wf_results.get(strat, {})
        print(f"  {strat:<28} {wf.get('n_windows', 0):>8} {wf.get('n_passed', 0):>8} "
              f"{wf.get('pass_rate', 0):>8.1f}% {wf.get('avg_oos_sharpe', 0):>10.2f}")

    # 신호 밀도 & 스킵 사유
    print(f"\n  신호/체결/스킵 분석")
    print(f"  {'전략':<28} {'BUY신호':>8} {'BUY체결':>8} {'Fill%':>7} {'주요 스킵':>20}")
    print("  " + "-" * 73)
    for strat in STRATEGIES:
        m = results.get(strat, {})
        sig = m.get("signal_buy", 0)
        exe = m.get("executed_buy", 0)
        fill = (exe / sig * 100) if sig > 0 else 0
        skips = m.get("skipped", {})
        top_skip = max(skips, key=skips.get) if skips else "-"
        top_n = skips.get(top_skip, 0) if skips else 0
        skip_str = f"{top_skip}({top_n})" if top_skip != "-" else "-"
        print(f"  {strat:<28} {sig:>8} {exe:>8} {fill:>6.1f}% {skip_str:>20}")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
