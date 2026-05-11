import json

import pandas as pd
import pytest


def test_build_canonical_research_candidate_specs_selects_risk_overlay():
    from tools.evaluate_and_promote import build_canonical_research_candidate_specs

    specs = build_canonical_research_candidate_specs()

    assert [spec.candidate_id for spec in specs] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35"
    ]
    assert specs[0].strategy == "target_weight_rotation"
    assert specs[0].params["market_exposure_mode"] == "benchmark_risk"
    assert specs[0].params["bear_target_exposure"] == 0.35


def test_canonical_research_candidate_metadata_hashes_params():
    from tools.evaluate_and_promote import (
        build_canonical_research_candidate_specs,
        canonical_research_candidate_metadata,
    )

    spec = build_canonical_research_candidate_specs()[0]
    metadata = canonical_research_candidate_metadata(spec)

    assert metadata["candidate_id"] == spec.candidate_id
    assert metadata["base_strategy"] == "target_weight_rotation"
    assert metadata["candidate_source"] == "canonicalized_research_candidate"
    assert metadata["params"] == spec.params
    assert len(metadata["params_hash"]) == 64

    mutated = canonical_research_candidate_metadata(
        type(spec)(
            spec.candidate_id,
            spec.strategy,
            {**spec.params, "bear_target_exposure": 0.45},
            spec.description,
        )
    )
    assert mutated["params_hash"] != metadata["params_hash"]


def test_run_canonical_research_candidate_dispatches_target_weight_runner():
    from tools.evaluate_and_promote import (
        build_canonical_research_candidate_specs,
        run_canonical_research_candidate,
    )

    calls = {}

    def fake_runner(**kwargs):
        calls.update(kwargs)
        return {"equity_curve": pd.DataFrame(), "trades": []}

    spec = build_canonical_research_candidate_specs()[0]
    result = run_canonical_research_candidate(
        spec,
        ["005930", "000660"],
        100_000,
        "2025-01-01",
        "2025-01-31",
        runner=fake_runner,
    )

    assert result["trades"] == []
    assert calls["symbols"] == ["005930", "000660"]
    assert calls["capital"] == 100_000
    assert calls["params"] == spec.params


def test_run_canonical_research_candidate_rejects_unsupported_strategy():
    from tools.evaluate_and_promote import run_canonical_research_candidate
    from tools.research_candidate_sweep import CandidateSpec

    with pytest.raises(ValueError, match="unsupported canonical research candidate"):
        run_canonical_research_candidate(
            CandidateSpec("bad", "momentum_factor", {}, "bad"),
            ["005930"],
            100_000,
            "2025-01-01",
            "2025-01-31",
            runner=lambda **_: {},
        )


def test_stable_payload_hash_is_order_independent():
    from tools.evaluate_and_promote import stable_payload_hash

    left = {"b": [2, 1], "a": {"x": 1, "y": 2}}
    right = {"a": {"y": 2, "x": 1}, "b": [2, 1]}

    assert stable_payload_hash(left) == stable_payload_hash(right)
    assert stable_payload_hash(left) != stable_payload_hash({"a": {"x": 1, "y": 3}, "b": [2, 1]})


def test_summarize_ohlcv_frame_records_deterministic_coverage():
    from tools.evaluate_and_promote import summarize_ohlcv_frame

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-03", "2025-01-02"]),
            "close": [20.0, 10.0],
            "volume": [200, 100],
        }
    )

    summary = summarize_ohlcv_frame(df)

    assert summary["rows"] == 2
    assert summary["start"] == "2025-01-02"
    assert summary["end"] == "2025-01-03"
    assert summary["first_close"] == 10.0
    assert summary["last_close"] == 20.0
    assert summary["close_non_null"] == 2
    assert summary["volume_non_null"] == 2


