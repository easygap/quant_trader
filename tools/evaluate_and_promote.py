"""
Canonical 평가 → Artifact 생성 → 승격 판정 → Status Report

실행: python tools/evaluate_and_promote.py --canonical
출력: reports/promotion/
  - metrics_summary.json
  - walk_forward_summary.json
  - benchmark_comparison.json
  - run_metadata.json
  - promotion_result.json  (최종 상태 계산 결과)
"""
import sys, os, json, hashlib, subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")


CANONICAL_TARGET_WEIGHT_CANDIDATE_IDS = (
    "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35",
)


def get_git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def build_canonical_research_candidate_specs(candidate_ids=None):
    """Return research candidates that are promoted into canonical evaluation."""
    from tools.research_candidate_sweep import build_target_weight_rotation_candidate_specs

    wanted = tuple(candidate_ids or CANONICAL_TARGET_WEIGHT_CANDIDATE_IDS)
    specs = {spec.candidate_id: spec for spec in build_target_weight_rotation_candidate_specs()}
    missing = [candidate_id for candidate_id in wanted if candidate_id not in specs]
    if missing:
        raise ValueError(f"canonical research candidate missing: {', '.join(missing)}")
    return [specs[candidate_id] for candidate_id in wanted]


def canonical_research_candidate_metadata(spec):
    params_json = json.dumps(spec.params, sort_keys=True, ensure_ascii=True, default=str)
    return {
        "candidate_id": spec.candidate_id,
        "base_strategy": spec.strategy,
        "candidate_source": "canonicalized_research_candidate",
        "params": spec.params,
        "params_hash": hashlib.sha256(params_json.encode("utf-8")).hexdigest(),
        "description": spec.description,
    }


def run_canonical_research_candidate(spec, symbols, capital, start, end, runner=None):
    """Run a research-only candidate inside the canonical artifact path."""
    if spec.strategy != "target_weight_rotation":
        raise ValueError(f"unsupported canonical research candidate strategy: {spec.strategy}")

    if runner is None:
        from tools.research_candidate_sweep import run_target_weight_rotation_backtest
        runner = run_target_weight_rotation_backtest

    return runner(
        symbols=symbols,
        start=start,
        end=end,
        capital=capital,
        params=spec.params,
    )


def calculate_canonical_metrics(result, capital):
    eq = result.get("equity_curve")
    trades = result.get("trades", [])
    if eq is None or eq.empty:
        metrics = {
            "total_return": 0,
            "sharpe": 0,
            "profit_factor": 0,
            "mdd": 0,
            "win_rate": 0,
            "total_trades": 0,
            "signal_density": 0,
            "wf_windows": 0,
            "wf_positive_rate": 0,
            "wf_sharpe_positive_rate": 0,
            "wf_total_trades": 0,
        }
        metrics.update(result.get("target_weight_metrics", {}) or {})
        return metrics
    if "date" in eq.columns:
        eq = eq.set_index("date")
    final = float(eq["value"].iloc[-1])
    ret = (final / capital - 1) * 100
    nd = len(eq)
    years = max(nd / 252, 1 / 252)
    dr = eq["value"].pct_change().dropna()
    dm = float(dr.mean()) if len(dr) > 0 else 0
    ds = float(dr.std()) if len(dr) > 1 else 0
    sharpe = (dm * 252 - 0.03) / (ds * np.sqrt(252)) if ds > 0 else 0
    peak = eq["value"].cummax()
    mdd = float(((eq["value"] - peak) / peak).min() * 100)
    sells = [t for t in trades if t.get("action") != "BUY"]
    nt = len(sells)
    wins = sum(1 for t in sells if t.get("pnl", 0) > 0)
    wr = (wins / nt * 100) if nt else 0
    gp = sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) > 0)
    gl = abs(sum(t.get("pnl", 0) for t in sells if t.get("pnl", 0) < 0))
    pf = gp / gl if gl > 0 else (99 if gp > 0 else 0)
    npos = eq.get("n_positions", pd.Series(0, index=eq.index))
    density = float((npos > 0).sum()) / max(nd, 1) * 100
    realized_pnl = sum(t.get("pnl", 0) for t in sells)
    ev_per_trade = realized_pnl / nt if nt else 0
    trade_notional = sum(
        abs(float(t.get("price", 0) or 0) * float(t.get("quantity", 0) or 0))
        for t in trades
    )
    turnover_per_year = (trade_notional / capital / years * 100) if capital > 0 else 0
    if final > 0 and capital > 0:
        cost_adjusted_cagr = ((final / capital) ** (1 / years) - 1) * 100
    else:
        cost_adjusted_cagr = -100
    metrics = {
        "total_return": round(ret, 2),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(pf, 2),
        "mdd": round(mdd, 2),
        "win_rate": round(wr, 1),
        "total_trades": nt,
        "signal_density": round(density, 1),
        "ev_per_trade": round(ev_per_trade, 0),
        "cost_adjusted_cagr": round(cost_adjusted_cagr, 2),
        "turnover_per_year": round(turnover_per_year, 1),
    }
    metrics.update(result.get("target_weight_metrics", {}) or {})
    return metrics


