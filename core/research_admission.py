"""Research evidence required for newly generated canonical promotion bundles.

Legacy bundles remain explicitly unverified; existing runtime authorizations and
basket policies are not retroactively changed by this research policy.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

RESEARCH_ADMISSION_POLICY = "required_v1"


def research_code_fingerprint(root: Path | None = None) -> str:
    """Bind research to file contents, including uncommitted source changes."""
    root = root or Path(__file__).resolve().parents[1]
    paths = [path for directory in ("core", "strategies", "backtest", "config")
             for path in (root / directory).rglob("*.py")]
    paths.extend(root / name for name in ("main.py", "tools/research_candidate_sweep.py", "tools/evaluate_and_promote.py"))
    material = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(set(paths)) if path.is_file()}
    return payload_hash(material)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                    default=str).encode("utf-8")).hexdigest()


def proof_from_sweep(bundle: dict) -> dict:
    """Retain auditable inputs, not a trusted copy of a report's PASS label."""
    holdout = bundle.get("oos_holdout") or {}
    selected = holdout.get("selected_candidate_id")
    candidates = [c for c in bundle.get("candidates", []) if c.get("candidate_id") == selected]
    proof = {key: bundle.get(key) for key in (
        "artifact_type", "schema_version", "run_id", "generated_at", "commit_hash",
        "config_yaml_hash", "config_resolved_hash", "eval_start", "eval_end", "universe",
        "universe_selection", "oos_holdout", "candidate_ids_evaluated", "initial_capital",
        "research_code_fingerprint",
    )}
    proof["candidate"] = candidates[0] if len(candidates) == 1 else None
    proof["source_payload_hash"] = payload_hash(bundle)
    proof["proof_hash"] = payload_hash(proof)
    return proof


