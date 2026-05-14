"""
Live trading readiness gate.

The live gate intentionally trusts only canonical promotion artifacts that match
the current code and resolved config, then cross-checks current go-live blockers
and real paper evidence.
Legacy walk-forward JSON files and hand-edited approval files are not enough to
allow live trading.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


LIVE_GATE_SCHEMA_VERSION = 1
LIVE_GATE_ARTIFACT_TYPE = "canonical_promotion_bundle"
LIVE_GATE_MAX_ARTIFACT_AGE_DAYS = 7
LIVE_GATE_MAX_PAPER_EVIDENCE_AGE_DAYS = 14
LIVE_GATE_DATA_SNAPSHOT_HASH_LENGTH = 64
TARGET_WEIGHT_BASE_STRATEGIES = frozenset({"target_weight_rotation"})
CURRENT_BLOCKERS_ARTIFACT_TYPE = "current_go_live_blockers"
CURRENT_BLOCKERS_SCHEMA_VERSION = 2
PROMOTION_BLOCKER_SUMMARY_ARTIFACT_TYPE = "promotion_blocker_summary"
PROMOTION_BLOCKER_SUMMARY_SCHEMA_VERSION = 1

REQUIRED_PROMOTION_ARTIFACTS = (
    "metrics_summary.json",
    "walk_forward_summary.json",
    "benchmark_comparison.json",
    "run_metadata.json",
    "promotion_result.json",
)

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


def get_current_git_hash() -> str:
    """Return the current short commit hash, or unknown if git is unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "top-level JSON is not an object"
    return data, None


def _parse_iso_datetime(value: Any) -> datetime | None:
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


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _latest_evidence_date(evidence: dict[str, Any]) -> datetime | None:
    latest = _parse_date(evidence.get("latest_evidence_date"))
    if latest is not None:
        return latest
    period = evidence.get("period")
    if isinstance(period, str) and "~" in period:
        return _parse_date(period.split("~")[-1].strip())
    return None


def _config_hash(config: Any, attr: str) -> str:
    value = getattr(config, attr, "")
    return value if isinstance(value, str) else ""


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _promotion_blocker_metric_snapshots(metrics_all: dict[str, Any], strategy_names) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    if not isinstance(metrics_all, dict):
        metrics_all = {}
    for name in sorted(str(item) for item in strategy_names):
        metrics = metrics_all.get(name) or {}
        if not isinstance(metrics, dict):
            metrics = {}
        snapshots[name] = {
            key: metrics.get(key)
            for key in PROMOTION_BLOCKER_METRIC_KEYS
            if metrics.get(key) is not None
        }
    return snapshots