def test_build_data_snapshot_manifest_hash_changes_with_coverage():
    from tools.evaluate_and_promote import build_data_snapshot_manifest

    base_kwargs = {
        "provider": "test-provider",
        "universe_rule": "top liquidity",
        "eval_start": "2025-01-01",
        "eval_end": "2025-12-31",
        "universe_lookback_start": "2024-10-01",
        "universe_lookback_end": "2024-12-31",
        "universe": ["005930", "000660"],
        "liquidity_coverage": {
            "000660": {"rows": 61, "start": "2024-10-01", "end": "2024-12-31"},
            "005930": {"rows": 62, "start": "2024-10-01", "end": "2024-12-31"},
        },
        "benchmark_coverage": {
            "005930": {"rows": 700, "start": "2025-01-01", "end": "2025-12-31"},
            "000660": {"rows": 700, "start": "2025-01-01", "end": "2025-12-31"},
        },
        "fetch_errors": {},
    }

    first = build_data_snapshot_manifest(**base_kwargs)
    reordered = build_data_snapshot_manifest(
        **{
            **base_kwargs,
            "liquidity_coverage": dict(reversed(list(base_kwargs["liquidity_coverage"].items()))),
        }
    )
    changed = build_data_snapshot_manifest(
        **{
            **base_kwargs,
            "benchmark_coverage": {
                **base_kwargs["benchmark_coverage"],
                "000660": {"rows": 699, "start": "2025-01-01", "end": "2025-12-31"},
            },
        }
    )

    assert len(first["data_snapshot_hash"]) == 64
    assert first["data_snapshot_hash"] == reordered["data_snapshot_hash"]
    assert first["data_snapshot_hash"] != changed["data_snapshot_hash"]
    assert list(first["liquidity_coverage"]) == ["000660", "005930"]


def test_failed_canonical_metrics_separates_evaluation_error_from_zero_return():
    from tools.evaluate_and_promote import failed_canonical_metrics

    metrics = failed_canonical_metrics(RuntimeError("data provider unavailable"), "full_period")

    assert metrics["total_return"] == 0
    assert metrics["evaluation_status"] == "failed"
    assert metrics["evaluation_stage"] == "full_period"
    assert metrics["evaluation_error_type"] == "RuntimeError"
    assert "data provider unavailable" in metrics["error"]


def test_calculate_canonical_metrics_preserves_target_weight_diagnostics():
    from tools.evaluate_and_promote import calculate_canonical_metrics

    dates = pd.bdate_range("2025-01-01", periods=4)
    result = {
        "equity_curve": pd.DataFrame(
            {
                "date": dates,
                "value": [100.0, 102.0, 104.0, 108.0],
                "cash": [30.0, 28.0, 25.0, 22.0],
                "n_positions": [2, 2, 2, 2],
            }
        ),
        "trades": [
            {
                "date": dates[1],
                "symbol": "005930",
                "action": "REBALANCE_SELL",
                "price": 11,
                "quantity": 1,
                "pnl": 1,
            }
        ],
        "target_weight_metrics": {
            "target_top_n": 5,
            "risk_off_rebalance_pct": 38.9,
            "min_target_exposure_pct": 35.0,
        },
    }

    metrics = calculate_canonical_metrics(result, 100.0)

    assert metrics["total_return"] == 8.0
    assert metrics["total_trades"] == 1
    assert metrics["ev_per_trade"] == 1
    assert metrics["target_top_n"] == 5
    assert metrics["risk_off_rebalance_pct"] == 38.9
    assert metrics["min_target_exposure_pct"] == 35.0


def test_attach_canonical_walk_forward_metrics_mutates_metrics_and_returns_summary():
    from tools.evaluate_and_promote import attach_canonical_walk_forward_metrics

    metrics = {}
    summary = attach_canonical_walk_forward_metrics(
        metrics,
        [
            {"total_return": 10.0, "sharpe": 0.5, "total_trades": 4},
            {"total_return": -1.0, "sharpe": -0.2, "total_trades": 2},
            {"total_return": 2.0, "sharpe": 0.1, "total_trades": 3},
        ],
    )

    assert metrics["wf_windows"] == 3
    assert metrics["wf_positive_rate"] == 0.667
    assert metrics["wf_sharpe_positive_rate"] == 0.667
    assert metrics["wf_total_trades"] == 9
    assert summary["positive"] == 2
    assert summary["sharpe_pos"] == 2
    assert summary["total_trades"] == 9


