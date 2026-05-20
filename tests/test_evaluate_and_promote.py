import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


def _daily_ops_with_summary_hash(payload: dict) -> dict:
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload["summary_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


def test_build_canonical_research_candidate_specs_selects_target_weight_candidates():
    from tools.evaluate_and_promote import build_canonical_research_candidate_specs

    specs = build_canonical_research_candidate_specs()

    assert [spec.candidate_id for spec in specs] == [
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
    ]
    assert specs[0].strategy == "target_weight_rotation"
    assert specs[0].params["market_exposure_mode"] == "benchmark_risk"
    assert specs[0].params["bear_target_exposure"] == 0.35
    assert specs[1].params["portfolio_drawdown_guard_trigger_pct"] == 8.0
    assert specs[1].params["portfolio_drawdown_guard_exposure"] == 0.25
    assert specs[1].params["portfolio_drawdown_guard_cooldown_rebalances"] == 1
    assert specs[2].params["rank_penalty_lookback"] == 120
    assert specs[2].params["downside_vol_penalty_weight"] == 0.50
    assert specs[2].params["drawdown_penalty_weight"] == 0.70
    assert specs[2].params["target_tolerance_pct"] == 4.0
    assert specs[3].params["rank_penalty_lookback"] == 120
    assert specs[3].params["max_targets_per_sector"] == 2
    assert specs[3].params["target_tolerance_pct"] == 4.0
    assert specs[4].params["rank_penalty_lookback"] == 90
    assert specs[4].params["drawdown_penalty_weight"] == 0.75
    assert specs[4].params["target_tolerance_pct"] == 4.0
    assert specs[5].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[5].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[6].params["target_tolerance_pct"] == 3.0
    assert specs[6].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[6].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[7].params["target_tolerance_pct"] == 4.0
    assert specs[7].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[7].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[8].params["max_pairwise_correlation"] == 0.85
    assert specs[8].params["correlation_lookback_days"] == 90
    assert specs[8].params["target_tolerance_pct"] == 4.0
    assert specs[8].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[8].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[9].params["correlation_rank_penalty_weight"] == 0.05
    assert specs[9].params["correlation_rank_penalty_lookback_days"] == 90
    assert specs[9].params["correlation_rank_penalty_mode"] == "mean_positive"
    assert specs[9].params["target_tolerance_pct"] == 4.0
    assert specs[9].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[9].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[10].params["correlation_rank_penalty_weight"] == 0.10
    assert specs[10].params["target_tolerance_pct"] == 4.0
    assert specs[10].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[10].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[11].params["max_targets_per_sector"] == 2
    assert specs[11].params["target_tolerance_pct"] == 4.0
    assert specs[11].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[11].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[12].params["max_targets_per_sector"] == 2
    assert specs[12].params["max_pairwise_correlation"] == 0.85
    assert specs[12].params["target_tolerance_pct"] == 4.0
    assert specs[12].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[12].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[13].params["max_targets_per_sector"] == 2
    assert specs[13].params["correlation_rank_penalty_weight"] == 0.05
    assert specs[13].params["target_tolerance_pct"] == 4.0
    assert specs[13].params["portfolio_drawdown_guard_trigger_pct"] == 10.0
    assert specs[13].params["portfolio_drawdown_guard_exposure"] == 0.40
    assert specs[14].params["target_allocation_mode"] == "inverse_volatility"
    assert specs[14].params["allocation_vol_lookback_days"] == 60
    assert specs[14].params["allocation_vol_min_periods"] == 30
    assert specs[14].params["allocation_max_sleeve_weight_pct"] == 35.0
    assert specs[15].params["max_targets_per_sector"] == 2
    assert specs[15].params["target_allocation_mode"] == "inverse_volatility"
    assert specs[15].params["allocation_vol_lookback_days"] == 60
    assert specs[15].params["allocation_max_sleeve_weight_pct"] == 35.0
    assert specs[16].params["max_targets_per_sector"] == 2
    assert specs[16].params["position_loss_reduce_trigger_pct"] == 8.0
    assert specs[16].params["position_loss_reduce_target_fraction"] == 0.50
    assert specs[17].params["position_loss_reduce_trigger_pct"] == 8.0
    assert specs[17].params["position_loss_reduce_target_fraction"] == 0.50
    assert specs[18].params["position_loss_reduce_trigger_pct"] == 10.0
    assert specs[18].params["position_loss_reduce_target_fraction"] == 0.50
    assert specs[19].params["target_tolerance_pct"] == 5.0
    assert specs[19].params["max_targets_per_sector"] == 2
    assert specs[19].params["position_loss_reduce_trigger_pct"] == 8.0
    assert specs[19].params["position_loss_reduce_target_fraction"] == 0.50


def test_default_target_weight_candidate_uses_supported_latest_provisional():
    from core.target_weight_rotation import (
        DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        unsupported_plan_params,
    )
    from tools.evaluate_and_promote import build_canonical_research_candidate_specs

    assert (
        DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
        == "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_sectorcap2_posloss8_frac50_pdd10_floor40_cd1"
    )
    spec = build_canonical_research_candidate_specs([DEFAULT_TARGET_WEIGHT_CANDIDATE_ID])[0]
    assert unsupported_plan_params(spec.params) == []
    assert spec.params["target_tolerance_pct"] == 5.0
    assert spec.params["max_targets_per_sector"] == 2
    assert spec.params["position_loss_reduce_trigger_pct"] == 8.0
    assert spec.params["portfolio_drawdown_guard_trigger_pct"] == 10.0


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


def test_load_reusable_canonical_universe_snapshot_accepts_matching_metadata(tmp_path):
    from tools.evaluate_and_promote import (
        CANONICAL_EVAL_END,
        CANONICAL_EVAL_START,
        CANONICAL_TOP_N,
        CANONICAL_UNIVERSE_LOOKBACK_END,
        CANONICAL_UNIVERSE_LOOKBACK_START,
        CANONICAL_UNIVERSE_RULE,
        _load_reusable_canonical_universe_snapshot,
    )

    universe = [f"{idx:06d}" for idx in range(CANONICAL_TOP_N)]
    metadata_path = tmp_path / "run_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "artifact_type": "canonical_promotion_bundle",
                "generated_at": "2026-05-19T10:00:00",
                "eval_start": CANONICAL_EVAL_START,
                "eval_end": CANONICAL_EVAL_END,
                "universe_rule": CANONICAL_UNIVERSE_RULE,
                "universe": universe,
                "data_snapshot_hash": "abc123",
                "data_snapshot_manifest": {
                    "universe": universe,
                    "universe_lookback_start": CANONICAL_UNIVERSE_LOOKBACK_START,
                    "universe_lookback_end": CANONICAL_UNIVERSE_LOOKBACK_END,
                    "liquidity_coverage": {
                        universe[0]: {"rows": 61},
                        universe[1]: {"rows": 62},
                    },
                    "fetch_errors": {
                        "liquidity:000001": {"stage": "universe_liquidity"},
                        "benchmark:000002": {"stage": "benchmark"},
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshot = _load_reusable_canonical_universe_snapshot(metadata_path)

    assert snapshot["universe"] == universe
    assert snapshot["liquidity_coverage"][universe[0]]["rows"] == 61
    assert snapshot["fetch_errors"] == {
        "liquidity:000001": {"stage": "universe_liquidity"}
    }
    assert snapshot["source_data_snapshot_hash"] == "abc123"


def test_load_reusable_canonical_universe_snapshot_rejects_mismatch(tmp_path):
    from tools.evaluate_and_promote import (
        CANONICAL_EVAL_END,
        CANONICAL_EVAL_START,
        CANONICAL_TOP_N,
        CANONICAL_UNIVERSE_LOOKBACK_END,
        CANONICAL_UNIVERSE_LOOKBACK_START,
        _load_reusable_canonical_universe_snapshot,
    )

    metadata_path = tmp_path / "run_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "artifact_type": "canonical_promotion_bundle",
                "eval_start": CANONICAL_EVAL_START,
                "eval_end": CANONICAL_EVAL_END,
                "universe_rule": "old rule",
                "universe": [f"{idx:06d}" for idx in range(CANONICAL_TOP_N)],
                "data_snapshot_manifest": {
                    "universe_lookback_start": CANONICAL_UNIVERSE_LOOKBACK_START,
                    "universe_lookback_end": CANONICAL_UNIVERSE_LOOKBACK_END,
                    "liquidity_coverage": {},
                    "fetch_errors": {},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert _load_reusable_canonical_universe_snapshot(metadata_path) is None


def test_write_canonical_progress_records_stage(tmp_path):
    from tools.evaluate_and_promote import _write_canonical_progress

    progress_path = tmp_path / "canonical_progress.json"

    _write_canonical_progress(
        "research_walk_forward",
        progress_path=progress_path,
        strategy="target_weight_candidate",
        window=2,
        total_windows=6,
    )

    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "canonical_promotion_progress"
    assert payload["stage"] == "research_walk_forward"
    assert payload["strategy"] == "target_weight_candidate"
    assert payload["window"] == 2
    assert payload["total_windows"] == 6
    assert "updated_at" in payload


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
        "wf_windows": 5,
        "wf_total_trades": 120,
        "sharpe": 0.7,
        "benchmark_excess_return": 2.0,
        "benchmark_excess_sharpe": 0.2,
        "ev_per_trade": 1000.0,
        "cost_adjusted_cagr": 8.0,
        "turnover_per_year": 300.0,
    }


def _write_paper_package(evidence_dir, strategy, **overrides):
    from core.paper_evidence import (
        PROMOTION_PACKAGE_INTEGRITY_SCHEMA_VERSION,
        compute_promotion_package_integrity_hash,
    )

    latest_evidence_date = date.today().isoformat()
    payload = {
        "strategy": strategy,
        "generated_at": f"{latest_evidence_date}T15:30:00",
        "period": f"2026-01-01 ~ {latest_evidence_date}",
        "latest_evidence_date": latest_evidence_date,
        "recommendation": "ELIGIBLE",
        "promotable_evidence_days": 60,
        "paper_sharpe": 0.55,
        "avg_same_universe_excess": 0.2,
        "avg_cash_adjusted_excess": 0.15,
        "benchmark_final_ratio": 0.9,
        "sell_count": 8,
        "win_rate": 55.0,
        "frozen_days": 0,
        "cumulative_return": 4.0,
        "trade_quality": {"status": "ok"},
    }
    payload.update(overrides)
    payload.pop("package_integrity", None)
    payload["package_integrity"] = {
        "schema_version": PROMOTION_PACKAGE_INTEGRITY_SCHEMA_VERSION,
        "payload_hash": compute_promotion_package_integrity_hash(payload),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"promotion_evidence_{strategy}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _promotion_metadata(*, generated_at="2026-05-13T14:00:00", strategy_specs=None):
    from core.live_gate import LIVE_GATE_ARTIFACT_TYPE, LIVE_GATE_SCHEMA_VERSION
    from tools.evaluate_and_promote import build_data_snapshot_manifest

    manifest = build_data_snapshot_manifest(
        provider="test-provider",
        universe_rule="test universe",
        eval_start="2026-01-01",
        eval_end="2026-03-31",
        universe_lookback_start="2025-10-01",
        universe_lookback_end="2025-12-31",
        universe=["005930"],
        liquidity_coverage={
            "005930": {"rows": 62, "start": "2025-10-01", "end": "2025-12-31"}
        },
        benchmark_coverage={
            "005930": {"rows": 60, "start": "2026-01-01", "end": "2026-03-31"}
        },
        fetch_errors={},
    )
    return {
        "schema_version": LIVE_GATE_SCHEMA_VERSION,
        "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
        "generated_at": generated_at,
        "data_snapshot_hash": manifest["data_snapshot_hash"],
        "data_snapshot_manifest": manifest,
        "evaluation_errors": {},
        "walk_forward_errors": {},
        "strategy_specs": strategy_specs or [],
    }


def _fresh_promotion_metadata(*, strategy_specs=None):
    return _promotion_metadata(
        generated_at=f"{date.today().isoformat()}T16:00:00",
        strategy_specs=strategy_specs,
    )


def _write_source_artifacts(artifact_dir, metrics):
    walk_forward = {}
    excess_return = {}
    excess_sharpe = {}
    for name, metric in metrics.items():
        windows = int(metric.get("wf_windows") or 0)
        walk_forward[name] = {
            "windows": windows,
            "positive": int(round(float(metric.get("wf_positive_rate") or 0) * windows)),
            "sharpe_pos": int(round(float(metric.get("wf_sharpe_positive_rate") or 0) * windows)),
            "total_trades": int(metric.get("wf_total_trades") or 0),
            "details": [],
        }
        excess_return[name] = metric.get("benchmark_excess_return")
        excess_sharpe[name] = metric.get("benchmark_excess_sharpe")
    (artifact_dir / "walk_forward_summary.json").write_text(
        json.dumps(walk_forward, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "benchmark_comparison.json").write_text(
        json.dumps({
            "strategy_excess_return_pct": excess_return,
            "strategy_excess_sharpe": excess_sharpe,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_consistent_promotion_artifacts(
    artifact_dir,
    metrics,
    *,
    evidence_dir=None,
    metadata=None,
):
    from tools.evaluate_and_promote import (
        build_promotion_blocker_summary,
        build_promotion_results,
        write_promotion_blocker_summary,
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = evidence_dir or artifact_dir.parent / "paper_evidence"
    metadata = metadata or _promotion_metadata()
    metrics_for_artifact = json.loads(json.dumps(metrics, ensure_ascii=False))
    promotions = build_promotion_results(
        metrics_for_artifact,
        evidence_dir=str(evidence_dir),
        strategy_specs=metadata.get("strategy_specs", []),
        canonical_metadata=metadata,
    )
    summary = build_promotion_blocker_summary(
        promotions,
        metrics_for_artifact,
        metadata=metadata,
    )
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps(promotions, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps(metrics_for_artifact, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_source_artifacts(artifact_dir, metrics_for_artifact)
    write_promotion_blocker_summary(summary, artifact_dir)
    return promotions, metrics_for_artifact, summary


def test_build_promotion_results_promotes_live_when_eligible_paper_evidence_exists(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_ready_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "live_candidate"
    assert "live" in promotions[strategy]["allowed_modes"]
    assert metrics[strategy]["paper_days"] == 60
    assert metrics[strategy]["paper_cash_adjusted_excess"] == 0.15
    assert metrics[strategy]["paper_evidence_recommendation"] == "ELIGIBLE"
    assert metrics[strategy]["paper_evidence_fresh"] is True


def test_build_promotion_results_blocks_without_canonical_metadata(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "metadata_missing_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
    )

    assert promotions[strategy]["status"] == "paper_only"
    assert "canonical data integrity failed" in promotions[strategy]["reason"]
    assert "canonical metadata missing or invalid" in promotions[strategy]["reason"]
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert metrics[strategy]["canonical_data_integrity_ok"] is False


def test_build_promotion_results_ignores_metrics_summary_paper_fields_without_evidence_package(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_metrics_only_strategy"
    metrics = {strategy: {
        **_provisional_metrics(),
        "paper_days": 60,
        "paper_sharpe": 0.7,
        "paper_excess": 0.2,
        "paper_cash_adjusted_excess": 0.15,
        "paper_evidence_recommendation": "ELIGIBLE",
        "paper_benchmark_final_ratio": 0.9,
        "paper_sell_count": 8,
        "paper_win_rate": 55.0,
        "paper_frozen_days": 0,
        "paper_cumulative_return": 4.0,
        "paper_trade_quality_status": "ok",
    }}

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(tmp_path / "missing_evidence"),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "paper evidence recommendation missing != ELIGIBLE" in promotions[strategy]["reason"]
    assert "paper_days" not in metrics[strategy]


def test_build_promotion_results_requires_paper_evidence_strategy_identity(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_missing_identity_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)
    evidence_path = evidence_dir / f"promotion_evidence_{strategy}.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["strategy"] = None
    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert "paper evidence recommendation missing" in promotions[strategy]["reason"]
    assert metrics[strategy].get("paper_evidence_recommendation") is None


def test_build_promotion_results_blocks_stale_paper_evidence(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_stale_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(
        evidence_dir,
        strategy,
        generated_at="2026-01-15T15:30:00",
        period="2025-11-01 ~ 2026-01-01",
        latest_evidence_date="2026-01-01",
    )

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "paper evidence stale" in promotions[strategy]["reason"]
    assert metrics[strategy]["paper_evidence_fresh"] is False


def test_build_promotion_results_uses_canonical_generated_at_for_paper_freshness(
    tmp_path,
    monkeypatch,
):
    import tools.evaluate_and_promote as promote_tool

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 20, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(promote_tool, "datetime", FrozenDatetime)

    strategy = "paper_reference_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(
        evidence_dir,
        strategy,
        generated_at="2026-04-30T15:30:00",
        period="2026-03-01 ~ 2026-04-30",
        latest_evidence_date="2026-04-30",
    )
    metadata = _promotion_metadata(generated_at="2026-05-13T09:00:00")

    promotions = promote_tool.build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=metadata,
    )

    assert promotions[strategy]["status"] == "live_candidate"
    assert metrics[strategy]["paper_evidence_age_days"] == 13
    assert metrics[strategy]["paper_evidence_fresh"] is True


def test_build_promotion_results_blocks_when_canonical_benchmark_coverage_invalid(tmp_path):
    from tools.evaluate_and_promote import build_data_snapshot_manifest, build_promotion_results

    strategy = "paper_ready_strategy"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)
    manifest = build_data_snapshot_manifest(
        provider="test-provider",
        universe_rule="top liquidity",
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
        },
        fetch_errors={},
    )
    metadata = {
        "data_snapshot_hash": manifest["data_snapshot_hash"],
        "data_snapshot_manifest": manifest,
        "evaluation_errors": {},
        "walk_forward_errors": {},
    }

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=metadata,
    )

    assert promotions[strategy]["status"] == "paper_only"
    assert "canonical data integrity failed" in promotions[strategy]["reason"]
    assert "벤치마크 coverage 누락" in promotions[strategy]["reason"]
    assert metrics[strategy]["canonical_data_integrity_ok"] is False


def test_build_promotion_results_requires_positive_benchmark_excess(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_no_alpha_strategy"
    metrics = {strategy: {
        **_provisional_metrics(),
        "benchmark_excess_return": 0.0,
        "benchmark_excess_sharpe": 0.2,
    }}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "paper_only"
    assert "benchmark excess return 0.0 <= 0" in promotions[strategy]["reason"]


def test_build_promotion_results_requires_benchmark_excess_fields(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_missing_benchmark_strategy"
    metric = _provisional_metrics()
    metric.pop("benchmark_excess_return")
    metric.pop("benchmark_excess_sharpe")
    metrics = {strategy: metric}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "paper_only"
    assert "benchmark excess return missing" in promotions[strategy]["reason"]
    assert "benchmark excess Sharpe missing" in promotions[strategy]["reason"]


def test_build_promotion_results_stays_provisional_when_paper_evidence_missing(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "paper_missing_strategy"
    metrics = {strategy: _provisional_metrics()}

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(tmp_path / "paper_evidence"),
        canonical_metadata=_fresh_promotion_metadata(),
    )

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

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert metrics[strategy]["paper_evidence_recommendation"] == "BLOCKED"


def test_build_promotion_blocker_summary_writes_operator_artifacts(tmp_path):
    from tools.evaluate_and_promote import (
        build_promotion_blocker_source_hash,
        build_promotion_blocker_summary,
        write_promotion_blocker_summary,
    )

    promotions = {
        "paper_ready_strategy": {
            "status": "live_candidate",
            "allowed_modes": ["backtest", "paper", "live"],
            "reason": "live_candidate 충족",
        },
        "paper_blocked_strategy": {
            "status": "paper_only",
            "allowed_modes": ["backtest", "paper"],
            "reason": (
                "paper_only 충족; provisional 차단: "
                "canonical data integrity failed: 벤치마크 coverage 누락 종목"
            ),
        },
        "paper_stale_strategy": {
            "status": "provisional_paper_candidate",
            "allowed_modes": ["backtest", "paper"],
            "reason": (
                "provisional_paper_candidate 충족; live 차단: "
                "paper evidence stale latest=2026-01-01 age=120d > 14d"
            ),
        },
        "paper_fill_quality_strategy": {
            "status": "provisional_paper_candidate",
            "allowed_modes": ["backtest", "paper"],
            "reason": (
                "provisional_paper_candidate 충족; live 차단: "
                "paper evidence recommendation BLOCKED != ELIGIBLE: "
                "fill_quality_adverse_gap_bps=56.60"
            ),
        },
        "target_weight_missing_pilot_strategy": {
            "status": "provisional_paper_candidate",
            "allowed_modes": ["backtest", "paper"],
            "reason": (
                "provisional_paper_candidate 충족; live 차단: "
                "paper 0일 < 60일, paper benchmark_final_ratio 0 < 0.8, "
                "target-weight evidence required flag missing, "
                "target-weight verified pilot days 0 < 60, "
                "target-weight params_hash missing"
            ),
        },
    }
    metrics = {
        "paper_ready_strategy": {
            "total_return": 18.0,
            "benchmark_excess_return": 2.0,
            "paper_days": 60,
        },
        "paper_blocked_strategy": {
            "total_return": 8.0,
            "benchmark_excess_return": None,
            "canonical_data_integrity_ok": False,
        },
        "paper_stale_strategy": {
            "total_return": 18.0,
            "paper_latest_evidence_date": "2026-01-01",
            "paper_evidence_fresh": False,
        },
        "paper_fill_quality_strategy": {
            "total_return": 18.0,
            "paper_evidence_recommendation": "BLOCKED",
            "paper_trade_quality_status": "review",
            "paper_trade_quality_adverse_gap_bps": 56.6,
            "paper_trade_quality_missing_expected_ratio": 0.0,
            "paper_trade_quality_missing_execution_link_ratio": 0.25,
        },
        "target_weight_missing_pilot_strategy": {
            "total_return": 198.15,
            "benchmark_excess_return": 48.76,
            "sharpe": 1.57,
            "mdd": -17.18,
            "paper_days": 0,
            "paper_benchmark_final_ratio": 0.0,
            "target_weight_verified_pilot_days": 0,
        },
    }

    summary = build_promotion_blocker_summary(
        promotions,
        metrics,
        metadata={"generated_at": "2026-05-12T09:00:00"},
    )
    json_path, md_path = write_promotion_blocker_summary(summary, tmp_path)

    assert summary["artifact_type"] == "promotion_blocker_summary"
    assert summary["source_artifact_hash"] == build_promotion_blocker_source_hash(
        promotions,
        metrics,
        {"generated_at": "2026-05-12T09:00:00"},
    )
    assert len(summary["source_artifact_hash"]) == 64
    assert summary["summary"]["total_strategies"] == 5
    assert summary["summary"]["live_ready_count"] == 1
    assert summary["summary"]["blocked_from_live_count"] == 4
    assert summary["strategies"]["paper_blocked_strategy"]["next_action"].startswith("canonical")
    assert summary["strategies"]["paper_stale_strategy"]["next_action"].startswith("paper evidence")
    assert summary["strategies"]["paper_fill_quality_strategy"]["next_action"].startswith("paper 체결")
    assert summary["strategies"]["target_weight_missing_pilot_strategy"]["next_action"].startswith(
        "target-weight capped paper pilot readiness audit"
    )
    assert summary["strategies"]["paper_ready_strategy"]["metrics"]["paper_days"] == 60
    assert (
        summary["strategies"]["target_weight_missing_pilot_strategy"]["metrics"]["target_weight_verified_pilot_days"]
        == 0
    )
    assert (
        summary["strategies"]["paper_fill_quality_strategy"]["metrics"]["paper_trade_quality_adverse_gap_bps"]
        == 56.6
    )
    assert (
        summary["strategies"]["paper_fill_quality_strategy"]["metrics"]["paper_trade_quality_missing_execution_link_ratio"]
        == 0.25
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["live_ready_count"] == 1
    report = md_path.read_text(encoding="utf-8")
    assert "# Promotion Blocker Summary" in report
    assert "paper_blocked_strategy" in report
    assert "canonical data integrity failed" in report


def test_load_promotion_blocker_summary_from_existing_artifacts(tmp_path):
    from tools.evaluate_and_promote import (
        load_promotion_blocker_summary_from_artifacts,
        write_promotion_blocker_summary,
    )

    artifact_dir = tmp_path / "promotion"
    artifact_dir.mkdir()
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps({
            "paper_blocked_strategy": {
                "status": "paper_only",
                "allowed_modes": ["backtest", "paper"],
                "reason": "paper_only 충족; provisional 차단: benchmark excess return missing",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps({
            "paper_blocked_strategy": {
                "total_return": 7.5,
                "sharpe": 0.4,
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps({"generated_at": "2026-05-12T10:00:00"}, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = load_promotion_blocker_summary_from_artifacts(
        artifact_dir,
        validate=False,
    )
    json_path, md_path = write_promotion_blocker_summary(summary, artifact_dir)

    assert summary["generated_at"] == "2026-05-12T10:00:00"
    assert len(summary["source_artifact_hash"]) == 64
    assert summary["summary"]["blocked_from_live_count"] == 1
    assert summary["strategies"]["paper_blocked_strategy"]["metrics"]["total_return"] == 7.5
    assert json_path.name == "promotion_blocker_summary.json"
    assert "benchmark excess return missing" in md_path.read_text(encoding="utf-8")


def test_load_promotion_blocker_summary_requires_recalculated_promotion_by_default(tmp_path):
    from tools.evaluate_and_promote import (
        build_promotion_blocker_summary,
        load_promotion_blocker_summary_from_artifacts,
        write_promotion_blocker_summary,
    )

    artifact_dir = tmp_path / "promotion"
    artifact_dir.mkdir()
    strategy = "paper_ready_strategy"
    metrics = {
        strategy: {
            **_provisional_metrics(),
            "benchmark_excess_return": -1.0,
        }
    }
    metadata = _promotion_metadata()
    stale_promotions = {
        strategy: {
            "status": "live_candidate",
            "allowed_modes": ["backtest", "paper", "live"],
            "reason": "live_candidate 충족",
        }
    }
    _write_paper_package(tmp_path / "paper_evidence", strategy)
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps(stale_promotions, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_source_artifacts(artifact_dir, metrics)
    write_promotion_blocker_summary(
        build_promotion_blocker_summary(stale_promotions, metrics, metadata=metadata),
        artifact_dir,
    )

    with pytest.raises(ValueError, match="promotion_result 재계산 검증 실패"):
        load_promotion_blocker_summary_from_artifacts(artifact_dir)

    summary = load_promotion_blocker_summary_from_artifacts(
        artifact_dir,
        validate=False,
    )
    assert summary["strategies"][strategy]["status"] == "live_candidate"


def test_validate_promotion_blocker_summary_detects_stale_summary(tmp_path):
    from tools.evaluate_and_promote import (
        validate_promotion_blocker_summary_artifact,
    )

    artifact_dir = tmp_path / "promotion"
    strategy = "paper_blocked_strategy"
    _write_consistent_promotion_artifacts(
        artifact_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    promotion_path = artifact_dir / "promotion_result.json"

    assert validate_promotion_blocker_summary_artifact(artifact_dir) == []

    promotion_path.write_text(
        json.dumps({
            strategy: {
                "status": "live_candidate",
                "allowed_modes": ["backtest", "paper", "live"],
                "reason": "live_candidate 충족",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_promotion_blocker_summary_artifact(artifact_dir)
    assert any("source_artifact_hash 불일치" in issue for issue in issues)
    assert any("summary 내용 불일치" in issue for issue in issues)
    assert any("strategies 내용 불일치" in issue for issue in issues)
    assert any("promotion_result" in issue and "재계산 결과 불일치" in issue for issue in issues)


def test_validate_promotion_blocker_summary_detects_stale_promotion_result_against_metrics(tmp_path):
    from tools.evaluate_and_promote import (
        build_promotion_blocker_summary,
        build_current_blockers_report,
        validate_current_blockers_artifact,
        validate_promotion_blocker_summary_artifact,
        write_current_blockers_report,
        write_promotion_blocker_summary,
    )

    artifact_dir = tmp_path / "promotion"
    artifact_dir.mkdir()
    strategy = "paper_ready_strategy"
    metrics = {
        strategy: {
            **_provisional_metrics(),
            "benchmark_excess_return": -1.0,
        }
    }
    metadata = _promotion_metadata()
    stale_promotions = {
        strategy: {
            "status": "live_candidate",
            "allowed_modes": ["backtest", "paper", "live"],
            "reason": "live_candidate 충족",
        }
    }
    _write_paper_package(tmp_path / "paper_evidence", strategy)
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps(stale_promotions, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_source_artifacts(artifact_dir, metrics)
    stale_summary = build_promotion_blocker_summary(
        stale_promotions,
        metrics,
        metadata=metadata,
    )
    write_promotion_blocker_summary(stale_summary, artifact_dir)
    current_blockers_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        build_current_blockers_report(stale_summary),
        current_blockers_path,
    )

    summary_issues = validate_promotion_blocker_summary_artifact(artifact_dir)
    current_issues = validate_current_blockers_artifact(
        artifact_dir,
        current_blockers_path,
    )

    assert any("promotion_result" in issue and "재계산 결과 불일치" in issue for issue in summary_issues)
    assert any("promotion blocker summary 동기화 실패" in issue for issue in current_issues)
    assert any("promotion_result" in issue and "재계산 결과 불일치" in issue for issue in current_issues)


def test_blocker_summary_regeneration_requires_fresh_promotion_result(tmp_path):
    from tools.evaluate_and_promote import (
        build_promotion_blocker_summary,
        load_validated_promotion_blocker_summary_from_artifacts,
        write_promotion_blocker_summary,
    )

    artifact_dir = tmp_path / "promotion"
    artifact_dir.mkdir()
    strategy = "paper_ready_strategy"
    metrics = {
        strategy: {
            **_provisional_metrics(),
            "benchmark_excess_return": -1.0,
        }
    }
    metadata = _promotion_metadata()
    stale_promotions = {
        strategy: {
            "status": "live_candidate",
            "allowed_modes": ["backtest", "paper", "live"],
            "reason": "live_candidate 충족",
        }
    }
    _write_paper_package(tmp_path / "paper_evidence", strategy)
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps(stale_promotions, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_source_artifacts(artifact_dir, metrics)
    write_promotion_blocker_summary(
        build_promotion_blocker_summary(stale_promotions, metrics, metadata=metadata),
        artifact_dir,
    )

    with pytest.raises(ValueError, match="promotion_result 재계산 검증 실패"):
        load_validated_promotion_blocker_summary_from_artifacts(artifact_dir)


def test_refresh_promotion_artifacts_rebuilds_stale_promotion_and_current_blockers(tmp_path):
    from tools.evaluate_and_promote import (
        build_current_blockers_report,
        build_promotion_blocker_summary,
        refresh_promotion_artifacts_from_existing_inputs,
        validate_current_blockers_artifact,
        validate_promotion_blocker_summary_artifact,
        write_current_blockers_report,
        write_promotion_blocker_summary,
    )

    artifact_dir = tmp_path / "promotion"
    artifact_dir.mkdir()
    strategy = "paper_ready_strategy"
    metrics = {
        strategy: {
            **_provisional_metrics(),
            "benchmark_excess_return": -1.0,
        }
    }
    metadata = _promotion_metadata()
    stale_promotions = {
        strategy: {
            "status": "live_candidate",
            "allowed_modes": ["backtest", "paper", "live"],
            "reason": "live_candidate 충족",
        }
    }
    _write_paper_package(tmp_path / "paper_evidence", strategy)
    (artifact_dir / "promotion_result.json").write_text(
        json.dumps(stale_promotions, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "metrics_summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_source_artifacts(artifact_dir, metrics)
    stale_summary = build_promotion_blocker_summary(
        stale_promotions,
        metrics,
        metadata=metadata,
    )
    write_promotion_blocker_summary(stale_summary, artifact_dir)
    current_blockers_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        build_current_blockers_report(stale_summary),
        current_blockers_path,
    )

    paths = refresh_promotion_artifacts_from_existing_inputs(
        artifact_dir,
        evidence_dir=tmp_path / "paper_evidence",
        current_blockers_path=current_blockers_path,
    )

    refreshed_promotions = json.loads(
        (artifact_dir / "promotion_result.json").read_text(encoding="utf-8")
    )
    refreshed_metrics = json.loads(
        (artifact_dir / "metrics_summary.json").read_text(encoding="utf-8")
    )
    refreshed_current = json.loads(current_blockers_path.read_text(encoding="utf-8"))
    assert paths["promotion_result"] == artifact_dir / "promotion_result.json"
    assert refreshed_promotions[strategy]["status"] == "paper_only"
    assert refreshed_metrics[strategy]["paper_days"] == 60
    assert refreshed_current["go_live"] is False
    assert validate_promotion_blocker_summary_artifact(artifact_dir) == []
    assert validate_current_blockers_artifact(artifact_dir, current_blockers_path) == []


def test_validate_promotion_blocker_summary_requires_summary_file(tmp_path):
    from tools.evaluate_and_promote import validate_promotion_blocker_summary_artifact

    issues = validate_promotion_blocker_summary_artifact(tmp_path / "promotion")

    assert issues
    assert "promotion_blocker_summary.json 없음" in issues[0]


def test_build_current_blockers_report_from_promotion_summary():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "a" * 64,
        "summary": {
            "total_strategies": 3,
            "status_counts": {
                "paper_only": 1,
                "provisional_paper_candidate": 1,
                "research_only": 1,
            },
            "live_ready_count": 0,
            "blocked_from_live_count": 3,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
                "reason": "provisional_paper_candidate 충족; live 차단: paper 0일 < 60일",
            },
            "scoring": {
                "status": "paper_only",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": -139.5},
                "reason": "paper_only 충족; provisional 차단",
            },
            "breakout": {
                "status": "research_only",
                "allowed_modes": ["backtest"],
                "metrics": {},
                "reason": "paper_only 미달",
            },
        },
    }

    report = build_current_blockers_report(
        blocker_summary,
        generated_at="2026-05-18T15:00:00",
    )

    assert report["artifact_type"] == "current_go_live_blockers"
    assert report["schema_version"] == 3
    assert report["generated_at"] == "2026-05-18T15:00:00"
    assert report["source_generated_at"] == "2026-05-13T14:07:37"
    assert report["source_artifact_hash"] == "a" * 64
    assert report["promotion_artifact_freshness"]["status"] == "AGING"
    assert report["promotion_artifact_freshness"]["age_days"] == 5.04
    assert (
        report["operator_runbook"]["promotion_artifact_freshness"]["status"]
        == "AGING"
    )
    assert report["go_live"] is False
    assert "NO-GO" in report["verdict"]
    assert report["provisional_paper_candidates"] == ["target_weight_best"]
    assert report["hard_blockers"][0]["desc"] == "live_candidate 상태의 전략이 없음"
    assert report["next_actions"][0]["strategy"] == "target_weight_best"
    assert report["next_actions"][0]["command"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --daily-ops-summary"
    )
    assert report["next_actions"][0]["follow_up"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --readiness-audit"
    )
    assert report["next_actions"][1]["command"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --readiness-audit"
    )
    assert report["next_actions"][2]["order_safety"] == "no_order"
    assert report["next_actions"][2]["command"].startswith("# blocked:")
    assert report["operator_runbook"]["primary_strategy"] == "target_weight_best"
    assert report["operator_runbook"]["commands"]["daily_ops_summary"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --daily-ops-summary"
    )
    assert report["operator_runbook"]["commands"]["finalize_pilot_evidence"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --finalize-pilot-evidence --finalize-date YYYY-MM-DD"
    )
    assert report["operator_runbook"]["commands"]["repair_pilot_evidence"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --repair-pilot-evidence --repair-date YYYY-MM-DD"
    )
    assert report["operator_runbook"]["commands"]["execute_capped_paper_after_ready"].startswith("# blocked:")
    assert report["operator_runbook"]["sequence"][0]["order_safety"] == "no_order"
    assert report["operator_runbook"]["sequence"][3]["order_safety"] == "no_order"
    assert "target_weight_best" in report["default_strategy"]


def test_build_current_blockers_blocks_live_when_canonical_artifact_is_stale():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-01T09:00:00",
        "source_artifact_hash": "a" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"live_candidate": 1},
            "live_ready_count": 1,
            "blocked_from_live_count": 0,
        },
        "strategies": {
            "paper_ready_strategy": {
                "status": "live_candidate",
                "allowed_modes": ["backtest", "paper", "live"],
                "metrics": {},
                "reason": "live_candidate 충족",
            },
        },
    }

    report = build_current_blockers_report(
        blocker_summary,
        generated_at="2026-05-13T12:00:00",
    )

    assert report["promotion_artifact_freshness"]["status"] == "STALE"
    assert report["go_live"] is False
    assert "NO-GO" in report["verdict"]
    assert any(
        blocker["desc"] == "canonical promotion artifact 최신성 미충족"
        for blocker in report["hard_blockers"]
    )


def test_build_current_blockers_report_prioritizes_shadow_from_daily_ops():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "a" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "status": "BLOCKED",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 0,
        },
        "decision": {
            "blocking_reasons": [
                "launch_readiness: clean_final_days 0/3",
                "evidence_freshness: no evidence",
                "notifier: Discord webhook 미설정",
            ],
        },
        "operator_commands": {
            "collect_shadow_days": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --shadow-days 3 --shadow-end-date 2026-05-18"
            ),
            "rerun_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["desc"].startswith("target-weight shadow 3일 수집")
    assert action["command"].endswith("--shadow-days 3 --shadow-end-date 2026-05-18")
    assert action["order_safety"] == "no_order"
    assert action["daily_ops_status"] == "BLOCKED"
    assert report["operator_runbook"]["current_priority_action"]["desc"] == action["desc"]
    assert report["operator_runbook"]["sequence"][2]["command"] == action["command"]


def test_build_current_blockers_report_prioritizes_cap_approval_when_ready_even_with_two_shadow_days(monkeypatch):
    import tools.evaluate_and_promote as ep

    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-19")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "a" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-19.json",
        "trade_day": "2026-05-19",
        "status": "READY_TO_ENABLE_CAPS",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
        },
        "decision": {
            "blocking_reasons": [
                "pilot_authorization_snapshot: target_weight_pilot_authorization_snapshot_mismatch",
            ],
        },
        "operator_commands": {
            "collect_shadow_days": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --shadow-days 3 --shadow-end-date 2026-05-19"
            ),
            "enable_suggested_caps": (
                "python tools/paper_pilot_control.py --strategy target_weight_best "
                "--enable --from 2026-05-19 --to 2026-08-10 "
                "--max-orders 4 --max-positions 4 --max-notional 1500000 --max-exposure 5060000 "
                '--reason "target-weight shadow dry-run matched suggested pilot caps"'
            ),
            "rerun_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-19 --readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["daily_ops_status"] == "READY_TO_ENABLE_CAPS"
    assert action["desc"] == "readiness artifact의 추천 cap 승인 후 readiness 재점검"
    assert action["command"].startswith("python tools/paper_pilot_control.py")
    assert "--enable" in action["command"]
    assert action["follow_up"].endswith("--as-of-date 2026-05-19 --readiness-audit")
    assert "--shadow-days 3" not in action["command"]
    assert report["operator_runbook"]["sequence"][2]["command"] == action["command"]


def test_build_current_blockers_report_promotes_discord_test_after_shadow():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "b" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "status": "BLOCKED",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 3,
        },
        "decision": {
            "blocking_reasons": ["notifier: Discord webhook 미설정"],
        },
        "operator_commands": {},
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["desc"] == "Discord webhook 설정 후 도달성 확인 preflight 실행"
    assert action["setup_required"] is True
    assert action["required_env"] == "DISCORD_WEBHOOK_URL"
    assert action["config_path"] == "config/settings.yaml: discord.enabled=true"
    assert "DISCORD_WEBHOOK_URL" in action["setup_hint"]
    assert action["command"] == (
        "python tools/paper_preflight.py --strategy target_weight_best "
        "--with-pilot-check --send-test-notification"
    )
    assert action["order_safety"] == "no_order"
    assert report["operator_runbook"]["current_priority_action"]["setup_required"] is True
    assert report["operator_runbook"]["sequence"][2]["setup_required"] is True


def test_build_current_blockers_report_promotes_discord_retest_when_configured():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "b" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "status": "BLOCKED",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 3,
        },
        "decision": {
            "blocking_reasons": ["notifier: webhook configured but test send not verified"],
        },
        "operator_commands": {},
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["desc"] == "Discord webhook 도달성 확인 preflight 실행"
    assert action["setup_required"] is False
    assert "required_env" not in action
    assert action["command"].endswith("--with-pilot-check --send-test-notification")


def test_build_current_blockers_report_promotes_ready_execute_from_daily_ops(monkeypatch):
    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr("tools.evaluate_and_promote._current_kst_date", lambda: "2026-05-18")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "c" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {
            "verified_pilot_days": 12,
            "shadow_days": 3,
        },
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["desc"].startswith("READY_TO_EXECUTE")
    assert action["order_safety"] == "paper_order_only"
    assert action["requires"] == "daily_ops_summary.status == READY_TO_EXECUTE"
    assert action["command"].endswith("--execute --collect-evidence")


def test_build_current_blockers_report_marks_recorded_pilot_day_from_daily_ops(monkeypatch):
    import tools.evaluate_and_promote as eap

    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr(eap, "_current_kst_date", lambda: "2026-05-18")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "d" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "next_operator_trade_day": "2026-05-19",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {
            "verified_pilot_days": 1,
            "shadow_days": 2,
        },
        "decision": {
            "blocking_reasons": [],
            "post_evidence_diagnostics": [
                "execution_idempotency: duplicate",
                "pilot_authorization_snapshot: stale same-day approval",
            ],
        },
        "operator_commands": {
            "next_daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-19 "
                "--daily-ops-summary"
            ),
            "next_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-19 "
                "--readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["daily_ops_status"] == "PILOT_EVIDENCE_RECORDED"
    assert action["next_operator_trade_day"] == "2026-05-19"
    assert action["order_safety"] == "no_order"
    assert action["requires"] == "next KRX business day fresh readiness"
    assert action["not_before_date"] == "2026-05-19"
    assert action["premature_run_guard"] == "target_weight_future_as_of_date_blocked"
    assert action["command"].startswith("# blocked: not before 2026-05-19")
    assert action["scheduled_command"].endswith("--as-of-date 2026-05-19 --daily-ops-summary")
    assert action["follow_up"].startswith("# blocked: not before 2026-05-19")
    assert action["scheduled_follow_up"].endswith("--as-of-date 2026-05-19 --readiness-audit")
    assert report["operator_runbook"]["sequence"][2]["not_before_date"] == "2026-05-19"
    assert (
        report["operator_runbook"]["sequence"][2]["premature_run_guard"]
        == "target_weight_future_as_of_date_blocked"
    )
    assert report["operator_runbook"]["sequence"][2]["command"].startswith(
        "# blocked: not before 2026-05-19"
    )
    assert report["operator_runbook"]["sequence"][2]["scheduled_command"].endswith(
        "--as-of-date 2026-05-19 --daily-ops-summary"
    )
    assert report["operator_runbook"]["sequence"][2]["scheduled_follow_up"].endswith(
        "--as-of-date 2026-05-19 --readiness-audit"
    )
    assert report["operator_runbook"]["sequence"][3]["command"].startswith(
        "# blocked: not before 2026-05-19"
    )
    assert report["operator_runbook"]["sequence"][3]["scheduled_command"].endswith(
        "--as-of-date 2026-05-19 --readiness-audit"
    )
    assert report["next_actions"][1]["command"].startswith("# blocked:")
    assert report["next_actions"][1]["order_safety"] == "no_order"


def test_build_current_blockers_report_releases_recorded_pilot_day_on_not_before_date(monkeypatch):
    import tools.evaluate_and_promote as eap

    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr(eap, "_current_kst_date", lambda: "2026-05-19")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "d" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "next_operator_trade_day": "2026-05-19",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {
            "verified_pilot_days": 1,
            "shadow_days": 2,
        },
        "decision": {
            "blocking_reasons": [],
            "post_evidence_diagnostics": [
                "execution_idempotency: duplicate",
                "pilot_authorization_snapshot: stale same-day approval",
            ],
        },
        "operator_commands": {
            "next_daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-19 "
                "--daily-ops-summary"
            ),
            "next_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-19 "
                "--readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["daily_ops_status"] == "PILOT_EVIDENCE_RECORDED"
    assert action["order_safety"] == "no_order"
    assert action["requires"] == "current KRX business day fresh readiness"
    assert "not_before_date" not in action
    assert "premature_run_guard" not in action
    assert action["command"].endswith("--as-of-date 2026-05-19 --daily-ops-summary")
    assert action["follow_up"].endswith("--as-of-date 2026-05-19 --readiness-audit")
    assert not action["command"].startswith("# blocked:")
    assert report["operator_runbook"]["sequence"][2]["command"].endswith(
        "--as-of-date 2026-05-19 --daily-ops-summary"
    )
    assert report["operator_runbook"]["sequence"][2]["follow_up"].endswith(
        "--as-of-date 2026-05-19 --readiness-audit"
    )
    assert "not_before_date" not in report["operator_runbook"]["sequence"][2]


def test_build_current_blockers_report_repairs_next_check_command_scope_mismatch(monkeypatch):
    import tools.evaluate_and_promote as eap

    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr(eap, "_current_kst_date", lambda: "2026-05-19")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "d" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-18",
        "next_operator_trade_day": "2026-05-19",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 2},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "next_daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best_shadow --as-of-date 2026-05-18 "
                "--readiness-audit"
            ),
            "next_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-18 "
                "--daily-ops-summary"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["command"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --as-of-date 2026-05-19 --daily-ops-summary"
    )
    assert action["follow_up"] == (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_best --as-of-date 2026-05-19 --readiness-audit"
    )
    assert report["operator_runbook"]["sequence"][2]["command"] == action["command"]


def test_build_current_blockers_report_prioritizes_invalid_pilot_evidence_from_daily_ops():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_INVALID",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_benchmark_status_not_final": 1},
        },
        "decision": {"blocking_reasons": ["execution_idempotency: duplicate"]},
        "operator_commands": {
            "daily_ops_summary": "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best --daily-ops-summary",
            "finalize_pilot_evidence": (
                "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
                "--finalize-pilot-evidence --finalize-date 2026-05-18"
            ),
            "repair_pilot_evidence": (
                "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
                "--repair-pilot-evidence --repair-date 2026-05-18"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["daily_ops_status"] == "PILOT_EVIDENCE_INVALID"
    assert action["order_safety"] == "no_order"
    assert action["requires"] == "benchmark/portfolio evidence finalization"
    assert action["invalid_reasons"] == {"target_weight_benchmark_status_not_final": 1}
    assert action["command"].endswith("--finalize-pilot-evidence --finalize-date 2026-05-18")
    assert action["follow_up"].endswith("--daily-ops-summary")


def test_current_blockers_waits_when_finalize_missing_performance():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--finalize-pilot-evidence --finalize-date 2026-05-20"
    )
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-20.json",
        "trade_day": "2026-05-20",
        "status": "PILOT_EVIDENCE_INVALID",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_benchmark_status_not_final": 1},
        },
        "decision": {"blocking_reasons": ["execution_idempotency: duplicate"]},
        "operator_commands": {
            "daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --daily-ops-summary"
            ),
            "finalize_pilot_evidence": finalize_command,
        },
    }
    latest_finalize_report = {
        "artifact_type": "target_weight_pilot_evidence_finalize",
        "candidate_id": "target_weight_best",
        "finalize_date": "2026-05-20",
        "generated_at": "2026-05-20T15:40:00",
        "status": "blocked",
        "reason": (
            "target_weight_pilot_evidence_finalize_missing_performance: "
            "total_value/daily_return unavailable"
        ),
        "performance_evidence_status": {
            "source_record_fields_present": ["cash"],
            "source_record_fields_usable": ["cash"],
            "source_record_fields_unusable": [],
            "portfolio_metrics_checked": True,
            "portfolio_metrics_probe_status": "missing_current_snapshot_after_trades",
            "portfolio_metrics_probe_reason": (
                "trades exist after previous snapshot but current snapshot is missing"
            ),
            "portfolio_metrics_current_snapshot_found": False,
            "portfolio_metrics_previous_snapshot_found": True,
            "portfolio_metrics_previous_snapshot_at": "2026-05-19T15:35:00",
            "portfolio_metrics_trades_today": 1,
            "portfolio_metrics_trades_since_previous": 1,
            "portfolio_metrics_fields_present": [],
            "missing_fields_after_probe": ["total_value", "daily_return"],
        },
        "source_path": "reports/paper_runtime/target_weight_pilot_evidence_finalize_target_weight_best_2026-05-20.json",
    }

    report = build_current_blockers_report(
        blocker_summary,
        latest_daily_ops=latest_daily_ops,
        latest_finalize_report=latest_finalize_report,
    )

    action = report["next_actions"][0]
    assert action["command"].startswith(
        "# blocked: final performance evidence unavailable"
    )
    assert action["scheduled_command"] == finalize_command
    assert action["performance_evidence_guard"] == (
        "target_weight_pilot_evidence_finalize_missing_performance"
    )
    assert action["requires"] == "final portfolio performance evidence available"
    assert action["finalize_report_status"] == "blocked"
    assert action["finalize_report_generated_at"] == "2026-05-20T15:40:00"
    assert action["finalize_source_record_fields_present"] == ["cash"]
    assert action["finalize_source_record_fields_usable"] == ["cash"]
    assert action["finalize_source_record_fields_unusable"] == []
    assert action["finalize_portfolio_metrics_checked"] is True
    assert action["finalize_portfolio_metrics_probe_status"] == (
        "missing_current_snapshot_after_trades"
    )
    assert action["finalize_portfolio_metrics_recovery_hint"] == (
        "run end-of-day portfolio snapshot capture for the trade day"
    )
    assert action["finalize_portfolio_metrics_current_snapshot_found"] is False
    assert action["finalize_portfolio_metrics_previous_snapshot_found"] is True
    assert action["finalize_portfolio_metrics_previous_snapshot_at"] == (
        "2026-05-19T15:35:00"
    )
    assert action["finalize_portfolio_metrics_trades_today"] == 1
    assert action["finalize_portfolio_metrics_trades_since_previous"] == 1
    assert action["finalize_portfolio_metrics_fields_present"] == []
    assert action["finalize_missing_performance_fields"] == [
        "total_value",
        "daily_return",
    ]
    assert action["finalize_report_diagnostics_status"] == "present"


def test_current_blockers_refreshes_legacy_finalize_report_without_diagnostics():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--finalize-pilot-evidence --finalize-date 2026-05-20"
    )
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-20.json",
        "trade_day": "2026-05-20",
        "status": "PILOT_EVIDENCE_INVALID",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_benchmark_status_not_final": 1},
        },
        "decision": {"blocking_reasons": ["execution_idempotency: duplicate"]},
        "operator_commands": {
            "daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --daily-ops-summary"
            ),
            "finalize_pilot_evidence": finalize_command,
        },
    }
    latest_finalize_report = {
        "artifact_type": "target_weight_pilot_evidence_finalize",
        "candidate_id": "target_weight_best",
        "finalize_date": "2026-05-20",
        "generated_at": "2026-05-20T10:17:51",
        "status": "blocked",
        "reason": (
            "target_weight_pilot_evidence_finalize_missing_performance: "
            "total_value/daily_return unavailable"
        ),
        "source_path": "reports/paper_runtime/target_weight_pilot_evidence_finalize_target_weight_best_2026-05-20.json",
    }

    report = build_current_blockers_report(
        blocker_summary,
        latest_daily_ops=latest_daily_ops,
        latest_finalize_report=latest_finalize_report,
    )

    action = report["next_actions"][0]
    assert action["command"] == finalize_command
    assert action["scheduled_command"] == finalize_command
    assert action["requires"] == "finalize performance diagnostics refresh"
    assert action["performance_evidence_guard"] == (
        "target_weight_pilot_evidence_finalize_missing_performance"
    )
    assert action["finalize_report_diagnostics_status"] == "missing"
    assert action["finalize_diagnostics_refresh_command"] == finalize_command
    assert action["finalize_missing_performance_fields"] == []


def test_current_blockers_prioritizes_finalize_for_performance_missing_reason():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--finalize-pilot-evidence --finalize-date 2026-05-20"
    )
    repair_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--repair-pilot-evidence --repair-date 2026-05-20"
    )
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-20.json",
        "trade_day": "2026-05-20",
        "status": "PILOT_EVIDENCE_INVALID",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_daily_return_missing": 1},
        },
        "decision": {"blocking_reasons": ["execution_idempotency: duplicate"]},
        "operator_commands": {
            "daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --daily-ops-summary"
            ),
            "finalize_pilot_evidence": finalize_command,
            "repair_pilot_evidence": repair_command,
        },
    }

    report = build_current_blockers_report(
        blocker_summary,
        latest_daily_ops=latest_daily_ops,
    )

    action = report["next_actions"][0]
    assert action["command"] == finalize_command
    assert action["command"] != repair_command
    assert action["requires"] == "benchmark/portfolio evidence finalization"
    assert action["follow_up"].endswith("--daily-ops-summary")


def test_current_blockers_routes_db_persistence_gap_to_diagnostics():
    from tools.evaluate_and_promote import build_current_blockers_report

    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    diagnose_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--diagnose-portfolio-snapshot --snapshot-date 2026-05-20"
    )
    repair_command = (
        "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
        "--repair-pilot-evidence --repair-date 2026-05-20"
    )
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-20.json",
        "trade_day": "2026-05-20",
        "status": "PILOT_EVIDENCE_INVALID",
        "evidence_progress": {
            "verified_pilot_days": 0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_db_persistence_complete_false": 1},
        },
        "decision": {"blocking_reasons": ["execution_idempotency: duplicate"]},
        "operator_commands": {
            "daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --daily-ops-summary"
            ),
            "diagnose_portfolio_snapshot": diagnose_command,
            "repair_pilot_evidence": repair_command,
        },
    }

    report = build_current_blockers_report(
        blocker_summary,
        latest_daily_ops=latest_daily_ops,
    )

    action = report["next_actions"][0]
    assert action["command"] == diagnose_command
    assert action["command"] != repair_command
    assert action["order_safety"] == "no_order"
    assert action["requires"] == "database trade/position persistence proof"
    assert action["db_persistence_guard"] == "target_weight_db_persistence_proof_required"
    assert "cannot be repaired from artifact" in action["blocked_repair_command"]
    assert action["follow_up"].endswith("--daily-ops-summary")


