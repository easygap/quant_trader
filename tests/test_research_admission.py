import copy
import json

import pytest

from core.research_admission import (
    RESEARCH_ADMISSION_POLICY, canonical_research_review, proof_from_sweep,
)


def evidence():
    from backtest.statistical_validation import deflated_sharpe_ratio
    dsr = deflated_sharpe_ratio(2.0, 500, 2, 0.01, skew=0.2, kurtosis=4.0)
    dsr["status"] = "observed"
    bundle = {
        "schema_version": 1, "artifact_type": "research_candidate_sweep_bundle",
        "run_id": "test", "generated_at": "2026-10-13T11:00:00", "commit_hash": "abc",
        "config_yaml_hash": "yaml", "config_resolved_hash": "resolved",
        "initial_capital": 10_000_000, "research_code_fingerprint": "a" * 64,
        "eval_start": "2023-01-01", "eval_end": "2025-12-31", "universe": ["005930", "000660"],
        "universe_selection": {"survivorship_controlled": True},
        "candidate_ids_evaluated": ["winner", "loser"],
        "candidates": [{"candidate_id": "winner", "strategy": "target_weight_rotation", "params": {"lookback": 60}}],
        "oos_holdout": {
            "status": "ok", "selected_candidate_id": "winner", "selected_on": "train_rank_score_only",
            "selection_window": ["2023-01-01", "2024-12-31"],
            "holdout_window": ["2025-01-01", "2025-12-31"], "trials": 2,
            "train_sharpe": 2.0, "test_sharpe": 1.1, "test_benchmark_excess_return": 3.0, "test_benchmark_excess_sharpe": 0.2,
            "test_benchmark_coverage_complete": True,
            "selected_train_deflated_sharpe": dsr,
            "selected_train_return_statistics": {"status": "observed", "n_obs": 500, "sharpe_annual": 2.0, "skew": 0.2, "kurtosis": 4.0,
                "first_return_date": "2023-01-03", "last_return_date": "2024-12-31", "periods_per_year": 252,
                "kurtosis_convention": "pearson", "source": "equity_curve.pct_change"},
            "train_multiple_testing": {"n_trials": 2, "sharpe_variance_across_trials_raw": 0.01},
        },
    }
    metadata = {k: bundle[k] for k in ("commit_hash", "config_yaml_hash", "config_resolved_hash", "eval_start", "eval_end", "universe", "initial_capital", "research_code_fingerprint")}
    metadata.update({"research_admission_policy": RESEARCH_ADMISSION_POLICY,
                     "strategy_specs": [{"candidate_id": "winner", "base_strategy": "target_weight_rotation", "params": {"lookback": 60}}],
                     "research_admission_proofs": {"winner": proof_from_sweep(bundle)}})
    return bundle, metadata


def test_proof_binds_candidate_and_config_instead_of_trusting_pass_labels():
    _, metadata = evidence()
    assert canonical_research_review(metadata, "winner")["status"] == "passed"
    assert canonical_research_review(metadata, "loser")["status"] == "unavailable"
    metadata["config_resolved_hash"] = "changed"
    assert "research_config_resolved_hash_mismatch" in canonical_research_review(metadata, "winner")["issues"]


@pytest.mark.parametrize("change,expected", [
    (lambda b: b["oos_holdout"].update(test_benchmark_excess_return=-1), "research_test_benchmark_excess_return_failed_or_missing"),
    (lambda b: b["oos_holdout"]["selected_train_deflated_sharpe"].update(status="approximate"), "research_train_dsr_failed_or_unavailable"),
    (lambda b: b["universe_selection"].update(survivorship_controlled=False), "research_point_in_time_universe_unverified"),
    (lambda b: b["oos_holdout"].update(selection_window=["2023-01-01", "2025-02-01"]), "research_windows_invalid"),
    (lambda b: b["candidates"][0]["params"].update(lookback=120), "research_candidate_parameters_mismatch"),
    (lambda b: b["oos_holdout"]["selected_train_deflated_sharpe"].update(n_trials=1), "research_trial_count_mismatch"),
    (lambda b: b.update(initial_capital=300_000), "research_initial_capital_mismatch"),
    (lambda b: b.pop("initial_capital"), "research_initial_capital_mismatch"),
    (lambda b: b.update(initial_capital=float("nan")), "research_initial_capital_mismatch"),
    (lambda b: b.update(research_code_fingerprint="b" * 64), "research_code_fingerprint_mismatch"),
    (lambda b: b["oos_holdout"]["selected_train_return_statistics"].update(first_return_date="2025-01-02", last_return_date="2025-12-31"), "research_observed_return_statistics_missing_or_invalid"),
    (lambda b: b["oos_holdout"]["selected_train_return_statistics"].update(periods_per_year=365), "research_observed_return_statistics_missing_or_invalid"),
    (lambda b: b["oos_holdout"]["selected_train_return_statistics"].update(kurtosis_convention="fisher"), "research_observed_return_statistics_missing_or_invalid"),
    (lambda b: b["oos_holdout"]["selected_train_return_statistics"].update(n_obs=750), "research_observed_return_statistics_missing_or_invalid"),
    (lambda b: b["oos_holdout"].update(train_sharpe=3.0), "research_observed_return_statistics_missing_or_invalid"),
])
def test_invalid_evidence_cannot_pass_even_with_fresh_hash(change, expected):
    bundle, metadata = evidence()
    change(bundle)
    metadata["research_admission_proofs"]["winner"] = proof_from_sweep(bundle)
    result = canonical_research_review(metadata, "winner")
    assert result["status"] == "failed"
    assert expected in result["issues"]


