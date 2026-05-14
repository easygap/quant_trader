"""
Canonical 평가 → Artifact 생성 → 승격 판정 → Status Report

실행: python tools/evaluate_and_promote.py --canonical
요약 재생성: python tools/evaluate_and_promote.py --blocker-summary
요약 검증: python tools/evaluate_and_promote.py --blocker-summary-check
현재 blocker 갱신: python tools/evaluate_and_promote.py --current-blockers
출력: reports/promotion/
  - metrics_summary.json
  - walk_forward_summary.json
  - benchmark_comparison.json
  - run_metadata.json
  - promotion_result.json  (최종 상태 계산 결과)
  - promotion_blocker_summary.json/md (운영자용 차단 사유 요약)
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
    canonical_integrity_issues = (
        validate_canonical_metadata_integrity(canonical_metadata)
        if isinstance(canonical_metadata, dict)
        else []
    )
    canonical_integrity_ok = (
        not canonical_integrity_issues
        if isinstance(canonical_metadata, dict)
        else None
    )
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
    paper_reference_date = datetime.now()
    for name, m in metrics_all.items():
        for key in paper_fields:
            m.pop(key, None)
        paper_metrics = paper_evidence_metrics_from_package(
            load_paper_evidence_package(name, evidence_dir),
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
        if canonical_integrity_ok is not None:
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


def load_promotion_blocker_summary_from_artifacts(artifact_dir: str | Path = "reports/promotion") -> dict:
    """기존 promotion artifact에서 blocker summary를 재구성한다."""
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
        expected = load_promotion_blocker_summary_from_artifacts(base)
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


def build_current_blockers_report(blocker_summary: dict) -> dict:
    """promotion blocker summary에서 현재 go-live blocker 운영 파일을 생성한다."""
    if not isinstance(blocker_summary, dict):
        blocker_summary = {}
    summary = blocker_summary.get("summary") or {}
    live_candidates = _strategy_names_by_status(blocker_summary, "live_candidate")
    provisional_candidates = _ranked_provisional_candidates(blocker_summary)
    paper_only = _strategy_names_by_status(blocker_summary, "paper_only")
    research_only = _strategy_names_by_status(blocker_summary, "research_only")
    go_live = bool(live_candidates)

    hard_blockers = []
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
    if provisional_candidates:
        next_actions.append({
            "priority": 1,
            "desc": "target-weight capped paper pilot readiness audit 실행 후 추천 cap만 승인",
            "strategy": provisional_candidates[0],
        })
        next_actions.append({
            "priority": 2,
            "desc": "live 검토 전 60영업일 execution-backed pilot_paper 증거 누적",
            "strategy": provisional_candidates[0],
        })
    else:
        next_actions.append({
            "priority": 1,
            "desc": "provisional_paper_candidate 회복을 위한 research sweep 계속 진행",
        })
    next_actions.append({
        "priority": len(next_actions) + 1,
        "desc": "current_blockers.go_live=true 및 live gate 통과 전까지 live 모드 차단 유지",
    })

    verdict = (
        f"GO: live_candidate {len(live_candidates)}개 사용 가능"
        if go_live
        else "NO-GO: 현재 canonical/paper evidence 기준 live_candidate 없음"
    )
    default_strategy = (
        f"{provisional_candidates[0]} capped paper pilot 우선, scoring은 관찰만 유지"
        if provisional_candidates
        else "paper pilot 후보 없음, research 재설계 필요"
    )
    return {
        "artifact_type": "current_go_live_blockers",
        "schema_version": 2,
        "generated_at": blocker_summary.get("generated_at") or datetime.now().isoformat(),
        "source": "reports/promotion/promotion_blocker_summary.json",
        "source_artifact_hash": blocker_summary.get("source_artifact_hash"),
        "go_live": go_live,
        "verdict": verdict,
        "promotion_summary": summary,
        "live_candidates": live_candidates,
        "provisional_paper_candidates": provisional_candidates,
        "hard_blockers": hard_blockers,
        "soft_blockers": soft_blockers,
        "next_actions": next_actions,
        "default_strategy": default_strategy,
    }


def load_current_blockers_from_artifacts(
    promotion_dir: str | Path = "reports/promotion",
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
    return build_current_blockers_report(blocker_summary)


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
        expected = load_current_blockers_from_artifacts(promotion_dir)
    except Exception as exc:
        issues.append(f"current blocker source artifact 로드 실패: {exc}")
        return issues

    for key in (
        "artifact_type",
        "schema_version",
        "source_artifact_hash",
        "go_live",
        "verdict",
        "promotion_summary",
        "live_candidates",
        "provisional_paper_candidates",
        "hard_blockers",
        "soft_blockers",
        "next_actions",
        "default_strategy",
    ):
        if current.get(key) != expected.get(key):
            issues.append(f"{key} 불일치: --current-blockers로 재생성 필요")
    return issues


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

    # ── Promotion 계산 ──
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
        build_current_blockers_report(blocker_summary),
        "reports/current_blockers.json",
    )
    print(f"  → {current_blockers_path}")

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
    parser.add_argument(
        "--blocker-summary",
        action="store_true",
        help="기존 promotion artifact에서 blocker summary JSON/MD 재생성",
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
    args = parser.parse_args()

    if args.canonical:
        run_canonical()
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
            summary = load_promotion_blocker_summary_from_artifacts(out_dir)
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
        print("OK: artifact 로드 성공")
        for name, p in result.items():
            print(f"  {name}: {p['status']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
