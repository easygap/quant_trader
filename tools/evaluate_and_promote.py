"""
Canonical 평가 → Artifact 생성 → 승격 판정 → Status Report

실행: python tools/evaluate_and_promote.py --canonical
승격/요약 재생성: python tools/evaluate_and_promote.py --promotion-artifacts-refresh
운영 검증: python tools/evaluate_and_promote.py --check-only
요약 재생성: python tools/evaluate_and_promote.py --blocker-summary
요약 검증: python tools/evaluate_and_promote.py --blocker-summary-check
현재 blocker 갱신: python tools/evaluate_and_promote.py --current-blockers
invalid paper evidence 격리: python tools/evaluate_and_promote.py --paper-evidence-quarantine-invalid
출력: reports/promotion/
  - metrics_summary.json
  - walk_forward_summary.json
  - benchmark_comparison.json
  - run_metadata.json
  - promotion_result.json  (최종 상태 계산 결과)
  - promotion_blocker_summary.json/md (운영자용 차단 사유 요약)
"""
import sys, os, json, hashlib, subprocess, csv, math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from loguru import logger
from core.target_weight_commands import (
    command_scope_issues as target_weight_command_scope_issues,
    next_check_command_or_default as target_weight_next_check_command_or_default,
)
from core.target_weight_rotation import normalize_symbol

logger.remove()
logger.add(sys.stderr, level="WARNING")

KST = timezone(timedelta(hours=9))
TARGET_WEIGHT_DAILY_OPS_ACTIONABLE_STATUSES = frozenset({
    "READY_TO_EXECUTE",
    "READY_TO_ENABLE_CAPS",
    "WAITING_FOR_MARKET_SESSION",
})
TARGET_WEIGHT_RESTORE_TRADE_COMPARE_COLUMNS = [
    "account_key",
    "symbol",
    "action",
    "price",
    "quantity",
    "total_amount",
    "commission",
    "tax",
    "slippage",
    "strategy",
    "mode",
    "executed_at",
    "execution_session_id",
    "order_id",
]
TARGET_WEIGHT_RESTORE_POSITION_COMPARE_COLUMNS = [
    "account_key",
    "symbol",
    "quantity",
    "avg_price",
    "total_invested",
    "strategy",
]
TARGET_WEIGHT_RESTORE_NUMERIC_COLUMNS = {
    "price",
    "total_amount",
    "commission",
    "tax",
    "slippage",
    "avg_price",
    "total_invested",
}
TARGET_WEIGHT_RESTORE_AUTHORITATIVE_METADATA_COLUMNS = [
    "authoritative_source",
    "reviewed_by",
    "reviewed_at",
]
TARGET_WEIGHT_RESTORE_METADATA_PLACEHOLDER_VALUES = {
    "-",
    "--",
    "fill_me",
    "n/a",
    "na",
    "none",
    "placeholder",
    "tbd",
    "todo",
    "unknown",
}
TARGET_WEIGHT_RESTORE_REVIEWED_AT_FUTURE_TOLERANCE = timedelta(minutes=5)
TARGET_WEIGHT_RESTORE_TRADE_SAMPLE_COLUMNS = [
    "symbol",
    "action",
    "quantity",
    "price",
    "order_id",
]
TARGET_WEIGHT_RESTORE_POSITION_SAMPLE_COLUMNS = [
    "symbol",
    "quantity",
    "avg_price",
]
TARGET_WEIGHT_DAILY_OPS_ACTIONABLE_MAX_AGE_MINUTES = 30
TARGET_WEIGHT_FINALIZE_FIRST_INVALID_REASONS = frozenset({
    "target_weight_benchmark_status_not_final",
    "target_weight_excess_metrics_missing",
    "target_weight_daily_return_missing",
    "target_weight_portfolio_value_missing",
})
TARGET_WEIGHT_DB_PERSISTENCE_INVALID_REASONS = frozenset({
    "target_weight_db_persistence_complete_false",
    "target_weight_db_persistence_proof_missing",
    "target_weight_db_persistence_proof_not_checked",
    "target_weight_db_persistence_proof_incomplete",
    "target_weight_trade_history_source_not_database",
    "target_weight_positions_source_not_database",
    "target_weight_db_persistence_session_id_mismatch",
    "target_weight_db_trade_history_row_count_missing",
    "target_weight_db_trade_history_row_count_mismatch",
    "target_weight_db_trade_history_row_count_invalid",
    "target_weight_db_trade_history_ids_missing",
    "target_weight_db_trade_history_id_invalid",
    "target_weight_db_trade_history_id_duplicate",
    "target_weight_db_position_quantity_mismatch",
})
TARGET_WEIGHT_NON_REPAIRABLE_EXEMPT_INVALID_REASONS = frozenset({
    "target_weight_repaired_performance_not_promotable",
})

CANONICAL_EVAL_START = "2023-01-01"
CANONICAL_EVAL_END = "2025-12-31"
CANONICAL_UNIVERSE_LOOKBACK_START = "2022-10-01"
CANONICAL_UNIVERSE_LOOKBACK_END = "2022-12-31"
CANONICAL_TOP_N = 20
CANONICAL_UNIVERSE_RULE = (
    "FDR KOSPI 보통주 시총 1000억+, 2022-10~12 거래대금 상위 20"
)
CANONICAL_PROGRESS_PATH = Path("reports/promotion/canonical_progress.json")