def test_proof_mutation_is_detected_and_legacy_authorization_is_not_rewritten():
    _, metadata = evidence()
    metadata["research_admission_proofs"]["winner"]["oos_holdout"]["test_sharpe"] = 999
    assert "research_proof_hash_mismatch" in canonical_research_review(metadata, "winner")["issues"]
    legacy = canonical_research_review({}, "old_strategy")
    assert legacy["required"] is False
    assert legacy["status"] == "legacy_unverified"


def test_unknown_commit_is_not_a_valid_matching_identity():
    bundle, metadata = evidence()
    bundle["commit_hash"] = metadata["commit_hash"] = "unknown"
    metadata["research_admission_proofs"]["winner"] = proof_from_sweep(bundle)
    assert "research_commit_identity_unavailable" in canonical_research_review(metadata, "winner")["issues"]


def test_code_fingerprint_detects_uncommitted_file_changes(tmp_path):
    from core.research_admission import research_code_fingerprint
    (tmp_path / "strategies").mkdir()
    source = tmp_path / "strategies" / "uncommitted_strategy.py"
    source.write_text("threshold = 10\n", encoding="utf-8")
    before = research_code_fingerprint(tmp_path)
    source.write_text("threshold = 20\n", encoding="utf-8")
    assert research_code_fingerprint(tmp_path) != before


def test_new_canonical_build_and_artifact_reload_both_enforce_admission(tmp_path, monkeypatch):
    from tools.evaluate_and_promote import build_promotion_results, build_data_snapshot_manifest
    from core.promotion_engine import load_metrics_from_artifact, promote
    from core.live_gate import LIVE_GATE_ARTIFACT_TYPE, LIVE_GATE_SCHEMA_VERSION
    manifest = build_data_snapshot_manifest(
        provider="test", universe_rule="test", eval_start="2023-01-01", eval_end="2025-12-31",
        universe_lookback_start="2022-01-01", universe_lookback_end="2022-12-31",
        universe=["005930"], liquidity_coverage={"005930": {"rows": 252}},
        benchmark_coverage={"005930": {"rows": 750}}, fetch_errors={},
    )
    metrics = {"scoring": {"total_return": 18, "profit_factor": 1.5, "mdd": -8,
               "wf_positive_rate": 0.8, "wf_sharpe_positive_rate": 0.8, "wf_windows": 5,
               "wf_total_trades": 120, "sharpe": 0.7, "benchmark_excess_return": 1.0,
               "benchmark_excess_sharpe": 0.2}}
    metadata = {"schema_version": LIVE_GATE_SCHEMA_VERSION, "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
                "data_snapshot_hash": manifest["data_snapshot_hash"], "data_snapshot_manifest": manifest,
                "evaluation_errors": {}, "walk_forward_errors": {},
                "research_admission_policy": RESEARCH_ADMISSION_POLICY}
    promotions = build_promotion_results(metrics, evidence_dir=str(tmp_path / "no_evidence"), canonical_metadata=metadata)
    assert promotions["scoring"]["status"] == "research_only"
    assert "research_admission_proof_missing" in promotions["scoring"]["reason"]
    artifacts = {"run_metadata.json": metadata, "metrics_summary.json": metrics,
                 "walk_forward_summary.json": {"scoring": {"windows": 5, "positive": 4, "sharpe_pos": 4, "total_trades": 120}},
                 "benchmark_comparison.json": {"strategy_excess_return_pct": {"scoring": 1.0}, "strategy_excess_sharpe": {"scoring": 0.2}},
                 "promotion_result.json": promotions}
    for name, value in artifacts.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    loaded = load_metrics_from_artifact(str(tmp_path), evidence_dir=str(tmp_path / "no_evidence"))
    assert loaded["scoring"].research_admission_required is True
    assert promote(loaded["scoring"]).status == "research_only"
    metadata.pop("research_admission_policy")
    legacy = build_promotion_results(copy.deepcopy(metrics), evidence_dir=str(tmp_path / "no_evidence"), canonical_metadata=metadata)
    assert legacy["scoring"]["status"] == "provisional_paper_candidate"
