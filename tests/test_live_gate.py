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
    reason=None,
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
    metrics = {
        "total_return": 12.0,
        "sharpe": 0.6,
        "profit_factor": 1.3,
        "mdd": -8.0,
        "wf_positive_rate": 0.8,
        "wf_sharpe_positive_rate": 0.8,
        "wf_windows": 5,
        "wf_total_trades": 80,
        "benchmark_excess_return": benchmark_excess,
        "benchmark_excess_sharpe": benchmark_excess_sharpe,
        "ev_per_trade": 1000.0,
        "cost_adjusted_cagr": 6.0,
        "turnover_per_year": 350.0,
    }
    metrics.update(metric_overrides or {})
    metrics_by_strategy = {strategy: metrics}
    _write_json(
        promotion_dir / "metrics_summary.json",
        metrics_by_strategy,
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
    if reason is None:
        reason = "live_candidate 충족" if status == "live_candidate" else "test"
    promotions = {strategy: {"status": status, "allowed_modes": allowed_modes, "reason": reason}}
    _write_json(promotion_dir / "promotion_result.json", promotions)

    from tools.evaluate_and_promote import (
        build_current_blockers_report,
        build_promotion_blocker_summary,
    )

    blocker_summary = build_promotion_blocker_summary(promotions, metrics_by_strategy, metadata)
    _write_json(promotion_dir / "promotion_blocker_summary.json", blocker_summary)
    _write_json(
        promotion_dir.parent / "current_blockers.json",
        build_current_blockers_report(blocker_summary),
    )


def _write_evidence(evidence_dir, *, strategy="scoring", **overrides):
    payload = {
        "strategy": strategy,
        "period": "2026-02-01 ~ 2026-04-29",
        "latest_evidence_date": "2026-04-29",
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
        "trade_quality": {"status": "ok"},
    }
    payload.update(overrides)
    seed_records = payload.pop("_seed_records", True)
    with_integrity = payload.pop("_with_integrity", True)
    if seed_records:
        latest = payload.get("latest_evidence_date") or "2026-04-29"
        try:
            latest_dt = datetime.strptime(str(latest)[:10], "%Y-%m-%d")
        except ValueError:
            latest_dt = datetime(2026, 4, 29)
        count = int(payload.get("promotable_evidence_days") or 0)
        if count <= 0:
            count = 60
        start_dt = latest_dt - timedelta(days=count - 1)
        payload.setdefault("earliest_evidence_date", start_dt.strftime("%Y-%m-%d"))
        jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        target_summary = payload.get("target_weight_evidence") or {}
        use_pilot = target_summary.get("all_promotable_days_verified") is True
        params_hash = (
            payload.get("target_weight_params_hash")
            or target_summary.get("params_hash")
            or "hash"
        )
        rows = []
        for idx in range(count):
            day = start_dt + timedelta(days=idx)
            record = {
                "date": day.strftime("%Y-%m-%d"),
                "day_number": idx + 1,
                "strategy": strategy,
                "execution_backed": True,
                "evidence_mode": "pilot_paper" if use_pilot else "real_paper",
                "session_mode": "pilot_paper" if use_pilot else "normal_paper",
                "pilot_authorized": use_pilot,
                "daily_return": 0.1,
                "cumulative_return": payload.get("cumulative_return", 3.2),
                "mdd": -2.0,
                "total_trades": 2,
                "sell_count": 1,
                "winning_trades": 1,
                "losing_trades": 0,
                "same_universe_excess": payload.get("avg_same_universe_excess", 0.15),
                "exposure_matched_excess": payload.get("avg_same_universe_excess", 0.15),
                "cash_adjusted_excess": payload.get("avg_cash_adjusted_excess", 0.12),
                "benchmark_status": "final",
                "status": "normal",
                "anomalies": [],
            }
            if use_pilot:
                record["pilot_caps_snapshot"] = {
                    "target_weight_plan": {
                        "candidate_id": strategy,
                        "trade_day": record["date"],
                        "params_hash": params_hash,
                    },
                    "target_weight_execution": {
                        "params_hash": params_hash,
                        "complete": True,
                        "execution_trade_day_allowed": True,
                        "execution_market_session_allowed": True,
                        "pilot_authorization_snapshot_allowed": True,
                        "pre_execution_complete": True,
                        "liquidity_complete": True,
                        "pre_trade_risk_complete": True,
                        "order_count_complete": True,
                        "order_result_complete": True,
                        "order_complete": True,
                        "order_result_reconciliation": {"complete": True},
                        "fill_complete": True,
                        "fill_reconciliation": {"complete": True},
                        "position_reconciliation": {"complete": True},
                    },
                }
            rows.append(json.dumps(record, ensure_ascii=False))
        jsonl_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if with_integrity:
        from core.paper_evidence import (
            build_promotion_source_records_summary,
            compute_promotion_package_integrity_hash,
        )

        payload["source_records"] = build_promotion_source_records_summary(
            strategy,
            promotion_dir=evidence_dir.parent / "promotion",
            evidence_dir=evidence_dir,
        )
        payload["package_integrity"] = {
            "schema_version": 1,
            "payload_hash": compute_promotion_package_integrity_hash(payload),
        }
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


def test_live_gate_recomputes_promotion_status_from_artifact_metrics(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(
        promotion_dir,
        status="live_candidate",
        allowed_modes=["backtest", "paper", "live"],
        metric_overrides={"mdd": -25.0},
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

    assert any("promotion 재계산 결과 live_candidate가 아님" in issue for issue in issues)
    assert any("MDD -25.0% < -20%" in issue for issue in issues)


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


def test_paper_evidence_package_integrity_must_match_payload(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    evidence_path = evidence_dir / "promotion_evidence_scoring.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["avg_same_universe_excess"] = 9.99
    _write_json(evidence_path, payload)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("package_integrity payload_hash 불일치" in issue for issue in issues)


def test_paper_evidence_source_records_must_match_daily_jsonl(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    evidence_path = evidence_dir / "promotion_evidence_scoring.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["source_records"]["records_hash"] = "bad-hash"
    from core.paper_evidence import compute_promotion_package_integrity_hash

    payload["package_integrity"]["payload_hash"] = compute_promotion_package_integrity_hash(payload)
    _write_json(evidence_path, payload)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("source_records 불일치" in issue for issue in issues)


def test_paper_evidence_headline_summary_must_match_source_records(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    evidence_path = evidence_dir / "promotion_evidence_scoring.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    source_count = payload["source_records"]["record_count"]
    payload["promotable_evidence_days"] = source_count + 1
    payload["real_paper_days"] = source_count + 1
    payload["real_paper_days_total"] = source_count + 1
    payload["earliest_evidence_date"] = "2026-01-01"
    from core.paper_evidence import compute_promotion_package_integrity_hash

    payload["package_integrity"]["payload_hash"] = compute_promotion_package_integrity_hash(
        payload
    )
    _write_json(evidence_path, payload)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any(
        "promotable_evidence_days와 source_records record_count 불일치" in issue
        for issue in issues
    )
    assert any(
        "earliest_evidence_date와 source_records first_date 불일치" in issue
        for issue in issues
    )


def test_paper_evidence_latest_date_must_be_fresh(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(
        evidence_dir,
        period="2026-02-01 ~ 2026-04-01",
        latest_evidence_date="2026-04-01",
    )

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("paper evidence가 오래됨" in issue for issue in issues)


def test_paper_evidence_latest_date_is_required(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(
        evidence_dir,
        period="",
        latest_evidence_date=None,
    )

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("latest_evidence_date 누락" in issue for issue in issues)


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


def test_current_blockers_no_go_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    current_path = promotion_dir.parent / "current_blockers.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["go_live"] = False
    current["verdict"] = "NO-GO: manual test blocker"
    current["live_candidates"] = []
    current["hard_blockers"] = [{"desc": "test hard blocker"}]
    _write_json(current_path, current)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("current_blockers.go_live" in issue for issue in issues)
    assert any("current_blockers live_candidates" in issue for issue in issues)
    assert any("current_blockers hard_blockers" in issue for issue in issues)


def test_stale_current_blockers_source_hash_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    current_path = promotion_dir.parent / "current_blockers.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["source_artifact_hash"] = "bad-hash"
    _write_json(current_path, current)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("current_blockers source_artifact_hash 불일치" in issue for issue in issues)


def test_stale_promotion_blocker_summary_hash_blocks_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    summary_path = promotion_dir / "promotion_blocker_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["source_artifact_hash"] = "bad-hash"
    _write_json(summary_path, summary)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("promotion blocker summary source_artifact_hash 불일치" in issue for issue in issues)


def test_stale_promotion_blocker_summary_strategy_actions_block_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    summary_path = promotion_dir / "promotion_blocker_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["strategies"]["scoring"]["next_action"] = "stale operator hint"
    _write_json(summary_path, summary)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("promotion blocker summary strategies 내용 불일치" in issue for issue in issues)


def test_stale_current_blockers_next_actions_block_live_gate(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    current_path = promotion_dir.parent / "current_blockers.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["next_actions"] = [{"priority": 1, "desc": "stale action"}]
    _write_json(current_path, current)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("current_blockers next_actions 내용 불일치" in issue for issue in issues)


def test_current_blockers_uses_recalculated_full_promotion_bundle(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)

    other_strategy = "manual_live_overlay"
    metrics_path = promotion_dir / "metrics_summary.json"
    wf_path = promotion_dir / "walk_forward_summary.json"
    benchmark_path = promotion_dir / "benchmark_comparison.json"
    promotion_path = promotion_dir / "promotion_result.json"
    metadata_path = promotion_dir / "run_metadata.json"

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics[other_strategy] = dict(metrics["scoring"])
    _write_json(metrics_path, metrics)

    wf = json.loads(wf_path.read_text(encoding="utf-8"))
    wf[other_strategy] = dict(wf["scoring"])
    _write_json(wf_path, wf)

    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark["strategy_excess_return_pct"][other_strategy] = benchmark["strategy_excess_return_pct"]["scoring"]
    benchmark["strategy_excess_sharpe"][other_strategy] = benchmark["strategy_excess_sharpe"]["scoring"]
    _write_json(benchmark_path, benchmark)

    promotions = json.loads(promotion_path.read_text(encoding="utf-8"))
    promotions[other_strategy] = {
        "status": "live_candidate",
        "allowed_modes": ["backtest", "paper", "live"],
        "reason": "live_candidate 충족",
    }
    _write_json(promotion_path, promotions)

    from tools.evaluate_and_promote import (
        build_current_blockers_report,
        build_promotion_blocker_summary,
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    stale_summary = build_promotion_blocker_summary(promotions, metrics, metadata)
    _write_json(promotion_dir / "promotion_blocker_summary.json", stale_summary)
    _write_json(
        promotion_dir.parent / "current_blockers.json",
        build_current_blockers_report(stale_summary),
    )

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any(
        f"promotion_result {other_strategy}.status 재계산 결과 불일치" in issue
        for issue in issues
    )
    assert any("promotion blocker summary source_artifact_hash 불일치" in issue for issue in issues)
    assert any("current_blockers live_candidates 내용 불일치" in issue for issue in issues)


def test_paper_evidence_strategy_is_required(tmp_path):
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(promotion_dir)
    _write_evidence(evidence_dir)
    evidence_path = evidence_dir / "promotion_evidence_scoring.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload.pop("strategy")
    _write_json(evidence_path, payload)

    issues = validate_live_readiness(
        DummyConfig(),
        "scoring",
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("paper evidence strategy 누락" in issue for issue in issues)


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


def test_target_weight_live_gate_uses_metadata_for_strategy_identity(tmp_path):
    strategy = "rotation_candidate_test"
    promotion_dir = tmp_path / "reports" / "promotion"
    evidence_dir = tmp_path / "reports" / "paper_evidence"
    _write_bundle(
        promotion_dir,
        strategy=strategy,
        strategy_specs=[{
            "candidate_id": strategy,
            "base_strategy": "target_weight_rotation",
            "params_hash": "hash",
        }],
    )
    _write_evidence(evidence_dir, strategy=strategy)

    issues = validate_live_readiness(
        DummyConfig(),
        strategy,
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 29, 12, 0, 0),
    )

    assert any("promotion 재계산 결과 live_candidate가 아님" in issue for issue in issues)
    assert any("target-weight paper evidence proof summary 누락" in issue for issue in issues)


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