def build_promotion_blocker_source_hash(
    promotions: dict[str, Any],
    metrics_all: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    """Return the source hash used by promotion/current blocker artifacts."""
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
    return _stable_payload_hash(payload)


def _material_fetch_error_keys(fetch_errors: dict[str, Any], universe: list[Any]) -> list[str]:
    """Return fetch errors that affect the final canonical universe.

    Universe discovery can probe many liquidity candidates before selecting the
    final top-N universe. A failed liquidity probe for a symbol outside that
    final universe is audit context, not a reason to invalidate the artifact.
    """
    universe_set = {str(symbol) for symbol in universe}
    material: list[str] = []
    for key, payload in fetch_errors.items():
        key_text = str(key)
        stage = payload.get("stage") if isinstance(payload, dict) else None
        if key_text.startswith("liquidity:"):
            symbol = key_text.split(":", 1)[1]
            if symbol not in universe_set:
                continue
        elif stage == "universe_liquidity":
            symbol = (
                str(payload.get("symbol") or key_text)
                if isinstance(payload, dict)
                else key_text
            )
            if symbol not in universe_set:
                continue
        material.append(key_text)
    return material


def validate_canonical_metadata_integrity(metadata: dict[str, Any]) -> list[str]:
    """canonical metadata의 입력 snapshot과 평가 오류 상태를 검증한다."""
    issues: list[str] = []

    data_hash = metadata.get("data_snapshot_hash")
    if not isinstance(data_hash, str) or len(data_hash) != LIVE_GATE_DATA_SNAPSHOT_HASH_LENGTH:
        issues.append("run_metadata.data_snapshot_hash 누락 또는 형식 오류.")

    manifest = metadata.get("data_snapshot_manifest")
    if not isinstance(manifest, dict):
        issues.append("run_metadata.data_snapshot_manifest 누락 또는 형식 오류.")
        return issues

    manifest_hash = manifest.get("data_snapshot_hash")
    if manifest_hash != data_hash:
        issues.append("run_metadata.data_snapshot_hash와 manifest hash 불일치.")

    manifest_material = dict(manifest)
    manifest_material.pop("data_snapshot_hash", None)
    computed_hash = _stable_payload_hash(manifest_material)
    if isinstance(data_hash, str) and len(data_hash) == LIVE_GATE_DATA_SNAPSHOT_HASH_LENGTH:
        if computed_hash != data_hash:
            issues.append("data_snapshot_manifest 재계산 hash 불일치.")

    universe = manifest.get("universe")
    if not isinstance(universe, list) or not universe:
        issues.append("data_snapshot_manifest.universe 누락 또는 비어 있음.")
        universe = []

    universe_size = _as_int(manifest.get("universe_size"))
    if universe and universe_size != len(universe):
        issues.append(
            f"data_snapshot_manifest universe_size 불일치: {universe_size} != {len(universe)}."
        )

    liquidity = manifest.get("liquidity_coverage")
    benchmark = manifest.get("benchmark_coverage")
    if not isinstance(liquidity, dict):
        issues.append("data_snapshot_manifest.liquidity_coverage 누락 또는 형식 오류.")
        liquidity = {}
    if not isinstance(benchmark, dict):
        issues.append("data_snapshot_manifest.benchmark_coverage 누락 또는 형식 오류.")
        benchmark = {}

    missing_liquidity = [symbol for symbol in universe if symbol not in liquidity]
    missing_benchmark = [symbol for symbol in universe if symbol not in benchmark]
    if missing_liquidity:
        issues.append(f"유동성 coverage 누락 종목: {missing_liquidity[:5]}.")
    if missing_benchmark:
        issues.append(f"벤치마크 coverage 누락 종목: {missing_benchmark[:5]}.")

    zero_liquidity = [
        symbol
        for symbol in universe
        if isinstance(liquidity.get(symbol), dict)
        and ((_as_int(liquidity[symbol].get("rows")) or 0) <= 0)
    ]
    zero_benchmark = [
        symbol
        for symbol in universe
        if isinstance(benchmark.get(symbol), dict)
        and ((_as_int(benchmark[symbol].get("rows")) or 0) <= 0)
    ]
    if zero_liquidity:
        issues.append(f"유동성 coverage rows가 비어 있는 종목: {zero_liquidity[:5]}.")
    if zero_benchmark:
        issues.append(f"벤치마크 coverage rows가 비어 있는 종목: {zero_benchmark[:5]}.")

    fetch_errors = manifest.get("fetch_errors")
    if not isinstance(fetch_errors, dict):
        issues.append("data_snapshot_manifest.fetch_errors 형식 오류.")
    elif fetch_errors:
        material_fetch_errors = _material_fetch_error_keys(fetch_errors, universe)
        if material_fetch_errors:
            issues.append(f"data snapshot 수집 오류 존재: {material_fetch_errors[:5]}.")

    evaluation_errors = metadata.get("evaluation_errors")
    if not isinstance(evaluation_errors, dict):
        issues.append("run_metadata.evaluation_errors 형식 오류.")
    elif evaluation_errors:
        issues.append(f"canonical 평가 오류 존재: {list(evaluation_errors)[:5]}.")

    walk_forward_errors = metadata.get("walk_forward_errors")
    if not isinstance(walk_forward_errors, dict):
        issues.append("run_metadata.walk_forward_errors 형식 오류.")
    elif walk_forward_errors:
        issues.append(f"walk-forward 평가 오류 존재: {list(walk_forward_errors)[:5]}.")

    return issues


def _strategy_spec_for(metadata: dict[str, Any], strategy_name: str) -> dict[str, Any] | None:
    specs = metadata.get("strategy_specs")
    if not isinstance(specs, list):
        return None
    for spec in specs:
        if isinstance(spec, dict) and spec.get("candidate_id") == strategy_name:
            return spec
    return None


def _is_target_weight_strategy(strategy_name: str, metadata: dict[str, Any] | None = None) -> bool:
    if isinstance(metadata, dict):
        spec = _strategy_spec_for(metadata, strategy_name)
        if isinstance(spec, dict):
            base_strategy = spec.get("base_strategy") or spec.get("strategy")
            if isinstance(base_strategy, str) and base_strategy in TARGET_WEIGHT_BASE_STRATEGIES:
                return True
    return strategy_name.startswith("target_weight_")


def _canonical_target_weight_params_hash(
    metadata: dict[str, Any],
    strategy_name: str,
) -> str | None:
    spec = _strategy_spec_for(metadata, strategy_name)
    if isinstance(spec, dict):
        params_hash = spec.get("params_hash")
        return params_hash if isinstance(params_hash, str) and params_hash else None
    return None


def _validate_target_weight_evidence_summary(
    strategy_name: str,
    evidence: dict[str, Any],
    canonical_params_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    if not _is_target_weight_strategy(strategy_name, metadata):
        return []

    summary = evidence.get("target_weight_evidence")
    if not isinstance(summary, dict) or summary.get("required") is not True:
        return ["target-weight paper evidence proof summary 누락."]

    issues: list[str] = []
    promotable_days = _as_int(evidence.get("promotable_evidence_days")) or 0
    valid_days = _as_int(summary.get("valid_pilot_days")) or 0
    invalid_days = _as_int(summary.get("invalid_days")) or 0
    if summary.get("all_promotable_days_verified") is not True:
        issues.append("target-weight promotable evidence가 모두 검증된 pilot_paper 실행 증거가 아님.")
    if summary.get("params_hash_consistent") is not True:
        issues.append("target-weight pilot evidence params_hash가 60영업일 전체에서 일관되지 않음.")
    evidence_params_hash = summary.get("params_hash") or evidence.get("target_weight_params_hash")
    if not isinstance(evidence_params_hash, str) or not evidence_params_hash:
        issues.append("target-weight pilot evidence params_hash 누락.")
    elif not canonical_params_hash:
        issues.append("target-weight canonical params_hash 누락.")
    elif evidence_params_hash != canonical_params_hash:
        issues.append(
            "target-weight canonical params_hash 불일치: "
            f"evidence={evidence_params_hash}, canonical={canonical_params_hash}."
        )
    if valid_days < promotable_days or valid_days < 60:
        issues.append(
            "target-weight verified pilot_paper evidence 60영업일 미달 "
            f"(valid={valid_days}, promotable={promotable_days})."
        )
    if invalid_days > 0:
        issues.append(
            "target-weight invalid execution evidence 존재: "
            f"{invalid_days} days reasons={summary.get('invalid_reasons', {})}"
        )
    return issues


def _validate_paper_evidence_package_integrity(
    strategy_name: str,
    evidence: dict[str, Any],
    *,
    promotion_base: Path,
    evidence_base: Path,
    canonical_metadata: dict[str, Any],
) -> list[str]:
    """promotion evidence package와 원본 daily evidence JSONL 무결성을 확인한다."""
    issues: list[str] = []

    try:
        from core.paper_evidence import (
            PROMOTION_PACKAGE_INTEGRITY_SCHEMA_VERSION,
            PROMOTION_SOURCE_RECORDS_SCHEMA_VERSION,
            build_promotion_source_records_summary,
            compute_promotion_package_integrity_hash,
        )
    except Exception as exc:
        return [f"paper evidence integrity verifier 로드 실패: {exc}"]

    integrity = evidence.get("package_integrity")
    if not isinstance(integrity, dict):
        issues.append("paper evidence package_integrity 누락.")
    else:
        if integrity.get("schema_version") != PROMOTION_PACKAGE_INTEGRITY_SCHEMA_VERSION:
            issues.append("paper evidence package_integrity schema_version 불일치.")
        expected_payload_hash = compute_promotion_package_integrity_hash(evidence)
        if integrity.get("payload_hash") != expected_payload_hash:
            issues.append("paper evidence package_integrity payload_hash 불일치.")

    actual_source = evidence.get("source_records")
    if not isinstance(actual_source, dict):
        issues.append("paper evidence source_records 누락.")
        return issues
    if actual_source.get("schema_version") != PROMOTION_SOURCE_RECORDS_SCHEMA_VERSION:
        issues.append("paper evidence source_records schema_version 불일치.")

    try:
        expected_source = build_promotion_source_records_summary(
            strategy_name,
            canonical_metadata=canonical_metadata,
            promotion_dir=promotion_base,
            evidence_dir=evidence_base,
        )
    except Exception as exc:
        issues.append(f"paper evidence source_records 재계산 실패: {exc}")
        return issues

    for field in (
        "records_hash",
        "record_count",
        "first_date",
        "last_date",
        "canonical_record_count",
        "promotable_record_count",
        "target_weight_required",
    ):
        if actual_source.get(field) != expected_source.get(field):
            issues.append(
                "paper evidence source_records 불일치: "
                f"{field} expected={expected_source.get(field)!r}, actual={actual_source.get(field)!r}."
            )

    latest_evidence = evidence.get("latest_evidence_date")
    if expected_source.get("last_date") and latest_evidence != expected_source.get("last_date"):
        issues.append(
            "paper evidence latest_evidence_date와 source_records last_date 불일치: "
            f"{latest_evidence!r} != {expected_source.get('last_date')!r}."
        )

    return issues


def _recalculate_promotion_result(
    strategy_name: str,
    promotion_base: Path,
    evidence_base: Path,
) -> tuple[str | None, list[str], str | None]:
    """JSON status를 그대로 믿지 않고 canonical artifact 기준으로 승격을 재계산한다."""
    try:
        from core.promotion_engine import load_metrics_from_artifact, promote
    except Exception as exc:
        return None, [], f"promotion engine 로드 실패: {exc}"

    try:
        metrics_by_strategy = load_metrics_from_artifact(
            str(promotion_base),
            evidence_dir=str(evidence_base),
        )
    except Exception as exc:
        return None, [], f"promotion 재계산 실패: {exc}"

    metrics = metrics_by_strategy.get(strategy_name)
    if metrics is None:
        return None, [], f"promotion 재계산 metrics 없음: {strategy_name}"

    result = promote(metrics)
    return result.status, result.allowed_modes, result.reason


def _validate_current_blockers_gate(
    strategy_name: str,
    promotion_base: Path,
    artifacts: dict[str, dict[str, Any]],
    current_blockers_path: Path,
) -> list[str]:
    """current_blockers.json이 현재 promotion bundle과 일치하고 live를 허용하는지 검증."""
    issues: list[str] = []
    summary_path = promotion_base / "promotion_blocker_summary.json"
    if not summary_path.exists():
        return [
            "promotion blocker summary 없음: "
            f"{summary_path}. python tools/evaluate_and_promote.py --canonical 또는 --blocker-summary 실행 후 재검증하세요."
        ]

    blocker_summary, summary_err = _read_json(summary_path)
    if blocker_summary is None:
        return [f"promotion blocker summary 파싱 오류: {summary_path} ({summary_err})"]

    if blocker_summary.get("artifact_type") != PROMOTION_BLOCKER_SUMMARY_ARTIFACT_TYPE:
        issues.append(
            "promotion blocker summary artifact_type 불일치: "
            f"{blocker_summary.get('artifact_type')!r} != {PROMOTION_BLOCKER_SUMMARY_ARTIFACT_TYPE!r}"
        )
    if blocker_summary.get("schema_version") != PROMOTION_BLOCKER_SUMMARY_SCHEMA_VERSION:
        issues.append(
            "promotion blocker summary schema_version 불일치: "
            f"{blocker_summary.get('schema_version')} != {PROMOTION_BLOCKER_SUMMARY_SCHEMA_VERSION}"
        )

    expected_source_hash = build_promotion_blocker_source_hash(
        artifacts["promotion_result.json"],
        artifacts["metrics_summary.json"],
        artifacts["run_metadata.json"],
    )
    expected_blocker_summary = None
    expected_current_blockers = None
    try:
        from tools.evaluate_and_promote import (
            build_current_blockers_report,
            build_promotion_blocker_summary,
        )

        expected_blocker_summary = build_promotion_blocker_summary(
            artifacts["promotion_result.json"],
            artifacts["metrics_summary.json"],
            artifacts["run_metadata.json"],
        )
        expected_current_blockers = build_current_blockers_report(expected_blocker_summary)
    except Exception as exc:
        issues.append(f"promotion blocker summary 재계산 실패: {exc}")

    summary_source_hash = blocker_summary.get("source_artifact_hash")
    if summary_source_hash != expected_source_hash:
        issues.append(
            "promotion blocker summary source_artifact_hash 불일치: "
            f"summary={summary_source_hash}, expected={expected_source_hash}. "
            "python tools/evaluate_and_promote.py --blocker-summary 실행 후 재검증하세요."
        )
    if expected_blocker_summary is not None:
        for key in ("summary", "strategies"):
            if blocker_summary.get(key) != expected_blocker_summary.get(key):
                issues.append(
                    f"promotion blocker summary {key} 내용 불일치: "
                    "python tools/evaluate_and_promote.py --blocker-summary 실행 후 재검증하세요."
                )

    if not current_blockers_path.exists():
        issues.append(
            "current blockers 없음: "
            f"{current_blockers_path}. python tools/evaluate_and_promote.py --current-blockers 실행 후 재검증하세요."
        )
        return issues

    current, current_err = _read_json(current_blockers_path)
    if current is None:
        issues.append(f"current blockers 파싱 오류: {current_blockers_path} ({current_err})")
        return issues

    if current.get("artifact_type") != CURRENT_BLOCKERS_ARTIFACT_TYPE:
        issues.append(
            "current_blockers artifact_type 불일치: "
            f"{current.get('artifact_type')!r} != {CURRENT_BLOCKERS_ARTIFACT_TYPE!r}"
        )
    if current.get("schema_version") != CURRENT_BLOCKERS_SCHEMA_VERSION:
        issues.append(
            "current_blockers schema_version 불일치: "
            f"{current.get('schema_version')} != {CURRENT_BLOCKERS_SCHEMA_VERSION}"
        )
    if current.get("source_artifact_hash") != summary_source_hash:
        issues.append(
            "current_blockers source_artifact_hash 불일치: "
            f"current={current.get('source_artifact_hash')}, summary={summary_source_hash}. "
            "python tools/evaluate_and_promote.py --current-blockers 실행 후 재검증하세요."
        )
    if current.get("promotion_summary") != blocker_summary.get("summary"):
        issues.append(
            "current_blockers promotion_summary 불일치: "
            "python tools/evaluate_and_promote.py --current-blockers 실행 후 재검증하세요."
        )
    if expected_current_blockers is not None:
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
            if current.get(key) != expected_current_blockers.get(key):
                issues.append(
                    f"current_blockers {key} 내용 불일치: "
                    "python tools/evaluate_and_promote.py --current-blockers 실행 후 재검증하세요."
                )

    live_candidates = current.get("live_candidates")
    if not isinstance(live_candidates, list):
        issues.append("current_blockers live_candidates 형식 오류.")
        live_candidates = []
    if current.get("go_live") is not True:
        issues.append(
            "current_blockers.go_live가 true가 아님 "
            f"(verdict={current.get('verdict')!r}). live 전환 불가."
        )
    if strategy_name not in live_candidates:
        issues.append(
            f"current_blockers live_candidates에 전략 '{strategy_name}'이 없음 "
            f"(live_candidates={live_candidates})."
        )
    hard_blockers = current.get("hard_blockers")
    if hard_blockers:
        issues.append(f"current_blockers hard_blockers 존재: {hard_blockers}")

    return issues


def validate_live_readiness(
    config: Any,
    strategy_name: str,
    *,
    promotion_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path = "reports/paper_evidence",
    current_blockers_path: str | Path | None = None,
    now: datetime | None = None,
    current_git_hash: str | None = None,
    max_artifact_age_days: int = LIVE_GATE_MAX_ARTIFACT_AGE_DAYS,
) -> list[str]:
    """Validate the canonical evidence chain required before live trading."""
    issues: list[str] = []
    promotion_base = Path(promotion_dir)
    evidence_base = Path(evidence_dir)
    blockers_path = (
        Path(current_blockers_path)
        if current_blockers_path is not None
        else promotion_base.parent / "current_blockers.json"
    )
    now = now or datetime.now()
    current_git_hash = current_git_hash or get_current_git_hash()

    missing = [name for name in REQUIRED_PROMOTION_ARTIFACTS if not (promotion_base / name).exists()]
    if missing:
        issues.append(
            "승인 파일 없음: canonical promotion bundle 누락 "
            f"({promotion_base}; missing={missing}). "
            "python tools/evaluate_and_promote.py --canonical 실행 후 재검증하세요."
        )
        return issues

    artifacts: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_PROMOTION_ARTIFACTS:
        path = promotion_base / name
        data, err = _read_json(path)
        if data is None:
            issues.append(f"canonical artifact 파싱 오류: {path} ({err})")
        else:
            artifacts[name] = data
    if issues:
        return issues

    issues.extend(
        _validate_current_blockers_gate(
            strategy_name,
            promotion_base,
            artifacts,
            blockers_path,
        )
    )

    metadata = artifacts["run_metadata.json"]
    if metadata.get("schema_version") != LIVE_GATE_SCHEMA_VERSION:
        issues.append(
            f"promotion artifact schema_version 불일치: "
            f"{metadata.get('schema_version')} != {LIVE_GATE_SCHEMA_VERSION}"
        )
    if metadata.get("artifact_type") != LIVE_GATE_ARTIFACT_TYPE:
        issues.append(
            f"promotion artifact_type 불일치: "
            f"{metadata.get('artifact_type')!r} != {LIVE_GATE_ARTIFACT_TYPE!r}"
        )
    issues.extend(validate_canonical_metadata_integrity(metadata))

    artifact_commit = metadata.get("commit_hash")
    if current_git_hash == "unknown":
        issues.append("현재 git commit hash 확인 실패. live 전환 불가.")
    elif artifact_commit != current_git_hash:
        issues.append(
            f"promotion artifact commit_hash 불일치: artifact={artifact_commit}, current={current_git_hash}. "
            "현재 코드로 canonical 평가를 다시 실행하세요."
        )

    yaml_hash = _config_hash(config, "yaml_hash")
    resolved_hash = _config_hash(config, "resolved_hash")
    if metadata.get("config_yaml_hash") != yaml_hash:
        issues.append(
            "promotion artifact config_yaml_hash 불일치. "
            "설정 파일 변경 후 canonical 평가를 다시 실행하세요."
        )
    if metadata.get("config_resolved_hash") != resolved_hash:
        issues.append(
            "promotion artifact config_resolved_hash 불일치. "
            "환경변수/실행 설정 변경 후 canonical 평가를 다시 실행하세요."
        )

    generated_at = _parse_iso_datetime(metadata.get("generated_at"))
    if generated_at is None:
        issues.append("promotion artifact generated_at 누락 또는 형식 오류.")
    else:
        age_days = _datetime_age_days(generated_at, now)
        if age_days < 0:
            issues.append("promotion artifact generated_at이 현재 시각보다 미래입니다.")
        elif age_days > max_artifact_age_days:
            issues.append(
                f"promotion artifact가 오래됨: {age_days:.1f}일 경과 "
                f"(최대 {max_artifact_age_days}일). canonical 평가를 다시 실행하세요."
            )

    promotions = artifacts["promotion_result.json"]
    strategy_promotion = promotions.get(strategy_name)
    if not isinstance(strategy_promotion, dict):
        issues.append(f"전략 '{strategy_name}'의 promotion_result가 없음.")
    else:
        status = strategy_promotion.get("status")
        allowed_modes = strategy_promotion.get("allowed_modes") or []
        if status != "live_candidate" or "live" not in allowed_modes:
            issues.append(
                f"전략 '{strategy_name}'은 live_candidate가 아님 "
                f"(status={status}, allowed_modes={allowed_modes})."
            )

    recalculated_status, recalculated_modes, recalculated_reason = _recalculate_promotion_result(
        strategy_name,
        promotion_base,
        evidence_base,
    )
    if recalculated_status != "live_candidate" or "live" not in recalculated_modes:
        issues.append(
            f"전략 '{strategy_name}' promotion 재계산 결과 live_candidate가 아님 "
            f"(status={recalculated_status}, allowed_modes={recalculated_modes}, "
            f"reason={recalculated_reason})."
        )

    metrics = artifacts["metrics_summary.json"].get(strategy_name)
    if not isinstance(metrics, dict):
        issues.append(f"전략 '{strategy_name}'의 metrics_summary가 없음.")
    else:
        evaluation_status = metrics.get("evaluation_status")
        if evaluation_status == "failed":
            issues.append(
                f"전략 '{strategy_name}' canonical 평가 실패: "
                f"{metrics.get('evaluation_stage')} {metrics.get('evaluation_error_type')} "
                f"{metrics.get('error')}"
            )
        elif evaluation_status not in (None, "ok"):
            issues.append(f"전략 '{strategy_name}' evaluation_status={evaluation_status!r} 확인 필요.")
        total_return = _as_float(metrics.get("total_return"))
        sharpe = _as_float(metrics.get("sharpe"))
        profit_factor = _as_float(metrics.get("profit_factor"))
        if total_return is None or total_return <= 0:
            issues.append(f"전략 '{strategy_name}'의 canonical total_return이 양수가 아님.")
        if sharpe is None or sharpe <= 0:
            issues.append(f"전략 '{strategy_name}'의 canonical Sharpe가 양수가 아님.")
        if profit_factor is None or profit_factor < 1.1:
            issues.append(f"전략 '{strategy_name}'의 canonical profit_factor가 1.1 미만.")

    wf = artifacts["walk_forward_summary.json"].get(strategy_name)
    if not isinstance(wf, dict):
        issues.append(f"전략 '{strategy_name}'의 walk_forward_summary가 없음.")
    else:
        windows = _as_int(wf.get("windows")) or 0
        positive = _as_int(wf.get("positive")) or 0
        sharpe_pos = _as_int(wf.get("sharpe_pos")) or 0
        total_trades = _as_int(wf.get("total_trades")) or 0
        if windows < 3:
            issues.append(f"WF windows {windows}개 < 3개.")
        if windows > 0 and positive / windows < 0.6:
            issues.append(f"WF positive ratio {positive}/{windows} < 60%.")
        if windows > 0 and sharpe_pos / windows < 0.6:
            issues.append(f"WF Sharpe>0 ratio {sharpe_pos}/{windows} < 60%.")
        if total_trades < 30:
            issues.append(f"WF trades {total_trades}개 < 30개.")

    benchmark = artifacts["benchmark_comparison.json"]
    excess_return_map = benchmark.get("strategy_excess_return_pct")
    excess_sharpe_map = benchmark.get("strategy_excess_sharpe")
    if not isinstance(excess_return_map, dict):
        issues.append("benchmark_comparison.strategy_excess_return_pct 누락.")
    else:
        excess_return = excess_return_map.get(strategy_name)
        if excess_return is None:
            issues.append(f"전략 '{strategy_name}'의 benchmark excess return 누락.")
        else:
            excess_return_value = _as_float(excess_return)
            if excess_return_value is None or excess_return_value <= 0:
                issues.append(
                    f"전략 '{strategy_name}' 벤치마크 대비 초과수익 {excess_return!r} <= 0."
                )
    if not isinstance(excess_sharpe_map, dict):
        issues.append("benchmark_comparison.strategy_excess_sharpe 누락.")
    else:
        excess_sharpe = excess_sharpe_map.get(strategy_name)
        if excess_sharpe is None:
            issues.append(f"전략 '{strategy_name}'의 benchmark excess Sharpe 누락.")
        else:
            excess_sharpe_value = _as_float(excess_sharpe)
            if excess_sharpe_value is None or excess_sharpe_value <= 0:
                issues.append(
                    f"전략 '{strategy_name}' 벤치마크 대비 excess Sharpe {excess_sharpe!r} <= 0."
                )

    evidence_path = evidence_base / f"promotion_evidence_{strategy_name}.json"
    evidence, evidence_err = _read_json(evidence_path)
    if evidence is None:
        issues.append(
            f"paper promotion evidence 없음 또는 파싱 오류: {evidence_path} ({evidence_err}). "
            "tools/run_paper_evidence_pipeline.py로 60영업일 증거 패키지를 생성하세요."
        )
    else:
        evidence_strategy = evidence.get("strategy")
        if evidence_strategy != strategy_name:
            if evidence_strategy is None:
                issues.append(
                    f"paper evidence strategy 누락: {strategy_name} 패키지에 strategy 필드가 필요함."
                )
            else:
                issues.append(
                    f"paper evidence strategy 불일치: {evidence_strategy} != {strategy_name}"
                )
        if evidence.get("recommendation") != "ELIGIBLE":
            issues.append(
                f"paper evidence recommendation={evidence.get('recommendation')} "
                f"block_reasons={evidence.get('block_reasons', [])}"
            )
        promotable_days = _as_int(evidence.get("promotable_evidence_days")) or 0
        benchmark_final_ratio = _as_float(evidence.get("benchmark_final_ratio")) or 0
        paper_sharpe = _as_float(evidence.get("paper_sharpe"))
        same_excess = _as_float(evidence.get("avg_same_universe_excess")) or 0
        cash_excess = _as_float(evidence.get("avg_cash_adjusted_excess")) or 0
        cumulative_return = _as_float(evidence.get("cumulative_return")) or 0
        sell_count = _as_int(evidence.get("sell_count")) or 0
        win_rate = _as_float(evidence.get("win_rate")) or 0
        frozen_days = _as_int(evidence.get("frozen_days")) or 0
        latest_evidence = _latest_evidence_date(evidence)
        if latest_evidence is None:
            issues.append("paper evidence latest_evidence_date 누락 또는 형식 오류.")
        else:
            evidence_age_days = (now.date() - latest_evidence.date()).days
            if evidence_age_days < 0:
                issues.append("paper evidence latest_evidence_date가 현재 시각보다 미래입니다.")
            elif evidence_age_days > LIVE_GATE_MAX_PAPER_EVIDENCE_AGE_DAYS:
                issues.append(
                    "paper evidence가 오래됨: "
                    f"{evidence_age_days}일 경과 "
                    f"(최대 {LIVE_GATE_MAX_PAPER_EVIDENCE_AGE_DAYS}일)."
                )
        if promotable_days < 60:
            issues.append("paper evidence 60영업일 미달.")
        if benchmark_final_ratio < 0.8:
            issues.append("paper evidence benchmark_final_ratio 80% 미달.")
        if paper_sharpe is None or paper_sharpe < 0.3:
            issues.append("paper evidence paper_sharpe 0.3 미달 또는 누락.")
        if same_excess <= 0:
            issues.append("paper evidence same-universe excess가 양수가 아님.")
        if cash_excess <= 0:
            issues.append("paper evidence cash-adjusted excess가 양수가 아님.")
        if cumulative_return <= 0:
            issues.append("paper evidence cumulative_return이 양수가 아님.")
        if sell_count < 5:
            issues.append("paper evidence sell_count 5건 미달.")
        if win_rate < 45:
            issues.append("paper evidence win_rate 45% 미달.")
        if frozen_days > 0:
            issues.append("paper evidence에 frozen day가 존재함.")
        issues.extend(
            _validate_paper_evidence_package_integrity(
                strategy_name,
                evidence,
                promotion_base=promotion_base,
                evidence_base=evidence_base,
                canonical_metadata=metadata,
            )
        )
        issues.extend(
            _validate_target_weight_evidence_summary(
                strategy_name,
                evidence,
                canonical_params_hash=_canonical_target_weight_params_hash(metadata, strategy_name),
                metadata=metadata,
            )
        )

    return issues
