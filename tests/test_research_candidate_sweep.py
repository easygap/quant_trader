import json
from pathlib import Path


def _minimal_bundle(candidates=None):
    return {
        "schema_version": 1,
        "artifact_type": "research_candidate_sweep_bundle",
        "run_id": "20260429_171000_relative_strength_rotation",
        "generated_at": "2026-04-29T17:10:00",
        "commit_hash": "abc123",
        "config_yaml_hash": "yaml",
        "config_resolved_hash": "resolved",
        "eval_start": "2023-01-01",
        "eval_end": "2025-12-31",
        "initial_capital": 10_000_000,
        "candidate_family": "rotation",
        "universe": ["005930", "000660"],
        "benchmark": {"ew_bh_return": 10.0, "ew_bh_sharpe": 0.5},
        "walk_forward": {"enabled": False, "windows": []},
        "ranking_rule": "test",
        "candidates": candidates or [],
        "summary": {"evaluated": len(candidates or []), "eligible_for_canonical_eval": 0, "best_candidate_id": None},
    }


def test_validate_sweep_artifact_accepts_required_schema():
    from tools.research_candidate_sweep import validate_sweep_artifact

    ok, reason = validate_sweep_artifact(_minimal_bundle())
    assert ok is True
    assert reason == "ok"


def test_validate_sweep_artifact_rejects_wrong_artifact_type():
    from tools.research_candidate_sweep import validate_sweep_artifact

    payload = _minimal_bundle()
    payload["artifact_type"] = "canonical_promotion_bundle"
    ok, reason = validate_sweep_artifact(payload)
    assert ok is False
    assert "artifact_type" in reason


def test_parse_symbols_restores_numeric_codes_losing_leading_zeroes():
    from tools.research_candidate_sweep import parse_symbols

    assert parse_symbols("5930,660,035720") == ["005930", "000660", "035720"]


def test_build_candidate_specs_supports_all_families():
    from tools.research_candidate_sweep import build_candidate_specs

    specs = build_candidate_specs("all")
    ids = {spec.candidate_id for spec in specs}
    strategies = {spec.strategy for spec in specs}

    assert "rotation_base" in ids
    assert "momentum_factor_60d" in ids
    assert "breakout_volume_strict" in ids
    assert "trend_pullback_balanced" in ids
    assert "benchmark_relative_momentum_120d" in ids
    assert "risk_budget_momentum_120d_balanced" in ids
    assert strategies == {
        "relative_strength_rotation",
        "momentum_factor",
        "breakout_volume",
        "trend_pullback",
    }


def test_build_candidate_specs_supports_pullback_family_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("pullback")
    alias = build_candidate_specs("trend_pullback")

    assert [spec.candidate_id for spec in direct] == [
        "trend_pullback_base",
        "trend_pullback_aggressive",
        "trend_pullback_balanced",
        "trend_pullback_conservative",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"trend_pullback"}


def test_build_candidate_specs_supports_benchmark_relative_family_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("benchmark_relative")
    alias = build_candidate_specs("relative_momentum")

    assert [spec.candidate_id for spec in direct] == [
        "benchmark_relative_momentum_60d",
        "benchmark_relative_momentum_120d",
        "benchmark_relative_momentum_lowvol",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"momentum_factor"}
    assert all(spec.params["benchmark_relative"] is True for spec in direct)


