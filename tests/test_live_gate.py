import json
from datetime import datetime, timedelta

from core.live_gate import (
    LIVE_GATE_ARTIFACT_TYPE,
    LIVE_GATE_SCHEMA_VERSION,
    validate_canonical_metadata_integrity,
    validate_live_readiness,
)


class DummyConfig:
    yaml_hash = "yaml-ok"
    resolved_hash = "resolved-ok"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_manifest(*, fetch_errors=None):
    from tools.evaluate_and_promote import build_data_snapshot_manifest

    return build_data_snapshot_manifest(
        provider="test-provider",
        universe_rule="테스트 유동성 상위 종목",
        eval_start="2025-01-01",
        eval_end="2025-12-31",
        universe_lookback_start="2024-10-01",
        universe_lookback_end="2024-12-31",
        universe=["005930", "000660"],
        liquidity_coverage={
            "005930": {"rows": 62, "start": "2024-10-01", "end": "2024-12-31"},
            "000660": {"rows": 61, "start": "2024-10-01", "end": "2024-12-31"},
        },
        benchmark_coverage={
            "005930": {"rows": 245, "start": "2025-01-01", "end": "2025-12-31"},
            "000660": {"rows": 245, "start": "2025-01-01", "end": "2025-12-31"},
        },
        fetch_errors=fetch_errors or {},
    )


def _write_bundle(
    promotion_dir,
    *,
    strategy="scoring",
    status="live_candidate",
    allowed_modes=None,
    generated_at=None,
    commit_hash="abc123",
    yaml_hash="yaml-ok",
    resolved_hash="resolved-ok",
    benchmark_excess=2.0,
    benchmark_excess_sharpe=0.2,
    snapshot_manifest=None,
    evaluation_errors=None,
    walk_forward_errors=None,
    metric_overrides=None,
    strategy_specs=None,
):
    allowed_modes = ["backtest", "paper", "live"] if allowed_modes is None else allowed_modes
    generated_at = generated_at or datetime(2026, 4, 29, 12, 0, 0).isoformat()
    snapshot_manifest = snapshot_manifest or _snapshot_manifest()

    metadata = {
        "schema_version": LIVE_GATE_SCHEMA_VERSION,
        "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
        "commit_hash": commit_hash,
        "config_yaml_hash": yaml_hash,
        "config_resolved_hash": resolved_hash,
        "generated_at": generated_at,
        "data_snapshot_hash": snapshot_manifest["data_snapshot_hash"],
        "data_snapshot_manifest": snapshot_manifest,
        "evaluation_errors": evaluation_errors or {},
        "walk_forward_errors": walk_forward_errors or {},
    }
    if strategy_specs is not None:
        metadata["strategy_specs"] = strategy_specs
    elif strategy.startswith("target_weight_"):
        metadata["strategy_specs"] = [{"candidate_id": strategy, "params_hash": "hash"}]
    _write_json(promotion_dir / "run_metadata.json", metadata)
    metrics = {"total_return": 12.0, "sharpe": 0.6, "profit_factor": 1.3}
    metrics.update(metric_overrides or {})
    _write_json(
        promotion_dir / "metrics_summary.json",
        {strategy: metrics},
    )
    _write_json(
        promotion_dir / "walk_forward_summary.json",
        {strategy: {"windows": 5, "positive": 4, "sharpe_pos": 4, "total_trades": 80}},
    )
    _write_json(
        promotion_dir / "benchmark_comparison.json",
        {
            "ew_bh_return": 8.0,
            "ew_bh_sharpe": 0.4,
            "strategy_excess_return_pct": {strategy: benchmark_excess},
            "strategy_excess_sharpe": {strategy: benchmark_excess_sharpe},
        },
    )
    _write_json(
        promotion_dir / "promotion_result.json",
        {strategy: {"status": status, "allowed_modes": allowed_modes, "reason": "test"}},
    )


