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


def stable_payload_hash(payload) -> str:
    """JSON 직렬화 가능한 메타데이터의 순서 독립 해시를 반환."""
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "date" in out.columns:
        out = out.set_index("date")
    out.index = pd.to_datetime(out.index)
    out = out.sort_index()
    return out


def summarize_ohlcv_frame(df: pd.DataFrame) -> dict:
    """canonical 입력 진단을 위한 결정적 OHLCV coverage 요약."""
    out = _normalize_ohlcv_frame(df)
    if out.empty:
        return {"rows": 0, "start": None, "end": None, "columns": []}

    summary = {
        "rows": int(len(out)),
        "start": out.index.min().strftime("%Y-%m-%d"),
        "end": out.index.max().strftime("%Y-%m-%d"),
        "columns": sorted(str(c) for c in out.columns),
    }
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            series = pd.to_numeric(out[col], errors="coerce").dropna()
            summary[f"{col}_non_null"] = int(len(series))
            if col == "close" and len(series) > 0:
                summary["first_close"] = round(float(series.iloc[0]), 6)
                summary["last_close"] = round(float(series.iloc[-1]), 6)
    return summary


def build_data_snapshot_manifest(
    *,
    provider: str,
    universe_rule: str,
    eval_start: str,
    eval_end: str,
    universe_lookback_start: str,
    universe_lookback_end: str,
    universe: list[str],
    liquidity_coverage: dict,
    benchmark_coverage: dict,
    fetch_errors: dict | None = None,
) -> dict:
    """재현성 해시를 포함한 canonical 평가 입력 manifest 생성."""
    manifest = {
        "provider": provider,
        "universe_rule": universe_rule,
        "eval_start": eval_start,
        "eval_end": eval_end,
        "universe_lookback_start": universe_lookback_start,
        "universe_lookback_end": universe_lookback_end,
        "universe": list(universe),
        "universe_size": len(universe),
        "liquidity_coverage": {
            symbol: liquidity_coverage[symbol]
            for symbol in sorted(liquidity_coverage)
        },
        "benchmark_coverage": {
            symbol: benchmark_coverage[symbol]
            for symbol in sorted(benchmark_coverage)
        },
        "fetch_errors": {
            symbol: fetch_errors[symbol]
            for symbol in sorted(fetch_errors or {})
        },
    }
    manifest["data_snapshot_hash"] = stable_payload_hash(manifest)
    return manifest


def failed_canonical_metrics(exc: Exception, stage: str) -> dict:
    return {
        "total_return": 0,
        "sharpe": 0,
        "profit_factor": 0,
        "mdd": 0,
        "total_trades": 0,
        "evaluation_status": "failed",
        "evaluation_stage": stage,
        "evaluation_error_type": type(exc).__name__,
        "error": str(exc)[:120],
    }


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


def build_promotion_results(metrics_all, evidence_dir="reports/paper_evidence", strategy_specs=None):
    from core.promotion_engine import (
        StrategyMetrics,
        attach_paper_evidence_metrics,
        attach_target_weight_canonical_hash_check,
        load_paper_evidence_package,
        paper_evidence_metrics_from_package,
        promote,
    )

    promotions = {}
    canonical_params_hashes = {
        spec.get("candidate_id"): spec.get("params_hash")
        for spec in (strategy_specs or [])
        if isinstance(spec, dict)
        and isinstance(spec.get("candidate_id"), str)
        and isinstance(spec.get("params_hash"), str)
    }
    paper_fields = (
        "paper_days",
        "paper_sharpe",
        "paper_excess",
        "paper_evidence_recommendation",
        "paper_benchmark_final_ratio",
        "paper_sell_count",
        "paper_win_rate",
        "paper_frozen_days",
        "paper_cumulative_return",
        "paper_latest_evidence_date",
        "paper_evidence_age_days",
        "paper_evidence_fresh",
        "target_weight_evidence_required",
        "target_weight_verified_pilot_days",
        "target_weight_invalid_days",
        "target_weight_all_promotable_days_verified",
        "target_weight_params_hash_consistent",
        "target_weight_params_hash",
        "target_weight_canonical_params_hash",
        "target_weight_params_hash_matches_canonical",
    )
    paper_reference_date = datetime.now()
    for name, m in metrics_all.items():
        paper_metrics = paper_evidence_metrics_from_package(
            load_paper_evidence_package(name, evidence_dir),
            reference_date=paper_reference_date,
        )
        paper_metrics = attach_target_weight_canonical_hash_check(
            name,
            paper_metrics,
            canonical_params_hashes,
        )
        for key in paper_fields:
            value = paper_metrics.get(key)
            if value is not None:
                m[key] = value

        sm = attach_paper_evidence_metrics(StrategyMetrics(
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
        ), {key: m.get(key) for key in paper_fields})
        result = promote(sm)
        promotions[name] = {
            "status": result.status,
            "allowed_modes": result.allowed_modes,
            "reason": result.reason,
        }
    return promotions