def attach_canonical_walk_forward_metrics(metrics, window_metrics):
    nw = len(window_metrics)
    npos = sum(1 for wm in window_metrics if wm["total_return"] > 0)
    nsh = sum(1 for wm in window_metrics if wm["sharpe"] > 0)
    tot_t = sum(wm.get("total_trades", 0) for wm in window_metrics)
    metrics["wf_windows"] = nw
    metrics["wf_positive_rate"] = round(npos / max(nw, 1), 3)
    metrics["wf_sharpe_positive_rate"] = round(nsh / max(nw, 1), 3)
    metrics["wf_total_trades"] = tot_t
    return {
        "windows": nw,
        "positive": npos,
        "sharpe_pos": nsh,
        "total_trades": tot_t,
        "details": [
            {"return": wm["total_return"], "sharpe": wm["sharpe"]}
            for wm in window_metrics
        ],
    }


def run_canonical():
    """canonical 평가 실행 → artifact 저장."""
    from config.config_loader import Config
    from backtest.portfolio_backtester import PortfolioBacktester
    from core.data_collector import DataCollector
    import FinanceDataReader as fdr

    EVAL_START = "2023-01-01"
    EVAL_END = "2025-12-31"
    INITIAL_CAPITAL = 10_000_000
    TOP_N = 20
    ROTATION_DIV = {"max_positions": 2, "max_position_ratio": 0.45,
                    "max_investment_ratio": 0.85, "min_cash_ratio": 0.10}
    STRATEGIES = ["scoring", "breakout_volume", "relative_strength_rotation",
                  "mean_reversion", "trend_following"]

    # ── Universe (거래대금 기반 ex-ante proxy) ──
    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    stocks = fdr.StockListing('KOSPI')
    common = stocks[~stocks['Code'].str.match(r'^\d{5}[5-9KL]$')]
    if 'Marcap' in common.columns:
        common = common[common['Marcap'] > 1e11]
    candidates = common['Code'].tolist()

    # 거래대금 순위
    amounts = {}
    for sym in candidates[:100]:
        try:
            df = dc.fetch_korean_stock(sym, "2022-10-01", "2022-12-31")
            if df is not None and not df.empty:
                if "date" in df.columns:
                    df = df.set_index("date")
                amounts[sym] = (df["close"].astype(float) * df["volume"].astype(float)).mean()
        except Exception:
            pass
    universe = sorted(amounts, key=amounts.get, reverse=True)[:TOP_N]

    print(f"Universe ({len(universe)}): {universe}")

    # ── Benchmark ──
    per = INITIAL_CAPITAL / len(universe)
    parts = []
    for sym in universe:
        df = dc.fetch_korean_stock(sym, EVAL_START, EVAL_END)
        if df is None or df.empty:
            continue
        if "date" in df.columns:
            df = df.set_index("date")
        df = df[df.index >= pd.Timestamp(EVAL_START)]
        if len(df) < 2:
            continue
        parts.append(per / float(df["close"].iloc[0]) * df["close"].astype(float))
    combined_bh = pd.concat(parts, axis=1).sum(axis=1).dropna() if parts else pd.Series()
    bh_ret = (float(combined_bh.iloc[-1]) / INITIAL_CAPITAL - 1) * 100 if len(combined_bh) > 1 else 0
    bh_dr = combined_bh.pct_change().dropna()
    bh_std = float(bh_dr.std()) if len(bh_dr) > 1 else 0
    bh_sharpe = (float(bh_dr.mean()) * 252 - 0.03) / (bh_std * np.sqrt(252)) if bh_std > 0 else 0

    benchmark = {"ew_bh_return": round(bh_ret, 2), "ew_bh_sharpe": round(bh_sharpe, 2),
                 "universe_size": len(universe)}

    # ── Walk-forward windows ──
    def make_windows(start, end, wm=12, sm=6):
        ws = []
        s = pd.Timestamp(start)
        emax = pd.Timestamp(end)
        while True:
            we = s + pd.DateOffset(months=wm) - pd.Timedelta(days=1)
            if we > emax:
                we = emax
            if s >= emax or (we - s).days < 60:
                break
            ws.append((s.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")))
            s += pd.DateOffset(months=sm)
        return ws

    windows = make_windows(EVAL_START, EVAL_END)

    # ── 전략별 평가 ──
    def run_strat(strategy, syms, capital, start, end, div=None):
        config = Config.get()
        if div:
            dc2 = config.risk_params.setdefault("diversification", {})
            saved = {k: dc2.get(k) for k in div}
            dc2.update(div)
        else:
            saved = None
        try:
            fetch_s = (pd.Timestamp(start) - pd.DateOffset(months=8)).strftime("%Y-%m-%d")
            pbt = PortfolioBacktester(config)
            r = pbt.run(symbols=syms, strategy_name=strategy, initial_capital=capital,
                        start_date=fetch_s, end_date=end)
        finally:
            if saved:
                dc2 = config.risk_params.setdefault("diversification", {})
                for k, v in saved.items():
                    if v is not None:
                        dc2[k] = v
        eq = r.get("equity_curve")
        if eq is not None and not eq.empty and "date" in eq.columns:
            r["equity_curve"] = eq[pd.to_datetime(eq["date"]) >= pd.Timestamp(start)].copy()
        r["trades"] = [t for t in r.get("trades", [])
                       if pd.Timestamp(t.get("date", t.get("entry_date", "2020-01-01"))) >= pd.Timestamp(start)]
        return r

    calc = calculate_canonical_metrics

    metrics_all = {}
    wf_all = {}

    for strat in STRATEGIES:
        print(f"  {strat}...", end=" ", flush=True)
        div = ROTATION_DIV if strat == "relative_strength_rotation" else None
        try:
            r = run_strat(strat, universe, INITIAL_CAPITAL, EVAL_START, EVAL_END, div)
            m = calc(r, INITIAL_CAPITAL)
        except Exception as e:
            m = {"total_return": 0, "sharpe": 0, "profit_factor": 0, "mdd": 0, "error": str(e)[:60]}
            print(f"ERROR")
            metrics_all[strat] = m
            wf_all[strat] = {"windows": 0, "positive": 0, "sharpe_pos": 0, "total_trades": 0, "details": []}
            continue

        # WF
        w_metrics = []
        for ws, we in windows:
            try:
                wr = run_strat(strat, universe, INITIAL_CAPITAL, ws, we, div)
                wm = calc(wr, INITIAL_CAPITAL)
                w_metrics.append(wm)
            except Exception:
                w_metrics.append({"total_return": 0, "sharpe": 0, "profit_factor": 0, "mdd": 0, "total_trades": 0})

        wf_summary = attach_canonical_walk_forward_metrics(m, w_metrics)
        metrics_all[strat] = m
        wf_all[strat] = wf_summary
        print(f"ret={m['total_return']}%")

    research_specs = build_canonical_research_candidate_specs()
    for spec in research_specs:
        name = spec.candidate_id
        print(f"  {name}...", end=" ", flush=True)
        try:
            r = run_canonical_research_candidate(spec, universe, INITIAL_CAPITAL, EVAL_START, EVAL_END)
            m = calc(r, INITIAL_CAPITAL)
        except Exception as e:
            m = {"total_return": 0, "sharpe": 0, "profit_factor": 0, "mdd": 0, "error": str(e)[:60]}
            print("ERROR")
            metrics_all[name] = m
            wf_all[name] = {"windows": 0, "positive": 0, "sharpe_pos": 0, "total_trades": 0, "details": []}
            continue

        w_metrics = []
        for ws, we in windows:
            try:
                wr = run_canonical_research_candidate(spec, universe, INITIAL_CAPITAL, ws, we)
                wm = calc(wr, INITIAL_CAPITAL)
                w_metrics.append(wm)
            except Exception:
                w_metrics.append({"total_return": 0, "sharpe": 0, "profit_factor": 0, "mdd": 0, "total_trades": 0})

        wf_summary = attach_canonical_walk_forward_metrics(m, w_metrics)
        metrics_all[name] = m
        wf_all[name] = wf_summary
        print(f"ret={m['total_return']}%")

    benchmark["strategy_excess_return_pct"] = {
        name: round(float(m.get("total_return", 0)) - bh_ret, 2)
        for name, m in metrics_all.items()
    }
    benchmark["strategy_excess_sharpe"] = {
        name: round(float(m.get("sharpe", 0)) - bh_sharpe, 2)
        for name, m in metrics_all.items()
    }
    for name, m in metrics_all.items():
        m["benchmark_excess_return"] = benchmark["strategy_excess_return_pct"].get(name)
        m["benchmark_excess_sharpe"] = benchmark["strategy_excess_sharpe"].get(name)

    # ── Promotion 계산 ──
    from core.promotion_engine import StrategyMetrics, promote

    promotions = {}
    for name, m in metrics_all.items():
        sm = StrategyMetrics(
            name=name,
            total_return=m.get("total_return", 0),
            profit_factor=m.get("profit_factor", 0),
            mdd=m.get("mdd", 0),
            wf_positive_rate=m.get("wf_positive_rate", 0),
            wf_sharpe_positive_rate=m.get("wf_sharpe_positive_rate", 0),
            wf_windows=m.get("wf_windows", 0),
            wf_total_trades=m.get("wf_total_trades", 0),
            sharpe=m.get("sharpe", 0),
            benchmark_excess_return=m.get("benchmark_excess_return"),
            benchmark_excess_sharpe=m.get("benchmark_excess_sharpe"),
            ev_per_trade=m.get("ev_per_trade"),
            cost_adjusted_cagr=m.get("cost_adjusted_cagr"),
            turnover_per_year=m.get("turnover_per_year"),
        )
        result = promote(sm)
        promotions[name] = {
            "status": result.status,
            "allowed_modes": result.allowed_modes,
            "reason": result.reason,
        }

    # ── Artifact 저장 ──
    out_dir = Path("reports/promotion")
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "schema_version": 1,
        "artifact_type": "canonical_promotion_bundle",
        "eval_start": EVAL_START,
        "eval_end": EVAL_END,
        "universe_rule": "FDR KOSPI 보통주 시총 1000억+, 2022-10~12 거래대금 상위 20",
        "universe": universe,
        "canonical_research_candidate_ids": [spec.candidate_id for spec in research_specs],
        "strategy_specs": [
            canonical_research_candidate_metadata(spec)
            for spec in research_specs
        ],
        "wf_window_months": 12,
        "wf_step_months": 6,
        "wf_n_windows": len(windows),
        "initial_capital": INITIAL_CAPITAL,
        "commit_hash": get_git_hash(),
        "config_yaml_hash": Config.get().yaml_hash,
        "config_resolved_hash": Config.get().resolved_hash,
        "generated_at": datetime.now().isoformat(),
    }

    artifacts = {
        "run_metadata.json": metadata,
        "metrics_summary.json": metrics_all,
        "walk_forward_summary.json": wf_all,
        "benchmark_comparison.json": benchmark,
        "promotion_result.json": promotions,
    }

    for fname, data in artifacts.items():
        path = out_dir / fname
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"  → {path}")

    # ── 상태 요약 출력 ──
    print("\n" + "=" * 80)
    print("  Promotion Result")
    print("=" * 80)
    for name in [*STRATEGIES, *[spec.candidate_id for spec in research_specs]]:
        p = promotions.get(name, {})
        m = metrics_all.get(name, {})
        print(f"  {name:<28} {p.get('status','?'):<30} ret={m.get('total_return',0):>7.2f}% PF={m.get('profit_factor',0):.2f}")
    print("=" * 80)

    return artifacts


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Canonical 평가 + 승격 판정")
    parser.add_argument("--canonical", action="store_true", help="전체 평가 실행")
    parser.add_argument("--check-only", action="store_true", help="기존 artifact 검증만")
    args = parser.parse_args()

    if args.canonical:
        run_canonical()
    elif args.check_only:
        from core.promotion_engine import load_promotion_artifact
        result = load_promotion_artifact()
        if result is None:
            print("FAIL: artifact 없음 또는 로드 실패")
            sys.exit(1)
        print("OK: artifact 로드 성공")
        for name, p in result.items():
            print(f"  {name}: {p['status']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