def _write_evidence(evidence_dir, *, strategy="scoring", **overrides):
    payload = {
        "strategy": strategy,
        "recommendation": "ELIGIBLE",
        "promotable_evidence_days": 60,
        "benchmark_final_ratio": 0.9,
        "paper_sharpe": 0.5,
        "avg_same_universe_excess": 0.15,
        "avg_cash_adjusted_excess": 0.12,
        "cumulative_return": 3.2,
        "sell_count": 8,
        "win_rate": 55.0,
        "frozen_days": 0,
    }
    payload.update(overrides)
    _write_json(evidence_dir / f"promotion_evidence_{strategy}.json", payload)


def test_legacy_walkforward_file_does_not_unlock_live(tmp_path):
    reports_dir = tmp_path / "reports"
    _write_json(
        reports_dir / "validation_walkforward_20260429_120000_scoring.json",
        {"strategy": "scoring", "wf_passed": True},
    )

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=reports_dir / "promotion",
        evidence_dir=reports_dir / "paper_evidence",
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert issues
    assert any("승인 파일 없음" in issue for issue in issues)
    assert any("canonical promotion bundle" in issue for issue in issues)


def test_provisional_candidate_cannot_pass_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir, status="provisional_paper_candidate", allowed_modes=["backtest", "paper"])
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("live_candidate가 아님" in issue for issue in issues)


def test_commit_and_config_mismatch_block_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir, commit_hash="old999", yaml_hash="old-yaml", resolved_hash="old-resolved")
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("commit_hash 불일치" in issue for issue in issues)
    assert any("config_yaml_hash 불일치" in issue for issue in issues)
    assert any("config_resolved_hash 불일치" in issue for issue in issues)


def test_stale_artifact_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    generated_at = (datetime(2026, 4, 29, 12, 0, 0) - timedelta(days=8)).isoformat()
    _write_bundle(promotion_dir, generated_at=generated_at)
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("오래됨" in issue for issue in issues)


def test_data_snapshot_hash_required_for_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)

    metadata_path = promotion_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("data_snapshot_hash")
    metadata.pop("data_snapshot_manifest")
    _write_json(metadata_path, metadata)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("data_snapshot_hash" in issue for issue in issues)
    assert any("data_snapshot_manifest" in issue for issue in issues)


def test_data_snapshot_manifest_hash_mismatch_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    manifest = _snapshot_manifest()
    manifest["benchmark_coverage"]["005930"]["rows"] = 0
    _write_bundle(promotion_dir, snapshot_manifest=manifest)
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("재계산 hash 불일치" in issue for issue in issues)
    assert any("벤치마크 coverage rows" in issue for issue in issues)


def test_data_snapshot_fetch_errors_block_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(
        promotion_dir,
        snapshot_manifest=_snapshot_manifest(
            fetch_errors={
                "005930": {
                    "stage": "benchmark",
                    "error_type": "RuntimeError",
                    "error": "provider unavailable",
                }
            }
        ),
    )
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("data snapshot 수집 오류" in issue for issue in issues)


def test_non_universe_liquidity_fetch_errors_do_not_block_metadata_integrity():
    manifest = _snapshot_manifest(
        fetch_errors={
            "liquidity:0126Z0": {
                "stage": "universe_liquidity",
                "error_type": "DataCollectionError",
                "error": "provider unavailable",
            }
        }
    )
    metadata = {
        "data_snapshot_hash": manifest["data_snapshot_hash"],
        "data_snapshot_manifest": manifest,
        "evaluation_errors": {},
        "walk_forward_errors": {},
    }

    issues = validate_canonical_metadata_integrity(metadata)

    assert not any("data snapshot 수집 오류" in issue for issue in issues)