def test_build_current_blockers_report_schedules_next_day_after_repaired_non_promotable_daily_ops(monkeypatch):
    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr("tools.evaluate_and_promote._current_kst_date", lambda: "2026-05-18")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-13T14:07:37",
        "source_artifact_hash": "e" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {
                    "benchmark_excess_return": 48.7,
                    "sharpe": 1.57,
                    "mdd": -17.18,
                },
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-18.json",
        "trade_day": "2026-05-18",
        "next_operator_trade_day": "2026-05-19",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
        "evidence_progress": {
            "target_days": 60,
            "verified_pilot_days": 0,
            "remaining_pilot_days": 60,
            "progress_ratio": 0.0,
            "shadow_days": 2,
            "invalid_execution_days": 1,
            "invalid_reasons": {"target_weight_repaired_performance_not_promotable": 1},
            "repaired_pilot_days": 1,
        },
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "enable_suggested_caps": (
                "python tools/paper_pilot_control.py --strategy target_weight_best --enable"
            ),
            "next_daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
                "--as-of-date 2026-05-19 --daily-ops-summary"
            ),
            "next_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py --candidate-id target_weight_best "
                "--as-of-date 2026-05-19 --readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    action = report["next_actions"][0]
    assert action["daily_ops_status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"
    assert action["target_days"] == 60
    assert action["remaining_pilot_days"] == 60
    assert action["progress_ratio"] == 0.0
    assert action["invalid_execution_days"] == 1
    assert action["repaired_pilot_days"] == 1
    assert action["invalid_reasons"] == {"target_weight_repaired_performance_not_promotable": 1}
    assert action["order_safety"] == "no_order"
    assert action["not_before_date"] == "2026-05-19"
    assert action["command"].startswith("# blocked: not before 2026-05-19")
    assert action["scheduled_command"].endswith("--as-of-date 2026-05-19 --daily-ops-summary")
    assert "복구 보존" in action["desc"]
    sanitized_commands = report["operator_runbook"]["latest_daily_ops"]["operator_commands"]
    assert sanitized_commands["enable_suggested_caps"].startswith(
        "# blocked: repaired pilot_paper evidence already recorded"
    )
    assert "--strategy target_weight_best --enable" not in sanitized_commands["enable_suggested_caps"]


def test_load_current_blockers_from_artifacts_uses_latest_daily_ops(tmp_path, monkeypatch):
    import tools.evaluate_and_promote as ep

    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")
    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:00:00",
        "trade_day": "2026-05-18",
        "status": "READY_TO_ENABLE_CAPS",
        "evidence_progress": {"verified_pilot_days": 0, "shadow_days": 3},
        "decision": {"blocking_reasons": ["pilot_authorization: no active capped pilot authorization"]},
        "operator_commands": {
            "enable_suggested_caps": (
                f"python tools/paper_pilot_control.py --strategy {strategy} "
                "--enable --from 2026-05-18 --to 2026-05-31"
            ),
            "rerun_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy} --readiness-audit"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)

    action = report["next_actions"][0]
    assert action["desc"].startswith("readiness artifact의 추천 cap 승인")
    assert "--enable" in action["command"]
    assert action["follow_up"].endswith("--readiness-audit")
    assert report["next_actions"][1]["command"].startswith("# blocked:")


def test_current_blockers_blocks_stale_ready_to_enable_caps_command(monkeypatch):
    import tools.evaluate_and_promote as ep

    from tools.evaluate_and_promote import build_current_blockers_report

    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-20")
    blocker_summary = {
        "artifact_type": "promotion_blocker_summary",
        "schema_version": 1,
        "generated_at": "2026-05-19T14:07:37",
        "source_artifact_hash": "d" * 64,
        "summary": {
            "total_strategies": 1,
            "status_counts": {"provisional_paper_candidate": 1},
            "live_ready_count": 0,
            "blocked_from_live_count": 1,
        },
        "strategies": {
            "target_weight_best": {
                "status": "provisional_paper_candidate",
                "allowed_modes": ["backtest", "paper"],
                "metrics": {"benchmark_excess_return": 48.7},
            },
        },
    }
    latest_daily_ops = {
        "source_path": "reports/target_weight_daily_ops_summary_target_weight_best_2026-05-19.json",
        "candidate_id": "target_weight_best",
        "trade_day": "2026-05-19",
        "status": "READY_TO_ENABLE_CAPS",
        "evidence_progress": {"verified_pilot_days": 0, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "enable_suggested_caps": (
                "python tools/paper_pilot_control.py --strategy target_weight_best "
                "--enable --from 2026-05-19 --to 2026-08-10"
            ),
            "rerun_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --readiness-audit"
            ),
        },
    }

    report = build_current_blockers_report(blocker_summary, latest_daily_ops=latest_daily_ops)

    commands = report["operator_runbook"]["latest_daily_ops"]["operator_commands"]
    assert commands["enable_suggested_caps"].startswith(
        "# blocked: daily_ops_summary.trade_day is stale"
    )
    assert "--enable --from 2026-05-19" not in commands["enable_suggested_caps"]
    action = report["next_actions"][0]
    assert action["command"].startswith("# blocked:")
    assert action["order_safety"] == "no_order"