def _provisional_metrics():
    return {
        "total_return": 18.0,
        "profit_factor": 1.5,
        "mdd": -8.0,
        "wf_positive_rate": 0.8,
        "wf_sharpe_positive_rate": 0.8,
        "wf_windows": 6,
        "wf_total_trades": 120,
        "sharpe": 0.7,
        "ev_per_trade": 1000.0,
        "cost_adjusted_cagr": 8.0,
        "turnover_per_year": 300.0,
    }


def _write_paper_package(evidence_dir, strategy, **overrides):
    payload = {
        "strategy": strategy,
        "recommendation": "ELIGIBLE",
        "promotable_evidence_days": 60,
        "paper_sharpe": 0.55,
        "avg_same_universe_excess": 0.2,
        "benchmark_final_ratio": 0.9,
        "sell_count": 8,
        "win_rate": 55.0,
        "frozen_days": 0,
        "cumulative_return": 4.0,
    }
    payload.update(overrides)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"promotion_evidence_{strategy}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_build_promotion_results_promotes_live_when_eligible_paper_evidence_exists(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_ready_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(metrics, evidence_dir=str(evidence_dir))

    assert promotions[strategy]["status"] == "live_candidate"
    assert "live" in promotions[strategy]["allowed_modes"]
    assert metrics[strategy]["paper_days"] == 60
    assert metrics[strategy]["paper_evidence_recommendation"] == "ELIGIBLE"


def test_build_promotion_results_stays_provisional_when_paper_evidence_missing(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_missing_strategy"
    metrics = {strategy: _provisional_metrics()}

    promotions = build_promotion_results(metrics, evidence_dir=str(tmp_path / "paper_evidence"))

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert "paper_days" not in metrics[strategy]


def test_build_promotion_results_does_not_promote_live_when_evidence_blocked(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_blocked_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(
        evidence_dir,
        strategy,
        recommendation="BLOCKED",
        promotable_evidence_days=60,
    )

    promotions = build_promotion_results(metrics, evidence_dir=str(evidence_dir))

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert metrics[strategy]["paper_evidence_recommendation"] == "BLOCKED"


def test_build_promotion_results_blocks_target_weight_without_verified_proof(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "target_weight_rotation_test"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(metrics, evidence_dir=str(evidence_dir))

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert "target-weight evidence required flag missing" in promotions[strategy]["reason"]


def test_build_promotion_results_promotes_target_weight_with_verified_proof(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "target_weight_rotation_test"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(
        evidence_dir,
        strategy,
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

    promotions = build_promotion_results(metrics, evidence_dir=str(evidence_dir))

    assert promotions[strategy]["status"] == "live_candidate"
    assert "live" in promotions[strategy]["allowed_modes"]
    assert metrics[strategy]["target_weight_verified_pilot_days"] == 60
    assert metrics[strategy]["target_weight_params_hash"] == "hash"


def test_build_promotion_results_blocks_target_weight_mixed_params_hash(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "target_weight_rotation_test"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(
        evidence_dir,
        strategy,
        target_weight_evidence={
            "required": True,
            "valid_pilot_days": 60,
            "invalid_days": 0,
            "invalid_reasons": {},
            "params_hash": None,
            "params_hashes": ["hash-a", "hash-b"],
            "params_hash_consistent": False,
            "all_promotable_days_verified": False,
        },
        target_weight_verified_pilot_days=60,
        target_weight_invalid_days=0,
        target_weight_params_hash=None,
    )

    promotions = build_promotion_results(metrics, evidence_dir=str(evidence_dir))

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "target-weight promotable evidence not fully verified" in promotions[strategy]["reason"]
    assert "target-weight params_hash not consistent" in promotions[strategy]["reason"]


def test_canonical_research_candidate_metadata_is_json_serializable():
    from tools.evaluate_and_promote import (
        build_canonical_research_candidate_specs,
        canonical_research_candidate_metadata,
    )

    metadata = canonical_research_candidate_metadata(
        build_canonical_research_candidate_specs()[0]
    )

    json.dumps(metadata, ensure_ascii=False)