def canonical_research_review(metadata: dict, strategy_name: str) -> dict:
    """Validate a candidate's lineage and replay evidence before new paper entry."""
    policy = metadata.get("research_admission_policy")
    if policy is None:
        return {"required": False, "status": "legacy_unverified", "issues": ["legacy_research_admission_missing"]}
    if policy != RESEARCH_ADMISSION_POLICY:
        return {"required": True, "status": "unavailable", "issues": ["unknown_research_admission_policy"]}
    proofs = metadata.get("research_admission_proofs") or {}
    proof = proofs.get(strategy_name) if isinstance(proofs, dict) else None
    if not isinstance(proof, dict):
        return {"required": True, "status": "unavailable", "issues": ["research_admission_proof_missing"]}
    if (any(proof.get(key) is not None and not isinstance(proof[key], dict)
            for key in ("universe_selection", "oos_holdout", "candidate"))
            or any(not isinstance(proof.get(key, []), list)
                   or not all(isinstance(x, str) for x in proof.get(key, []))
                   for key in ("universe", "candidate_ids_evaluated"))):
        return {"required": True, "status": "unavailable", "issues": ["research_proof_structure_invalid"]}
    issues = []
    material = {key: value for key, value in proof.items() if key != "proof_hash"}
    if proof.get("proof_hash") != payload_hash(material):
        issues.append("research_proof_hash_mismatch")
    if proof.get("artifact_type") != "research_candidate_sweep_bundle" or proof.get("schema_version") != 1:
        issues.append("research_proof_schema_invalid")
    for key in ("commit_hash", "config_yaml_hash", "config_resolved_hash", "eval_start", "eval_end"):
        if not proof.get(key) or proof.get(key) != metadata.get(key):
            issues.append(f"research_{key}_mismatch")
    if proof.get("commit_hash") in ("unknown", "dirty"):
        issues.append("research_commit_identity_unavailable")
    fingerprint = proof.get("research_code_fingerprint")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or fingerprint != metadata.get("research_code_fingerprint")):
        issues.append("research_code_fingerprint_mismatch")
    capital, canonical_capital = proof.get("initial_capital"), metadata.get("initial_capital")
    if (any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or x <= 0
            for x in (capital, canonical_capital)) or capital != canonical_capital):
        issues.append("research_initial_capital_mismatch")
    if not proof.get("universe") or sorted(proof["universe"]) != sorted(metadata.get("universe") or []):
        issues.append("research_universe_mismatch")
    if (proof.get("universe_selection") or {}).get("survivorship_controlled") is not True:
        issues.append("research_point_in_time_universe_unverified")
    holdout = proof.get("oos_holdout") or {}
    candidate = proof.get("candidate") or {}
    if holdout.get("selected_candidate_id") != strategy_name or candidate.get("candidate_id") != strategy_name:
        issues.append("research_train_selected_candidate_mismatch")
    specs = [s for s in (metadata.get("strategy_specs") or [])
             if isinstance(s, dict) and s.get("candidate_id") == strategy_name]
    if len(specs) != 1 or not isinstance(candidate.get("params"), dict):
        issues.append("research_candidate_spec_missing")
    elif (candidate.get("params") != specs[0].get("params")
          or candidate.get("strategy") != specs[0].get("base_strategy")):
        issues.append("research_candidate_parameters_mismatch")
    try:
        train_start, train_end = [date.fromisoformat(x) for x in holdout["selection_window"]]
        test_start, test_end = [date.fromisoformat(x) for x in holdout["holdout_window"]]
        if not (train_start <= train_end < test_start <= test_end
                and str(train_start) == metadata.get("eval_start")
                and str(test_end) == metadata.get("eval_end")):
            issues.append("research_windows_invalid")
    except (KeyError, TypeError, ValueError):
        issues.append("research_windows_missing")
        train_start = train_end = None
    if (holdout.get("status") != "ok" or holdout.get("selected_on") != "train_rank_score_only"
            or holdout.get("test_benchmark_coverage_complete") is not True):
        issues.append("research_holdout_unverified")
    for key in ("test_sharpe", "test_benchmark_excess_return", "test_benchmark_excess_sharpe"):
        value = holdout.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            issues.append(f"research_{key}_failed_or_missing")
    dsr = holdout.get("selected_train_deflated_sharpe") or {}
    if not isinstance(dsr, dict):
        dsr = {}
    value = dsr.get("dsr")
    if (dsr.get("status") != "observed" or dsr.get("passes") is not True
            or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0.95):
        issues.append("research_train_dsr_failed_or_unavailable")
    stats = holdout.get("selected_train_return_statistics") or {}
    multiple = holdout.get("train_multiple_testing") or {}
    if not isinstance(stats, dict):
        stats = {}
    if not isinstance(multiple, dict):
        multiple = {}
    try:
        from backtest.statistical_validation import deflated_sharpe_ratio
        first = date.fromisoformat(stats["first_return_date"])
        last = date.fromisoformat(stats["last_return_date"])
        if (train_start is None or train_end is None or not train_start <= first <= last <= train_end
                or stats.get("periods_per_year") != 252 or stats.get("kurtosis_convention") != "pearson"
                or stats.get("source") != "equity_curve.pct_change"):
            raise ValueError("return observations are not bound to training window")
        span = (last - first).days
        if span > 20000:
            raise ValueError("implausible daily training window")
        max_observations = sum((first + timedelta(days=offset)).weekday() < 5 for offset in range(span + 1))
        if int(stats["n_obs"]) > max_observations:
            raise ValueError("too many daily observations for training window")
        if round(float(stats["sharpe_annual"]), 2) != holdout.get("train_sharpe"):
            raise ValueError("training Sharpe disagrees with observed moments")
        raw_variance = float(multiple["sharpe_variance_across_trials_raw"])
        if (stats.get("status") != "observed" or int(stats["n_obs"]) < 4
                or int(stats["n_obs"]) != dsr.get("n_obs") or raw_variance < 0
                or not all(math.isfinite(float(x)) for x in (
                    stats["sharpe_annual"], stats["skew"], stats["kurtosis"], raw_variance))):
            raise ValueError("invalid observed moments")
        recomputed = deflated_sharpe_ratio(
            float(stats["sharpe_annual"]), int(stats["n_obs"]), int(dsr["n_trials"]), raw_variance,
            skew=float(stats["skew"]), kurtosis=float(stats["kurtosis"]),
        )
        if not recomputed["passes"] or recomputed["dsr"] != dsr.get("dsr"):
            issues.append("research_train_dsr_recalculation_failed")
    except (KeyError, TypeError, ValueError, OverflowError):
        issues.append("research_observed_return_statistics_missing_or_invalid")
    trial_ids = proof.get("candidate_ids_evaluated") or []
    if (strategy_name not in trial_ids or dsr.get("n_trials") != len(trial_ids)
            or holdout.get("trials") != len(trial_ids) or multiple.get("n_trials") != len(trial_ids)):
        issues.append("research_trial_count_mismatch")
    return {"required": True, "status": "passed" if not issues else "failed", "issues": issues,
            "source_run_id": proof.get("run_id"), "source_payload_hash": proof.get("source_payload_hash"),
            "limits": ["holdout_reuse_not_tracked", "current_sweep_trials_only"]}