def test_universe_liquidity_fetch_errors_block_metadata_integrity():
    manifest = _snapshot_manifest(
        fetch_errors={
            "liquidity:005930": {
                "stage": "universe_liquidity",
                "error_type": "DataCollectionError",
                "error": "provider unavailable",
            }
        }
    )
    metadata = {
        "data_snapshot_hash": manifest["data_snapshot_hash"],
        "data_snapshot_manifest": manifest,
        "evaluation_errors": {},
        "walk_forward_errors": {},
    }

    issues = validate_canonical_metadata_integrity(metadata)

    assert any("data snapshot 수집 오류" in issue for issue in issues)
    assert any("liquidity:005930" in issue for issue in issues)


def test_failed_canonical_metric_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(
        promotion_dir,
        metric_overrides={
            "evaluation_status": "failed",
            "evaluation_stage": "full_period",
            "evaluation_error_type": "RuntimeError",
            "error": "provider unavailable",
        },
    )
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("canonical 평가 실패" in issue for issue in issues)


def test_benchmark_excess_must_be_positive_and_present(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir, benchmark_excess=0.0, benchmark_excess_sharpe=-0.1)
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("초과수익" in issue for issue in issues)
    assert any("excess Sharpe" in issue for issue in issues)


def test_paper_evidence_must_be_eligible(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(
        evidence_dir,
        recommendation="BLOCKED",
        block_reasons=["non_positive_same_universe_excess=-0.1"],
        promotable_evidence_days=42,
        avg_same_universe_excess=-0.1,
    )

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("recommendation=BLOCKED" in issue for issue in issues)
    assert any("60영업일 미달" in issue for issue in issues)
    assert any("same-universe excess" in issue for issue in issues)


def test_valid_canonical_bundle_and_paper_evidence_pass(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert issues == []


def test_target_weight_live_gate_requires_verified_pilot_evidence(tmp_path):
    strategy = "target_weight_rotation_test"
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir, strategy=strategy)
    _write_evidence(
        evidence_dir,
        strategy=strategy,
        target_weight_evidence={
            "required": True,
            "valid_pilot_days": 59,
            "invalid_days": 1,
            "invalid_reasons": {"missing_target_weight_execution": 1},
            "all_promotable_days_verified": False,
        },
    )

    issues = validate_live_readiness(
        DummyConfig(),
        strategy,
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("target-weight promotable evidence" in issue for issue in issues)
    assert any("target-weight invalid execution evidence" in issue for issue in issues)


def test_target_weight_live_gate_accepts_verified_pilot_evidence(tmp_path):
    strategy = "target_weight_rotation_test"
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir, strategy=strategy)
    _write_evidence(
        evidence_dir,
        strategy=strategy,
        target_weight_evidence={
            "required": True,
            "valid_pilot_days": 60,
            "invalid_days": 0,
            "invalid_reasons": {},
            "params_hash": "hash",
            "params_hashes": ["hash"],
            "params_hash_consistent": True,
            "all_promotable_days_verified": True,
        },
        target_weight_verified_pilot_days=60,
        target_weight_invalid_days=0,
        target_weight_params_hash="hash",
    )

    issues = validate_live_readiness(
        DummyConfig(),
        strategy,
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert issues == []


def test_target_weight_live_gate_blocks_params_hash_mismatch(tmp_path):
    strategy = "target_weight_rotation_test"
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(
        promotion_dir,
        strategy=strategy,
        strategy_specs=[{"candidate_id": strategy, "params_hash": "canonical-hash"}],
    )
    _write_evidence(
        evidence_dir,
        strategy=strategy,
        target_weight_evidence={
            "required": True,
            "valid_pilot_days": 60,
            "invalid_days": 0,
            "invalid_reasons": {},
            "params_hash": "evidence-hash",
            "params_hashes": ["evidence-hash"],
            "params_hash_consistent": True,
            "all_promotable_days_verified": True,
        },
        target_weight_verified_pilot_days=60,
        target_weight_invalid_days=0,
        target_weight_params_hash="evidence-hash",
    )

    issues = validate_live_readiness(
        DummyConfig(),
        strategy,
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("target-weight canonical params_hash 불일치" in issue for issue in issues)