CANONICAL_TARGET_WEIGHT_CANDIDATE_IDS = (
    "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35",
    "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_pdd8_floor25_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120_tol4_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120_tol4_sectorcap2_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_dd75_tol4_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol3_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrcap85_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrpen05_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrpen10_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_corrcap85_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_corrpen05_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1_volbudget60_cap35",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_posloss8_frac50_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_posloss8_frac50_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_posloss10_frac50_pdd10_floor40_cd1",
    "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1",
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


def _write_canonical_progress(
    stage: str,
    *,
    progress_path: str | Path = CANONICAL_PROGRESS_PATH,
    **payload,
) -> None:
    progress = {
        "artifact_type": "canonical_promotion_progress",
        "schema_version": 1,
        "stage": stage,
        "updated_at": datetime.now().isoformat(),
        **payload,
    }
    path = Path(progress_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _load_reusable_canonical_universe_snapshot(
    metadata_path: str | Path,
    *,
    eval_start: str = CANONICAL_EVAL_START,
    eval_end: str = CANONICAL_EVAL_END,
    universe_rule: str = CANONICAL_UNIVERSE_RULE,
    universe_lookback_start: str = CANONICAL_UNIVERSE_LOOKBACK_START,
    universe_lookback_end: str = CANONICAL_UNIVERSE_LOOKBACK_END,
    top_n: int = CANONICAL_TOP_N,
) -> dict | None:
    path = Path(metadata_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("artifact_type") != "canonical_promotion_bundle":
        return None
    if payload.get("eval_start") != eval_start or payload.get("eval_end") != eval_end:
        return None
    if payload.get("universe_rule") != universe_rule:
        return None
    manifest = payload.get("data_snapshot_manifest")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("universe_lookback_start") != universe_lookback_start:
        return None
    if manifest.get("universe_lookback_end") != universe_lookback_end:
        return None
    universe = payload.get("universe") or manifest.get("universe")
    if not isinstance(universe, list) or len(universe) != top_n:
        return None
    universe = [str(symbol).strip() for symbol in universe if str(symbol).strip()]
    if len(universe) != top_n:
        return None
    liquidity_coverage = manifest.get("liquidity_coverage")
    if not isinstance(liquidity_coverage, dict):
        return None
    fetch_errors = manifest.get("fetch_errors")
    if not isinstance(fetch_errors, dict):
        fetch_errors = {}
    return {
        "universe": universe,
        "liquidity_coverage": liquidity_coverage,
        "fetch_errors": {
            key: value
            for key, value in fetch_errors.items()
            if str(key).startswith("liquidity:")
        },
        "source": path.as_posix(),
        "source_generated_at": payload.get("generated_at"),
        "source_data_snapshot_hash": payload.get("data_snapshot_hash")
        or manifest.get("data_snapshot_hash"),
    }


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
    from tools.research_candidate_sweep import (
        build_target_weight_drawdown_guard_candidate_specs,
        build_target_weight_rotation_candidate_specs,
        build_target_weight_volatility_budget_candidate_specs,
    )

    wanted = tuple(candidate_ids or CANONICAL_TARGET_WEIGHT_CANDIDATE_IDS)
    specs = {
        spec.candidate_id: spec
        for spec in (
            *build_target_weight_rotation_candidate_specs(),
            *build_target_weight_drawdown_guard_candidate_specs(),
            *build_target_weight_volatility_budget_candidate_specs(),
        )
    }
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


def build_promotion_results(
    metrics_all,
    evidence_dir="reports/paper_evidence",
    strategy_specs=None,
    canonical_metadata=None,
):
    from core.live_gate import validate_canonical_metadata_integrity
    from core.promotion_engine import (
        StrategyMetrics,
        attach_paper_evidence_metrics,
        attach_target_weight_canonical_hash_check,
        load_paper_evidence_package,
        paper_evidence_metrics_from_package,
        promote,
        target_weight_params_hashes_from_strategy_specs,
    )

    promotions = {}
    target_weight_params_hashes = target_weight_params_hashes_from_strategy_specs(
        strategy_specs or []
    )
    if isinstance(canonical_metadata, dict):
        canonical_integrity_issues = validate_canonical_metadata_integrity(
            canonical_metadata
        )
    else:
        canonical_integrity_issues = [
            "canonical metadata missing or invalid; run_metadata.json is required"
        ]
    canonical_integrity_ok = not canonical_integrity_issues
    paper_fields = (
        "paper_days",
        "paper_sharpe",
        "paper_excess",
        "paper_cash_adjusted_excess",
        "paper_evidence_recommendation",
        "paper_evidence_block_reasons",
        "paper_benchmark_final_ratio",
        "paper_sell_count",
        "paper_win_rate",
        "paper_frozen_days",
        "paper_cumulative_return",
        "paper_latest_evidence_date",
        "paper_evidence_age_days",
        "paper_evidence_fresh",
        "paper_trade_quality_status",
        "paper_trade_quality_adverse_gap_bps",
        "paper_trade_quality_missing_expected_ratio",
        "paper_trade_quality_missing_expected_count",
        "paper_trade_quality_missing_execution_link_ratio",
        "paper_trade_quality_missing_execution_link_count",
        "target_weight_strategy_required",
        "target_weight_evidence_required",
        "target_weight_verified_pilot_days",
        "target_weight_invalid_days",
        "target_weight_all_promotable_days_verified",
        "target_weight_params_hash_consistent",
        "target_weight_params_hash",
        "target_weight_canonical_params_hash",
        "target_weight_params_hash_matches_canonical",
    )
    paper_reference_date = (
        canonical_metadata.get("generated_at")
        if isinstance(canonical_metadata, dict) and canonical_metadata.get("generated_at")
        else datetime.now()
    )
    for name, m in metrics_all.items():
        for key in paper_fields:
            m.pop(key, None)
        paper_metrics = paper_evidence_metrics_from_package(
            load_paper_evidence_package(name, evidence_dir, log_warnings=False),
            reference_date=paper_reference_date,
        )
        paper_metrics = attach_target_weight_canonical_hash_check(
            name,
            paper_metrics,
            target_weight_params_hashes,
        )
        for key in paper_fields:
            value = paper_metrics.get(key)
            if value is not None:
                m[key] = value
        m["canonical_data_integrity_ok"] = canonical_integrity_ok
        m["canonical_data_integrity_issues"] = canonical_integrity_issues

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
            canonical_benchmark_required=True,
            canonical_data_integrity_ok=m.get("canonical_data_integrity_ok"),
            canonical_data_integrity_issues=m.get("canonical_data_integrity_issues"),
            ev_per_trade=m.get("ev_per_trade"),
            cost_adjusted_cagr=m.get("cost_adjusted_cagr"),
            turnover_per_year=m.get("turnover_per_year"),
        ), {key: paper_metrics.get(key) for key in paper_fields})
        result = promote(sm)
        promotions[name] = {
            "status": result.status,
            "allowed_modes": result.allowed_modes,
            "reason": result.reason,
        }
    return promotions


PROMOTION_BLOCKER_METRIC_KEYS = (
    "total_return",
    "profit_factor",
    "mdd",
    "sharpe",
    "benchmark_excess_return",
    "benchmark_excess_sharpe",
    "wf_positive_rate",
    "wf_sharpe_positive_rate",
    "wf_windows",
    "wf_total_trades",
    "paper_days",
    "paper_cash_adjusted_excess",
    "paper_evidence_recommendation",
    "paper_latest_evidence_date",
    "paper_evidence_age_days",
    "paper_evidence_fresh",
    "paper_trade_quality_status",
    "paper_trade_quality_adverse_gap_bps",
    "paper_trade_quality_missing_expected_ratio",
    "paper_trade_quality_missing_expected_count",
    "paper_trade_quality_missing_execution_link_ratio",
    "paper_trade_quality_missing_execution_link_count",
    "target_weight_strategy_required",
    "target_weight_verified_pilot_days",
    "target_weight_invalid_days",
    "target_weight_params_hash_matches_canonical",
    "canonical_data_integrity_ok",
)

PROMOTION_BLOCKER_SOURCE_METADATA_KEYS = (
    "generated_at",
    "data_snapshot_hash",
    "commit_hash",
    "config_yaml_hash",
    "config_resolved_hash",
)

CURRENT_BLOCKERS_SCHEMA_VERSION = 3


def _promotion_reason_items(reason: str) -> list[str]:
    if not isinstance(reason, str) or not reason.strip():
        return []
    return [
        item.strip()
        for item in reason.replace("\n", " ").split(";")
        if item.strip()
    ]


def _next_promotion_action(status: str, blockers: list[str]) -> str:
    text = " ".join(blockers).lower()
    if status == "live_candidate":
        return "live readiness gate 검증 후 제한 캡 운영 검토"
    if "fill_quality" in text or "expected_price" in text or "adverse_fill_gap" in text:
        return "paper 체결 품질 리포트 확인 후 체결 왜곡/가격 기준 재검토"
    if "target-weight" in text:
        if (
            "pilot" in text
            or "paper" in text
            or "60영업일" in text
            or "params_hash" in text
        ):
            return "target-weight capped paper pilot readiness audit 실행 후 verified pilot_paper 증거 누적"
        return "target-weight pilot proof와 params hash 일치 여부 재검증"
    if "paper evidence" in text or "paper " in text or "60영업일" in text:
        return "paper evidence 최신화/누적 후 promotion package 재생성"
    if "canonical data integrity" in text or "benchmark" in text:
        return "canonical 데이터/벤치마크 coverage 재생성 후 재평가"
    if status == "paper_only":
        return "research 품질, MDD, turnover, WF 안정성 개선"
    if status == "research_only":
        return "research 후보 재설계 또는 후보군 제외 검토"
    return "승격 조건과 운영 증거를 재검토"


def _promotion_blocker_metric_snapshots(metrics_all: dict, strategy_names) -> dict:
    snapshots = {}
    if not isinstance(metrics_all, dict):
        metrics_all = {}
    for name in sorted(strategy_names):
        metrics = metrics_all.get(name) or {}
        snapshots[name] = {
            key: metrics.get(key)
            for key in PROMOTION_BLOCKER_METRIC_KEYS
            if metrics.get(key) is not None
        }
    return snapshots


def build_promotion_blocker_source_hash(promotions: dict, metrics_all: dict, metadata: dict | None = None) -> str:
    """blocker summary가 어떤 promotion artifact 조합에서 생성됐는지 추적한다."""
    if not isinstance(promotions, dict):
        promotions = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    metadata_snapshot = {
        key: metadata.get(key)
        for key in PROMOTION_BLOCKER_SOURCE_METADATA_KEYS
        if metadata.get(key) is not None
    }
    payload = {
        "promotions": {name: promotions[name] for name in sorted(promotions)},
        "metrics": _promotion_blocker_metric_snapshots(metrics_all, promotions.keys()),
        "metadata": metadata_snapshot,
    }
    return stable_payload_hash(payload)


def build_promotion_blocker_summary(promotions: dict, metrics_all: dict, metadata: dict | None = None) -> dict:
    """promotion_result를 사람이 바로 읽을 blocker 중심 운영 요약으로 변환."""
    if not isinstance(promotions, dict):
        promotions = {}
    generated_at = (
        metadata.get("generated_at")
        if isinstance(metadata, dict) and metadata.get("generated_at")
        else datetime.now().isoformat()
    )
    status_counts: dict[str, int] = {}
    strategies = {}
    metric_snapshots = _promotion_blocker_metric_snapshots(metrics_all, promotions.keys())
    for name in sorted(promotions):
        promotion = promotions.get(name) or {}
        status = promotion.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        blockers = _promotion_reason_items(promotion.get("reason", ""))
        strategies[name] = {
            "status": status,
            "allowed_modes": promotion.get("allowed_modes", []),
            "next_action": _next_promotion_action(status, blockers),
            "blockers": blockers,
            "metrics": metric_snapshots.get(name, {}),
            "reason": promotion.get("reason", ""),
        }
    return {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": generated_at,
        "source_artifact_hash": build_promotion_blocker_source_hash(promotions, metrics_all, metadata),
        "summary": {
            "total_strategies": len(strategies),
            "status_counts": dict(sorted(status_counts.items())),
            "live_ready_count": status_counts.get("live_candidate", 0),
            "blocked_from_live_count": len(strategies) - status_counts.get("live_candidate", 0),
        },
        "strategies": strategies,
    }


def _md_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def write_promotion_blocker_summary(summary: dict, output_dir: str | Path) -> tuple[Path, Path]:
    """promotion blocker 요약 JSON/Markdown artifact를 저장."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "promotion_blocker_summary.json"
    md_path = out_dir / "promotion_blocker_summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Promotion Blocker Summary",
        "",
        f"- Generated: {summary.get('generated_at')}",
        f"- Total strategies: {summary.get('summary', {}).get('total_strategies', 0)}",
        f"- Live ready: {summary.get('summary', {}).get('live_ready_count', 0)}",
        f"- Blocked from live: {summary.get('summary', {}).get('blocked_from_live_count', 0)}",
        "",
        "| Strategy | Status | Next Action | Key Blockers |",
        "|---|---|---|---|",
    ]
    for name, item in (summary.get("strategies") or {}).items():
        blockers = item.get("blockers") or []
        blocker_text = "<br>".join(blockers[:4]) if blockers else "없음"
        lines.append(
            "| "
            + " | ".join([
                _md_cell(name),
                _md_cell(item.get("status")),
                _md_cell(item.get("next_action")),
                _md_cell(blocker_text),
            ])
            + " |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _load_promotion_blocker_summary_from_artifacts_unchecked(
    artifact_dir: str | Path = "reports/promotion",
) -> dict:
    """검증 없이 기존 promotion artifact에서 blocker summary를 재구성한다."""
    base = Path(artifact_dir)
    promotion_path = base / "promotion_result.json"
    metrics_path = base / "metrics_summary.json"
    metadata_path = base / "run_metadata.json"
    promotions = json.loads(promotion_path.read_text(encoding="utf-8"))
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    if not isinstance(promotions, dict):
        raise ValueError(f"{promotion_path} top-level JSON is not an object")
    if not isinstance(metrics, dict):
        raise ValueError(f"{metrics_path} top-level JSON is not an object")
    if not isinstance(metadata, dict):
        raise ValueError(f"{metadata_path} top-level JSON is not an object")
    return build_promotion_blocker_summary(promotions, metrics, metadata)


def load_promotion_blocker_summary_from_artifacts(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
    *,
    validate: bool = True,
) -> dict:
    """기존 promotion artifact에서 검증된 blocker summary를 재구성한다.

    운영 파일 생성에 쓰는 public helper는 기본적으로 저장된 promotion_result가
    현재 metrics/evidence 재계산 결과와 맞을 때만 summary를 반환한다.
    """
    if validate:
        issues = validate_promotion_result_recalculation(
            artifact_dir,
            evidence_dir=evidence_dir,
        )
        if issues:
            raise ValueError(
                "promotion_result 재계산 검증 실패: "
                + "; ".join(issues)
                + "; --promotion-artifacts-refresh 먼저 실행 필요"
            )
    return _load_promotion_blocker_summary_from_artifacts_unchecked(artifact_dir)


def _read_json_object(path: Path) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{path} 로드 실패: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path} top-level JSON is not an object"
    return payload, None


def validate_metrics_summary_source_artifact_sync(
    artifact_dir: str | Path = "reports/promotion",
) -> list[str]:
    """metrics_summary의 파생 값이 benchmark/WF 원천 artifact와 일치하는지 검사."""
    from core.promotion_engine import validate_metrics_source_artifact_sync

    base = Path(artifact_dir)
    metrics, metrics_error = _read_json_object(base / "metrics_summary.json")
    walk_forward, wf_error = _read_json_object(base / "walk_forward_summary.json")
    benchmark, benchmark_error = _read_json_object(base / "benchmark_comparison.json")

    issues = [
        issue
        for issue in (metrics_error, wf_error, benchmark_error)
        if issue
    ]
    if issues:
        return issues

    assert metrics is not None
    assert walk_forward is not None
    assert benchmark is not None

    return validate_metrics_source_artifact_sync(metrics, walk_forward, benchmark)


def recalculate_promotion_results_from_artifacts(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> dict:
    """metrics/evidence/metadata 기준으로 promotion_result를 다시 계산한다."""
    promotions, _metrics, _metadata = recalculate_promotion_bundle_from_artifacts(
        artifact_dir,
        evidence_dir=evidence_dir,
    )
    return promotions


def recalculate_promotion_bundle_from_artifacts(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> tuple[dict, dict, dict]:
    """metrics/evidence/metadata 기준 promotion_result와 갱신 metrics를 계산한다."""
    base = Path(artifact_dir)
    source_issues = validate_metrics_summary_source_artifact_sync(base)
    if source_issues:
        raise ValueError(
            "metrics source artifact 동기화 실패: "
            + "; ".join(source_issues)
            + "; --canonical 재생성 필요"
        )
    metrics_path = base / "metrics_summary.json"
    metadata_path = base / "run_metadata.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metrics, dict):
        raise ValueError(f"{metrics_path} top-level JSON is not an object")
    if not isinstance(metadata, dict):
        raise ValueError(f"{metadata_path} top-level JSON is not an object")
    metrics_for_recalc = json.loads(json.dumps(metrics, ensure_ascii=False, default=str))
    inferred_evidence_dir = (
        Path(evidence_dir)
        if evidence_dir is not None
        else base.parent / "paper_evidence"
    )
    strategy_specs = metadata.get("strategy_specs")
    promotions = build_promotion_results(
        metrics_for_recalc,
        evidence_dir=str(inferred_evidence_dir),
        strategy_specs=strategy_specs if isinstance(strategy_specs, list) else [],
        canonical_metadata=metadata,
    )
    return promotions, metrics_for_recalc, metadata


def refresh_promotion_artifacts_from_existing_inputs(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
    current_blockers_path: str | Path = "reports/current_blockers.json",
) -> dict[str, Path]:
    """기존 metrics/evidence/metadata에서 promotion과 파생 운영 파일을 재생성한다."""
    base = Path(artifact_dir)
    base.mkdir(parents=True, exist_ok=True)
    promotions, metrics, metadata = recalculate_promotion_bundle_from_artifacts(
        base,
        evidence_dir=evidence_dir,
    )
    promotion_path = base / "promotion_result.json"
    metrics_path = base / "metrics_summary.json"
    promotion_path.write_text(
        json.dumps(promotions, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    blocker_summary = build_promotion_blocker_summary(promotions, metrics, metadata)
    blocker_json_path, blocker_md_path = write_promotion_blocker_summary(
        blocker_summary,
        base,
    )
    current_path = write_current_blockers_report(
        _build_current_blockers_report_with_latest_ops(
            blocker_summary,
            reports_dir=base.parent,
        ),
        current_blockers_path,
    )
    return {
        "promotion_result": promotion_path,
        "metrics_summary": metrics_path,
        "promotion_blocker_summary": blocker_json_path,
        "promotion_blocker_summary_md": blocker_md_path,
        "current_blockers": current_path,
    }


def validate_promotion_result_recalculation(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> list[str]:
    """저장된 promotion_result가 현재 metrics/evidence 재계산 결과와 같은지 검사."""
    base = Path(artifact_dir)
    promotion_path = base / "promotion_result.json"
    try:
        current = json.loads(promotion_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{promotion_path} 로드 실패: {exc}"]
    if not isinstance(current, dict):
        return [f"{promotion_path} top-level JSON is not an object"]

    try:
        expected = recalculate_promotion_results_from_artifacts(base, evidence_dir=evidence_dir)
    except Exception as exc:
        return [f"promotion_result 재계산 실패: {exc}"]

    issues = []
    current_names = set(current)
    expected_names = set(expected)
    missing = sorted(expected_names - current_names)
    extra = sorted(current_names - expected_names)
    if missing:
        issues.append(f"promotion_result 누락 전략: {missing[:8]}")
    if extra:
        issues.append(f"promotion_result 불필요 전략: {extra[:8]}")
    for name in sorted(current_names & expected_names):
        current_item = current.get(name) or {}
        expected_item = expected.get(name) or {}
        for key in ("status", "allowed_modes", "reason"):
            if current_item.get(key) != expected_item.get(key):
                issues.append(
                    f"promotion_result {name}.{key} 재계산 결과 불일치: "
                    "--canonical 또는 promotion_result 재생성 필요"
                )
    return issues


def load_validated_promotion_blocker_summary_from_artifacts(
    artifact_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> dict:
    """promotion_result 재계산 검증 통과 후 blocker summary를 재구성한다."""
    return load_promotion_blocker_summary_from_artifacts(
        artifact_dir,
        evidence_dir=evidence_dir,
        validate=True,
    )


def validate_promotion_blocker_summary_artifact(artifact_dir: str | Path = "reports/promotion") -> list[str]:
    """저장된 blocker summary가 현재 promotion artifact와 동기화됐는지 검사한다."""
    base = Path(artifact_dir)
    summary_path = base / "promotion_blocker_summary.json"
    if not summary_path.exists():
        return [f"{summary_path} 없음: --blocker-summary로 재생성 필요"]

    try:
        current = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{summary_path} 로드 실패: {exc}"]
    if not isinstance(current, dict):
        return [f"{summary_path} top-level JSON is not an object"]

    try:
        expected = load_promotion_blocker_summary_from_artifacts(
            base,
            validate=False,
        )
    except Exception as exc:
        return [f"promotion source artifact 로드 실패: {exc}"]

    issues = []
    if current.get("artifact_type") != expected.get("artifact_type"):
        issues.append("artifact_type 불일치")
    if current.get("schema_version") != expected.get("schema_version"):
        issues.append("schema_version 불일치")
    if current.get("source_artifact_hash") != expected.get("source_artifact_hash"):
        issues.append("source_artifact_hash 불일치: --blocker-summary로 재생성 필요")
    for key in ("summary", "strategies"):
        if current.get(key) != expected.get(key):
            issues.append(f"{key} 내용 불일치: --blocker-summary로 재생성 필요")
    issues.extend(validate_promotion_result_recalculation(base))
    return issues


def _strategy_names_by_status(blocker_summary: dict, status: str) -> list[str]:
    strategies = blocker_summary.get("strategies") or {}
    return [
        name
        for name, item in strategies.items()
        if isinstance(item, dict) and item.get("status") == status
    ]


def _ranked_provisional_candidates(blocker_summary: dict) -> list[str]:
    """운영 우선순위 힌트용 provisional 후보 정렬."""
    strategies = blocker_summary.get("strategies") or {}
    candidates = _strategy_names_by_status(blocker_summary, "provisional_paper_candidate")

    def score(name: str) -> tuple[float, float, float, str]:
        metrics = (strategies.get(name) or {}).get("metrics") or {}
        return (
            float(metrics.get("benchmark_excess_return") or 0),
            float(metrics.get("sharpe") or 0),
            -abs(float(metrics.get("mdd") or 0)),
            name,
        )

    return sorted(candidates, key=score, reverse=True)


def _target_weight_operator_commands(strategy: str) -> dict[str, str]:
    base = f"python tools/target_weight_rotation_pilot.py --candidate-id {strategy}"
    return {
        "check_promotion_artifacts": "python tools/evaluate_and_promote.py --check-only",
        "daily_ops_summary": f"{base} --daily-ops-summary",
        "readiness_audit": f"{base} --readiness-audit",
        "collect_shadow_days": f"{base} --shadow-days 3 --shadow-end-date YYYY-MM-DD",
        "pilot_status": f"python tools/paper_pilot_control.py --strategy {strategy} --status",
        "preflight_recheck": _target_weight_preflight_recheck_command(strategy),
        "finalize_pilot_evidence": f"{base} --finalize-pilot-evidence --finalize-date YYYY-MM-DD",
        "repair_pilot_evidence": f"{base} --repair-pilot-evidence --repair-date YYYY-MM-DD",
        "execute_capped_paper_after_ready": f"{base} --execute --collect-evidence",
        "regenerate_current_blockers": "python tools/evaluate_and_promote.py --current-blockers",
    }


def _current_kst_datetime() -> datetime:
    return datetime.now(KST)


def _current_kst_date() -> str:
    return _current_kst_datetime().date().isoformat()


def _artifact_source_path(path: Path) -> str:
    return path.as_posix()


def _daily_ops_trade_day_is_available(payload: dict, *, current_date: str | None = None) -> bool:
    trade_day = str(payload.get("trade_day") or "").strip()
    if not trade_day:
        return False
    today = current_date or _current_kst_date()
    try:
        trade_date = datetime.strptime(trade_day, "%Y-%m-%d").date()
        current = datetime.strptime(today, "%Y-%m-%d").date()
        return trade_date <= current
    except ValueError:
        return False


def _daily_ops_trade_day_sort_key(payload: dict, path: Path) -> tuple[str, float, float]:
    source_mtime = path.stat().st_mtime
    payload_with_source = {
        **payload,
        "source_path": _artifact_source_path(path),
        "source_mtime": source_mtime,
    }
    artifact_ts = _artifact_time_key(payload_with_source)[0]
    return (str(payload.get("trade_day") or ""), artifact_ts, source_mtime)


def _stable_daily_ops_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _daily_ops_summary_hash_is_valid(payload: dict) -> bool:
    summary_hash = str(payload.get("summary_hash") or "").strip()
    if not summary_hash:
        return False
    normalized = dict(payload)
    normalized.pop("summary_hash", None)
    return _stable_daily_ops_hash(normalized) == summary_hash


def _target_weight_enable_blocker(payload: dict, command: str | None = None) -> str | None:
    status = str(payload.get("status") or "")
    trade_day = str(payload.get("trade_day") or "").strip() or "UNKNOWN"
    next_trade_day = str(payload.get("next_operator_trade_day") or "").strip()
    next_hint = next_trade_day or "next KRX business day"
    if status == "PILOT_EVIDENCE_RECORDED":
        return (
            f"# blocked: pilot_paper evidence already recorded for {trade_day}; "
            f"rerun readiness audit for {next_hint}"
        )
    if status == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE":
        return (
            f"# blocked: repaired pilot_paper evidence already recorded for {trade_day}; "
            f"rerun readiness audit for {next_hint}"
        )
    if status == "PILOT_EVIDENCE_INVALID":
        return (
            f"# blocked: pilot_paper evidence invalid for {trade_day}; "
            "finalize or repair evidence before changing pilot caps"
        )
    if status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}:
        return None
    if str(command or "").strip():
        return (
            f"# blocked: daily_ops_summary.status == {status}; "
            "READY_TO_ENABLE_CAPS 전 cap 변경 금지"
        )
    return None


def _ready_to_execute_trade_day_is_current(payload: dict) -> bool:
    trade_day = str(payload.get("trade_day") or "").strip()
    if not trade_day:
        return False
    try:
        trade_date = datetime.strptime(trade_day, "%Y-%m-%d").date()
        current = datetime.strptime(_current_kst_date(), "%Y-%m-%d").date()
    except ValueError:
        return False
    return trade_date == current


def _parse_target_weight_daily_ops_generated_at(payload: dict) -> datetime | None:
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        return None
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _target_weight_actionable_daily_ops_freshness_blocker(payload: dict) -> str | None:
    status = str(payload.get("status") or "").strip()
    if status not in TARGET_WEIGHT_DAILY_OPS_ACTIONABLE_STATUSES:
        return None
    if not _ready_to_execute_trade_day_is_current(payload):
        return None

    now = _current_kst_datetime()
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    else:
        now = now.astimezone(KST)
    current_date = _current_kst_date()
    if now.date().isoformat() != current_date:
        return None

    generated_at_raw = str(payload.get("generated_at") or "").strip()
    if not generated_at_raw:
        return (
            "# blocked: daily_ops_summary.generated_at missing; "
            "rerun daily ops summary before action"
        )
    generated_at = _parse_target_weight_daily_ops_generated_at(payload)
    if generated_at is None:
        return (
            "# blocked: daily_ops_summary.generated_at invalid; "
            "rerun daily ops summary before action"
        )
    if generated_at.date().isoformat() != current_date:
        return (
            "# blocked: daily_ops_summary.generated_at is stale; "
            "rerun daily ops summary for the current KRX business day"
        )

    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < -60:
        return (
            "# blocked: daily_ops_summary.generated_at is in the future; "
            "rerun daily ops summary before action"
        )
    max_age_seconds = TARGET_WEIGHT_DAILY_OPS_ACTIONABLE_MAX_AGE_MINUTES * 60
    if age_seconds > max_age_seconds:
        return (
            "# blocked: daily_ops_summary.generated_at is stale "
            f"(>{TARGET_WEIGHT_DAILY_OPS_ACTIONABLE_MAX_AGE_MINUTES}m); "
            "rerun daily ops summary before action"
        )
    return None


def _target_weight_command_scope_issues(
    payload: dict,
    command: str,
    *,
    require_trade_day: bool,
    required_flags: tuple[str, ...],
) -> list[str]:
    return target_weight_command_scope_issues(
        payload,
        command,
        require_trade_day=require_trade_day,
        required_flags=required_flags,
    )


def _sanitize_target_weight_daily_ops_summary(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    sanitized = dict(payload)
    status = sanitized.get("status")
    decision = sanitized.get("decision") or {}
    operator_commands = dict(sanitized.get("operator_commands") or {})
    actionable_freshness_blocker = (
        _target_weight_actionable_daily_ops_freshness_blocker(sanitized)
    )

    enable_command = str(operator_commands.get("enable_suggested_caps") or "")
    enable_blocker = _target_weight_enable_blocker(sanitized, enable_command)
    if enable_blocker:
        operator_commands["enable_suggested_caps"] = enable_blocker
    elif (
        status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}
        and not _ready_to_execute_trade_day_is_current(sanitized)
    ):
        operator_commands["enable_suggested_caps"] = (
            "# blocked: daily_ops_summary.trade_day is stale; "
            "rerun daily ops summary for the current KRX business day"
        )
    elif (
        status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}
        and actionable_freshness_blocker
    ):
        operator_commands["enable_suggested_caps"] = actionable_freshness_blocker
    elif status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}:
        enable_issues = _target_weight_command_scope_issues(
            sanitized,
            enable_command,
            require_trade_day=False,
            required_flags=("--enable",),
        )
        if enable_issues:
            operator_commands["enable_suggested_caps"] = (
                "# blocked: daily_ops_enable_command_unavailable: "
                + "; ".join(enable_issues)
            )

    execute_command = str(operator_commands.get("execute_capped_paper") or "")
    if status == "READY_TO_EXECUTE" and not _ready_to_execute_trade_day_is_current(sanitized):
        operator_commands["execute_capped_paper"] = (
            "# blocked: daily_ops_summary.trade_day is stale; "
            "rerun daily ops summary for the current KRX business day"
        )
    elif status == "READY_TO_EXECUTE" and actionable_freshness_blocker:
        operator_commands["execute_capped_paper"] = actionable_freshness_blocker
    elif status == "READY_TO_EXECUTE":
        execute_issues = _target_weight_command_scope_issues(
            sanitized,
            execute_command,
            require_trade_day=True,
            required_flags=("--execute", "--collect-evidence"),
        )
        if execute_issues:
            operator_commands["execute_capped_paper"] = (
                "# blocked: daily_ops_execute_command_unavailable: "
                + "; ".join(execute_issues)
            )
    elif status != "READY_TO_EXECUTE":
        if not execute_command.lstrip().startswith("# blocked:"):
            reason = (
                _first_text(decision.get("blocking_reasons"))
                or sanitized.get("next_step")
                or status
            )
            operator_commands["execute_capped_paper"] = f"# blocked: {reason}"

    if status in {"PILOT_EVIDENCE_RECORDED", "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"}:
        operator_commands["next_daily_ops_summary"] = (
            target_weight_next_check_command_or_default(
                sanitized,
                operator_commands.get("next_daily_ops_summary"),
                "--daily-ops-summary",
            )
        )
        operator_commands["next_readiness_audit"] = (
            target_weight_next_check_command_or_default(
                sanitized,
                operator_commands.get("next_readiness_audit"),
                "--readiness-audit",
            )
        )

    sanitized["decision"] = decision
    sanitized["operator_commands"] = operator_commands
    return sanitized


def _load_latest_target_weight_daily_ops(
    strategy: str,
    reports_dir: str | Path = "reports",
) -> dict | None:
    """후보별 최신 daily ops summary가 있으면 current blockers에 반영한다."""
    base = Path(reports_dir)
    prefix = f"target_weight_daily_ops_summary_{strategy}_"
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)
    candidates = sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob(f"{prefix}*.json")
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    valid_candidates: list[tuple[tuple[str, float], dict]] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_daily_ops_summary":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if not _daily_ops_summary_hash_is_valid(payload):
            continue
        if not _daily_ops_trade_day_is_available(payload):
            continue
        sanitized = _sanitize_target_weight_daily_ops_summary({
            "source_path": _artifact_source_path(path),
            "generated_at": payload.get("generated_at"),
            "candidate_id": payload.get("candidate_id"),
            "trade_day": payload.get("trade_day"),
            "next_operator_trade_day": payload.get("next_operator_trade_day"),
            "status": payload.get("status"),
            "next_step": payload.get("next_step"),
            "evidence_progress": payload.get("evidence_progress") or {},
            "decision": payload.get("decision") or {},
            "operator_commands": payload.get("operator_commands") or {},
        })
        if sanitized is not None:
            valid_candidates.append((_daily_ops_trade_day_sort_key(payload, path), sanitized))
    if valid_candidates:
        return max(valid_candidates, key=lambda item: item[0])[1]
    return None


def _target_weight_daily_ops_failure_paths(
    strategy: str,
    reports_dir: str | Path = "reports",
) -> list[Path]:
    base = Path(reports_dir)
    prefix = f"target_weight_daily_ops_summary_failure_{strategy}_"
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)
    return sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob(f"{prefix}*.json")
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _artifact_generated_timestamp(payload: dict) -> float:
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        return 0.0
    try:
        return datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _artifact_source_mtime(payload: dict) -> float:
    try:
        return float(payload.get("source_mtime") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _artifact_time_key(payload: dict) -> tuple[float, float]:
    source_mtime = _artifact_source_mtime(payload)
    source_path = str(payload.get("source_path") or "").strip()
    if not source_mtime and source_path:
        try:
            source_mtime = Path(source_path).stat().st_mtime
        except OSError:
            source_mtime = 0.0
    generated_ts = _artifact_generated_timestamp(payload)
    return (generated_ts or source_mtime, source_mtime)


def _load_latest_target_weight_daily_ops_failure(
    strategy: str,
    reports_dir: str | Path = "reports",
) -> dict | None:
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in _target_weight_daily_ops_failure_paths(strategy, reports_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("candidate_id") != strategy:
            continue
        artifact_type = str(payload.get("artifact_type") or "")
        mode = str(payload.get("mode") or "")
        if artifact_type == "target_weight_no_order_operation_failure":
            if mode != "daily_ops_summary":
                continue
        elif artifact_type != "target_weight_daily_ops_summary_failure":
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append((_artifact_time_key(payload), payload))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _daily_ops_failure_is_active(
    failure: dict | None,
    latest_daily_ops: dict | None,
) -> bool:
    if not failure:
        return False
    if not latest_daily_ops:
        return True
    return _artifact_time_key(failure) > _artifact_time_key(latest_daily_ops)


def _target_weight_daily_ops_failure_reason(payload: dict) -> str:
    reason = str(payload.get("reason") or "").strip()
    if reason:
        return reason
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    elif error:
        return str(error)
    blocking_reasons = payload.get("blocking_reasons")
    if isinstance(blocking_reasons, list) and blocking_reasons:
        return str(blocking_reasons[0])
    return "unknown"


def _target_weight_daily_ops_failure_error(payload: dict) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return str(error or "").strip()
    error_type = str(error.get("type") or "").strip()
    message = str(error.get("message") or "").strip()
    if error_type and message:
        return f"{error_type}: {message}"
    return error_type or message


def _target_weight_daily_ops_failure_command(
    payload: dict,
    commands: dict[str, str],
) -> str:
    payload_commands = payload.get("operator_commands")
    if isinstance(payload_commands, dict):
        command = str(payload_commands.get("daily_ops_summary") or "").strip()
        if command:
            return command
    return commands.get("daily_ops_summary") or ""


def _target_weight_daily_ops_failure_follow_up(
    payload: dict,
    commands: dict[str, str],
) -> str:
    payload_commands = payload.get("operator_commands")
    if isinstance(payload_commands, dict):
        command = str(payload_commands.get("readiness_audit") or "").strip()
        if command:
            return command
    return commands.get("readiness_audit") or ""


def _target_weight_daily_ops_failure_waits_for_market_data(reason: str) -> bool:
    return "target_weight_requested_trade_day_unavailable" in str(reason or "")


def _target_weight_daily_ops_market_data_blocked_command(reason: str) -> str:
    reason_text = str(reason or "requested trade-day market data unavailable").strip()
    return f"# blocked: requested trade-day market data unavailable; {reason_text}"


def _target_weight_daily_ops_failure_action(
    strategy: str,
    commands: dict[str, str],
    failure: dict,
) -> dict:
    reason = _target_weight_daily_ops_failure_reason(failure)
    command = _target_weight_daily_ops_failure_command(failure, commands)
    follow_up = _target_weight_daily_ops_failure_follow_up(failure, commands)
    action = {
        "strategy": strategy,
        "source": "latest_daily_ops_failure",
        "source_path": failure.get("source_path"),
        "daily_ops_status": "FAILED",
        "failure_status": failure.get("status") or "BLOCKED",
        "failure_reason": reason,
        "generated_at": failure.get("generated_at"),
        "as_of_date": failure.get("as_of_date"),
        "desc": "daily ops summary 실패 원인 해소 후 summary 재생성",
        "command": command,
        "order_safety": "no_order",
        "requires": "daily ops failure resolved",
        "follow_up": follow_up,
    }
    if _target_weight_daily_ops_failure_waits_for_market_data(reason):
        action.update({
            "desc": "요청 거래일 시장 데이터 확인 후 daily ops summary 재생성",
            "command": _target_weight_daily_ops_market_data_blocked_command(reason),
            "scheduled_command": command,
            "requires": "requested trade-day market data available",
            "market_data_guard": "target_weight_requested_trade_day_unavailable",
        })
        if follow_up:
            action["follow_up"] = _target_weight_daily_ops_market_data_blocked_command(reason)
            action["scheduled_follow_up"] = follow_up
    failure_error = _target_weight_daily_ops_failure_error(failure)
    if failure_error:
        action["failure_error"] = failure_error
    return action


def _public_target_weight_daily_ops_failure(payload: dict) -> dict:
    public = dict(payload)
    public.pop("source_mtime", None)
    return public


def _target_weight_finalize_waits_for_performance(report: dict | None) -> bool:
    if not isinstance(report, dict):
        return False
    reason = str(report.get("reason") or "").strip()
    return "target_weight_pilot_evidence_finalize_missing_performance" in reason


def _target_weight_finalize_blocks_on_db_persistence(report: dict | None) -> bool:
    if not isinstance(report, dict):
        return False
    reasons = [
        str(report.get("reason") or "").strip(),
        str((report.get("proof_status_before") or {}).get("reason") or "").strip(),
        str((report.get("proof_status_after") or {}).get("reason") or "").strip(),
    ]
    return any(
        db_reason and any(db_reason in reason for reason in reasons)
        for db_reason in TARGET_WEIGHT_DB_PERSISTENCE_INVALID_REASONS
    )


def _target_weight_finalize_report_has_performance_diagnostics(report: dict | None) -> bool:
    if not isinstance(report, dict):
        return False
    status = report.get("performance_evidence_status")
    if not isinstance(status, dict):
        return False
    return any(
        key in status
        for key in (
            "source_record_fields_present",
            "portfolio_metrics_checked",
            "portfolio_metrics_fields_present",
            "missing_fields_after_probe",
        )
    )


def _target_weight_portfolio_metrics_recovery_hint(probe_status: str) -> str:
    if probe_status == "missing_snapshot_history":
        return (
            "restore or create portfolio snapshot history for the target-weight account_key"
        )
    if probe_status == "missing_current_snapshot_after_trades":
        return "run end-of-day portfolio snapshot capture for the trade day"
    return ""


def _target_weight_portfolio_snapshot_diagnostics_command(
    strategy: str,
    snapshot_date: str | None,
) -> str:
    if not strategy or not snapshot_date:
        return ""
    return (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} "
        f"--diagnose-portfolio-snapshot --snapshot-date {snapshot_date}"
    )


def _target_weight_snapshot_recovery_guard_from_blockers(blockers) -> str:
    blocker_set = {str(blocker) for blocker in blockers or []}
    if (
        "source_record_db_persistence_incomplete" in blocker_set
        or "artifact_fills_without_current_db_trades" in blocker_set
        or "db_execution_state_missing_for_account_key" in blocker_set
    ):
        return "target_weight_db_persistence_proof_required_before_snapshot"
    if "current_portfolio_snapshot_missing_after_trades" in blocker_set:
        return "target_weight_current_portfolio_snapshot_required"
    if "portfolio_snapshot_history_missing" in blocker_set:
        return "target_weight_portfolio_snapshot_history_required"
    return ""


def _target_weight_snapshot_recovery_hint_from_blockers(
    blockers,
    fallback: str,
) -> str:
    blocker_set = {str(blocker) for blocker in blockers or []}
    if (
        "source_record_db_persistence_incomplete" in blocker_set
        or "artifact_fills_without_current_db_trades" in blocker_set
    ):
        return (
            "restore target-weight DB trade_history/positions persistence proof "
            "before creating a portfolio snapshot"
        )
    if "db_execution_state_missing_for_account_key" in blocker_set:
        return (
            "restore target-weight paper DB execution state for the account_key "
            "before snapshot recovery"
        )
    return fallback


def _target_weight_snapshot_recovery_blocked_command(guard: str) -> str:
    if guard == "target_weight_db_persistence_proof_required_before_snapshot":
        return (
            "# blocked: restore authoritative DB trade_history/positions proof "
            "before target-weight snapshot recovery"
        )
    if guard == "target_weight_current_portfolio_snapshot_required":
        return (
            "# blocked: capture current portfolio snapshot before target-weight finalize"
        )
    if guard == "target_weight_portfolio_snapshot_history_required":
        return (
            "# blocked: restore portfolio snapshot history before target-weight finalize"
        )
    return "# blocked: portfolio snapshot recovery required before target-weight finalize"


def _load_target_weight_snapshot_diagnostics_report(
    strategy: str,
    snapshot_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not snapshot_date:
        return None
    base = Path(reports_dir)
    paths = [
        base / f"target_weight_portfolio_snapshot_diagnostics_{strategy}_{snapshot_date}.json",
        base / "paper_runtime" / f"target_weight_portfolio_snapshot_diagnostics_{strategy}_{snapshot_date}.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_portfolio_snapshot_diagnostics":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("snapshot_date") != snapshot_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        return payload
    return None


def _load_target_weight_db_restore_verification_report(
    strategy: str,
    snapshot_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not snapshot_date:
        return None
    base = Path(reports_dir)
    paths = [
        base
        / f"target_weight_db_restore_package_verification_{strategy}_{snapshot_date}.json",
        base
        / "paper_runtime"
        / f"target_weight_db_restore_package_verification_{strategy}_{snapshot_date}.json",
    ]
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_db_restore_package_verification":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("snapshot_date") != snapshot_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append((_artifact_time_key(payload), payload))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _load_target_weight_db_restore_review_bundle_report(
    strategy: str,
    snapshot_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not snapshot_date:
        return None
    base = Path(reports_dir)
    paths = [
        base
        / f"target_weight_db_restore_review_bundle_{strategy}_{snapshot_date}.json",
        base
        / "paper_runtime"
        / f"target_weight_db_restore_review_bundle_{strategy}_{snapshot_date}.json",
    ]
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_db_restore_review_bundle":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("snapshot_date") != snapshot_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append((_artifact_time_key(payload), payload))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _load_target_weight_db_restore_apply_plan_report(
    strategy: str,
    snapshot_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not snapshot_date:
        return None
    base = Path(reports_dir)
    paths = [
        base / f"target_weight_db_restore_apply_plan_{strategy}_{snapshot_date}.json",
        base
        / "paper_runtime"
        / f"target_weight_db_restore_apply_plan_{strategy}_{snapshot_date}.json",
    ]
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_db_restore_apply_plan":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("snapshot_date") != snapshot_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append((_artifact_time_key(payload), payload))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _load_target_weight_db_restore_backup_report(
    strategy: str,
    snapshot_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not snapshot_date:
        return None
    base = Path(reports_dir)
    paths = [
        base
        / f"target_weight_db_restore_pre_apply_backup_{strategy}_{snapshot_date}.json",
        base
        / "paper_runtime"
        / f"target_weight_db_restore_pre_apply_backup_{strategy}_{snapshot_date}.json",
    ]
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_db_restore_pre_apply_backup":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("snapshot_date") != snapshot_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append((_artifact_time_key(payload), payload))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _load_target_weight_finalize_report(
    strategy: str,
    finalize_date: str | None,
    reports_dir: str | Path = "reports",
) -> dict | None:
    if not strategy or not finalize_date:
        return None
    base = Path(reports_dir)
    paths = [
        base / f"target_weight_pilot_evidence_finalize_{strategy}_{finalize_date}.json",
        base / "paper_runtime" / f"target_weight_pilot_evidence_finalize_{strategy}_{finalize_date}.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_pilot_evidence_finalize":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("finalize_date") != finalize_date:
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        return payload
    return None


def validate_target_weight_daily_ops_artifacts(
    reports_dir: str | Path = "reports",
) -> list[str]:
    """저장된 target-weight daily ops summary artifact의 무결성을 검사한다."""
    base = Path(reports_dir)
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)

    issues: list[str] = []
    paths = sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob("target_weight_daily_ops_summary_*.json")
            if not path.name.startswith("target_weight_daily_ops_summary_failure_")
        }
    )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"{path} 로드 실패: {exc.__class__.__name__}")
            continue
        if not isinstance(payload, dict):
            issues.append(f"{path} top-level JSON is not an object")
            continue
        if payload.get("artifact_type") != "target_weight_daily_ops_summary":
            continue
        if payload.get("schema_version") != 1:
            issues.append(f"{path} schema_version 불일치")
        if not str(payload.get("candidate_id") or "").strip():
            issues.append(f"{path} candidate_id 누락")
        if not str(payload.get("trade_day") or "").strip():
            issues.append(f"{path} trade_day 누락")
        if not _daily_ops_summary_hash_is_valid(payload):
            issues.append(f"{path} summary_hash 불일치 또는 누락")
    return issues


def _reason_contains(reasons: list[str], *needles: str) -> bool:
    lowered = " ".join(str(reason).lower() for reason in reasons)
    return any(needle.lower() in lowered for needle in needles)


def _blocked_command(command: str | None) -> bool:
    return str(command or "").lstrip().startswith("# blocked:")


def _paper_execute_order_safety(command: str | None) -> str:
    return "no_order" if _blocked_command(command) else "paper_order_only"


def _target_weight_preflight_recheck_command(strategy: str) -> str:
    return (
        "python tools/paper_preflight.py "
        f"--strategy {strategy} --with-pilot-check --send-test-notification"
    )


def _target_weight_discord_action(
    strategy: str,
    base_action: dict,
    blockers: list[str],
) -> dict:
    command = _target_weight_preflight_recheck_command(strategy)
    if _reason_contains(blockers, "미설정", "not configured", "unconfigured"):
        return {
            **base_action,
            "desc": "Discord webhook 설정 후 도달성 확인 preflight 실행",
            "setup_required": True,
            "required_env": "DISCORD_WEBHOOK_URL",
            "config_path": "config/settings.yaml: discord.enabled=true",
            "setup_hint": ".env의 DISCORD_WEBHOOK_URL을 채운 뒤 preflight test send를 실행",
            "command": command,
            "order_safety": "no_order",
        }
    return {
        **base_action,
        "desc": "Discord webhook 도달성 확인 preflight 실행",
        "setup_required": False,
        "command": command,
        "order_safety": "no_order",
    }


def _target_weight_core_entry_needs_preflight_recheck(reason: str | None) -> bool:
    text = str(reason or "").lower()
    return (
        "--send-test-notification" in text
        or "notifier" in text
        or "discord" in text
    )


def _target_weight_core_entry_as_of_date(checked_at: str | None) -> str | None:
    parsed = _parse_iso_datetime(str(checked_at or "").strip())
    if parsed is None:
        return None
    return parsed.date().isoformat()


def _stable_target_weight_core_entry_reason(reason: str) -> str:
    stale_marker = "Discord webhook test stale ("
    start = reason.find(stale_marker)
    if start < 0:
        return reason
    end = reason.find(")", start + len(stale_marker))
    if end < 0:
        return reason
    return (
        reason[:start]
        + "Discord webhook test stale"
        + reason[end + 1:]
    ).strip()


def _target_weight_core_entry_check_snapshot(
    strategy: str,
    *,
    checked_at: str | None = None,
) -> dict | None:
    """현재 active pilot의 entry gate를 current blockers에 노출한다."""
    as_of_date = _target_weight_core_entry_as_of_date(checked_at)
    try:
        from core.paper_pilot import check_pilot_entry, get_active_pilot

        if get_active_pilot(strategy, as_of_date) is None:
            return None
        check = check_pilot_entry(strategy, as_of_date=as_of_date)
    except Exception as exc:
        return {
            "label": "Core Entry Check",
            "strategy": strategy,
            "status": "UNKNOWN",
            "allowed": False,
            "reason": f"core entry check failed: {exc.__class__.__name__}: {exc}",
            "checked_at": checked_at,
            "check_command": f"python tools/paper_pilot_control.py --strategy {strategy} --status",
            "order_safety": "no_order",
        }

    reason = _stable_target_weight_core_entry_reason(str(check.reason or "").strip())
    snapshot = {
        "label": "Core Entry Check",
        "strategy": strategy,
        "status": "ALLOWED" if check.allowed else "BLOCKED",
        "allowed": bool(check.allowed),
        "reason": reason,
        "checked_at": checked_at,
        "check_command": f"python tools/paper_pilot_control.py --strategy {strategy} --status",
        "order_safety": "no_order",
    }
    if check.remaining_orders is not None:
        snapshot["remaining_orders"] = check.remaining_orders
    if check.remaining_exposure is not None:
        snapshot["remaining_exposure"] = check.remaining_exposure
    if check.caps_snapshot is not None:
        caps_snapshot = dict(check.caps_snapshot)
        caps_snapshot.pop("target_weight_plan_snapshot", None)
        snapshot["caps_snapshot"] = caps_snapshot
    if not check.allowed and _target_weight_core_entry_needs_preflight_recheck(reason):
        snapshot.update({
            "recovery_command": _target_weight_preflight_recheck_command(strategy),
            "requires": "recent verified notifier health",
            "recovery_source": "paper_preflight_test_notification",
        })
    return snapshot


def _target_weight_core_entry_action(core_entry_check: dict | None) -> dict | None:
    if not isinstance(core_entry_check, dict):
        return None
    recovery_command = str(core_entry_check.get("recovery_command") or "").strip()
    if not recovery_command:
        return None
    reason = str(core_entry_check.get("reason") or "").strip()
    return {
        "strategy": core_entry_check.get("strategy"),
        "source": "core_entry_check",
        "desc": "핵심 진입 점검 알림 도달성 사전점검 재실행",
        "command": recovery_command,
        "order_safety": "no_order",
        "requires": core_entry_check.get("requires") or "recent verified notifier health",
        "core_entry_status": core_entry_check.get("status"),
        "core_entry_reason": reason,
        "check_command": core_entry_check.get("check_command"),
    }


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_restore_review_bundle_manifest_state(
    bundle: dict,
) -> dict[str, object]:
    manifest_path = str(bundle.get("manifest_path") or "").strip()
    expected_hash = str(bundle.get("manifest_hash") or "").strip()
    current_hash = ""
    exists = False
    stale = False
    if manifest_path:
        path = Path(manifest_path)
        exists = path.exists()
        if exists:
            current_hash = _file_sha256(path)
        stale = bool(expected_hash) and current_hash != expected_hash
    return {
        "manifest_path": manifest_path,
        "manifest_exists": exists,
        "manifest_hash": expected_hash,
        "current_manifest_hash": current_hash,
        "manifest_stale": stale,
    }


def _normalize_restore_compare_value(column: str, value) -> str:
    raw = "" if value is None else str(value).strip()
    if column == "symbol":
        return normalize_symbol(raw)
    if column == "action":
        return raw.upper()
    if column == "quantity":
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            return "0"
    if column in TARGET_WEIGHT_RESTORE_NUMERIC_COLUMNS:
        if raw == "":
            return ""
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return raw
        if not math.isfinite(number):
            return raw
        return f"{number:.8f}".rstrip("0").rstrip(".")
    return raw


def _restore_row_counter(
    rows: list[dict[str, object]],
    columns: list[str],
) -> Counter[tuple[tuple[str, str], ...]]:
    return Counter(
        tuple(
            (column, _normalize_restore_compare_value(column, row.get(column)))
            for column in columns
        )
        for row in rows
    )


def _restore_counter_sample(
    counter: Counter[tuple[tuple[str, str], ...]],
    *,
    sample_columns: list[str],
    limit: int = 3,
) -> list[dict[str, str]]:
    sample: list[dict[str, str]] = []
    for key, count in counter.items():
        row = {column: value for column, value in key}
        sampled = {
            column: str(row.get(column) or "")
            for column in sample_columns
            if str(row.get(column) or "")
        }
        sampled["_count"] = str(count)
        sample.append(sampled)
        if len(sample) >= limit:
            break
    return sample


def _compare_review_csv_rows(
    *,
    candidate_rows: list[dict[str, object]],
    authoritative_rows: list[dict[str, object]],
    columns: list[str],
    sample_columns: list[str],
) -> dict[str, object]:
    candidate_counter = _restore_row_counter(candidate_rows, columns)
    authoritative_counter = _restore_row_counter(authoritative_rows, columns)
    missing = candidate_counter - authoritative_counter
    unexpected = authoritative_counter - candidate_counter
    return {
        "missing_row_count": sum(missing.values()),
        "unexpected_row_count": sum(unexpected.values()),
        "missing_row_sample": _restore_counter_sample(
            missing,
            sample_columns=sample_columns,
        ),
        "unexpected_row_sample": _restore_counter_sample(
            unexpected,
            sample_columns=sample_columns,
        ),
    }


def _db_restore_authoritative_csv_action_fields(
    prefix: str,
    evidence: dict,
) -> dict[str, object]:
    expected_rows = evidence.get("expected_rows")
    if expected_rows is None:
        expected_rows = evidence.get("candidate_rows")
    return {
        f"{prefix}_row_count": _safe_int(evidence.get("row_count")),
        f"{prefix}_expected_rows": _safe_int(expected_rows),
        f"{prefix}_empty_template": bool(evidence.get("empty_template")),
        f"{prefix}_missing_columns": _text_list(evidence.get("missing_columns")),
        f"{prefix}_candidate_source_rejected": bool(
            evidence.get("candidate_source_rejected")
        ),
        f"{prefix}_candidate_marker_rejected": bool(
            evidence.get("candidate_marker_rejected")
        ),
        f"{prefix}_candidate_marker_row_count": _safe_int(
            evidence.get("candidate_marker_row_count")
        ),
        f"{prefix}_identity_match": bool(evidence.get("identity_match")),
        f"{prefix}_economic_match": bool(evidence.get("economic_match")),
        f"{prefix}_economic_difference_count": _safe_int(
            evidence.get("economic_difference_count")
        ),
        f"{prefix}_content_mismatch_scope": str(
            evidence.get("content_mismatch_scope") or ""
        ),
        f"{prefix}_review_metadata_ok": bool(evidence.get("review_metadata_ok")),
        f"{prefix}_metadata_missing_columns": _text_list(
            evidence.get("metadata_missing_columns")
        ),
        f"{prefix}_metadata_incomplete_row_count": _safe_int(
            evidence.get("metadata_incomplete_row_count")
        ),
        f"{prefix}_metadata_candidate_source_row_count": _safe_int(
            evidence.get("metadata_candidate_source_row_count")
        ),
        f"{prefix}_metadata_placeholder_row_count": _safe_int(
            evidence.get("metadata_placeholder_row_count")
        ),
        f"{prefix}_metadata_invalid_reviewed_at_row_count": _safe_int(
            evidence.get("metadata_invalid_reviewed_at_row_count")
        ),
        f"{prefix}_metadata_future_reviewed_at_row_count": _safe_int(
            evidence.get("metadata_future_reviewed_at_row_count")
        ),
        f"{prefix}_metadata_reviewed_at_before_source_row_count": _safe_int(
            evidence.get("metadata_reviewed_at_before_source_row_count")
        ),
    }


def _db_restore_verification_metadata_ready(
    trade_evidence: dict,
    positions_evidence: dict,
) -> bool:
    return bool(
        trade_evidence.get("review_metadata_ok")
        and positions_evidence.get("review_metadata_ok")
    )


def _db_restore_verification_blockers_with_metadata(
    verification: dict,
    *,
    trade_evidence: dict,
    positions_evidence: dict,
) -> list[str]:
    blockers = [
        str(blocker)
        for blocker in (verification.get("blockers") or [])
        if str(blocker).strip()
    ]
    if not verification.get("restore_ready"):
        return blockers
    if trade_evidence.get("review_metadata_ok") is not True:
        blockers.append("authoritative_trade_history_csv_review_metadata_required")
    if positions_evidence.get("review_metadata_ok") is not True:
        blockers.append("authoritative_positions_csv_review_metadata_required")
    return list(dict.fromkeys(blockers))


def _restore_metadata_value_is_placeholder(value: object) -> bool:
    text = str(value or "").strip()
    normalized = text.lower()
    return (
        normalized in TARGET_WEIGHT_RESTORE_METADATA_PLACEHOLDER_VALUES
        or (normalized.startswith("<") and normalized.endswith(">"))
    )


def _parse_restore_reviewed_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text or _restore_metadata_value_is_placeholder(text):
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        reviewed_at = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if reviewed_at.tzinfo is None:
        return reviewed_at.replace(tzinfo=KST)
    return reviewed_at.astimezone(KST)


def _restore_review_min_timestamp(
    *,
    candidate_rows: list[dict[str, object]],
    snapshot_date: str = "",
    timestamp_column: str = "executed_at",
) -> datetime | None:
    timestamps: list[datetime] = []
    for row in candidate_rows:
        parsed = _parse_restore_reviewed_at(row.get(timestamp_column))
        if parsed is not None:
            timestamps.append(parsed)
    try:
        snapshot_day = datetime.strptime(
            str(snapshot_date or "").strip(),
            "%Y-%m-%d",
        ).date()
    except ValueError:
        snapshot_day = None
    if snapshot_day is not None:
        timestamps.append(
            datetime(
                snapshot_day.year,
                snapshot_day.month,
                snapshot_day.day,
                tzinfo=KST,
            )
        )
    return max(timestamps) if timestamps else None


def _db_restore_authoritative_metadata_status(
    *,
    rows: list[dict[str, object]],
    fieldnames: list[str],
    min_reviewed_at: datetime | None = None,
) -> dict[str, object]:
    missing_columns = [
        column
        for column in TARGET_WEIGHT_RESTORE_AUTHORITATIVE_METADATA_COLUMNS
        if column not in fieldnames
    ]
    incomplete_count = 0
    candidate_source_count = 0
    placeholder_count = 0
    invalid_reviewed_at_count = 0
    future_reviewed_at_count = 0
    reviewed_at_before_source_count = 0
    now = datetime.now(KST)
    for row in rows:
        if any(
            not str(row.get(column) or "").strip()
            for column in TARGET_WEIGHT_RESTORE_AUTHORITATIVE_METADATA_COLUMNS
        ):
            incomplete_count += 1
        if any(
            _restore_metadata_value_is_placeholder(row.get(column))
            for column in TARGET_WEIGHT_RESTORE_AUTHORITATIVE_METADATA_COLUMNS
        ):
            placeholder_count += 1
        source = str(row.get("authoritative_source") or "").strip().lower()
        if source.startswith("artifact_candidate") or source in {
            "artifact",
            "candidate",
            "candidate_csv",
            "candidate-only",
        }:
            candidate_source_count += 1
        reviewed_at = _parse_restore_reviewed_at(row.get("reviewed_at"))
        if str(row.get("reviewed_at") or "").strip() and reviewed_at is None:
            invalid_reviewed_at_count += 1
        elif (
            reviewed_at is not None
            and reviewed_at > now + TARGET_WEIGHT_RESTORE_REVIEWED_AT_FUTURE_TOLERANCE
        ):
            future_reviewed_at_count += 1
        if (
            reviewed_at is not None
            and min_reviewed_at is not None
            and reviewed_at < min_reviewed_at
        ):
            reviewed_at_before_source_count += 1
    return {
        "review_metadata_ok": (
            not missing_columns
            and incomplete_count == 0
            and candidate_source_count == 0
            and placeholder_count == 0
            and invalid_reviewed_at_count == 0
            and future_reviewed_at_count == 0
            and reviewed_at_before_source_count == 0
        ),
        "metadata_missing_columns": missing_columns,
        "metadata_incomplete_row_count": incomplete_count,
        "metadata_candidate_source_row_count": candidate_source_count,
        "metadata_placeholder_row_count": placeholder_count,
        "metadata_invalid_reviewed_at_row_count": invalid_reviewed_at_count,
        "metadata_future_reviewed_at_row_count": future_reviewed_at_count,
        "metadata_reviewed_at_before_source_row_count": (
            reviewed_at_before_source_count
        ),
    }


def _read_csv_progress(path_value: object, expected_columns: list[str]) -> dict[str, object]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return {
            "provided": False,
            "exists": False,
            "row_count": 0,
            "missing_columns": [],
            "fieldnames": [],
            "sha256": "",
            "rows": [],
        }
    path = Path(path_text)
    if not path.exists():
        return {
            "provided": True,
            "exists": False,
            "row_count": 0,
            "missing_columns": expected_columns.copy(),
            "fieldnames": [],
            "sha256": "",
            "rows": [],
        }
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader]
            fieldnames = [
                str(column or "").strip() for column in reader.fieldnames or []
            ]
        sha256 = _file_sha256(path)
    except OSError:
        return {
            "provided": True,
            "exists": False,
            "row_count": 0,
            "missing_columns": expected_columns.copy(),
            "fieldnames": [],
            "sha256": "",
            "rows": [],
        }
    missing_columns = [
        column for column in expected_columns if column not in fieldnames
    ]
    return {
        "provided": True,
        "exists": True,
        "row_count": len(rows),
        "missing_columns": missing_columns,
        "fieldnames": fieldnames,
        "sha256": sha256,
        "rows": rows,
    }


def _db_restore_review_template_action_fields(
    prefix: str,
    *,
    path_value: object,
    candidate_path_value: object = "",
    expected_rows: int,
    expected_columns: list[str],
    sample_columns: list[str] | None = None,
    verification_sha256: object = "",
    snapshot_date: str = "",
) -> dict[str, object]:
    progress = _read_csv_progress(path_value, expected_columns)
    candidate_progress = _read_csv_progress(candidate_path_value, expected_columns)
    row_count = _safe_int(progress.get("row_count"))
    current_sha256 = str(progress.get("sha256") or "").strip()
    verified_sha256 = str(verification_sha256 or "").strip()
    verification_stale = bool(
        current_sha256 and verified_sha256 and current_sha256 != verified_sha256
    )
    row_gap = {
        "missing_row_count": 0,
        "unexpected_row_count": 0,
        "missing_row_sample": [],
        "unexpected_row_sample": [],
    }
    if progress.get("exists") and not progress.get("missing_columns"):
        if candidate_progress.get("exists") and not candidate_progress.get(
            "missing_columns"
        ):
            row_gap = _compare_review_csv_rows(
                candidate_rows=list(candidate_progress.get("rows") or []),
                authoritative_rows=list(progress.get("rows") or []),
                columns=expected_columns,
                sample_columns=sample_columns or expected_columns,
            )
    metadata_status = {
        "review_metadata_ok": False,
        "metadata_missing_columns": [],
        "metadata_incomplete_row_count": 0,
        "metadata_candidate_source_row_count": 0,
        "metadata_placeholder_row_count": 0,
        "metadata_invalid_reviewed_at_row_count": 0,
        "metadata_future_reviewed_at_row_count": 0,
        "metadata_reviewed_at_before_source_row_count": 0,
    }
    if progress.get("exists"):
        metadata_status = _db_restore_authoritative_metadata_status(
            rows=list(progress.get("rows") or []),
            fieldnames=list(progress.get("fieldnames") or []),
            min_reviewed_at=_restore_review_min_timestamp(
                candidate_rows=list(candidate_progress.get("rows") or []),
                snapshot_date=snapshot_date,
                timestamp_column="executed_at",
            ),
        )
    return {
        f"{prefix}_provided": bool(progress.get("provided")),
        f"{prefix}_row_count": row_count,
        f"{prefix}_expected_rows": expected_rows,
        f"{prefix}_empty_template": bool(progress.get("exists"))
        and expected_rows > 0
        and row_count == 0,
        f"{prefix}_missing_columns": _text_list(progress.get("missing_columns")),
        f"{prefix}_current_sha256": current_sha256,
        f"{prefix}_verified_sha256": verified_sha256,
        f"{prefix}_verification_stale": verification_stale,
        f"{prefix}_missing_row_count": _safe_int(row_gap.get("missing_row_count")),
        f"{prefix}_unexpected_row_count": _safe_int(
            row_gap.get("unexpected_row_count")
        ),
        f"{prefix}_missing_row_sample": row_gap.get("missing_row_sample") or [],
        f"{prefix}_unexpected_row_sample": row_gap.get("unexpected_row_sample") or [],
        f"{prefix}_review_metadata_ok": bool(
            metadata_status.get("review_metadata_ok")
        ),
        f"{prefix}_metadata_missing_columns": _text_list(
            metadata_status.get("metadata_missing_columns")
        ),
        f"{prefix}_metadata_incomplete_row_count": _safe_int(
            metadata_status.get("metadata_incomplete_row_count")
        ),
        f"{prefix}_metadata_candidate_source_row_count": _safe_int(
            metadata_status.get("metadata_candidate_source_row_count")
        ),
        f"{prefix}_metadata_placeholder_row_count": _safe_int(
            metadata_status.get("metadata_placeholder_row_count")
        ),
        f"{prefix}_metadata_invalid_reviewed_at_row_count": _safe_int(
            metadata_status.get("metadata_invalid_reviewed_at_row_count")
        ),
        f"{prefix}_metadata_future_reviewed_at_row_count": _safe_int(
            metadata_status.get("metadata_future_reviewed_at_row_count")
        ),
        f"{prefix}_metadata_reviewed_at_before_source_row_count": _safe_int(
            metadata_status.get("metadata_reviewed_at_before_source_row_count")
        ),
    }


def _first_text(items) -> str | None:
    if not isinstance(items, list):
        return None
    for item in items:
        text = str(item).strip()
        if text:
            return text
    return None


def _target_weight_invalid_reason_keys(invalid_reasons) -> set[str]:
    if not isinstance(invalid_reasons, dict):
        return set()
    return {str(reason).strip() for reason in invalid_reasons if str(reason).strip()}


def _target_weight_non_repairable_invalid_reasons(invalid_reasons) -> bool:
    repairable = (
        TARGET_WEIGHT_FINALIZE_FIRST_INVALID_REASONS
        | TARGET_WEIGHT_DB_PERSISTENCE_INVALID_REASONS
        | TARGET_WEIGHT_NON_REPAIRABLE_EXEMPT_INVALID_REASONS
    )
    return any(
        reason not in repairable
        for reason in _target_weight_invalid_reason_keys(invalid_reasons)
    )


def _not_before_blocked_command(not_before_date: str | None) -> str:
    if not_before_date:
        return f"# blocked: not before {not_before_date}; target_weight_future_as_of_date_blocked"
    return "# blocked: next KRX business day fresh readiness required"


def _not_before_date_pending(not_before_date: str | None, *, current_date: str | None = None) -> bool:
    if not not_before_date:
        return False
    try:
        target = datetime.strptime(str(not_before_date), "%Y-%m-%d").date()
        current = datetime.strptime(current_date or _current_kst_date(), "%Y-%m-%d").date()
    except ValueError:
        return True
    return target > current


def _target_weight_ops_priority_action(
    strategy: str,
    commands: dict[str, str],
    latest_daily_ops: dict | None,
    latest_finalize_report: dict | None = None,
    latest_snapshot_diagnostics: dict | None = None,
    latest_db_restore_verification: dict | None = None,
    latest_db_restore_review_bundle: dict | None = None,
    latest_db_restore_apply_plan: dict | None = None,
    latest_db_restore_backup: dict | None = None,
) -> dict | None:
    if not latest_daily_ops:
        return None

    status = str(latest_daily_ops.get("status") or "")
    progress = latest_daily_ops.get("evidence_progress") or {}
    decision = latest_daily_ops.get("decision") or {}
    blockers = [str(reason) for reason in decision.get("blocking_reasons") or []]
    ops_commands = latest_daily_ops.get("operator_commands") or {}
    shadow_days = _safe_int(progress.get("shadow_days"))
    verified_days = _safe_int(progress.get("verified_pilot_days"))

    base_action = {
        "strategy": strategy,
        "source": "latest_daily_ops_summary",
        "source_path": latest_daily_ops.get("source_path"),
        "daily_ops_status": status,
        "daily_ops_trade_day": latest_daily_ops.get("trade_day"),
        "next_operator_trade_day": latest_daily_ops.get("next_operator_trade_day"),
        "verified_pilot_days": verified_days,
        "shadow_days": shadow_days,
    }
    for field in (
        "target_days",
        "remaining_pilot_days",
        "invalid_execution_days",
        "repaired_pilot_days",
        "non_promotable_days",
    ):
        if field in progress:
            base_action[field] = _safe_int(progress.get(field))
    if "progress_ratio" in progress:
        try:
            base_action["progress_ratio"] = float(progress.get("progress_ratio"))
        except (TypeError, ValueError):
            base_action["progress_ratio"] = 0.0
    if progress.get("invalid_reasons"):
        base_action["invalid_reasons"] = progress.get("invalid_reasons")

    if status in {"PILOT_EVIDENCE_RECORDED", "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"}:
        next_trade_day = latest_daily_ops.get("next_operator_trade_day")
        if status == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE":
            desc = (
                "오늘 target-weight pilot_paper 실행 증거는 복구 보존됐지만 "
                "promotion 카운트에서는 제외, 다음 KRX 영업일 fresh readiness 점검"
            )
        else:
            desc = "오늘 target-weight pilot_paper 증거 기록 완료, 다음 KRX 영업일 fresh readiness와 cap 재승인 점검"
        scheduled_command = (
            target_weight_next_check_command_or_default(
                latest_daily_ops,
                ops_commands.get("next_daily_ops_summary"),
                "--daily-ops-summary",
            )
            or ops_commands.get("daily_ops_summary")
            or commands.get("daily_ops_summary")
        )
        scheduled_follow_up = (
            target_weight_next_check_command_or_default(
                latest_daily_ops,
                ops_commands.get("next_readiness_audit"),
                "--readiness-audit",
            )
            or ops_commands.get("rerun_readiness_audit")
            or commands.get("readiness_audit")
        )
        if _not_before_date_pending(next_trade_day):
            blocked_command = _not_before_blocked_command(next_trade_day)
            return {
                **base_action,
                "desc": desc,
                "command": blocked_command,
                "scheduled_command": scheduled_command,
                "order_safety": "no_order",
                "requires": "next KRX business day fresh readiness",
                "not_before_date": next_trade_day,
                "premature_run_guard": "target_weight_future_as_of_date_blocked",
                "follow_up": blocked_command,
                "scheduled_follow_up": scheduled_follow_up,
            }
        return {
            **base_action,
            "desc": desc,
            "command": scheduled_command,
            "scheduled_command": scheduled_command,
            "order_safety": "no_order",
            "requires": "current KRX business day fresh readiness",
            "follow_up": scheduled_follow_up,
            "scheduled_follow_up": scheduled_follow_up,
        }

    if status == "PILOT_EVIDENCE_INVALID":
        invalid_reasons = progress.get("invalid_reasons") or {}
        db_persistence_blocked = any(
            reason in TARGET_WEIGHT_DB_PERSISTENCE_INVALID_REASONS
            for reason in _target_weight_invalid_reason_keys(invalid_reasons)
        ) or _target_weight_finalize_blocks_on_db_persistence(latest_finalize_report)
        if db_persistence_blocked:
            diagnose_command = (
                ops_commands.get("diagnose_portfolio_snapshot")
                or _target_weight_portfolio_snapshot_diagnostics_command(
                    strategy,
                    latest_daily_ops.get("trade_day"),
                )
            )
            follow_up = (
                ops_commands.get("daily_ops_summary") or commands.get("daily_ops_summary")
            )
            action = {
                **base_action,
                "invalid_execution_days": _safe_int(
                    progress.get("invalid_execution_days")
                ),
                "invalid_reasons": invalid_reasons,
                "desc": (
                    "오늘 target-weight pilot_paper DB 저장 증거 불완전, "
                    "portfolio snapshot/DB 진단 후 새 실행 증거 확보"
                ),
                "command": diagnose_command,
                "order_safety": "no_order",
                "requires": "database trade/position persistence proof",
                "db_persistence_guard": "target_weight_db_persistence_proof_required",
                "blocked_finalize_command": (
                    "# blocked: DB persistence proof incomplete; run diagnostics first"
                ),
                "blocked_repair_command": (
                    "# blocked: DB persistence proof cannot be repaired from artifact"
                ),
                "follow_up": follow_up,
                "finalize_report_source": (
                    latest_finalize_report.get("source_path")
                    if isinstance(latest_finalize_report, dict)
                    else None
                ),
                "finalize_report_generated_at": (
                    latest_finalize_report.get("generated_at")
                    if isinstance(latest_finalize_report, dict)
                    else None
                ),
                "finalize_report_status": (
                    latest_finalize_report.get("status")
                    if isinstance(latest_finalize_report, dict)
                    else None
                ),
                "finalize_report_reason": (
                    latest_finalize_report.get("reason")
                    if isinstance(latest_finalize_report, dict)
                    else None
                ),
            }
            if isinstance(latest_snapshot_diagnostics, dict):
                snapshot_db_restore = (
                    latest_snapshot_diagnostics.get("db_restore_checklist") or {}
                )
                if not isinstance(snapshot_db_restore, dict):
                    snapshot_db_restore = {}
                snapshot_db_restore_trade_history = (
                    snapshot_db_restore.get("trade_history") or {}
                )
                if not isinstance(snapshot_db_restore_trade_history, dict):
                    snapshot_db_restore_trade_history = {}
                snapshot_db_restore_positions = (
                    snapshot_db_restore.get("positions") or {}
                )
                if not isinstance(snapshot_db_restore_positions, dict):
                    snapshot_db_restore_positions = {}
                snapshot_db_restore_package = (
                    latest_snapshot_diagnostics.get("db_restore_candidate_package")
                    or {}
                )
                if not isinstance(snapshot_db_restore_package, dict):
                    snapshot_db_restore_package = {}
                snapshot_operator_commands = (
                    latest_snapshot_diagnostics.get("operator_commands") or {}
                )
                if not isinstance(snapshot_operator_commands, dict):
                    snapshot_operator_commands = {}
                action.update({
                    "snapshot_diagnostics_source": latest_snapshot_diagnostics.get(
                        "source_path"
                    ),
                    "snapshot_diagnostics_generated_at": latest_snapshot_diagnostics.get(
                        "generated_at"
                    ),
                    "snapshot_diagnostics_status": latest_snapshot_diagnostics.get(
                        "status"
                    ),
                    "snapshot_db_restore_status": str(
                        snapshot_db_restore.get("status") or ""
                    ),
                    "snapshot_db_restore_required": bool(
                        snapshot_db_restore.get("restore_required")
                    ),
                    "snapshot_db_restore_trade_rows_expected": _safe_int(
                        snapshot_db_restore_trade_history.get("expected_row_count")
                    ),
                    "snapshot_db_restore_trade_rows_current": _safe_int(
                        snapshot_db_restore_trade_history.get(
                            "current_db_rows_on_date"
                        )
                    ),
                    "snapshot_db_restore_trade_rows_missing_or_unverified": _safe_int(
                        snapshot_db_restore_trade_history.get(
                            "missing_or_unverified_row_count"
                        )
                    ),
                    "snapshot_db_restore_position_symbols_expected": _safe_int(
                        snapshot_db_restore_positions.get("expected_symbol_count")
                    ),
                    "snapshot_db_restore_positions_current": _safe_int(
                        snapshot_db_restore_positions.get("current_db_position_count")
                    ),
                    "snapshot_db_restore_missing_or_unverified_symbols": (
                        snapshot_db_restore_positions.get(
                            "missing_or_unverified_symbols"
                        )
                        or []
                    ),
                    "snapshot_db_restore_candidate_package_generated": bool(
                        snapshot_db_restore_package.get("generated")
                    ),
                    "snapshot_db_restore_candidate_manifest": str(
                        snapshot_db_restore_package.get("manifest_path") or ""
                    ),
                    "snapshot_db_restore_trade_history_candidate_csv": str(
                        snapshot_db_restore_package.get("trade_history_candidate_csv")
                        or ""
                    ),
                    "snapshot_db_restore_positions_candidate_csv": str(
                        snapshot_db_restore_package.get("positions_candidate_csv")
                        or ""
                    ),
                    "snapshot_db_restore_trade_history_candidate_rows": _safe_int(
                        snapshot_db_restore_package.get("trade_history_candidate_rows")
                    ),
                    "snapshot_db_restore_position_candidate_rows": _safe_int(
                        snapshot_db_restore_package.get("position_candidate_rows")
                    ),
                    "snapshot_db_restore_position_candidate_skipped_zero_quantity_symbols": (
                        snapshot_db_restore_package.get(
                            "position_candidate_skipped_zero_quantity_symbols"
                        )
                        or []
                    ),
                    "snapshot_db_restore_candidate_requires_authoritative_confirmation": bool(
                        snapshot_db_restore_package.get(
                            "requires_authoritative_confirmation"
                        )
                    ),
                    "snapshot_db_restore_package_verify_command": str(
                        snapshot_operator_commands.get("verify_db_restore_package")
                        or ""
                    ),
                })
            verification_trade = {}
            verification_positions = {}
            if isinstance(latest_db_restore_verification, dict):
                verification_candidate = (
                    latest_db_restore_verification.get("candidate_package") or {}
                )
                if not isinstance(verification_candidate, dict):
                    verification_candidate = {}
                verification_authoritative = (
                    latest_db_restore_verification.get("authoritative_evidence") or {}
                )
                if not isinstance(verification_authoritative, dict):
                    verification_authoritative = {}
                verification_trade = (
                    verification_authoritative.get("trade_history") or {}
                )
                if not isinstance(verification_trade, dict):
                    verification_trade = {}
                verification_positions = (
                    verification_authoritative.get("positions") or {}
                )
                if not isinstance(verification_positions, dict):
                    verification_positions = {}
                verification_current_db = (
                    latest_db_restore_verification.get("current_db_state") or {}
                )
                if not isinstance(verification_current_db, dict):
                    verification_current_db = {}
                verification_commands = (
                    latest_db_restore_verification.get("operator_commands") or {}
                )
                if not isinstance(verification_commands, dict):
                    verification_commands = {}
                verification_source = str(
                    latest_db_restore_verification.get("source_path")
                    or latest_db_restore_verification.get("artifact_path")
                    or ""
                )
                apply_plan_command = str(
                    verification_commands.get("plan_manual_db_apply") or ""
                ).strip()
                if not apply_plan_command and verification_source:
                    apply_plan_command = (
                        "python tools/target_weight_rotation_pilot.py "
                        "--plan-db-restore-apply "
                        f"--restore-verification {verification_source}"
                    )
                verification_raw_ready = bool(
                    latest_db_restore_verification.get("restore_ready")
                )
                verification_metadata_ready = _db_restore_verification_metadata_ready(
                    verification_trade,
                    verification_positions,
                )
                verification_blockers = _db_restore_verification_blockers_with_metadata(
                    latest_db_restore_verification,
                    trade_evidence=verification_trade,
                    positions_evidence=verification_positions,
                )
                action.update({
                    "snapshot_db_restore_verification_source": verification_source,
                    "snapshot_db_restore_verification_generated_at": str(
                        latest_db_restore_verification.get("generated_at") or ""
                    ),
                    "snapshot_db_restore_verification_status": str(
                        latest_db_restore_verification.get("status") or ""
                    ),
                    "snapshot_db_restore_verification_raw_ready": verification_raw_ready,
                    "snapshot_db_restore_verification_metadata_ready": (
                        verification_metadata_ready
                    ),
                    "snapshot_db_restore_verification_ready": bool(
                        verification_raw_ready and verification_metadata_ready
                    ),
                    "snapshot_db_restore_verification_blockers": verification_blockers,
                    "snapshot_db_restore_verification_warnings": (
                        latest_db_restore_verification.get("warnings") or []
                    ),
                    "snapshot_db_restore_verification_trade_hash_ok": bool(
                        (verification_candidate.get("trade_history") or {}).get(
                            "hash_ok"
                        )
                    ),
                    "snapshot_db_restore_verification_positions_hash_ok": bool(
                        (verification_candidate.get("positions") or {}).get("hash_ok")
                    ),
                    "snapshot_db_restore_authoritative_trade_history_provided": bool(
                        verification_trade.get("provided")
                    ),
                    "snapshot_db_restore_authoritative_trade_history_match": bool(
                        verification_trade.get("match")
                    ),
                    "snapshot_db_restore_authoritative_positions_provided": bool(
                        verification_positions.get("provided")
                    ),
                    "snapshot_db_restore_authoritative_positions_match": bool(
                        verification_positions.get("match")
                    ),
                    **_db_restore_authoritative_csv_action_fields(
                        "snapshot_db_restore_authoritative_trade_history",
                        verification_trade,
                    ),
                    **_db_restore_authoritative_csv_action_fields(
                        "snapshot_db_restore_authoritative_positions",
                        verification_positions,
                    ),
                    "snapshot_db_restore_verification_db_trade_rows_on_date": _safe_int(
                        verification_current_db.get("trade_count_on_date")
                    ),
                    "snapshot_db_restore_verification_db_positions": _safe_int(
                        verification_current_db.get("position_count")
                    ),
                    "snapshot_db_restore_apply_plan_command": apply_plan_command,
                })
            verify_command = str(
                action.get("snapshot_db_restore_package_verify_command") or ""
            ).strip()
            package_generated = bool(
                action.get("snapshot_db_restore_candidate_package_generated")
            )
            restore_manifest = str(
                action.get("snapshot_db_restore_candidate_manifest") or ""
            ).strip()
            review_bundle_command = (
                "python tools/target_weight_rotation_pilot.py "
                "--prepare-db-restore-review-bundle "
                f"--restore-manifest {restore_manifest}"
                if package_generated and restore_manifest
                else ""
            )
            if review_bundle_command:
                action["snapshot_db_restore_review_bundle_command"] = (
                    review_bundle_command
                )
            review_bundle_ready = False
            review_bundle_verify_command = ""
            review_bundle_manifest_stale = False
            if isinstance(latest_db_restore_review_bundle, dict):
                review_files = latest_db_restore_review_bundle.get("review_files") or {}
                if not isinstance(review_files, dict):
                    review_files = {}
                review_commands = (
                    latest_db_restore_review_bundle.get("operator_commands") or {}
                )
                if not isinstance(review_commands, dict):
                    review_commands = {}
                review_candidate = (
                    latest_db_restore_review_bundle.get("candidate_package") or {}
                )
                if not isinstance(review_candidate, dict):
                    review_candidate = {}
                review_trade = review_candidate.get("trade_history") or {}
                if not isinstance(review_trade, dict):
                    review_trade = {}
                review_positions = review_candidate.get("positions") or {}
                if not isinstance(review_positions, dict):
                    review_positions = {}
                review_bundle_ready = bool(
                    latest_db_restore_review_bundle.get("review_bundle_ready")
                )
                review_manifest_state = _db_restore_review_bundle_manifest_state(
                    latest_db_restore_review_bundle
                )
                review_bundle_manifest_stale = bool(
                    review_manifest_state.get("manifest_stale")
                )
                review_bundle_verify_command = str(
                    review_commands.get("verify_after_manual_review") or ""
                ).strip()
                action.update({
                    "snapshot_db_restore_review_bundle_source": str(
                        latest_db_restore_review_bundle.get("source_path") or ""
                    ),
                    "snapshot_db_restore_review_bundle_generated_at": str(
                        latest_db_restore_review_bundle.get("generated_at") or ""
                    ),
                    "snapshot_db_restore_review_bundle_status": str(
                        latest_db_restore_review_bundle.get("status") or ""
                    ),
                    "snapshot_db_restore_review_bundle_ready": review_bundle_ready,
                    "snapshot_db_restore_review_bundle_blockers": (
                        latest_db_restore_review_bundle.get("blockers") or []
                    ),
                    "snapshot_db_restore_review_bundle_warnings": (
                        latest_db_restore_review_bundle.get("warnings") or []
                    ),
                    "snapshot_db_restore_review_bundle_dir": str(
                        latest_db_restore_review_bundle.get("bundle_dir") or ""
                    ),
                    "snapshot_db_restore_review_bundle_candidate_trade_history_csv": str(
                        review_files.get("candidate_trade_history_csv") or ""
                    ),
                    "snapshot_db_restore_review_bundle_candidate_positions_csv": str(
                        review_files.get("candidate_positions_csv") or ""
                    ),
                    "snapshot_db_restore_review_bundle_manifest_path": str(
                        review_manifest_state.get("manifest_path") or ""
                    ),
                    "snapshot_db_restore_review_bundle_manifest_exists": bool(
                        review_manifest_state.get("manifest_exists")
                    ),
                    "snapshot_db_restore_review_bundle_manifest_hash": str(
                        review_manifest_state.get("manifest_hash") or ""
                    ),
                    "snapshot_db_restore_review_bundle_current_manifest_hash": str(
                        review_manifest_state.get("current_manifest_hash") or ""
                    ),
                    "snapshot_db_restore_review_bundle_manifest_stale": (
                        review_bundle_manifest_stale
                    ),
                    "snapshot_db_restore_authoritative_trade_history_template_csv": str(
                        review_files.get(
                            "authoritative_trade_history_template_csv"
                        )
                        or ""
                    ),
                    "snapshot_db_restore_authoritative_positions_template_csv": str(
                        review_files.get("authoritative_positions_template_csv")
                        or ""
                    ),
                    "snapshot_db_restore_verify_after_manual_review_command": (
                        review_bundle_verify_command
                    ),
                })
                action.update(
                    _db_restore_review_template_action_fields(
                        "snapshot_db_restore_authoritative_trade_history",
                        path_value=review_files.get(
                            "authoritative_trade_history_template_csv"
                        ),
                        candidate_path_value=review_files.get(
                            "candidate_trade_history_csv"
                        ),
                        expected_rows=_safe_int(
                            review_trade.get("row_count")
                            or review_trade.get("expected_rows")
                            or action.get(
                                "snapshot_db_restore_trade_history_candidate_rows"
                            )
                        ),
                        expected_columns=TARGET_WEIGHT_RESTORE_TRADE_COMPARE_COLUMNS,
                        sample_columns=TARGET_WEIGHT_RESTORE_TRADE_SAMPLE_COLUMNS,
                        verification_sha256=verification_trade.get("sha256")
                        if isinstance(verification_trade, dict)
                        else "",
                        snapshot_date=str(
                            latest_db_restore_review_bundle.get("snapshot_date")
                            or (latest_snapshot_diagnostics or {}).get(
                                "snapshot_date"
                            )
                            or ""
                        ),
                    )
                )
                action.update(
                    _db_restore_review_template_action_fields(
                        "snapshot_db_restore_authoritative_positions",
                        path_value=review_files.get(
                            "authoritative_positions_template_csv"
                        ),
                        candidate_path_value=review_files.get(
                            "candidate_positions_csv"
                        ),
                        expected_rows=_safe_int(
                            review_positions.get("row_count")
                            or review_positions.get("expected_rows")
                            or action.get(
                                "snapshot_db_restore_position_candidate_rows"
                            )
                        ),
                        expected_columns=TARGET_WEIGHT_RESTORE_POSITION_COMPARE_COLUMNS,
                        sample_columns=TARGET_WEIGHT_RESTORE_POSITION_SAMPLE_COLUMNS,
                        verification_sha256=verification_positions.get("sha256")
                        if isinstance(verification_positions, dict)
                        else "",
                        snapshot_date=str(
                            latest_db_restore_review_bundle.get("snapshot_date")
                            or (latest_snapshot_diagnostics or {}).get(
                                "snapshot_date"
                            )
                            or ""
                        ),
                    )
                )
                action["snapshot_db_restore_verification_stale_after_review_edit"] = bool(
                    action.get(
                        "snapshot_db_restore_authoritative_trade_history_verification_stale"
                    )
                    or action.get(
                        "snapshot_db_restore_authoritative_positions_verification_stale"
                    )
                )
            verification_ready = bool(
                action.get("snapshot_db_restore_verification_ready")
            )
            verification_stale_after_review_edit = bool(
                action.get("snapshot_db_restore_verification_stale_after_review_edit")
            )
            if (
                package_generated
                and verify_command
                and verification_ready
                and verification_stale_after_review_edit
            ):
                action.update({
                    "desc": (
                        "target-weight DB 복구 reviewed CSV 수정 감지, "
                        "authoritative 검증 재실행"
                    ),
                    "command": (
                        "# blocked: reviewed authoritative DB restore "
                        "verification stale after CSV edit"
                    ),
                    "scheduled_command": (
                        review_bundle_verify_command or verify_command
                    ),
                    "scheduled_follow_up": follow_up,
                    "requires": "rerun DB restore verification after reviewed CSV edit",
                    "db_restore_review_guard": (
                        "target_weight_authoritative_db_restore_verification_stale_after_review_edit"
                    ),
                    "blocked_finalize_command": (
                        "# blocked: reviewed authoritative DB restore "
                        "verification stale after CSV edit"
                    ),
                    "blocked_repair_command": (
                        "# blocked: reviewed authoritative DB restore "
                        "verification stale after CSV edit"
                    ),
                })
                return action
            if package_generated and review_bundle_ready and review_bundle_manifest_stale:
                action.update({
                    "desc": (
                        "target-weight DB 복구 review bundle 기준 manifest 변경 감지, "
                        "review bundle 재생성"
                    ),
                    "command": (
                        review_bundle_command
                        or "# blocked: DB restore review bundle manifest stale"
                    ),
                    "scheduled_command": review_bundle_command,
                    "scheduled_follow_up": follow_up,
                    "requires": "fresh DB restore review bundle",
                    "db_restore_review_guard": (
                        "target_weight_authoritative_db_restore_review_bundle_stale"
                    ),
                    "blocked_finalize_command": (
                        "# blocked: fresh DB restore review bundle required before finalize"
                    ),
                    "blocked_repair_command": (
                        "# blocked: fresh DB restore review bundle required before repair"
                    ),
                })
                return action
            if package_generated and verify_command and not verification_ready:
                if review_bundle_ready:
                    action.update({
                        "desc": (
                            "target-weight DB 복구 authoritative CSV 템플릿 작성 후 "
                            "verify 실행"
                        ),
                        "command": (
                            "# blocked: fill reviewed authoritative "
                            "trade_history/positions CSV templates before DB "
                            "restore verification"
                        ),
                        "scheduled_command": (
                            review_bundle_verify_command or verify_command
                        ),
                        "scheduled_follow_up": follow_up,
                        "requires": (
                            "filled reviewed authoritative trade_history/positions CSV"
                        ),
                        "db_restore_review_guard": (
                            "target_weight_authoritative_db_restore_csv_fill_required"
                        ),
                        "blocked_finalize_command": (
                            "# blocked: reviewed authoritative DB restore "
                            "verification required before finalize"
                        ),
                        "blocked_repair_command": (
                            "# blocked: reviewed authoritative DB restore "
                            "verification required before repair"
                        ),
                    })
                    return action
                action.update({
                    "desc": (
                        "target-weight DB 복구 패키지 authoritative CSV 검토 후 "
                        "verify 실행"
                    ),
                    "command": (
                        review_bundle_command
                        or "# blocked: reviewed authoritative trade_history/positions "
                        "CSV required before DB restore verification"
                    ),
                    "scheduled_command": verify_command,
                    "scheduled_follow_up": follow_up,
                    "requires": (
                        "manual authoritative review bundle and reviewed CSV"
                    ),
                    "db_restore_review_guard": (
                        "target_weight_authoritative_db_restore_csv_required"
                    ),
                    "blocked_finalize_command": (
                        "# blocked: reviewed authoritative DB restore verification "
                        "required before finalize"
                    ),
                    "blocked_repair_command": (
                        "# blocked: reviewed authoritative DB restore verification "
                        "required before repair"
                    ),
                })
            elif package_generated and verification_ready:
                apply_plan_command = str(
                    action.get("snapshot_db_restore_apply_plan_command") or ""
                ).strip()
                apply_plan_ready = False
                apply_plan_apply_command = ""
                apply_plan_backup_command = ""
                if isinstance(latest_db_restore_apply_plan, dict):
                    apply_plan_commands = (
                        latest_db_restore_apply_plan.get("operator_commands") or {}
                    )
                    if not isinstance(apply_plan_commands, dict):
                        apply_plan_commands = {}
                    apply_plan_ready = bool(
                        latest_db_restore_apply_plan.get("apply_ready")
                    )
                    apply_plan_apply_command = str(
                        apply_plan_commands.get("apply_manual_db_restore") or ""
                    ).strip()
                    apply_plan_backup_command = str(
                        apply_plan_commands.get("backup_db_restore_state") or ""
                    ).strip()
                    action.update({
                        "snapshot_db_restore_apply_plan_source": str(
                            latest_db_restore_apply_plan.get("source_path") or ""
                        ),
                        "snapshot_db_restore_apply_plan_generated_at": str(
                            latest_db_restore_apply_plan.get("generated_at") or ""
                        ),
                        "snapshot_db_restore_apply_plan_status": str(
                            latest_db_restore_apply_plan.get("status") or ""
                        ),
                        "snapshot_db_restore_apply_plan_ready": apply_plan_ready,
                        "snapshot_db_restore_apply_plan_blockers": (
                            latest_db_restore_apply_plan.get("blockers") or []
                        ),
                        "snapshot_db_restore_apply_command": (
                            apply_plan_apply_command
                        ),
                        "snapshot_db_restore_backup_command": (
                            apply_plan_backup_command
                        ),
                    })
                if apply_plan_ready:
                    backup_ready = False
                    backup_apply_command = ""
                    if isinstance(latest_db_restore_backup, dict):
                        backup_commands = (
                            latest_db_restore_backup.get("operator_commands") or {}
                        )
                        if not isinstance(backup_commands, dict):
                            backup_commands = {}
                        backup_ready = bool(
                            latest_db_restore_backup.get("backup_ready")
                        )
                        backup_apply_command = str(
                            backup_commands.get("apply_guarded_db_restore") or ""
                        ).strip()
                        action.update({
                            "snapshot_db_restore_backup_source": str(
                                latest_db_restore_backup.get("source_path") or ""
                            ),
                            "snapshot_db_restore_backup_generated_at": str(
                                latest_db_restore_backup.get("generated_at") or ""
                            ),
                            "snapshot_db_restore_backup_status": str(
                                latest_db_restore_backup.get("status") or ""
                            ),
                            "snapshot_db_restore_backup_ready": backup_ready,
                            "snapshot_db_restore_backup_blockers": (
                                latest_db_restore_backup.get("blockers") or []
                            ),
                            "snapshot_db_restore_backup_apply_command": (
                                backup_apply_command
                            ),
                        })
                    if not backup_ready:
                        action.update({
                            "desc": (
                                "target-weight DB 복구 적용 계획 검증 완료, "
                                "guarded apply 전 DB 상태 백업 생성"
                            ),
                            "command": (
                                apply_plan_backup_command
                                or "# blocked: pre-apply DB restore backup command missing"
                            ),
                            "scheduled_command": apply_plan_backup_command,
                            "scheduled_follow_up": follow_up,
                            "requires": "pre-apply DB restore backup artifact",
                            "db_restore_review_guard": (
                                "target_weight_authoritative_db_restore_backup_required"
                            ),
                            "blocked_finalize_command": (
                                "# blocked: DB restore pre-apply backup required before finalize"
                            ),
                            "blocked_repair_command": (
                                "# blocked: DB restore pre-apply backup required before repair"
                            ),
                        })
                        return action
                    action.update({
                        "desc": (
                            "target-weight DB 복구 적용 계획 및 백업 확인 완료, "
                            "guarded DB 복구 반영"
                        ),
                        "command": (
                            backup_apply_command
                            or "# blocked: guarded DB restore apply command missing"
                        ),
                        "scheduled_command": backup_apply_command,
                        "scheduled_follow_up": follow_up,
                        "requires": "confirmed DB backup and guarded DB restore apply",
                        "db_restore_review_guard": (
                            "target_weight_authoritative_db_restore_apply_ready_manual_confirm_required"
                        ),
                        "blocked_finalize_command": (
                            "# blocked: guarded DB restore apply required before finalize"
                        ),
                        "blocked_repair_command": (
                            "# blocked: guarded DB restore apply required before repair"
                        ),
                    })
                    return action
                action.update({
                    "desc": (
                        "target-weight DB 복구 패키지 검증 완료, no-write 적용 "
                        "계획 생성 후 authoritative DB 복구 반영"
                    ),
                    "command": (
                        apply_plan_command
                        or "# manual step required: prepare no-write DB restore "
                        "apply plan before manual DB restore"
                    ),
                    "scheduled_command": apply_plan_command,
                    "scheduled_follow_up": follow_up,
                    "requires": "no-write DB restore apply plan",
                    "db_restore_review_guard": (
                        "target_weight_authoritative_db_restore_apply_plan_required"
                    ),
                    "blocked_finalize_command": (
                        "# blocked: no-write DB restore apply plan required "
                        "before finalize"
                    ),
                    "blocked_repair_command": (
                        "# blocked: no-write DB restore apply plan required "
                        "before repair"
                    ),
                })
            return action
        if _target_weight_non_repairable_invalid_reasons(invalid_reasons):
            next_trade_day = latest_daily_ops.get("next_operator_trade_day")
            scheduled_command = (
                target_weight_next_check_command_or_default(
                    latest_daily_ops,
                    ops_commands.get("next_daily_ops_summary"),
                    "--daily-ops-summary",
                )
                or ops_commands.get("daily_ops_summary")
                or commands.get("daily_ops_summary")
            )
            scheduled_follow_up = (
                target_weight_next_check_command_or_default(
                    latest_daily_ops,
                    ops_commands.get("next_readiness_audit"),
                    "--readiness-audit",
                )
                or ops_commands.get("rerun_readiness_audit")
                or commands.get("readiness_audit")
            )
            command = scheduled_command
            if _not_before_date_pending(next_trade_day):
                command = _not_before_blocked_command(next_trade_day)
            return {
                **base_action,
                "invalid_execution_days": _safe_int(
                    progress.get("invalid_execution_days")
                ),
                "invalid_reasons": invalid_reasons,
                "desc": (
                    "오늘 target-weight pilot_paper 실행 증거가 승격 불가능, "
                    "다음 KRX 영업일 fresh READY_TO_EXECUTE 증거 재수집"
                ),
                "command": command,
                "scheduled_command": scheduled_command,
                "order_safety": "no_order",
                "requires": "fresh READY_TO_EXECUTE pilot evidence",
                "non_repairable_guard": (
                    "target_weight_pilot_evidence_fresh_execution_required"
                ),
                "not_before_date": next_trade_day,
                "premature_run_guard": (
                    "target_weight_future_as_of_date_blocked"
                    if next_trade_day
                    else None
                ),
                "follow_up": scheduled_follow_up,
                "scheduled_follow_up": scheduled_follow_up,
                "blocked_finalize_command": (
                    "# blocked: pilot_paper execution proof is not repairable; "
                    "collect fresh READY_TO_EXECUTE evidence"
                ),
                "blocked_repair_command": (
                    "# blocked: pilot_paper execution proof is not repairable; "
                    "collect fresh READY_TO_EXECUTE evidence"
                ),
            }
        finalize_first = any(
            reason in TARGET_WEIGHT_FINALIZE_FIRST_INVALID_REASONS
            for reason in _target_weight_invalid_reason_keys(invalid_reasons)
        )
        command = (
            ops_commands.get("finalize_pilot_evidence")
            if finalize_first
            else ops_commands.get("repair_pilot_evidence")
        )
        if not command:
            command = (
                ops_commands.get("repair_pilot_evidence")
                or ops_commands.get("daily_ops_summary")
                or commands.get("daily_ops_summary")
            )
        action = {
            **base_action,
            "invalid_execution_days": _safe_int(progress.get("invalid_execution_days")),
            "invalid_reasons": invalid_reasons,
            "desc": (
                "오늘 target-weight pilot_paper 증거 품질 미확정, final benchmark/portfolio evidence 확정 후 daily ops 재점검"
                if finalize_first
                else "오늘 target-weight pilot_paper 증거 품질 실패, benchmark/portfolio evidence 복구 후 daily ops 재점검"
            ),
            "command": command,
            "order_safety": "no_order",
            "requires": (
                "benchmark/portfolio evidence finalization"
                if finalize_first
                else "benchmark/portfolio evidence repair"
            ),
            "follow_up": ops_commands.get("daily_ops_summary") or commands.get("daily_ops_summary"),
        }
        if finalize_first and _target_weight_finalize_waits_for_performance(latest_finalize_report):
            reason = str(latest_finalize_report.get("reason") or "").strip()
            blocked = f"# blocked: final performance evidence unavailable; {reason}"
            diagnostics_present = (
                _target_weight_finalize_report_has_performance_diagnostics(
                    latest_finalize_report
                )
            )
            performance_status = (
                latest_finalize_report.get("performance_evidence_status")
                if isinstance(latest_finalize_report, dict)
                else {}
            )
            if not isinstance(performance_status, dict):
                performance_status = {}
            probe_status = str(
                performance_status.get("portfolio_metrics_probe_status") or ""
            ).strip()
            probe_recovery_hint = _target_weight_portfolio_metrics_recovery_hint(
                probe_status
            )
            snapshot_recovery = {}
            snapshot_database = {}
            snapshot_artifact_execution = {}
            snapshot_db_restore = {}
            snapshot_db_restore_trade_history = {}
            snapshot_db_restore_positions = {}
            snapshot_db_restore_package = {}
            if isinstance(latest_snapshot_diagnostics, dict):
                snapshot_recovery = (
                    latest_snapshot_diagnostics.get("snapshot_recovery_readiness")
                    or {}
                )
                if not isinstance(snapshot_recovery, dict):
                    snapshot_recovery = {}
                snapshot_database = latest_snapshot_diagnostics.get("database_state") or {}
                if not isinstance(snapshot_database, dict):
                    snapshot_database = {}
                snapshot_artifact_execution = (
                    latest_snapshot_diagnostics.get("artifact_execution_state")
                    or {}
                )
                if not isinstance(snapshot_artifact_execution, dict):
                    snapshot_artifact_execution = {}
                snapshot_db_restore = (
                    latest_snapshot_diagnostics.get("db_restore_checklist") or {}
                )
                if not isinstance(snapshot_db_restore, dict):
                    snapshot_db_restore = {}
                snapshot_db_restore_trade_history = (
                    snapshot_db_restore.get("trade_history") or {}
                )
                if not isinstance(snapshot_db_restore_trade_history, dict):
                    snapshot_db_restore_trade_history = {}
                snapshot_db_restore_positions = snapshot_db_restore.get("positions") or {}
                if not isinstance(snapshot_db_restore_positions, dict):
                    snapshot_db_restore_positions = {}
                snapshot_db_restore_package = (
                    latest_snapshot_diagnostics.get("db_restore_candidate_package")
                    or {}
                )
                if not isinstance(snapshot_db_restore_package, dict):
                    snapshot_db_restore_package = {}
                snapshot_operator_commands = (
                    latest_snapshot_diagnostics.get("operator_commands") or {}
                )
                if not isinstance(snapshot_operator_commands, dict):
                    snapshot_operator_commands = {}
                snapshot_recovery_blockers = snapshot_recovery.get("blockers") or []
                snapshot_recovery_guard = (
                    latest_snapshot_diagnostics.get("recovery_guard")
                    or _target_weight_snapshot_recovery_guard_from_blockers(
                        snapshot_recovery_blockers
                    )
                )
                probe_recovery_hint = (
                    _target_weight_snapshot_recovery_hint_from_blockers(
                        snapshot_recovery_blockers,
                        latest_snapshot_diagnostics.get("recovery_hint")
                        or probe_recovery_hint,
                    )
                    or probe_recovery_hint
                )
            snapshot_diagnostics_command = _target_weight_portfolio_snapshot_diagnostics_command(
                strategy,
                latest_finalize_report.get("finalize_date")
                or latest_daily_ops.get("trade_day"),
            )
            action.update({
                "command": blocked,
                "scheduled_command": command,
                "requires": "final portfolio performance evidence available",
                "performance_evidence_guard": (
                    "target_weight_pilot_evidence_finalize_missing_performance"
                ),
                "finalize_report_source": latest_finalize_report.get("source_path"),
                "finalize_report_generated_at": latest_finalize_report.get("generated_at"),
                "finalize_report_status": latest_finalize_report.get("status"),
                "finalize_source_record_fields_present": (
                    performance_status.get("source_record_fields_present") or []
                ),
                "finalize_source_record_fields_usable": (
                    performance_status.get("source_record_fields_usable") or []
                ),
                "finalize_source_record_fields_unusable": (
                    performance_status.get("source_record_fields_unusable") or []
                ),
                "finalize_portfolio_metrics_checked": bool(
                    performance_status.get("portfolio_metrics_checked")
                ),
                "finalize_portfolio_metrics_probe_status": (
                    probe_status
                ),
                "finalize_portfolio_metrics_probe_reason": (
                    performance_status.get("portfolio_metrics_probe_reason") or ""
                ),
                "finalize_portfolio_metrics_current_snapshot_found": bool(
                    performance_status.get("portfolio_metrics_current_snapshot_found")
                ),
                "finalize_portfolio_metrics_previous_snapshot_found": bool(
                    performance_status.get("portfolio_metrics_previous_snapshot_found")
                ),
                "finalize_portfolio_metrics_previous_snapshot_at": (
                    performance_status.get("portfolio_metrics_previous_snapshot_at")
                ),
                "finalize_portfolio_metrics_trades_today": (
                    performance_status.get("portfolio_metrics_trades_today") or 0
                ),
                "finalize_portfolio_metrics_trades_since_previous": (
                    performance_status.get("portfolio_metrics_trades_since_previous") or 0
                ),
                "finalize_portfolio_metrics_fields_present": (
                    performance_status.get("portfolio_metrics_fields_present") or []
                ),
                "finalize_missing_performance_fields": (
                    performance_status.get("missing_fields_after_probe") or []
                ),
                "finalize_portfolio_metrics_recovery_hint": probe_recovery_hint,
                "finalize_portfolio_snapshot_diagnostics_command": (
                    snapshot_diagnostics_command
                ),
                "finalize_report_diagnostics_status": (
                    "present" if diagnostics_present else "missing"
                ),
            })
            if isinstance(latest_snapshot_diagnostics, dict):
                action.update({
                    "snapshot_diagnostics_source": latest_snapshot_diagnostics.get("source_path"),
                    "snapshot_diagnostics_generated_at": latest_snapshot_diagnostics.get("generated_at"),
                    "snapshot_diagnostics_status": latest_snapshot_diagnostics.get("status"),
                    "snapshot_diagnostics_reason": latest_snapshot_diagnostics.get("reason"),
                    "snapshot_recovery_guard": snapshot_recovery_guard,
                    "snapshot_recovery_hint": probe_recovery_hint,
                    "snapshot_recovery_next_action": latest_snapshot_diagnostics.get("next_action") or "",
                    "snapshot_recovery_status": snapshot_recovery.get("status") or "",
                    "snapshot_recovery_safe_to_write": bool(
                        snapshot_recovery.get("safe_to_write_snapshot")
                    ),
                    "snapshot_recovery_reason": snapshot_recovery.get("reason") or "",
                    "snapshot_recovery_blockers": snapshot_recovery.get("blockers") or [],
                    "snapshot_recovery_warnings": snapshot_recovery.get("warnings") or [],
                    "snapshot_db_snapshot_count": _safe_int(
                        snapshot_database.get("snapshot_count")
                    ),
                    "snapshot_db_current_snapshot_found": bool(
                        snapshot_database.get("current_snapshot_found")
                    ),
                    "snapshot_db_trade_count_total": _safe_int(
                        snapshot_database.get("trade_count_total")
                    ),
                    "snapshot_db_trade_count_on_date": _safe_int(
                        snapshot_database.get("trade_count_on_date")
                    ),
                    "snapshot_db_position_count": _safe_int(
                        snapshot_database.get("position_count")
                    ),
                    "snapshot_artifact_fill_count": _safe_int(
                        snapshot_artifact_execution.get("fill_count")
                    ),
                    "snapshot_artifact_execution_session_id": str(
                        snapshot_artifact_execution.get("execution_session_id") or ""
                    ),
                    "snapshot_artifact_db_persistence_complete": bool(
                        snapshot_artifact_execution.get("db_persistence_complete")
                    ),
                    "snapshot_artifact_db_trade_history_source": str(
                        snapshot_artifact_execution.get("db_trade_history_source") or ""
                    ),
                    "snapshot_artifact_db_positions_source": str(
                        snapshot_artifact_execution.get("db_positions_source") or ""
                    ),
                    "snapshot_db_restore_status": str(
                        snapshot_db_restore.get("status") or ""
                    ),
                    "snapshot_db_restore_required": bool(
                        snapshot_db_restore.get("restore_required")
                    ),
                    "snapshot_db_restore_trade_rows_expected": _safe_int(
                        snapshot_db_restore_trade_history.get("expected_row_count")
                    ),
                    "snapshot_db_restore_trade_rows_current": _safe_int(
                        snapshot_db_restore_trade_history.get("current_db_rows_on_date")
                    ),
                    "snapshot_db_restore_trade_rows_missing_or_unverified": _safe_int(
                        snapshot_db_restore_trade_history.get(
                            "missing_or_unverified_row_count"
                        )
                    ),
                    "snapshot_db_restore_position_symbols_expected": _safe_int(
                        snapshot_db_restore_positions.get("expected_symbol_count")
                    ),
                    "snapshot_db_restore_positions_current": _safe_int(
                        snapshot_db_restore_positions.get("current_db_position_count")
                    ),
                    "snapshot_db_restore_missing_or_unverified_symbols": (
                        snapshot_db_restore_positions.get(
                            "missing_or_unverified_symbols"
                        )
                        or []
                    ),
                    "snapshot_db_restore_candidate_package_generated": bool(
                        snapshot_db_restore_package.get("generated")
                    ),
                    "snapshot_db_restore_candidate_manifest": str(
                        snapshot_db_restore_package.get("manifest_path") or ""
                    ),
                    "snapshot_db_restore_trade_history_candidate_csv": str(
                        snapshot_db_restore_package.get("trade_history_candidate_csv")
                        or ""
                    ),
                    "snapshot_db_restore_positions_candidate_csv": str(
                        snapshot_db_restore_package.get("positions_candidate_csv") or ""
                    ),
                    "snapshot_db_restore_trade_history_candidate_rows": _safe_int(
                        snapshot_db_restore_package.get("trade_history_candidate_rows")
                    ),
                    "snapshot_db_restore_position_candidate_rows": _safe_int(
                        snapshot_db_restore_package.get("position_candidate_rows")
                    ),
                    "snapshot_db_restore_position_candidate_skipped_zero_quantity_symbols": (
                        snapshot_db_restore_package.get(
                            "position_candidate_skipped_zero_quantity_symbols"
                        )
                        or []
                    ),
                    "snapshot_db_restore_candidate_requires_authoritative_confirmation": bool(
                        snapshot_db_restore_package.get(
                            "requires_authoritative_confirmation"
                        )
                    ),
                    "snapshot_db_restore_package_verify_command": str(
                        snapshot_operator_commands.get("verify_db_restore_package")
                        or ""
                    ),
                })
                if isinstance(latest_db_restore_verification, dict):
                    verification_candidate = (
                        latest_db_restore_verification.get("candidate_package") or {}
                    )
                    verification_authoritative = (
                        latest_db_restore_verification.get("authoritative_evidence")
                        or {}
                    )
                    verification_trade = (
                        verification_authoritative.get("trade_history") or {}
                    )
                    verification_positions = (
                        verification_authoritative.get("positions") or {}
                    )
                    verification_db = (
                        latest_db_restore_verification.get("current_db_state") or {}
                    )
                    verification_raw_ready = bool(
                        latest_db_restore_verification.get("restore_ready")
                    )
                    verification_metadata_ready = (
                        _db_restore_verification_metadata_ready(
                            verification_trade,
                            verification_positions,
                        )
                    )
                    verification_blockers = (
                        _db_restore_verification_blockers_with_metadata(
                            latest_db_restore_verification,
                            trade_evidence=verification_trade,
                            positions_evidence=verification_positions,
                        )
                    )
                    action.update({
                        "snapshot_db_restore_verification_source": str(
                            latest_db_restore_verification.get("source_path") or ""
                        ),
                        "snapshot_db_restore_verification_generated_at": str(
                            latest_db_restore_verification.get("generated_at") or ""
                        ),
                        "snapshot_db_restore_verification_status": str(
                            latest_db_restore_verification.get("status") or ""
                        ),
                        "snapshot_db_restore_verification_raw_ready": (
                            verification_raw_ready
                        ),
                        "snapshot_db_restore_verification_metadata_ready": (
                            verification_metadata_ready
                        ),
                        "snapshot_db_restore_verification_ready": bool(
                            verification_raw_ready and verification_metadata_ready
                        ),
                        "snapshot_db_restore_verification_blockers": verification_blockers,
                        "snapshot_db_restore_verification_warnings": (
                            latest_db_restore_verification.get("warnings") or []
                        ),
                        "snapshot_db_restore_verification_trade_hash_ok": bool(
                            (verification_candidate.get("trade_history") or {}).get(
                                "hash_ok"
                            )
                        ),
                        "snapshot_db_restore_verification_positions_hash_ok": bool(
                            (verification_candidate.get("positions") or {}).get(
                                "hash_ok"
                            )
                        ),
                        "snapshot_db_restore_authoritative_trade_history_provided": bool(
                            verification_trade.get("provided")
                        ),
                        "snapshot_db_restore_authoritative_trade_history_match": bool(
                            verification_trade.get("match")
                        ),
                        "snapshot_db_restore_authoritative_positions_provided": bool(
                            verification_positions.get("provided")
                        ),
                        "snapshot_db_restore_authoritative_positions_match": bool(
                            verification_positions.get("match")
                        ),
                        **_db_restore_authoritative_csv_action_fields(
                            "snapshot_db_restore_authoritative_trade_history",
                            verification_trade,
                        ),
                        **_db_restore_authoritative_csv_action_fields(
                            "snapshot_db_restore_authoritative_positions",
                            verification_positions,
                        ),
                        "snapshot_db_restore_verification_db_trade_rows_on_date": _safe_int(
                            verification_db.get("trade_count_on_date")
                        ),
                        "snapshot_db_restore_verification_db_positions": _safe_int(
                            verification_db.get("position_count")
                        ),
                    })
                if snapshot_recovery_guard:
                    blocked_finalize_command = (
                        "# blocked: portfolio snapshot recovery requires "
                        "authoritative DB/snapshot evidence before finalize"
                    )
                    blocked_repair_command = (
                        "# blocked: portfolio snapshot recovery requires "
                        "authoritative DB/snapshot evidence before repair"
                    )
                    action["desc"] = (
                        "target-weight snapshot 복구 전 DB trade_history/positions "
                        "증거 복구 후 진단 재점검"
                    )
                    action["requires"] = "authoritative DB snapshot/trade/position evidence"
                    action["command"] = _target_weight_snapshot_recovery_blocked_command(
                        snapshot_recovery_guard
                    )
                    action["scheduled_command"] = snapshot_diagnostics_command
                    action["scheduled_follow_up"] = blocked_finalize_command
                    action["snapshot_recovery_order"] = [
                        "restore_authoritative_db_trade_history_positions_proof",
                        "rerun_portfolio_snapshot_diagnostics",
                        "rerun_finalize_pilot_evidence",
                    ]
                    action["blocked_finalize_command"] = blocked_finalize_command
                    action["blocked_repair_command"] = blocked_repair_command
            if not diagnostics_present:
                action.update({
                    "command": command,
                    "requires": "finalize performance diagnostics refresh",
                    "finalize_diagnostics_refresh_command": command,
                })
        return action

    if status == "READY_TO_EXECUTE":
        command = (
            ops_commands.get("execute_capped_paper")
            or commands.get("execute_capped_paper_after_ready")
        )
        return {
            **base_action,
            "desc": "READY_TO_EXECUTE 당일 capped paper 실행 및 pilot_paper 증거 수집",
            "command": command,
            "order_safety": _paper_execute_order_safety(command),
            "requires": "daily_ops_summary.status == READY_TO_EXECUTE",
        }

    if status == "READY_TO_ENABLE_CAPS":
        return {
            **base_action,
            "desc": "readiness artifact의 추천 cap 승인 후 readiness 재점검",
            "command": ops_commands.get("enable_suggested_caps") or "",
            "order_safety": "no_order",
            "follow_up": ops_commands.get("rerun_readiness_audit") or commands.get("readiness_audit"),
        }

    if shadow_days < 3 or _reason_contains(
        blockers,
        "clean_final_days",
        "no eligible",
        "evidence_freshness",
        "no evidence",
    ):
        return {
            **base_action,
            "desc": "target-weight shadow 3일 수집으로 launch readiness 증거 먼저 확보",
            "command": ops_commands.get("collect_shadow_days") or commands.get("collect_shadow_days"),
            "order_safety": "no_order",
        }

    if _reason_contains(blockers, "discord", "webhook", "notifier"):
        return _target_weight_discord_action(strategy, base_action, blockers)

    if _reason_contains(blockers, "pilot_authorization"):
        return {
            **base_action,
            "desc": "readiness artifact의 추천 cap 승인 후 readiness 재점검",
            "command": ops_commands.get("enable_suggested_caps") or "",
            "order_safety": "no_order",
            "follow_up": ops_commands.get("rerun_readiness_audit") or commands.get("readiness_audit"),
        }

    if status == "WAITING_FOR_MARKET_SESSION":
        return {
            **base_action,
            "desc": "KRX 정규장 시간에 readiness audit 재실행",
            "command": ops_commands.get("rerun_readiness_audit") or commands.get("readiness_audit"),
            "order_safety": "no_order",
        }

    return {
        **base_action,
        "desc": "daily ops blocker 해소 후 readiness 재점검",
        "command": ops_commands.get("rerun_readiness_audit") or commands.get("readiness_audit"),
        "order_safety": "no_order",
    }


def _target_weight_execute_after_ready_command(
    commands: dict[str, str],
    latest_daily_ops: dict | None,
) -> str:
    if latest_daily_ops and latest_daily_ops.get("status") == "READY_TO_EXECUTE":
        ops_commands = latest_daily_ops.get("operator_commands") or {}
        return (
            ops_commands.get("execute_capped_paper")
            or commands.get("execute_capped_paper_after_ready")
            or ""
        )
    if latest_daily_ops:
        ops_commands = latest_daily_ops.get("operator_commands") or {}
        execute_command = str(ops_commands.get("execute_capped_paper") or "").strip()
        if execute_command.startswith("# blocked:"):
            return execute_command
        status = latest_daily_ops.get("status") or "UNKNOWN"
        reason = _first_text((latest_daily_ops.get("decision") or {}).get("blocking_reasons"))
        return f"# blocked: daily_ops_summary.status == {status}; {reason or 'READY_TO_EXECUTE 전 실행 금지'}"
    return "# blocked: daily ops summary 생성 후 READY_TO_EXECUTE 상태의 execute_capped_paper 명령만 사용"


def _research_operator_commands() -> dict[str, str]:
    return {
        "check_promotion_artifacts": "python tools/evaluate_and_promote.py --check-only",
        "run_canonical_research": "python tools/evaluate_and_promote.py --canonical",
        "regenerate_current_blockers": "python tools/evaluate_and_promote.py --current-blockers",
    }


def _promotion_artifact_freshness_snapshot(
    blocker_summary: dict,
    *,
    checked_at: str,
    max_artifact_age_days: int | None = None,
) -> dict:
    from core.live_gate import LIVE_GATE_MAX_ARTIFACT_AGE_DAYS

    max_days = (
        LIVE_GATE_MAX_ARTIFACT_AGE_DAYS
        if max_artifact_age_days is None
        else max_artifact_age_days
    )
    canonical_generated_at = str(blocker_summary.get("generated_at") or "").strip()
    base = {
        "canonical_generated_at": canonical_generated_at or None,
        "checked_at": checked_at,
        "age_days": None,
        "max_age_days": max_days,
        "status": "UNKNOWN",
        "warning": "canonical_generated_at missing",
        "check_command": "python tools/evaluate_and_promote.py --check-only",
        "refresh_command": "python tools/evaluate_and_promote.py --canonical",
    }
    source_time = _parse_iso_datetime(canonical_generated_at)
    checked_time = _parse_iso_datetime(checked_at)
    if source_time is None or checked_time is None:
        return base

    age_days = _datetime_age_days(source_time, checked_time)
    snapshot = {**base, "age_days": round(age_days, 2)}
    if age_days < 0:
        return {
            **snapshot,
            "status": "FUTURE",
            "warning": "canonical artifact timestamp is in the future",
        }
    if age_days > max_days:
        return {
            **snapshot,
            "status": "STALE",
            "warning": (
                "canonical artifact is older than the live gate freshness limit; "
                "rerun canonical evaluation before operator decisions"
            ),
        }
    aging_threshold_days = max(0.0, max_days - 2)
    if age_days >= aging_threshold_days:
        return {
            **snapshot,
            "status": "AGING",
            "warning": (
                "canonical artifact is close to the freshness limit; "
                "plan a canonical refresh before live review"
            ),
        }
    return {
        **snapshot,
        "status": "FRESH",
        "warning": "",
    }


def _promotion_freshness_blocks_live(freshness: dict) -> bool:
    return freshness.get("status") in {"UNKNOWN", "FUTURE", "STALE"}


def _build_current_blockers_operator_runbook(
    *,
    provisional_candidates: list[str],
    live_candidates: list[str],
    latest_daily_ops: dict | None = None,
    latest_daily_ops_failure: dict | None = None,
    latest_finalize_report: dict | None = None,
    latest_snapshot_diagnostics: dict | None = None,
    latest_db_restore_verification: dict | None = None,
    latest_db_restore_review_bundle: dict | None = None,
    latest_db_restore_apply_plan: dict | None = None,
    latest_db_restore_backup: dict | None = None,
    promotion_artifact_freshness: dict | None = None,
) -> dict:
    primary_strategy = provisional_candidates[0] if provisional_candidates else None
    if primary_strategy:
        commands = _target_weight_operator_commands(primary_strategy)
        checked_at = None
        if isinstance(promotion_artifact_freshness, dict):
            checked_at = promotion_artifact_freshness.get("checked_at")
        core_entry_check = _target_weight_core_entry_check_snapshot(
            primary_strategy,
            checked_at=str(checked_at or "") or None,
        )
        core_entry_action = _target_weight_core_entry_action(core_entry_check)
        active_failure = (
            latest_daily_ops_failure
            if _daily_ops_failure_is_active(latest_daily_ops_failure, latest_daily_ops)
            else None
        )
        if active_failure:
            commands["daily_ops_summary"] = _target_weight_daily_ops_failure_command(
                active_failure,
                commands,
            )
            commands["execute_capped_paper_after_ready"] = (
                "# blocked: latest daily ops summary failure unresolved; "
                "rerun daily ops summary before capped paper execution"
            )
            ops_priority_action = _target_weight_daily_ops_failure_action(
                primary_strategy,
                commands,
                active_failure,
            )
        else:
            commands["execute_capped_paper_after_ready"] = (
                _target_weight_execute_after_ready_command(
                    commands,
                    latest_daily_ops,
                )
            )
            ops_priority_action = _target_weight_ops_priority_action(
                primary_strategy,
                commands,
                latest_daily_ops,
                latest_finalize_report=latest_finalize_report,
                latest_snapshot_diagnostics=latest_snapshot_diagnostics,
                latest_db_restore_verification=latest_db_restore_verification,
                latest_db_restore_review_bundle=latest_db_restore_review_bundle,
                latest_db_restore_apply_plan=latest_db_restore_apply_plan,
                latest_db_restore_backup=latest_db_restore_backup,
            )
        sequence = [
            {
                "step": 1,
                "desc": "promotion/current blockers 동기화 점검",
                "command": commands["check_promotion_artifacts"],
                "order_safety": "no_order",
            },
            {
                "step": 2,
                "desc": "daily ops summary로 readiness와 pilot evidence 진행률 확인",
                "command": commands["daily_ops_summary"],
                "order_safety": "no_order",
            },
            {
                "step": 3,
                "desc": "readiness audit 실행 후 artifact의 추천 cap만 승인",
                "command": commands["readiness_audit"],
                "order_safety": "no_order",
            },
            {
                "step": 4,
                "desc": "READY_TO_EXECUTE가 나온 정규장 당일에만 capped paper 실행 및 pilot_paper 증거 수집",
                "command": commands["execute_capped_paper_after_ready"],
                "order_safety": _paper_execute_order_safety(commands["execute_capped_paper_after_ready"]),
                "requires": "daily_ops_summary.status == READY_TO_EXECUTE",
            },
        ]
        if ops_priority_action:
            priority_step = {
                "step": 3,
                "desc": ops_priority_action["desc"],
                "command": ops_priority_action.get("command"),
                "order_safety": ops_priority_action["order_safety"],
                **(
                    {"requires": ops_priority_action["requires"]}
                    if ops_priority_action.get("requires")
                    else {}
                ),
                **(
                    {"follow_up": ops_priority_action["follow_up"]}
                    if ops_priority_action.get("follow_up")
                    else {}
                ),
            }
            for key in ("setup_required", "required_env", "config_path", "setup_hint"):
                if key in ops_priority_action:
                    priority_step[key] = ops_priority_action[key]
            for key in ("not_before_date", "premature_run_guard"):
                if ops_priority_action.get(key):
                    priority_step[key] = ops_priority_action[key]
            for key in ("scheduled_command", "scheduled_follow_up"):
                if ops_priority_action.get(key):
                    priority_step[key] = ops_priority_action[key]
            sequence.insert(2, priority_step)
            follow_up = str(ops_priority_action.get("follow_up") or "")
            scheduled_follow_up = str(ops_priority_action.get("scheduled_follow_up") or "")
            if "--readiness-audit" in follow_up or "--readiness-audit" in scheduled_follow_up:
                for item in sequence:
                    if item.get("command") == commands["readiness_audit"]:
                        item["command"] = follow_up
                        if scheduled_follow_up:
                            item["scheduled_command"] = scheduled_follow_up
                        break
            for index, item in enumerate(sequence, start=1):
                item["step"] = index
        if core_entry_action:
            duplicate_priority = (
                ops_priority_action
                and str(ops_priority_action.get("command") or "").strip()
                == str(core_entry_action.get("command") or "").strip()
            )
            if not duplicate_priority:
                sequence.insert(
                    3 if ops_priority_action else 2,
                    {
                        "step": 0,
                        "desc": core_entry_action["desc"],
                        "command": core_entry_action["command"],
                        "order_safety": core_entry_action["order_safety"],
                        "requires": core_entry_action["requires"],
                        "check_command": core_entry_action.get("check_command"),
                    },
                )
                for index, item in enumerate(sequence, start=1):
                    item["step"] = index
        runbook = {
            "primary_strategy": primary_strategy,
            "mode": "target_weight_capped_paper_pilot",
            "commands": commands,
            "sequence": sequence,
            "safety_notes": [
                "current_blockers.go_live=true가 되기 전까지 live 모드는 유지 차단",
                "daily ops summary와 readiness audit은 주문과 pilot evidence를 쓰지 않는 no-order 점검",
                "execute_capped_paper_after_ready는 READY_TO_EXECUTE 상태와 KRX 정규장 조건을 만족한 날에만 사용",
            ],
        }
        if promotion_artifact_freshness:
            runbook["promotion_artifact_freshness"] = promotion_artifact_freshness
        if latest_daily_ops:
            runbook["latest_daily_ops"] = latest_daily_ops
        if active_failure:
            runbook["latest_daily_ops_failure"] = _public_target_weight_daily_ops_failure(
                active_failure
            )
        if core_entry_check:
            runbook["core_entry_check"] = core_entry_check
        if core_entry_action:
            runbook["core_entry_action"] = core_entry_action
        if latest_db_restore_verification:
            runbook["latest_db_restore_verification"] = latest_db_restore_verification
        if latest_db_restore_review_bundle:
            runbook["latest_db_restore_review_bundle"] = latest_db_restore_review_bundle
        if latest_db_restore_apply_plan:
            runbook["latest_db_restore_apply_plan"] = latest_db_restore_apply_plan
        if latest_db_restore_backup:
            runbook["latest_db_restore_backup"] = latest_db_restore_backup
        if ops_priority_action:
            runbook["current_priority_action"] = ops_priority_action
        return runbook

    commands = _research_operator_commands()
    runbook = {
        "primary_strategy": live_candidates[0] if live_candidates else None,
        "mode": "research_recovery" if not live_candidates else "live_gate_review",
        "commands": commands,
        "sequence": [
            {
                "step": 1,
                "desc": "promotion/current blockers 동기화 점검",
                "command": commands["check_promotion_artifacts"],
                "order_safety": "no_order",
            },
            {
                "step": 2,
                "desc": "canonical research/promotion artifact 재생성",
                "command": commands["run_canonical_research"],
                "order_safety": "no_order",
            },
            {
                "step": 3,
                "desc": "current blockers 재생성 후 live 후보 여부 재확인",
                "command": commands["regenerate_current_blockers"],
                "order_safety": "no_order",
            },
        ],
        "safety_notes": [
            "provisional_paper_candidate가 없으면 research 재설계가 우선",
            "live 후보가 생겨도 live gate와 current_blockers.go_live=true 전까지 실전 전환 금지",
        ],
    }
    if promotion_artifact_freshness:
        runbook["promotion_artifact_freshness"] = promotion_artifact_freshness
    return runbook


def build_current_blockers_report(
    blocker_summary: dict,
    *,
    latest_daily_ops: dict | None = None,
    latest_daily_ops_failure: dict | None = None,
    latest_finalize_report: dict | None = None,
    latest_snapshot_diagnostics: dict | None = None,
    latest_db_restore_verification: dict | None = None,
    latest_db_restore_review_bundle: dict | None = None,
    latest_db_restore_apply_plan: dict | None = None,
    latest_db_restore_backup: dict | None = None,
    generated_at: str | None = None,
) -> dict:
    """promotion blocker summary에서 현재 go-live blocker 운영 파일을 생성한다."""
    if not isinstance(blocker_summary, dict):
        blocker_summary = {}
    report_generated_at = generated_at or datetime.now().isoformat()
    promotion_artifact_freshness = _promotion_artifact_freshness_snapshot(
        blocker_summary,
        checked_at=report_generated_at,
    )
    latest_daily_ops = _sanitize_target_weight_daily_ops_summary(latest_daily_ops)
    summary = blocker_summary.get("summary") or {}
    live_candidates = _strategy_names_by_status(blocker_summary, "live_candidate")
    provisional_candidates = _ranked_provisional_candidates(blocker_summary)
    paper_only = _strategy_names_by_status(blocker_summary, "paper_only")
    research_only = _strategy_names_by_status(blocker_summary, "research_only")
    freshness_blocks_live = _promotion_freshness_blocks_live(promotion_artifact_freshness)
    go_live = bool(live_candidates) and not freshness_blocks_live

    hard_blockers = []
    if freshness_blocks_live:
        hard_blockers.append({
            "desc": "canonical promotion artifact 최신성 미충족",
            "evidence": promotion_artifact_freshness.get("warning")
                or f"freshness_status={promotion_artifact_freshness.get('status')}",
        })
    if not live_candidates:
        hard_blockers.append({
            "desc": "live_candidate 상태의 전략이 없음",
            "evidence": f"live_ready_count={summary.get('live_ready_count', 0)}",
        })
    if provisional_candidates:
        hard_blockers.append({
            "desc": "provisional 후보의 60영업일 execution-backed paper/pilot 증거 미충족",
            "strategies": provisional_candidates,
        })
    if not provisional_candidates and not live_candidates:
        hard_blockers.append({
            "desc": "capped paper pilot로 진행할 provisional_paper_candidate 없음",
            "evidence": f"status_counts={summary.get('status_counts', {})}",
        })

    soft_blockers = []
    if paper_only:
        soft_blockers.append({
            "desc": "paper_only 후보가 provisional gate 일부를 통과하지 못함",
            "count": len(paper_only),
            "sample": paper_only[:8],
        })
    if research_only:
        soft_blockers.append({
            "desc": "research_only 후보는 재설계 또는 제외 검토 필요",
            "count": len(research_only),
            "sample": research_only[:8],
        })

    next_actions = []
    operator_runbook = _build_current_blockers_operator_runbook(
        provisional_candidates=provisional_candidates,
        live_candidates=live_candidates,
        latest_daily_ops=latest_daily_ops,
        latest_daily_ops_failure=latest_daily_ops_failure,
        latest_finalize_report=latest_finalize_report,
        latest_snapshot_diagnostics=latest_snapshot_diagnostics,
        latest_db_restore_verification=latest_db_restore_verification,
        latest_db_restore_review_bundle=latest_db_restore_review_bundle,
        latest_db_restore_apply_plan=latest_db_restore_apply_plan,
        latest_db_restore_backup=latest_db_restore_backup,
        promotion_artifact_freshness=promotion_artifact_freshness,
    )
    runbook_commands = operator_runbook.get("commands") or {}
    ops_priority_action = operator_runbook.get("current_priority_action")
    core_entry_action = operator_runbook.get("core_entry_action")
    if provisional_candidates:
        next_priority = 1
        if ops_priority_action:
            next_actions.append({
                "priority": next_priority,
                **ops_priority_action,
            })
            next_priority += 1
            if (
                core_entry_action
                and str(core_entry_action.get("command") or "").strip()
                != str(ops_priority_action.get("command") or "").strip()
            ):
                next_actions.append({
                    "priority": next_priority,
                    **core_entry_action,
                })
                next_priority += 1
        else:
            next_actions.append({
                "priority": next_priority,
                "desc": "target-weight daily ops summary로 readiness와 pilot evidence 진행률 먼저 확인",
                "strategy": provisional_candidates[0],
                "command": runbook_commands.get("daily_ops_summary"),
                "order_safety": "no_order",
                "follow_up": runbook_commands.get("readiness_audit"),
            })
            next_priority += 1
            next_actions.append({
                "priority": next_priority,
                "desc": "daily ops 확인 후 target-weight capped paper pilot readiness audit 실행",
                "strategy": provisional_candidates[0],
                "command": runbook_commands.get("readiness_audit"),
                "order_safety": "no_order",
            })
            next_priority += 1
            if core_entry_action:
                next_actions.append({
                    "priority": next_priority,
                    **core_entry_action,
                })
                next_priority += 1
        next_actions.append({
            "priority": next_priority,
            "desc": "live 검토 전 60영업일 execution-backed pilot_paper 증거 누적",
            "strategy": provisional_candidates[0],
            "command": runbook_commands.get("execute_capped_paper_after_ready"),
            "order_safety": _paper_execute_order_safety(
                runbook_commands.get("execute_capped_paper_after_ready")
            ),
            "requires": "daily_ops_summary.status == READY_TO_EXECUTE",
        })
    else:
        next_actions.append({
            "priority": 1,
            "desc": "provisional_paper_candidate 회복을 위한 research sweep 계속 진행",
            "command": runbook_commands.get("run_canonical_research"),
            "order_safety": "no_order",
        })
    next_actions.append({
        "priority": len(next_actions) + 1,
        "desc": "current_blockers.go_live=true 및 live gate 통과 전까지 live 모드 차단 유지",
        "command": runbook_commands.get("check_promotion_artifacts"),
        "order_safety": "no_order",
    })

    if go_live:
        verdict = f"GO: live_candidate {len(live_candidates)}개 사용 가능"
    elif live_candidates and freshness_blocks_live:
        verdict = "NO-GO: canonical promotion artifact 최신성 미충족"
    else:
        verdict = "NO-GO: 현재 canonical/paper evidence 기준 live_candidate 없음"
    default_strategy = (
        f"{provisional_candidates[0]} capped paper pilot 우선, scoring은 관찰만 유지"
        if provisional_candidates
        else "paper pilot 후보 없음, research 재설계 필요"
    )
    return {
        "artifact_type": "current_go_live_blockers",
        "schema_version": CURRENT_BLOCKERS_SCHEMA_VERSION,
        "generated_at": report_generated_at,
        "source": "reports/promotion/promotion_blocker_summary.json",
        "source_generated_at": blocker_summary.get("generated_at"),
        "source_artifact_hash": blocker_summary.get("source_artifact_hash"),
        "promotion_artifact_freshness": promotion_artifact_freshness,
        "go_live": go_live,
        "verdict": verdict,
        "promotion_summary": summary,
        "live_candidates": live_candidates,
        "provisional_paper_candidates": provisional_candidates,
        "hard_blockers": hard_blockers,
        "soft_blockers": soft_blockers,
        "next_actions": next_actions,
        "operator_runbook": operator_runbook,
        "default_strategy": default_strategy,
    }


def load_current_blockers_from_artifacts(
    promotion_dir: str | Path = "reports/promotion",
    *,
    generated_at: str | None = None,
) -> dict:
    summary_issues = validate_promotion_blocker_summary_artifact(promotion_dir)
    if summary_issues:
        raise ValueError(
            "promotion blocker summary 동기화 실패: " + "; ".join(summary_issues)
        )
    summary_path = Path(promotion_dir) / "promotion_blocker_summary.json"
    blocker_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(blocker_summary, dict):
        raise ValueError(f"{summary_path} top-level JSON is not an object")
    return _build_current_blockers_report_with_latest_ops(
        blocker_summary,
        reports_dir=Path(promotion_dir).parent,
        generated_at=generated_at,
    )


def _build_current_blockers_report_with_latest_ops(
    blocker_summary: dict,
    *,
    reports_dir: str | Path = "reports",
    generated_at: str | None = None,
) -> dict:
    provisional_candidates = _ranked_provisional_candidates(blocker_summary)
    latest_daily_ops = (
        _load_latest_target_weight_daily_ops(provisional_candidates[0], reports_dir)
        if provisional_candidates
        else None
    )
    latest_daily_ops_failure = (
        _load_latest_target_weight_daily_ops_failure(
            provisional_candidates[0],
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_finalize_report = (
        _load_target_weight_finalize_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_snapshot_diagnostics = (
        _load_target_weight_snapshot_diagnostics_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_db_restore_verification = (
        _load_target_weight_db_restore_verification_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_db_restore_review_bundle = (
        _load_target_weight_db_restore_review_bundle_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_db_restore_apply_plan = (
        _load_target_weight_db_restore_apply_plan_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    latest_db_restore_backup = (
        _load_target_weight_db_restore_backup_report(
            provisional_candidates[0],
            latest_daily_ops.get("trade_day") if latest_daily_ops else None,
            reports_dir,
        )
        if provisional_candidates
        else None
    )
    return build_current_blockers_report(
        blocker_summary,
        latest_daily_ops=latest_daily_ops,
        latest_daily_ops_failure=latest_daily_ops_failure,
        latest_finalize_report=latest_finalize_report,
        latest_snapshot_diagnostics=latest_snapshot_diagnostics,
        latest_db_restore_verification=latest_db_restore_verification,
        latest_db_restore_review_bundle=latest_db_restore_review_bundle,
        latest_db_restore_apply_plan=latest_db_restore_apply_plan,
        latest_db_restore_backup=latest_db_restore_backup,
        generated_at=generated_at,
    )


def write_current_blockers_report(
    report: dict,
    output_path: str | Path = "reports/current_blockers.json",
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def validate_current_blockers_artifact(
    promotion_dir: str | Path = "reports/promotion",
    output_path: str | Path = "reports/current_blockers.json",
) -> list[str]:
    issues = [
        "promotion blocker summary 동기화 실패: " + issue
        for issue in validate_promotion_blocker_summary_artifact(promotion_dir)
    ]
    path = Path(output_path)
    if not path.exists():
        issues.append(f"{path} 없음: --current-blockers로 재생성 필요")
        return issues
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"{path} 로드 실패: {exc}")
        return issues
    if not isinstance(current, dict):
        issues.append(f"{path} top-level JSON is not an object")
        return issues
    if issues:
        return issues
    try:
        expected = load_current_blockers_from_artifacts(
            promotion_dir,
            generated_at=str(current.get("generated_at") or "") or None,
        )
    except Exception as exc:
        issues.append(f"current blocker source artifact 로드 실패: {exc}")
        return issues

    for key in (
        "artifact_type",
        "schema_version",
        "source_artifact_hash",
        "promotion_artifact_freshness",
        "go_live",
        "verdict",
        "promotion_summary",
        "live_candidates",
        "provisional_paper_candidates",
        "hard_blockers",
        "soft_blockers",
        "next_actions",
        "operator_runbook",
        "default_strategy",
    ):
        if current.get(key) != expected.get(key):
            issues.append(f"{key} 불일치: --current-blockers로 재생성 필요")
    return issues


def _parse_iso_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_age_days(generated_at: datetime, now: datetime) -> float:
    if generated_at.tzinfo is not None and now.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=None)
    if generated_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - generated_at).total_seconds() / 86400


def validate_promotion_artifact_freshness(
    promotion_dir: str | Path = "reports/promotion",
    *,
    now: datetime | None = None,
    max_artifact_age_days: int | None = None,
) -> list[str]:
    """운영 점검에서 canonical bundle 생성 시각이 live gate 기준 안에 있는지 확인한다."""
    from core.live_gate import LIVE_GATE_MAX_ARTIFACT_AGE_DAYS

    base = Path(promotion_dir)
    metadata_path = base / "run_metadata.json"
    if not metadata_path.exists():
        return ["run_metadata.json 누락."]
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"run_metadata.json 로드 실패: {exc}"]
    if not isinstance(metadata, dict):
        return ["run_metadata.json top-level JSON is not an object"]

    generated_at = _parse_iso_datetime(metadata.get("generated_at"))
    if generated_at is None:
        return ["run_metadata.json generated_at 누락 또는 형식 오류."]

    now = now or datetime.now()
    max_days = (
        LIVE_GATE_MAX_ARTIFACT_AGE_DAYS
        if max_artifact_age_days is None
        else max_artifact_age_days
    )
    age_days = _datetime_age_days(generated_at, now)
    if age_days < 0:
        return ["run_metadata.json generated_at이 현재 시각보다 미래입니다."]
    if age_days > max_days:
        return [
            "run_metadata.json generated_at 기준 canonical artifact가 오래됨: "
            f"{age_days:.1f}일 경과 (최대 {max_days}일). --canonical 재실행 필요."
        ]
    return []


def validate_paper_evidence_operator_artifacts(
    promotion_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> list[str]:
    """운영 점검에서 paper evidence package 파일 문제를 구조화된 WARN으로 노출한다."""
    warnings: list[str] = []
    for item in find_invalid_paper_evidence_operator_packages(
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
    ):
        strategy_name = item["strategy"]
        if not strategy_name:
            warnings.extend(item["issues"])
            continue
        for issue in item["issues"]:
            warnings.append(
                f"{strategy_name}: {issue}; package 재생성 또는 격리 필요"
            )
    return warnings


def find_invalid_paper_evidence_operator_packages(
    promotion_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
) -> list[dict]:
    """metrics_summary에 포함된 전략의 invalid paper evidence package 목록."""
    from core.promotion_engine import validate_paper_evidence_package_file

    base = Path(promotion_dir)
    metrics_path = base / "metrics_summary.json"
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{
            "strategy": "",
            "path": str(metrics_path),
            "issues": [f"{metrics_path} 로드 실패: {exc}"],
            "package": False,
        }]
    if not isinstance(metrics, dict):
        return [{
            "strategy": "",
            "path": str(metrics_path),
            "issues": [f"{metrics_path} top-level JSON is not an object"],
            "package": False,
        }]

    evidence_base = (
        Path(evidence_dir)
        if evidence_dir is not None
        else base.parent / "paper_evidence"
    )
    invalid: list[dict] = []
    for strategy_name in sorted(str(name) for name in metrics):
        issues = validate_paper_evidence_package_file(
            strategy_name,
            evidence_dir=str(evidence_base),
        )
        if issues:
            invalid.append({
                "strategy": strategy_name,
                "path": str(evidence_base / f"promotion_evidence_{strategy_name}.json"),
                "issues": issues,
                "package": True,
            })
    return invalid


def quarantine_invalid_paper_evidence_operator_packages(
    promotion_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path | None = None,
    quarantine_dir: str | Path | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """invalid paper evidence package를 운영 입력 경로 밖으로 이동한다."""
    evidence_base = (
        Path(evidence_dir)
        if evidence_dir is not None
        else Path(promotion_dir).parent / "paper_evidence"
    )
    quarantine_base = (
        Path(quarantine_dir)
        if quarantine_dir is not None
        else evidence_base / "_invalid_packages"
    )
    stamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    results: list[dict] = []
    for item in find_invalid_paper_evidence_operator_packages(
        promotion_dir=promotion_dir,
        evidence_dir=evidence_base,
    ):
        src = Path(item["path"])
        if not item.get("package", True):
            results.append({
                **item,
                "target": "",
                "moved": False,
                "reason": "operator metrics artifact invalid; package quarantine skipped",
            })
            continue
        if not src.exists() or not src.is_file():
            results.append({
                **item,
                "target": "",
                "moved": False,
                "reason": "source package file not found",
            })
            continue

        target = quarantine_base / f"{src.stem}_{stamp}{src.suffix}"
        suffix = 1
        while target.exists():
            target = quarantine_base / f"{src.stem}_{stamp}_{suffix}{src.suffix}"
            suffix += 1

        moved = False
        if not dry_run:
            quarantine_base.mkdir(parents=True, exist_ok=True)
            src.replace(target)
            moved = True
        results.append({
            **item,
            "target": str(target),
            "moved": moved,
            "dry_run": dry_run,
        })
    return results


def validate_promotion_operator_artifacts(
    promotion_dir: str | Path = "reports/promotion",
    current_blockers_path: str | Path = "reports/current_blockers.json",
    *,
    now: datetime | None = None,
    max_artifact_age_days: int | None = None,
) -> list[str]:
    """운영 점검 기본 경로에서 promotion 파생 artifact 전체 동기화를 검사한다."""
    freshness_issues = validate_promotion_artifact_freshness(
        promotion_dir,
        now=now,
        max_artifact_age_days=max_artifact_age_days,
    )
    if freshness_issues:
        return [
            "canonical artifact 최신성 실패: " + issue
            for issue in freshness_issues
        ]

    summary_issues = validate_promotion_blocker_summary_artifact(promotion_dir)
    if summary_issues:
        return [
            "blocker summary 동기화 실패: " + issue
            for issue in summary_issues
        ]

    daily_ops_issues = validate_target_weight_daily_ops_artifacts(Path(promotion_dir).parent)
    if daily_ops_issues:
        return [
            "target-weight daily ops artifact 무결성 실패: " + issue
            for issue in daily_ops_issues
        ]

    return [
        "current blockers 동기화 실패: " + issue
        for issue in validate_current_blockers_artifact(
            promotion_dir,
            current_blockers_path,
        )
    ]


def run_canonical():
    """canonical 평가 실행 → artifact 저장."""
    from config.config_loader import Config
    from backtest.portfolio_backtester import PortfolioBacktester
    from core.data_collector import DataCollector
    import FinanceDataReader as fdr

    EVAL_START = CANONICAL_EVAL_START
    EVAL_END = CANONICAL_EVAL_END
    UNIVERSE_LOOKBACK_START = CANONICAL_UNIVERSE_LOOKBACK_START
    UNIVERSE_LOOKBACK_END = CANONICAL_UNIVERSE_LOOKBACK_END
    INITIAL_CAPITAL = 10_000_000
    TOP_N = CANONICAL_TOP_N
    UNIVERSE_RULE = CANONICAL_UNIVERSE_RULE
    ROTATION_DIV = {"max_positions": 2, "max_position_ratio": 0.45,
                    "max_investment_ratio": 0.85, "min_cash_ratio": 0.10}
    STRATEGIES = ["scoring", "breakout_volume", "relative_strength_rotation",
                  "mean_reversion", "trend_following"]

    _write_canonical_progress("started")

    # ── Universe (거래대금 기반 ex-ante proxy) ──
    dc = DataCollector()
    dc.quiet_ohlcv_log = True
    reused_universe_snapshot = _load_reusable_canonical_universe_snapshot(
        "reports/promotion/run_metadata.json",
        eval_start=EVAL_START,
        eval_end=EVAL_END,
        universe_rule=UNIVERSE_RULE,
        universe_lookback_start=UNIVERSE_LOOKBACK_START,
        universe_lookback_end=UNIVERSE_LOOKBACK_END,
        top_n=TOP_N,
    )
    fetch_errors = {}
    universe_snapshot_reused = bool(reused_universe_snapshot)
    if reused_universe_snapshot:
        universe = reused_universe_snapshot["universe"]
        liquidity_coverage = reused_universe_snapshot["liquidity_coverage"]
        fetch_errors.update(reused_universe_snapshot["fetch_errors"])
        print(
            "Universe snapshot reused "
            f"({reused_universe_snapshot['source']}, {len(universe)} symbols)",
            flush=True,
        )
        _write_canonical_progress(
            "universe_reused",
            universe_size=len(universe),
            source=reused_universe_snapshot["source"],
            source_generated_at=reused_universe_snapshot.get("source_generated_at"),
        )
    else:
        stocks = fdr.StockListing('KOSPI')
        common = stocks[~stocks['Code'].str.match(r'^\d{5}[5-9KL]$')]
        if 'Marcap' in common.columns:
            common = common[common['Marcap'] > 1e11]
        candidates = common['Code'].tolist()

        # 거래대금 순위
        amounts = {}
        liquidity_coverage = {}
        total_candidates = min(100, len(candidates))
        for idx, sym in enumerate(candidates[:100], start=1):
            if idx == 1 or idx % 10 == 0:
                print(f"  liquidity universe scan {idx}/{total_candidates}...", flush=True)
                _write_canonical_progress(
                    "liquidity_universe_scan",
                    scanned=idx,
                    total=total_candidates,
                    collected=len(amounts),
                )
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
        _write_canonical_progress(
            "universe_built",
            universe_size=len(universe),
            liquidity_coverage_count=len(liquidity_coverage),
            fetch_error_count=len(fetch_errors),
        )

    print(f"Universe ({len(universe)}): {universe}")

    # ── Benchmark ──
    per = INITIAL_CAPITAL / len(universe)
    parts = []
    benchmark_coverage = {}
    for idx, sym in enumerate(universe, start=1):
        if idx == 1 or idx % 5 == 0 or idx == len(universe):
            _write_canonical_progress(
                "benchmark_fetch",
                processed=idx,
                total=len(universe),
                symbol=sym,
            )
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

    for idx, strat in enumerate(STRATEGIES, start=1):
        _write_canonical_progress(
            "strategy_full_period",
            strategy=strat,
            index=idx,
            total=len(STRATEGIES),
        )
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
        for window_idx, (ws, we) in enumerate(windows, start=1):
            _write_canonical_progress(
                "strategy_walk_forward",
                strategy=strat,
                window=window_idx,
                total_windows=len(windows),
                window_start=ws,
                window_end=we,
            )
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
    for idx, spec in enumerate(research_specs, start=1):
        name = spec.candidate_id
        _write_canonical_progress(
            "research_full_period",
            strategy=name,
            index=idx,
            total=len(research_specs),
        )
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
        for window_idx, (ws, we) in enumerate(windows, start=1):
            _write_canonical_progress(
                "research_walk_forward",
                strategy=name,
                window=window_idx,
                total_windows=len(windows),
                window_start=ws,
                window_end=we,
            )
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
        "universe_snapshot_reused": universe_snapshot_reused,
    }
    if reused_universe_snapshot:
        metadata["universe_snapshot_source"] = reused_universe_snapshot["source"]
        metadata["universe_snapshot_source_generated_at"] = reused_universe_snapshot.get(
            "source_generated_at"
        )
        metadata["universe_snapshot_source_data_snapshot_hash"] = (
            reused_universe_snapshot.get("source_data_snapshot_hash")
        )

    # ── Promotion 계산 ──
    _write_canonical_progress("promotion_building", strategy_count=len(metrics_all))
    promotions = build_promotion_results(
        metrics_all,
        strategy_specs=strategy_specs_metadata,
        canonical_metadata=metadata,
    )

    # ── Artifact 저장 ──
    out_dir = Path("reports/promotion")
    out_dir.mkdir(parents=True, exist_ok=True)
    blocker_summary = build_promotion_blocker_summary(promotions, metrics_all, metadata)

    artifacts = {
        "run_metadata.json": metadata,
        "metrics_summary.json": metrics_all,
        "walk_forward_summary.json": wf_all,
        "benchmark_comparison.json": benchmark,
        "promotion_result.json": promotions,
        "promotion_blocker_summary.json": blocker_summary,
    }

    for fname, data in artifacts.items():
        path = out_dir / fname
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"  → {path}")
    _, blocker_md_path = write_promotion_blocker_summary(blocker_summary, out_dir)
    print(f"  → {blocker_md_path}")
    current_blockers_path = write_current_blockers_report(
        _build_current_blockers_report_with_latest_ops(
            blocker_summary,
            reports_dir=out_dir.parent,
        ),
        "reports/current_blockers.json",
    )
    print(f"  → {current_blockers_path}")
    _write_canonical_progress(
        "completed",
        output_dir=out_dir.as_posix(),
        current_blockers_path=current_blockers_path,
        strategy_count=len(metrics_all),
    )

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
    parser.add_argument("--check-only", action="store_true", help="promotion artifact 로드 및 운영 artifact 동기화 검증")
    parser.add_argument(
        "--blocker-summary",
        action="store_true",
        help="기존 promotion artifact에서 blocker summary JSON/MD 재생성",
    )
    parser.add_argument(
        "--promotion-artifacts-refresh",
        action="store_true",
        help="기존 metrics/evidence/metadata에서 promotion_result와 파생 운영 파일 재생성",
    )
    parser.add_argument(
        "--blocker-summary-check",
        action="store_true",
        help="저장된 blocker summary가 현재 promotion artifact와 동기화됐는지 검증",
    )
    parser.add_argument(
        "--current-blockers",
        action="store_true",
        help="promotion blocker summary에서 reports/current_blockers.json 갱신",
    )
    parser.add_argument(
        "--current-blockers-check",
        action="store_true",
        help="reports/current_blockers.json이 현재 blocker summary와 동기화됐는지 검증",
    )
    parser.add_argument(
        "--paper-evidence-quarantine-invalid",
        action="store_true",
        help="invalid paper evidence package를 reports/paper_evidence/_invalid_packages로 이동",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="paper evidence package 격리 대상만 출력하고 이동하지 않음",
    )
    args = parser.parse_args()

    if args.canonical:
        run_canonical()
    elif args.paper_evidence_quarantine_invalid:
        results = quarantine_invalid_paper_evidence_operator_packages(
            "reports/promotion",
            "reports/paper_evidence",
            dry_run=args.dry_run,
        )
        if not results:
            print("OK: invalid paper evidence package 없음")
            return
        print(
            "DRY-RUN: invalid paper evidence package 격리 대상"
            if args.dry_run
            else "OK: invalid paper evidence package 격리 완료"
        )
        for item in results:
            action = "would move" if args.dry_run else "moved"
            print(f"  - {item['strategy']}: {action} {item['path']} -> {item['target']}")
            for issue in item["issues"]:
                print(f"    issue: {issue}")
    elif args.promotion_artifacts_refresh:
        try:
            paths = refresh_promotion_artifacts_from_existing_inputs(
                "reports/promotion",
                current_blockers_path="reports/current_blockers.json",
            )
        except Exception as exc:
            print(f"FAIL: promotion artifact 재생성 실패: {exc}")
            sys.exit(1)
        print("OK: promotion artifact 재생성 성공")
        for label, path in paths.items():
            print(f"  {label}: {path}")
    elif args.current_blockers_check:
        issues = validate_current_blockers_artifact("reports/promotion", "reports/current_blockers.json")
        if issues:
            print("FAIL: current blockers 동기화 검증 실패")
            for issue in issues:
                print(f"  - {issue}")
            sys.exit(1)
        print("OK: current blockers 동기화 검증 성공")
    elif args.current_blockers:
        try:
            report = load_current_blockers_from_artifacts("reports/promotion")
            path = write_current_blockers_report(report, "reports/current_blockers.json")
        except Exception as exc:
            print(f"FAIL: current blockers 생성 실패: {exc}")
            sys.exit(1)
        print(f"OK: current blockers 생성 성공\n  {path}")
    elif args.blocker_summary_check:
        issues = validate_promotion_blocker_summary_artifact("reports/promotion")
        if issues:
            print("FAIL: blocker summary 동기화 검증 실패")
            for issue in issues:
                print(f"  - {issue}")
            sys.exit(1)
        print("OK: blocker summary 동기화 검증 성공")
    elif args.blocker_summary:
        out_dir = Path("reports/promotion")
        try:
            summary = load_validated_promotion_blocker_summary_from_artifacts(out_dir)
            json_path, md_path = write_promotion_blocker_summary(summary, out_dir)
        except Exception as exc:
            print(f"FAIL: blocker summary 생성 실패: {exc}")
            sys.exit(1)
        print(f"OK: blocker summary 생성 성공\n  {json_path}\n  {md_path}")
    elif args.check_only:
        from core.promotion_engine import load_promotion_artifact
        result = load_promotion_artifact()
        if result is None:
            print("FAIL: artifact 없음 또는 로드 실패")
            sys.exit(1)
        issues = validate_promotion_operator_artifacts(
            "reports/promotion",
            "reports/current_blockers.json",
        )
        if issues:
            print("FAIL: 운영 artifact 동기화 검증 실패")
            for issue in issues:
                print(f"  - {issue}")
            sys.exit(1)
        paper_warnings = validate_paper_evidence_operator_artifacts(
            "reports/promotion",
            "reports/paper_evidence",
        )
        if paper_warnings:
            print("WARN: paper evidence package 검증 경고")
            for warning in paper_warnings:
                print(f"  - {warning}")
            print(
                "  정리: python tools/evaluate_and_promote.py "
                "--paper-evidence-quarantine-invalid --dry-run"
            )
        print("OK: artifact 로드 및 운영 artifact 동기화 검증 성공")
        for name, p in result.items():
            print(f"  {name}: {p['status']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