def test_load_current_blockers_from_artifacts_reads_paper_runtime_daily_ops(tmp_path):
    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "BLOCKED",
        "evidence_progress": {"verified_pilot_days": 0, "shadow_days": 0},
        "decision": {
            "blocking_reasons": [
                "launch_readiness: clean_final_days 0/3",
                "notifier: Discord webhook 미설정",
            ],
        },
        "operator_commands": {
            "collect_shadow_days": (
                f"python tools/target_weight_rotation_pilot.py --candidate-id {strategy} "
                "--shadow-days 3 --shadow-end-date 2026-05-18"
            ),
            "execute_capped_paper": (
                f"python tools/target_weight_rotation_pilot.py --candidate-id {strategy} "
                "--execute --collect-evidence"
            ),
        },
    }
    (paper_runtime / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)

    action = report["next_actions"][0]
    assert action["source_path"].endswith(
        f"paper_runtime/target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    ) or action["source_path"].endswith(
        f"paper_runtime\\target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    )
    assert action["desc"].startswith("target-weight shadow 3일 수집")
    assert action["command"].endswith("--shadow-days 3 --shadow-end-date 2026-05-18")
    assert report["next_actions"][1]["command"].startswith("# blocked:")
    assert (
        report["operator_runbook"]["latest_daily_ops"]["operator_commands"]["execute_capped_paper"]
        .startswith("# blocked:")
    )


def test_load_current_blockers_from_artifacts_uses_posix_source_paths(tmp_path):
    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "BLOCKED",
        "evidence_progress": {"verified_pilot_days": 0, "shadow_days": 0},
        "decision": {"blocking_reasons": ["launch_readiness: clean_final_days 0/3"]},
        "operator_commands": {
            "collect_shadow_days": (
                f"python tools/target_weight_rotation_pilot.py --candidate-id {strategy} "
                "--shadow-days 3 --shadow-end-date 2026-05-18"
            ),
        },
    }
    (
        paper_runtime / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)

    action_source = report["next_actions"][0]["source_path"]
    runbook_source = report["operator_runbook"]["latest_daily_ops"]["source_path"]
    assert "\\" not in action_source
    assert "\\" not in runbook_source
    assert action_source.endswith(
        f"paper_runtime/target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    )
    assert runbook_source == action_source


def test_load_current_blockers_from_artifacts_prioritizes_daily_ops_failure_when_no_summary(
    tmp_path,
):
    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    failure_path = (
        paper_runtime
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260518100500.json"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-05-18T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-05-18",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: market data stale",
                "error": {
                    "type": "ValueError",
                    "message": "market data stale",
                },
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        f"--candidate-id {strategy} --as-of-date 2026-05-18 "
                        "--daily-ops-summary"
                    ),
                    "readiness_audit": (
                        "python tools/target_weight_rotation_pilot.py "
                        f"--candidate-id {strategy} --as-of-date 2026-05-18 "
                        "--readiness-audit"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)

    action = report["next_actions"][0]
    runbook = report["operator_runbook"]
    assert action["source"] == "latest_daily_ops_failure"
    assert action["daily_ops_status"] == "FAILED"
    assert action["failure_reason"] == (
        "target_weight_daily_ops_summary_blocked: market data stale"
    )
    assert action["failure_error"] == "ValueError: market data stale"
    assert action["source_path"].endswith(failure_path.name)
    assert action["command"].endswith("--as-of-date 2026-05-18 --daily-ops-summary")
    assert action["follow_up"].endswith("--as-of-date 2026-05-18 --readiness-audit")
    assert action["order_safety"] == "no_order"
    assert runbook["latest_daily_ops_failure"]["source_path"].endswith(failure_path.name)
    assert runbook["current_priority_action"]["source"] == "latest_daily_ops_failure"
    assert report["next_actions"][1]["command"].startswith(
        "# blocked: latest daily ops summary failure unresolved"
    )
    assert report["next_actions"][1]["order_safety"] == "no_order"


def test_current_blockers_blocks_requested_trade_day_unavailable_failure(
    tmp_path,
):
    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    command = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --as-of-date 2026-05-20 --daily-ops-summary"
    )
    follow_up = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --as-of-date 2026-05-20 --readiness-audit"
    )
    failure_path = (
        paper_runtime
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260520090500.json"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-05-20T09:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-05-20",
                "status": "BLOCKED",
                "reason": (
                    "target_weight_daily_ops_summary_blocked: "
                    "target_weight_requested_trade_day_unavailable: "
                    "readiness audit as_of_date=2026-05-20 resolved_trade_day=2026-05-19"
                ),
                "operator_commands": {
                    "daily_ops_summary": command,
                    "readiness_audit": follow_up,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)

    action = report["next_actions"][0]
    assert action["source"] == "latest_daily_ops_failure"
    assert action["command"].startswith(
        "# blocked: requested trade-day market data unavailable"
    )
    assert action["scheduled_command"] == command
    assert action["follow_up"].startswith(
        "# blocked: requested trade-day market data unavailable"
    )
    assert action["scheduled_follow_up"] == follow_up
    assert action["market_data_guard"] == "target_weight_requested_trade_day_unavailable"
    assert action["requires"] == "requested trade-day market data available"
    runbook_action = report["operator_runbook"]["current_priority_action"]
    assert runbook_action["command"] == action["command"]
    assert runbook_action["scheduled_command"] == command
    assert runbook_action["market_data_guard"] == action["market_data_guard"]


def test_current_blockers_daily_ops_failure_payload_excludes_volatile_mtime(
    tmp_path,
):
    import os

    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_current_blockers_artifact,
        write_current_blockers_report,
    )

    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    failure_path = (
        paper_runtime
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260518100500.json"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-05-18T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-05-18",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: market data stale",
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        f"--candidate-id {strategy} --as-of-date 2026-05-18 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = load_current_blockers_from_artifacts(promotion_dir)
    current_blockers_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(report, current_blockers_path)
    os.utime(failure_path, (3_000, 3_000))

    failure = report["operator_runbook"]["latest_daily_ops_failure"]
    assert "source_mtime" not in failure
    assert validate_current_blockers_artifact(promotion_dir, current_blockers_path) == []


def test_load_current_blockers_from_artifacts_prioritizes_newer_daily_ops_failure(
    tmp_path,
    monkeypatch,
):
    import os
    import tools.evaluate_and_promote as ep

    from tools.evaluate_and_promote import load_current_blockers_from_artifacts

    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")
    strategy = "target_weight_best"
    promotion_dir = tmp_path / "promotion"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    paper_runtime = tmp_path / "paper_runtime"
    paper_runtime.mkdir()
    daily_ops_path = paper_runtime / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    failure_path = (
        paper_runtime
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260518101000.json"
    )
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:00:00",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy} --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }
    daily_ops_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-05-18T10:10:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-05-18",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: missing readiness audit",
                "error": {
                    "type": "ValueError",
                    "message": "missing readiness audit",
                },
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        f"--candidate-id {strategy} --as-of-date 2026-05-18 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(daily_ops_path, (1_000, 1_000))
    os.utime(failure_path, (2_000, 2_000))

    report = load_current_blockers_from_artifacts(promotion_dir)

    action = report["next_actions"][0]
    runbook = report["operator_runbook"]
    assert action["source"] == "latest_daily_ops_failure"
    assert action["failure_reason"] == (
        "target_weight_daily_ops_summary_blocked: missing readiness audit"
    )
    assert runbook["latest_daily_ops"]["status"] == "READY_TO_EXECUTE"
    assert runbook["latest_daily_ops_failure"]["source_path"].endswith(failure_path.name)
    assert runbook["commands"]["execute_capped_paper_after_ready"].startswith("# blocked:")
    assert report["next_actions"][1]["command"].startswith("# blocked:")
    assert report["next_actions"][1]["order_safety"] == "no_order"


def test_load_latest_target_weight_daily_ops_ignores_future_trade_day(tmp_path, monkeypatch):
    import os
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    current_daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 2},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    future_daily_ops = {
        **current_daily_ops,
        "generated_at": "2026-05-18T10:10:21",
        "trade_day": "2026-05-19",
        "status": "BLOCKED",
        "operator_commands": {
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute"
        },
    }
    malformed_daily_ops = {
        **current_daily_ops,
        "generated_at": "2026-05-18T10:15:21",
        "trade_day": "not-a-date",
        "status": "READY_TO_EXECUTE",
    }
    missing_trade_day_daily_ops = {
        key: value for key, value in current_daily_ops.items() if key != "trade_day"
    }
    missing_trade_day_daily_ops.update(
        {
            "generated_at": "2026-05-18T10:20:21",
            "status": "READY_TO_EXECUTE",
        }
    )
    current_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    future_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-19.json"
    malformed_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_malformed.json"
    missing_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_missing_trade_day.json"
    current_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(current_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    future_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(future_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    malformed_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(malformed_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    missing_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(missing_trade_day_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(current_path, (1_000, 1_000))
    os.utime(future_path, (2_000, 2_000))
    os.utime(malformed_path, (3_000, 3_000))
    os.utime(missing_path, (4_000, 4_000))
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    assert latest["trade_day"] == "2026-05-18"
    assert latest["status"] == "PILOT_EVIDENCE_RECORDED"


def test_load_latest_target_weight_daily_ops_prefers_latest_trade_day_over_mtime(tmp_path, monkeypatch):
    import os
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    older_daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    newer_daily_ops = {
        **older_daily_ops,
        "generated_at": "2026-05-19T10:05:21",
        "trade_day": "2026-05-19",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
    }
    older_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    newer_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-19.json"
    older_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(older_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    newer_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(newer_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(newer_path, (1_000, 1_000))
    os.utime(older_path, (2_000, 2_000))
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-19")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    assert latest["trade_day"] == "2026-05-19"
    assert latest["status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"


def test_load_latest_target_weight_daily_ops_prefers_latest_generated_same_trade_day(
    tmp_path,
    monkeypatch,
):
    import os
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    earlier_daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:00:00",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    later_daily_ops = {
        **earlier_daily_ops,
        "generated_at": "2026-05-18T10:10:00",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
    }
    earlier_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_earlier.json"
    later_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_later.json"
    earlier_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(earlier_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    later_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(later_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(later_path, (1_000, 1_000))
    os.utime(earlier_path, (2_000, 2_000))
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    assert latest["status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"
    assert latest["generated_at"] == "2026-05-18T10:10:00"


def test_load_latest_target_weight_daily_ops_skips_summary_hash_mismatch(tmp_path, monkeypatch):
    import os
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    valid_daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:00:21",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    tampered_daily_ops = {
        **valid_daily_ops,
        "generated_at": "2026-05-18T10:10:21",
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }
    valid_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json"
    tampered_path = tmp_path / f"target_weight_daily_ops_summary_{strategy}_tampered.json"
    valid_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(valid_daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    tampered_payload = _daily_ops_with_summary_hash(tampered_daily_ops)
    tampered_payload["next_step"] = "tampered after hash"
    tampered_path.write_text(json.dumps(tampered_payload, ensure_ascii=False), encoding="utf-8")
    os.utime(valid_path, (1_000, 1_000))
    os.utime(tampered_path, (2_000, 2_000))
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    assert latest["status"] == "PILOT_EVIDENCE_RECORDED"


def test_validate_target_weight_daily_ops_artifacts_detects_summary_hash_mismatch(tmp_path):
    from tools.evaluate_and_promote import validate_target_weight_daily_ops_artifacts

    strategy = "target_weight_best"
    valid_daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "schema_version": 1,
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:00:21",
        "trade_day": "2026-05-18",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    tampered_daily_ops = _daily_ops_with_summary_hash(valid_daily_ops)
    tampered_daily_ops["status"] = "READY_TO_EXECUTE"
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(tampered_daily_ops, ensure_ascii=False),
        encoding="utf-8",
    )
    failure_artifact = {
        "artifact_type": "target_weight_daily_ops_summary_failure",
        "candidate_id": strategy,
        "error": "operator failure artifact is not a summary",
    }
    (
        tmp_path / f"target_weight_daily_ops_summary_failure_{strategy}_20260518100000.json"
    ).write_text(json.dumps(failure_artifact, ensure_ascii=False), encoding="utf-8")

    issues = validate_target_weight_daily_ops_artifacts(tmp_path)

    assert len(issues) == 1
    assert "summary_hash 불일치 또는 누락" in issues[0]


def test_load_latest_target_weight_daily_ops_blocks_stale_ready_execute(tmp_path, monkeypatch):
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-19")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    assert latest["status"] == "READY_TO_EXECUTE"
    assert latest["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: daily_ops_summary.trade_day is stale"
    )


def test_load_latest_target_weight_daily_ops_blocks_stale_generated_ready_execute(
    tmp_path,
    monkeypatch,
):
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_best --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ep,
        "_current_kst_datetime",
        lambda: datetime(2026, 5, 18, 10, 36, tzinfo=ep.KST),
    )

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    command = latest["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_summary.generated_at is stale")
    assert "rerun daily ops summary before action" in command


def test_load_latest_target_weight_daily_ops_blocks_ready_execute_scope_mismatch(tmp_path, monkeypatch):
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id other_strategy --as-of-date 2026-05-17 "
                "--execute --collect-evidence"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    command = latest["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_execute_command_unavailable")
    assert "candidate_id mismatch" in command
    assert "as_of_date mismatch" in command


def test_load_latest_target_weight_daily_ops_blocks_candidate_prefix_collision(
    tmp_path,
    monkeypatch,
):
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy}_shadow --as-of-date 2026-05-18 "
                "--execute --collect-evidence"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    command = latest["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_execute_command_unavailable")
    assert "candidate_id mismatch" in command


def test_load_latest_target_weight_daily_ops_blocks_stale_generated_ready_enable(
    tmp_path,
    monkeypatch,
):
    import tools.evaluate_and_promote as ep

    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_ENABLE_CAPS",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "enable_suggested_caps": (
                "python tools/paper_pilot_control.py "
                f"--strategy {strategy} --enable --from 2026-05-18 --to 2026-08-10"
            ),
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ep,
        "_current_kst_datetime",
        lambda: datetime(2026, 5, 18, 10, 36, tzinfo=ep.KST),
    )

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    command = latest["operator_commands"]["enable_suggested_caps"]
    assert command.startswith("# blocked: daily_ops_summary.generated_at is stale")


def test_load_latest_target_weight_daily_ops_blocks_ready_enable_scope_mismatch(tmp_path, monkeypatch):
    import tools.evaluate_and_promote as ep

    monkeypatch.setattr(ep, "_current_kst_date", lambda: "2026-05-18")
    strategy = "target_weight_best"
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-05-18T10:05:21",
        "trade_day": "2026-05-18",
        "status": "READY_TO_ENABLE_CAPS",
        "evidence_progress": {"verified_pilot_days": 12, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "enable_suggested_caps": "python tools/paper_pilot_control.py --strategy other_strategy",
        },
    }
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-18.json").write_text(
        json.dumps(_daily_ops_with_summary_hash(daily_ops), ensure_ascii=False),
        encoding="utf-8",
    )

    latest = ep._load_latest_target_weight_daily_ops(strategy, tmp_path)

    assert latest is not None
    command = latest["operator_commands"]["enable_suggested_caps"]
    assert command.startswith("# blocked: daily_ops_enable_command_unavailable")
    assert "candidate_id mismatch" in command
    assert "missing --enable" in command


def test_static_paper_experiment_manifest_uses_explicit_target_weight_candidate_and_blocked_execute():
    manifest_path = Path("reports/paper_experiment_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pilot = manifest["target_weight_pilot"]
    candidate_id = pilot["candidate_id"]
    run_modes = pilot["run_modes"]

    for key in ("shadow_bootstrap", "daily_ops_summary", "readiness_audit"):
        assert f"--candidate-id {candidate_id}" in run_modes[key]

    assert run_modes["execute_capped_paper"].startswith("# blocked:")
    assert "--execute --collect-evidence" not in run_modes["execute_capped_paper"]


def test_current_blockers_check_detects_stale_report(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_current_blockers_artifact,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    _promotions, _metrics, summary = _write_consistent_promotion_artifacts(
        promotion_dir,
        {"candidate": _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    output_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        output_path,
    )

    assert validate_current_blockers_artifact(promotion_dir, output_path) == []

    (promotion_dir / "promotion_blocker_summary.json").write_text(
        json.dumps({
            **summary,
            "source_artifact_hash": "b" * 64,
            "summary": {
                **summary["summary"],
                "live_ready_count": 1,
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_current_blockers_artifact(promotion_dir, output_path)

    assert any("promotion blocker summary 동기화 실패" in issue for issue in issues)
    assert any("source_artifact_hash 불일치" in issue for issue in issues)
    assert any("summary 내용 불일치" in issue for issue in issues)


def test_current_blockers_check_fails_when_blocker_summary_strategy_actions_are_stale(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_current_blockers_artifact,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    _promotions, _metrics, _summary = _write_consistent_promotion_artifacts(
        promotion_dir,
        {"target_weight_candidate": _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    output_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        output_path,
    )

    stale_summary = json.loads((promotion_dir / "promotion_blocker_summary.json").read_text(encoding="utf-8"))
    stale_summary["strategies"]["target_weight_candidate"]["next_action"] = (
        "canonical 데이터/벤치마크 coverage 재생성 후 재평가"
    )
    (promotion_dir / "promotion_blocker_summary.json").write_text(
        json.dumps(stale_summary, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_current_blockers_artifact(promotion_dir, output_path)

    assert any("promotion blocker summary 동기화 실패" in issue for issue in issues)
    assert any("strategies 내용 불일치" in issue for issue in issues)


def test_validate_metrics_summary_source_artifact_sync_detects_stale_benchmark_fields(tmp_path):
    from tools.evaluate_and_promote import validate_metrics_summary_source_artifact_sync

    promotion_dir = tmp_path / "promotion"
    strategy = "paper_ready_strategy"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    benchmark_path = promotion_dir / "benchmark_comparison.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark["strategy_excess_return_pct"][strategy] = -10.0
    benchmark_path.write_text(
        json.dumps(benchmark, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_metrics_summary_source_artifact_sync(promotion_dir)

    assert any("benchmark_excess_return 불일치" in issue for issue in issues)


def test_validate_metrics_summary_source_artifact_sync_detects_stale_walk_forward_fields(tmp_path):
    from tools.evaluate_and_promote import validate_metrics_summary_source_artifact_sync

    promotion_dir = tmp_path / "promotion"
    strategy = "paper_ready_strategy"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    walk_forward_path = promotion_dir / "walk_forward_summary.json"
    walk_forward = json.loads(walk_forward_path.read_text(encoding="utf-8"))
    walk_forward[strategy]["positive"] = 0
    walk_forward_path.write_text(
        json.dumps(walk_forward, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_metrics_summary_source_artifact_sync(promotion_dir)

    assert any("wf_positive_rate 불일치" in issue for issue in issues)


def test_promotion_artifacts_refresh_rejects_metrics_source_artifact_mismatch(tmp_path):
    from tools.evaluate_and_promote import refresh_promotion_artifacts_from_existing_inputs

    promotion_dir = tmp_path / "promotion"
    strategy = "paper_ready_strategy"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    benchmark_path = promotion_dir / "benchmark_comparison.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark["strategy_excess_sharpe"][strategy] = -1.0
    benchmark_path.write_text(
        json.dumps(benchmark, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="metrics source artifact 동기화 실패"):
        refresh_promotion_artifacts_from_existing_inputs(
            promotion_dir,
            evidence_dir=tmp_path / "paper_evidence",
            current_blockers_path=tmp_path / "current_blockers.json",
        )


def test_validate_promotion_operator_artifacts_detects_stale_promotion_result(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_promotion_operator_artifacts,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    strategy = "paper_ready_strategy"
    _promotions, _metrics, _summary = _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    current_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        current_path,
    )

    (promotion_dir / "promotion_result.json").write_text(
        json.dumps({
            strategy: {
                "status": "live_candidate",
                "allowed_modes": ["backtest", "paper", "live"],
                "reason": "live_candidate 충족",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_promotion_operator_artifacts(
        promotion_dir,
        current_path,
        now=datetime(2026, 5, 13, 15, 0, 0),
    )

    assert any("blocker summary 동기화 실패" in issue for issue in issues)
    assert any("promotion_result" in issue and "재계산 결과 불일치" in issue for issue in issues)


def test_validate_promotion_operator_artifacts_detects_stale_canonical_generated_at(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_promotion_operator_artifacts,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    _promotions, _metrics, _summary = _write_consistent_promotion_artifacts(
        promotion_dir,
        {"candidate": _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
        metadata=_promotion_metadata(generated_at="2026-05-01T09:00:00"),
    )
    current_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        current_path,
    )

    issues = validate_promotion_operator_artifacts(
        promotion_dir,
        current_path,
        now=datetime(2026, 5, 13, 12, 0, 0),
        max_artifact_age_days=7,
    )

    assert any("canonical artifact 최신성 실패" in issue for issue in issues)
    assert any("canonical artifact가 오래됨" in issue for issue in issues)


def test_validate_promotion_operator_artifacts_detects_stale_current_blockers(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_promotion_operator_artifacts,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    _promotions, _metrics, _summary = _write_consistent_promotion_artifacts(
        promotion_dir,
        {"candidate": _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    current_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        current_path,
    )

    assert validate_promotion_operator_artifacts(
        promotion_dir,
        current_path,
        now=datetime(2026, 5, 13, 15, 0, 0),
    ) == []

    stale_current = json.loads(current_path.read_text(encoding="utf-8"))
    stale_current["go_live"] = True
    current_path.write_text(
        json.dumps(stale_current, ensure_ascii=False),
        encoding="utf-8",
    )

    issues = validate_promotion_operator_artifacts(
        promotion_dir,
        current_path,
        now=datetime(2026, 5, 13, 15, 0, 0),
    )

    assert any("current blockers 동기화 실패" in issue for issue in issues)
    assert any("go_live 불일치" in issue for issue in issues)


def test_validate_promotion_operator_artifacts_detects_invalid_daily_ops_artifact(tmp_path):
    from tools.evaluate_and_promote import (
        load_current_blockers_from_artifacts,
        validate_promotion_operator_artifacts,
        write_current_blockers_report,
    )

    promotion_dir = tmp_path / "promotion"
    strategy = "target_weight_candidate"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=tmp_path / "paper_evidence",
    )
    daily_ops = {
        "artifact_type": "target_weight_daily_ops_summary",
        "schema_version": 1,
        "candidate_id": strategy,
        "generated_at": "2026-05-13T10:00:21",
        "trade_day": "2026-05-13",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "shadow_days": 3},
        "decision": {"blocking_reasons": []},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    tampered_daily_ops = _daily_ops_with_summary_hash(daily_ops)
    tampered_daily_ops["next_step"] = "tampered after hash"
    (tmp_path / f"target_weight_daily_ops_summary_{strategy}_2026-05-13.json").write_text(
        json.dumps(tampered_daily_ops, ensure_ascii=False),
        encoding="utf-8",
    )
    current_path = tmp_path / "current_blockers.json"
    write_current_blockers_report(
        load_current_blockers_from_artifacts(promotion_dir),
        current_path,
    )

    issues = validate_promotion_operator_artifacts(
        promotion_dir,
        current_path,
        now=datetime(2026, 5, 13, 15, 0, 0),
    )

    assert any("target-weight daily ops artifact 무결성 실패" in issue for issue in issues)
    assert any("summary_hash 불일치 또는 누락" in issue for issue in issues)


def test_validate_paper_evidence_operator_artifacts_warns_invalid_package(tmp_path):
    from tools.evaluate_and_promote import validate_paper_evidence_operator_artifacts

    promotion_dir = tmp_path / "promotion"
    evidence_dir = tmp_path / "paper_evidence"
    strategy = "paper_ready_strategy"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=evidence_dir,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / f"promotion_evidence_{strategy}.json").write_text(
        json.dumps({
            "strategy": strategy,
            "recommendation": "ELIGIBLE",
            "promotable_evidence_days": 60,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    warnings = validate_paper_evidence_operator_artifacts(
        promotion_dir,
        evidence_dir=evidence_dir,
    )

    assert any(strategy in warning for warning in warnings)
    assert any("package_integrity 누락" in warning for warning in warnings)
    assert any("package 재생성 또는 격리 필요" in warning for warning in warnings)


def test_quarantine_invalid_paper_evidence_operator_packages_moves_invalid_package(tmp_path):
    from datetime import datetime

    from tools.evaluate_and_promote import (
        quarantine_invalid_paper_evidence_operator_packages,
        validate_paper_evidence_operator_artifacts,
    )

    promotion_dir = tmp_path / "promotion"
    evidence_dir = tmp_path / "paper_evidence"
    strategy = "paper_ready_strategy"
    _write_consistent_promotion_artifacts(
        promotion_dir,
        {strategy: _provisional_metrics()},
        evidence_dir=evidence_dir,
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    package_path = evidence_dir / f"promotion_evidence_{strategy}.json"
    package_path.write_text(
        json.dumps({
            "strategy": strategy,
            "recommendation": "ELIGIBLE",
            "promotable_evidence_days": 60,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    dry_run = quarantine_invalid_paper_evidence_operator_packages(
        promotion_dir,
        evidence_dir=evidence_dir,
        dry_run=True,
        now=datetime(2026, 5, 15, 12, 0, 0),
    )

    assert len(dry_run) == 1
    assert dry_run[0]["dry_run"] is True
    assert dry_run[0]["moved"] is False
    assert package_path.exists()

    moved = quarantine_invalid_paper_evidence_operator_packages(
        promotion_dir,
        evidence_dir=evidence_dir,
        dry_run=False,
        now=datetime(2026, 5, 15, 12, 0, 0),
    )

    target_path = Path(moved[0]["target"])
    assert moved[0]["moved"] is True
    assert not package_path.exists()
    assert target_path.exists()
    assert target_path.parent == evidence_dir / "_invalid_packages"
    assert validate_paper_evidence_operator_artifacts(
        promotion_dir,
        evidence_dir=evidence_dir,
    ) == []


def test_build_promotion_results_blocks_target_weight_without_verified_proof(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "target_weight_rotation_test"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert "target-weight evidence required flag missing" in promotions[strategy]["reason"]


def test_build_promotion_results_uses_metadata_for_target_weight_identity(tmp_path):
    from tools.evaluate_and_promote import build_promotion_results

    strategy = "rotation_candidate_test"
    metrics = {strategy: _provisional_metrics()}
    evidence_dir = tmp_path / "paper_evidence"
    _write_paper_package(evidence_dir, strategy)

    strategy_specs = [{
        "candidate_id": strategy,
        "base_strategy": "target_weight_rotation",
        "params_hash": "hash",
    }]
    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        strategy_specs=strategy_specs,
        canonical_metadata=_fresh_promotion_metadata(strategy_specs=strategy_specs),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "live" not in promotions[strategy]["allowed_modes"]
    assert "target-weight evidence required flag missing" in promotions[strategy]["reason"]
    assert metrics[strategy]["target_weight_strategy_required"] is True
    assert metrics[strategy]["target_weight_canonical_params_hash"] == "hash"


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

    strategy_specs = [{"candidate_id": strategy, "params_hash": "hash"}]
    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        strategy_specs=strategy_specs,
        canonical_metadata=_fresh_promotion_metadata(strategy_specs=strategy_specs),
    )

    assert promotions[strategy]["status"] == "live_candidate"
    assert "live" in promotions[strategy]["allowed_modes"]
    assert metrics[strategy]["target_weight_verified_pilot_days"] == 60
    assert metrics[strategy]["target_weight_params_hash"] == "hash"
    assert metrics[strategy]["target_weight_canonical_params_hash"] == "hash"
    assert metrics[strategy]["target_weight_params_hash_matches_canonical"] is True


def test_build_promotion_results_blocks_target_weight_hash_mismatch(tmp_path):
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
            "params_hash": "old-hash",
            "params_hashes": ["old-hash"],
            "params_hash_consistent": True,
            "all_promotable_days_verified": True,
        },
        target_weight_verified_pilot_days=60,
        target_weight_invalid_days=0,
        target_weight_params_hash="old-hash",
    )

    strategy_specs = [{"candidate_id": strategy, "params_hash": "current-hash"}]
    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        strategy_specs=strategy_specs,
        canonical_metadata=_fresh_promotion_metadata(strategy_specs=strategy_specs),
    )

    assert promotions[strategy]["status"] == "provisional_paper_candidate"
    assert "does not match canonical" in promotions[strategy]["reason"]
    assert metrics[strategy]["target_weight_canonical_params_hash"] == "current-hash"
    assert metrics[strategy]["target_weight_params_hash_matches_canonical"] is False


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

    promotions = build_promotion_results(
        metrics,
        evidence_dir=str(evidence_dir),
        canonical_metadata=_fresh_promotion_metadata(),
    )

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
