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


def test_canonical_research_candidate_metadata_is_json_serializable():
    from tools.evaluate_and_promote import (
        build_canonical_research_candidate_specs,
        canonical_research_candidate_metadata,
    )

    metadata = canonical_research_candidate_metadata(
        build_canonical_research_candidate_specs()[0]
    )

    json.dumps(metadata, ensure_ascii=False)