def test_build_candidate_specs_supports_risk_budget_family_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("risk_budget")
    alias = build_candidate_specs("exposure")

    assert [spec.candidate_id for spec in direct] == [
        "risk_budget_momentum_120d_concentrated",
        "risk_budget_momentum_120d_balanced",
        "risk_budget_momentum_120d_defensive",
        "risk_budget_rotation_slow_balanced",
        "risk_budget_rotation_slow_defensive",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert direct[1].diversification == {
        "max_positions": 4,
        "max_position_ratio": 0.25,
        "max_investment_ratio": 0.80,
        "min_cash_ratio": 0.15,
    }


def test_build_candidate_specs_rejects_unknown_family():
    import pytest
    from tools.research_candidate_sweep import build_candidate_specs

    with pytest.raises(ValueError, match="candidate_family"):
        build_candidate_specs("unknown")


def test_buy_and_hold_benchmark_tolerates_failed_symbol_fetch(monkeypatch):
    import core.data_collector as data_collector
    from tools.research_candidate_sweep import buy_and_hold_benchmark

    class FailingCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            raise RuntimeError(f"missing {symbol}")

    monkeypatch.setattr(data_collector, "DataCollector", FailingCollector)

    benchmark = buy_and_hold_benchmark(["5930"], "2024-01-01", "2024-12-31", 10_000_000)

    assert benchmark["universe_size"] == 0
    assert benchmark["benchmark_symbols"] == []


def test_candidate_to_strategy_metrics_maps_research_fields():
    from tools.research_candidate_sweep import candidate_to_strategy_metrics

    metrics = candidate_to_strategy_metrics(
        "rotation_test",
        {
            "total_return": 12.0,
            "profit_factor": 1.4,
            "mdd": -8.0,
            "wf_positive_rate": 0.8,
            "wf_sharpe_positive_rate": 0.7,
            "wf_windows": 5,
            "wf_total_trades": 42,
            "sharpe": 0.6,
            "benchmark_excess_return": 2.5,
            "benchmark_excess_sharpe": 0.2,
            "ev_per_trade": 10_000,
            "cost_adjusted_cagr": 4.2,
            "turnover_per_year": 300.0,
        },
    )

    assert metrics.name == "rotation_test"
    assert metrics.benchmark_excess_return == 2.5
    assert metrics.ev_per_trade == 10_000
    assert metrics.turnover_per_year == 300.0


def test_sort_candidates_prefers_alpha_pass_over_high_non_alpha_score():
    from tools.research_candidate_sweep import sort_candidate_records

    non_alpha = {
        "candidate_id": "high_score_negative_alpha",
        "alpha_pass": False,
        "rank_score": 999,
        "promotion": {"status": "provisional_paper_candidate"},
        "metrics": {"benchmark_excess_return": -50.0},
    }
    alpha = {
        "candidate_id": "lower_score_positive_alpha",
        "alpha_pass": True,
        "rank_score": 10,
        "promotion": {"status": "paper_only"},
        "metrics": {"benchmark_excess_return": 1.0},
    }

    ranked = sort_candidate_records([non_alpha, alpha])
    assert ranked[0]["candidate_id"] == "lower_score_positive_alpha"


def test_decision_summary_blocks_no_alpha_candidate():
    from tools.research_candidate_sweep import build_decision_summary

    decision = build_decision_summary(
        [
            {
                "candidate_id": "weak",
                "alpha_pass": False,
                "promotion": {"status": "paper_only"},
            }
        ],
        walk_forward_enabled=False,
        benchmark={"universe_size": 3},
    )

    assert decision["action"] == "NO_ALPHA_CANDIDATE"
    assert decision["eligible_candidate_ids"] == []


def test_decision_summary_sends_quick_alpha_to_full_walk_forward():
    from tools.research_candidate_sweep import build_decision_summary

    decision = build_decision_summary(
        [
            {
                "candidate_id": "quick_alpha",
                "alpha_pass": True,
                "promotion": {"status": "paper_only"},
            }
        ],
        walk_forward_enabled=False,
        benchmark={"universe_size": 3},
    )

    assert decision["action"] == "RUN_FULL_WALK_FORWARD"
    assert decision["alpha_candidate_ids"] == ["quick_alpha"]


def test_build_candidate_record_keeps_rejection_reason_for_weak_candidate():
    from tools.research_candidate_sweep import CandidateSpec, build_candidate_record

    rec = build_candidate_record(
        CandidateSpec("weak_rotation", "relative_strength_rotation", {}, "weak"),
        {
            "total_return": 1.0,
            "sharpe": -0.2,
            "profit_factor": 1.01,
            "mdd": -5.0,
            "total_trades": 10,
            "wf_positive_rate": 0.5,
            "wf_sharpe_positive_rate": 0.0,
            "wf_windows": 3,
            "wf_total_trades": 10,
            "ev_per_trade": -1,
            "cost_adjusted_cagr": 0.5,
            "turnover_per_year": 200.0,
        },
        {"ew_bh_return": 10.0, "ew_bh_sharpe": 0.5},
    )

    assert rec["alpha_pass"] is False
    assert rec["promotion"]["status"] == "paper_only"
    assert "benchmark_excess_return <= 0" in rec["rejection_reasons"]
    assert "ev_per_trade <= 0" in rec["rejection_reasons"]


def test_build_candidate_record_records_diversification_budget():
    from tools.research_candidate_sweep import CandidateSpec, build_candidate_record

    rec = build_candidate_record(
        CandidateSpec(
            "budgeted",
            "momentum_factor",
            {},
            "budgeted candidate",
            diversification={
                "max_positions": 3,
                "max_position_ratio": 0.20,
                "max_investment_ratio": 0.60,
                "min_cash_ratio": 0.30,
            },
        ),
        {
            "total_return": 1.0,
            "sharpe": 0.1,
            "profit_factor": 1.01,
            "mdd": -5.0,
            "total_trades": 10,
            "wf_positive_rate": 0.5,
            "wf_sharpe_positive_rate": 0.0,
            "wf_windows": 3,
            "wf_total_trades": 10,
            "ev_per_trade": 1,
            "cost_adjusted_cagr": 0.5,
            "turnover_per_year": 200.0,
        },
        {"ew_bh_return": 10.0, "ew_bh_sharpe": 0.5},
    )

    assert rec["diversification"]["max_positions"] == 3
    assert rec["diversification"]["min_cash_ratio"] == 0.30


def test_write_sweep_artifact_does_not_touch_promotion_dir(tmp_path):
    from tools.research_candidate_sweep import write_candidate_artifacts

    output_dir = tmp_path / "reports" / "research_sweeps"
    json_path, md_path = write_candidate_artifacts(_minimal_bundle(), output_dir)

    assert json_path.exists()
    assert md_path.exists()
    assert not (tmp_path / "reports" / "promotion").exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "research_candidate_sweep_bundle"


def test_portfolio_backtester_strategy_config_for_run_applies_overlay():
    from config.config_loader import Config
    from backtest.portfolio_backtester import PortfolioBacktester

    pbt = PortfolioBacktester(Config.get())
    overlay = pbt._strategy_config_for_run(
        "relative_strength_rotation",
        {"relative_strength_rotation": {"short_lookback": 42}},
    )

    assert overlay.strategies["relative_strength_rotation"]["short_lookback"] == 42


def test_portfolio_backtester_filters_trade_dates_after_warmup():
    import pandas as pd
    from backtest.portfolio_backtester import PortfolioBacktester

    dates = [
        pd.Timestamp("2024-12-30"),
        pd.Timestamp("2025-01-02"),
        pd.Timestamp("2025-01-03"),
    ]

    assert PortfolioBacktester._filter_trade_dates(dates, "2025-01-01") == dates[1:]
