import json
from datetime import datetime, timedelta

from core.live_gate import (
    LIVE_GATE_ARTIFACT_TYPE,
    LIVE_GATE_SCHEMA_VERSION,
    validate_live_readiness,
)


class DummyConfig:
    yaml_hash = "yaml-ok"
    resolved_hash = "resolved-ok"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_bundle(
    promotion_dir,
    *,
    status="live_candidate",
    allowed_modes=None,
    generated_at=None,
    commit_hash="abc123",
    yaml_hash="yaml-ok",
    resolved_hash="resolved-ok",
    benchmark_excess=2.0,
    benchmark_excess_sharpe=0.2,
):
    allowed_modes = ["backtest", "paper", "live"] if allowed_modes is None else allowed_modes
    generated_at = generated_at or datetime(2026, 4, 29, 12, 0, 0).isoformat()

    _write_json(
        promotion_dir / "run_metadata.json",
        {
            "schema_version": LIVE_GATE_SCHEMA_VERSION,
            "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
            "commit_hash": commit_hash,
            "config_yaml_hash": yaml_hash,
            "config_resolved_hash": resolved_hash,
            "generated_at": generated_at,
        },
    )
    _write_json(
        promotion_dir / "metrics_summary.json",
        {"scoring": {"total_return": 12.0, "sharpe": 0.6, "profit_factor": 1.3}},
    )
    _write_json(
        promotion_dir / "walk_forward_summary.json",
        {"scoring": {"windows": 5, "positive": 4, "sharpe_pos": 4, "total_trades": 80}},
    )
    _write_json(
        promotion_dir / "benchmark_comparison.json",
        {
            "ew_bh_return": 8.0,
            "ew_bh_sharpe": 0.4,
            "strategy_excess_return_pct": {"scoring": benchmark_excess},
            "strategy_excess_sharpe": {"scoring": benchmark_excess_sharpe},
        },
    )
    _write_json(
        promotion_dir / "promotion_result.json",
        {"scoring": {"status": status, "allowed_modes": allowed_modes, "reason": "test"}},
    )


def _write_evidence(evidence_dir, **overrides):
    payload = {
        "strategy": "scoring",
        "recommendation": "ELIGIBLE",
        "promotable_evidence_days": 60,
        "benchmark_final_ratio": 0.9,
        "avg_same_universe_excess": 0.15,
        "avg_cash_adjusted_excess": 0.12,
        "cumulative_return": 3.2,
        "sell_count": 8,
        "win_rate": 55.0,
        "frozen_days": 0,
    }
    payload.update(overrides)
    _write_json(evidence_dir / "promotion_evidence_scoring.json", payload)


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