def run_canonical():
    """canonical 평가 실행 → artifact 저장."""
    from config.config_loader import Config
    from backtest.portfolio_backtester import PortfolioBacktester
    from core.data_collector import DataCollector
    import FinanceDataReader as fdr

    EVAL_START = "2023-01-01"
    EVAL_END = "2025-12-31"
    UNIVERSE_LOOKBACK_START = "2022-10-01"
    UNIVERSE_LOOKBACK_END = "2022-12-31"
    INITIAL_CAPITAL = 10_000_000
    TOP_N = 20
    UNIVERSE_RULE = "FDR KOSPI 보통주 시총 1000억+, 2022-10~12 거래대금 상위 20"
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
    liquidity_coverage = {}
    fetch_errors = {}
    for sym in candidates[:100]:
        try:
            df = dc.fetch_korean_stock(sym, UNIVERSE_LOOKBACK_START, UNIVERSE_LOOKBACK_END)
            if df is not None and not df.empty:
                if "date" in df.columns:
                    df = df.set_index("date")
                amount = (df["close"].astype(float) * df["volume"].astype(float)).mean()
                amounts[sym] = amount
                coverage = summarize_ohlcv_frame(df)
                coverage["mean_trading_value"] = round(float(amount), 2)
                liquidity_coverage[sym] = coverage
        except Exception as exc:
            fetch_errors[f"liquidity:{sym}"] = {
                "stage": "universe_liquidity",
                "error_type": type(exc).__name__,
                "error": str(exc)[:120],
            }
    universe = sorted(amounts, key=amounts.get, reverse=True)[:TOP_N]

    print(f"Universe ({len(universe)}): {universe}")

    # ── Benchmark ──
    per = INITIAL_CAPITAL / len(universe)
    parts = []
    benchmark_coverage = {}
    for sym in universe:
        try:
            df = dc.fetch_korean_stock(sym, EVAL_START, EVAL_END)
            if df is None or df.empty:
                fetch_errors[f"benchmark:{sym}"] = {
                    "stage": "benchmark",
                    "error_type": "EmptyData",
                    "error": "empty benchmark frame",
                }
                continue
            df = _normalize_ohlcv_frame(df)
            df = df[df.index >= pd.Timestamp(EVAL_START)]
            if len(df) < 2:
                fetch_errors[f"benchmark:{sym}"] = {
                    "stage": "benchmark",
                    "error_type": "InsufficientData",
                    "error": f"rows={len(df)}",
                }
                continue
            benchmark_coverage[sym] = summarize_ohlcv_frame(df)
            parts.append(per / float(df["close"].iloc[0]) * df["close"].astype(float))
        except Exception as exc:
            fetch_errors[f"benchmark:{sym}"] = {
                "stage": "benchmark",
                "error_type": type(exc).__name__,
                "error": str(exc)[:120],
            }
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
    evaluation_errors = {}
    walk_forward_errors = {}

    for strat in STRATEGIES:
        print(f"  {strat}...", end=" ", flush=True)
        div = ROTATION_DIV if strat == "relative_strength_rotation" else None
        try:
            r = run_strat(strat, universe, INITIAL_CAPITAL, EVAL_START, EVAL_END, div)
            m = calc(r, INITIAL_CAPITAL)
            m.setdefault("evaluation_status", "ok")
        except Exception as e:
            m = failed_canonical_metrics(e, "full_period")
            evaluation_errors[strat] = {
                "stage": "full_period",
                "error_type": type(e).__name__,
                "error": str(e)[:120],
            }
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
                wm.setdefault("evaluation_status", "ok")
                w_metrics.append(wm)
            except Exception as exc:
                w_metrics.append(failed_canonical_metrics(exc, f"walk_forward:{ws}:{we}"))
                walk_forward_errors.setdefault(strat, []).append({
                    "window_start": ws,
                    "window_end": we,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:120],
                })

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
            m.setdefault("evaluation_status", "ok")
        except Exception as e:
            m = failed_canonical_metrics(e, "full_period")
            evaluation_errors[name] = {
                "stage": "full_period",
                "error_type": type(e).__name__,
                "error": str(e)[:120],
            }
            print("ERROR")
            metrics_all[name] = m
            wf_all[name] = {"windows": 0, "positive": 0, "sharpe_pos": 0, "total_trades": 0, "details": []}
            continue

        w_metrics = []
        for ws, we in windows:
            try:
                wr = run_canonical_research_candidate(spec, universe, INITIAL_CAPITAL, ws, we)
                wm = calc(wr, INITIAL_CAPITAL)
                wm.setdefault("evaluation_status", "ok")
                w_metrics.append(wm)
            except Exception as exc:
                w_metrics.append(failed_canonical_metrics(exc, f"walk_forward:{ws}:{we}"))
                walk_forward_errors.setdefault(name, []).append({
                    "window_start": ws,
                    "window_end": we,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:120],
                })

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

    strategy_specs_metadata = [
        canonical_research_candidate_metadata(spec)
        for spec in research_specs
    ]

    # ── Promotion 계산 ──
    promotions = build_promotion_results(metrics_all, strategy_specs=strategy_specs_metadata)

    # ── Artifact 저장 ──
    out_dir = Path("reports/promotion")
    out_dir.mkdir(parents=True, exist_ok=True)

    data_snapshot_manifest = build_data_snapshot_manifest(
        provider="FinanceDataReader/DataCollector",
        universe_rule=UNIVERSE_RULE,
        eval_start=EVAL_START,
        eval_end=EVAL_END,
        universe_lookback_start=UNIVERSE_LOOKBACK_START,
        universe_lookback_end=UNIVERSE_LOOKBACK_END,
        universe=universe,
        liquidity_coverage=liquidity_coverage,
        benchmark_coverage=benchmark_coverage,
        fetch_errors=fetch_errors,
    )

    metadata = {
        "schema_version": 1,
        "artifact_type": "canonical_promotion_bundle",
        "eval_start": EVAL_START,
        "eval_end": EVAL_END,
        "universe_rule": UNIVERSE_RULE,
        "universe": universe,
        "data_snapshot_hash": data_snapshot_manifest["data_snapshot_hash"],
        "data_snapshot_manifest": data_snapshot_manifest,
        "evaluation_errors": evaluation_errors,
        "walk_forward_errors": walk_forward_errors,
        "canonical_research_candidate_ids": [spec.candidate_id for spec in research_specs],
        "strategy_specs": strategy_specs_metadata,
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
