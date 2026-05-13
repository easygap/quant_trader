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


def test_parse_candidate_ids_accepts_repeated_and_comma_values():
    from tools.research_candidate_sweep import parse_candidate_ids

    assert parse_candidate_ids(["candidate_a,candidate_b", "candidate_a"]) == [
        "candidate_a",
        "candidate_b",
    ]
    assert parse_candidate_ids(None) is None


def test_cached_korean_stock_collector_reuses_and_copies_frames():
    import pandas as pd
    from tools.research_candidate_sweep import CachedKoreanStockCollector

    class FakeCollector:
        quiet_ohlcv_log = False

        def __init__(self):
            self.calls = 0

        def fetch_korean_stock(self, symbol, start, end):
            self.calls += 1
            return pd.DataFrame({"close": [100.0], "volume": [10.0]})

    raw = FakeCollector()
    collector = CachedKoreanStockCollector(raw)

    first = collector.fetch_korean_stock("5930", "2025-01-01", "2025-01-31")
    first.loc[0, "close"] = 0.0
    second = collector.fetch_korean_stock("005930", "2025-01-01", "2025-01-31")

    assert raw.calls == 1
    assert second.loc[0, "close"] == 100.0
    assert collector.stats() == {
        "enabled": True,
        "unique_fetches": 1,
        "cache_hits": 1,
        "cached_items": 1,
    }


def test_select_canonical_universe_scans_past_legacy_100_for_large_top_n(monkeypatch):
    import sys
    import types

    import pandas as pd

    import core.data_collector as data_collector
    from tools.research_candidate_sweep import select_canonical_universe

    codes = [f"{i:05d}0" for i in range(1, 151)]
    fetched = []

    fake_fdr = types.SimpleNamespace(
        StockListing=lambda market: pd.DataFrame({
            "Code": codes,
            "Marcap": [200_000_000_000] * len(codes),
        })
    )

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            fetched.append(symbol)
            return pd.DataFrame({"close": [float(symbol)], "volume": [1.0]})

    monkeypatch.setitem(sys.modules, "FinanceDataReader", fake_fdr)
    monkeypatch.setattr(data_collector, "DataCollector", FakeCollector)

    universe = select_canonical_universe(120)

    assert len(fetched) == 150
    assert len(universe) == 120
    assert "001500" in universe
    assert "001000" in universe


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
    assert "cash_switch_rotation_sma200" in ids
    assert "benchmark_aware_rotation_60_120_dense" in ids
    assert "target_weight_rotation_top3_60_120_excess" in ids
    assert "target_weight_rotation_top5_60_120_floor0_hold3_rankrisk60" in ids
    assert "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew2" in ids
    assert "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_pdd10_floor35_cd1" in ids
    assert "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35" in ids
    assert strategies == {
        "relative_strength_rotation",
        "momentum_factor",
        "breakout_volume",
        "trend_pullback",
        "target_weight_rotation",
    }


def test_filter_candidate_specs_limits_to_requested_ids_and_rejects_typos():
    import pytest
    from tools.research_candidate_sweep import CandidateSpec, filter_candidate_specs

    specs = [
        CandidateSpec("candidate_a", "relative_strength_rotation", {}, "A"),
        CandidateSpec("candidate_b", "relative_strength_rotation", {}, "B"),
    ]

    selected = filter_candidate_specs(specs, ["candidate_b"])

    assert [spec.candidate_id for spec in selected] == ["candidate_b"]
    with pytest.raises(ValueError, match="unknown candidate_id"):
        filter_candidate_specs(specs, ["candidate_missing"])


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


def test_build_candidate_specs_supports_cash_switch_family_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("cash_switch")
    alias = build_candidate_specs("market_exit")

    assert [spec.candidate_id for spec in direct] == [
        "cash_switch_rotation_sma200",
        "cash_switch_rotation_sma120",
        "cash_switch_rotation_slow_defensive",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"relative_strength_rotation"}
    assert all(spec.params["market_filter_exit"] is True for spec in direct)


def test_build_candidate_specs_supports_benchmark_aware_rotation_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("benchmark_aware_rotation")
    alias = build_candidate_specs("relative_rank")

    assert [spec.candidate_id for spec in direct] == [
        "benchmark_aware_rotation_60_120_dense",
        "benchmark_aware_rotation_80_160_dense",
        "benchmark_aware_rotation_40_100_dense",
        "benchmark_aware_rotation_60_120_balanced",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"relative_strength_rotation"}
    assert all(spec.params["score_mode"] == "benchmark_excess" for spec in direct)
    assert all(spec.params["rank_entry_mode"] == "dense_ranked" for spec in direct)
    assert direct[-1].diversification["max_positions"] == 4


def test_build_candidate_specs_supports_target_weight_rotation_aliases():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_rotation")
    alias = build_candidate_specs("monthly_topn")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top2_60_120_excess",
        "target_weight_rotation_top3_60_120_excess",
        "target_weight_rotation_top3_40_100_excess",
        "target_weight_rotation_top3_40_100_floor0",
        "target_weight_rotation_top3_40_100_floor3",
        "target_weight_rotation_top5_60_120_floor0",
        "target_weight_rotation_top5_60_120_floor0_hold3",
        "target_weight_rotation_top5_60_120_floor0_hold3_sma120_55",
        "target_weight_rotation_top5_60_120_floor0_hold3_sma200_55",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk120_55",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_45",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol3",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5",
        "target_weight_rotation_top5_60_120_floor0_exp80",
        "target_weight_rotation_top5_60_120_floor0_exp80_tol3",
        "target_weight_rotation_top5_60_120_floor0_exp75",
        "target_weight_rotation_top3_40_100_hold2",
        "target_weight_rotation_top5_60_120_floor0_tol3",
        "target_weight_rotation_top3_60_120_partial_cash",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["score_mode"] == "benchmark_excess" for spec in direct)
    assert all(spec.params["rebalance_frequency"] == "monthly" for spec in direct)
    assert direct[0].params["target_top_n"] == 2
    assert direct[3].params["min_score_floor_pct"] == 0.0
    assert direct[4].params["min_score_floor_pct"] == 3.0
    assert direct[6].params["hold_rank_buffer"] == 3
    assert direct[7].params["market_exposure_mode"] == "benchmark_sma"
    assert direct[7].params["market_ma_period"] == 120
    assert direct[9].params["market_exposure_mode"] == "benchmark_risk"
    assert direct[10].params["bear_target_exposure"] == 0.45
    assert direct[11].params["bear_target_exposure"] == 0.35
    assert direct[12].params["market_ma_period"] == 60
    assert direct[13].params["target_tolerance_pct"] == 3.0
    assert direct[14].params["target_tolerance_pct"] == 5.0
    assert direct[15].params["target_exposure"] == 0.80
    assert direct[16].params["target_tolerance_pct"] == 3.0
    assert direct[17].params["target_exposure"] == 0.75
    assert direct[19].params["target_tolerance_pct"] == 3.0
    assert direct[-1].params["market_exposure_mode"] == "benchmark_sma"


def test_build_candidate_specs_supports_target_weight_risk_relief_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_risk_relief")
    alias = build_candidate_specs("risk_relief")
    all_target = {
        spec.candidate_id
        for spec in build_candidate_specs("target_weight_rotation")
    }

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_45",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk120_55",
        "target_weight_rotation_top5_60_120_floor0_hold3_sma120_55",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol3",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5",
        "target_weight_rotation_top5_60_120_floor0_exp80_tol3",
        "target_weight_rotation_top5_60_120_floor0_exp75",
        "target_weight_rotation_top5_60_120_floor0_tol3",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.candidate_id for spec in direct}.issubset(all_target)
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert any(spec.params.get("target_tolerance_pct") == 5.0 for spec in direct)
    assert any(spec.params.get("bear_target_exposure") == 0.35 for spec in direct)


def test_build_candidate_specs_supports_target_weight_turnover_relief_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_turnover_relief")
    alias = build_candidate_specs("low_turnover")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_bimonthly",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_quarterly",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_bimonthly",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_quarterly",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_35_bimonthly",
        "target_weight_rotation_top5_60_120_floor0_exp75_bimonthly",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert {spec.params["rebalance_frequency"] for spec in direct} == {
        "bimonthly",
        "quarterly",
    }
    assert any(spec.params.get("target_tolerance_pct") == 5.0 for spec in direct)
    assert any(spec.params.get("target_exposure") == 0.75 for spec in direct)


def test_build_candidate_specs_supports_target_weight_volatility_target_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_volatility_target")
    alias = build_candidate_specs("vol_target")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_vol16_dd8_floor35",
        "target_weight_rotation_top5_60_120_floor0_hold3_vol14_dd6_floor25",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_vol16_dd8_floor35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_vol14_dd6_floor25",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_vol16_dd8_floor35",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk90_35_vol16_dd8_floor35",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["market_exposure_mode"] == "benchmark_vol_target" for spec in direct)
    assert {spec.params["benchmark_vol_target_pct"] for spec in direct} == {14.0, 16.0}
    assert {spec.params["bear_target_exposure"] for spec in direct} == {0.25, 0.35}


def test_build_candidate_specs_supports_target_weight_downside_rank_relief_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_downside_rank_relief")
    alias = build_candidate_specs("rank_relief")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_rankrisk60",
        "target_weight_rotation_top5_60_120_floor0_hold3_rankrisk90",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_rankrisk60",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_dd75",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["rank_penalty_mode"] == "downside_risk" for spec in direct)
    assert {spec.params["rank_penalty_lookback"] for spec in direct} == {60, 90, 120}
    assert {spec.params["drawdown_penalty_weight"] for spec in direct} == {0.45, 0.55, 0.70, 0.75}
    assert any(spec.params.get("target_tolerance_pct") == 5.0 for spec in direct)
    assert any(spec.params.get("target_exposure") == 0.75 for spec in direct)


def test_build_candidate_specs_supports_target_weight_churn_relief_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_churn_relief")
    alias = build_candidate_specs("rank_churn_relief")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew2",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew1",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_bimonthly_maxnew2",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_rankrisk60_maxnew2",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_bimonthly_maxnew2",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["rank_penalty_mode"] == "downside_risk" for spec in direct)
    assert {spec.params["max_new_targets_per_rebalance"] for spec in direct} == {1, 2}
    assert any(spec.params["rebalance_frequency"] == "bimonthly" for spec in direct)
    assert any(spec.params.get("target_tolerance_pct") == 5.0 for spec in direct)


def test_build_candidate_specs_supports_target_weight_drawdown_guard_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_drawdown_guard")
    alias = build_candidate_specs("drawdown_guard")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_pdd10_floor35_cd1",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_pdd8_floor25_cd1",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_tol5_rankrisk60_maxnew2_pdd10_floor35_cd1",
        "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35_rankrisk60_pdd10_floor35_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol3_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor35_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd8_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd8_floor35_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd2",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_dd75_tol4_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120_tol4_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap1_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120_tol4_sectorcap2_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrcap85_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrcap80_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_corrcap85_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrpen05_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_corrpen10_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_corrpen05_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_reentry4_maxnew0_cd1_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_reentry3_maxnew0_cd1_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_reentry4_maxnew0_cd1_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_posloss8_frac50_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_posloss10_frac50_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_posloss8_frac50_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol5_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_maxnew2_pdd10_floor40_cd1",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol3_maxnew2_pdd10_floor40_cd1",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["rank_penalty_mode"] == "downside_risk" for spec in direct)
    assert {spec.params["portfolio_drawdown_guard_trigger_pct"] for spec in direct} == {8.0, 10.0}
    assert {spec.params["portfolio_drawdown_guard_exposure"] for spec in direct} == {0.25, 0.35, 0.40}
    assert {spec.params["portfolio_drawdown_guard_cooldown_rebalances"] for spec in direct} == {1, 2}
    assert any(spec.params.get("max_new_targets_per_rebalance") == 2 for spec in direct)
    assert any(spec.params.get("target_tolerance_pct") == 3.0 for spec in direct)
    assert any(spec.params.get("target_tolerance_pct") == 4.0 for spec in direct)
    assert any(spec.params.get("target_tolerance_pct") == 5.0 for spec in direct)
    assert any(spec.params.get("max_targets_per_sector") == 1 for spec in direct)
    assert any(spec.params.get("max_targets_per_sector") == 2 for spec in direct)
    assert any(spec.params.get("max_pairwise_correlation") == 0.85 for spec in direct)
    assert any(spec.params.get("max_pairwise_correlation") == 0.80 for spec in direct)
    assert any(spec.params.get("correlation_rank_penalty_weight") == 0.05 for spec in direct)
    assert any(spec.params.get("correlation_rank_penalty_weight") == 0.10 for spec in direct)
    assert any(spec.params.get("loss_reentry_guard_trigger_pct") == 4.0 for spec in direct)
    assert any(spec.params.get("loss_reentry_guard_trigger_pct") == 3.0 for spec in direct)
    assert any(spec.params.get("position_loss_reduce_trigger_pct") == 8.0 for spec in direct)
    assert any(spec.params.get("position_loss_reduce_trigger_pct") == 10.0 for spec in direct)
    assert any(spec.params.get("position_loss_reduce_target_fraction") == 0.50 for spec in direct)


def test_build_candidate_specs_supports_target_weight_volatility_budget_family():
    from tools.research_candidate_sweep import build_candidate_specs

    direct = build_candidate_specs("target_weight_volatility_budget")
    alias = build_candidate_specs("vol_budget")

    assert [spec.candidate_id for spec in direct] == [
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget90_cap35",
        "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1_volbudget60_cap35",
    ]
    assert [spec.candidate_id for spec in alias] == [spec.candidate_id for spec in direct]
    assert {spec.strategy for spec in direct} == {"target_weight_rotation"}
    assert all(spec.params["target_allocation_mode"] == "inverse_volatility" for spec in direct)
    assert {spec.params["allocation_vol_lookback_days"] for spec in direct} == {60, 90}
    assert {spec.params["allocation_max_sleeve_weight_pct"] for spec in direct} == {35.0}
    assert any(spec.params.get("max_targets_per_sector") == 2 for spec in direct)
    assert all(spec.params["portfolio_drawdown_guard_trigger_pct"] == 10.0 for spec in direct)


def test_select_target_weight_targets_limits_new_entries_per_rebalance():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    score_row = pd.Series(
        [0.5, 0.4, 0.3, 0.2, 0.1],
        index=["NEW1", "NEW2", "OLD1", "OLD2", "OLD3"],
    )
    prices = {sym: 100.0 for sym in score_row.index}
    positions = {
        "OLD1": {"qty": 1.0},
        "OLD2": {"qty": 1.0},
        "OLD3": {"qty": 1.0},
    }

    limited = sweep._select_target_weight_targets(
        score_row,
        prices,
        positions,
        top_n=3,
        hold_rank_buffer=0,
        max_new_targets_per_rebalance=1,
    )
    no_new = sweep._select_target_weight_targets(
        score_row,
        prices,
        positions,
        top_n=3,
        hold_rank_buffer=0,
        max_new_targets_per_rebalance=0,
    )

    assert limited == ["NEW1", "OLD1", "OLD2"]
    assert no_new == ["OLD1", "OLD2", "OLD3"]


def test_select_target_weight_targets_limits_targets_per_sector():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    score_row = pd.Series(
        [0.5, 0.4, 0.3, 0.2, 0.1],
        index=["NEW1", "NEW2", "NEW3", "OLD1", "OLD2"],
    )
    prices = {sym: 100.0 for sym in score_row.index}
    sector_map = {
        "NEW1": "Tech",
        "NEW2": "Tech",
        "NEW3": "Tech",
        "OLD1": "Finance",
        "OLD2": "Industrial",
    }

    limited = sweep._select_target_weight_targets(
        score_row,
        prices,
        positions={},
        top_n=3,
        hold_rank_buffer=0,
        max_targets_per_sector=1,
        sector_map=sector_map,
    )

    assert limited == ["NEW1", "OLD1", "OLD2"]


def test_select_target_weight_targets_limits_pairwise_correlation():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    score_row = pd.Series(
        [0.5, 0.4, 0.3, 0.2],
        index=["AAA", "BBB", "CCC", "DDD"],
    )
    prices = {sym: 100.0 for sym in score_row.index}
    correlation_matrix = pd.DataFrame(
        [
            [1.00, 0.95, 0.20, 0.10],
            [0.95, 1.00, 0.15, 0.20],
            [0.20, 0.15, 1.00, 0.30],
            [0.10, 0.20, 0.30, 1.00],
        ],
        index=score_row.index,
        columns=score_row.index,
    )

    limited = sweep._select_target_weight_targets(
        score_row,
        prices,
        positions={},
        top_n=3,
        hold_rank_buffer=0,
        max_pairwise_correlation=0.80,
        correlation_matrix=correlation_matrix,
    )

    assert limited == ["AAA", "CCC", "DDD"]


def test_select_target_weight_targets_combines_sector_and_correlation_caps():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    score_row = pd.Series(
        [0.5, 0.4, 0.3, 0.2, 0.1],
        index=["AAA", "BBB", "CCC", "DDD", "EEE"],
    )
    prices = {sym: 100.0 for sym in score_row.index}
    sector_map = {
        "AAA": "Tech",
        "BBB": "Tech",
        "CCC": "Finance",
        "DDD": "Industrial",
        "EEE": "Healthcare",
    }
    correlation_matrix = pd.DataFrame(
        [
            [1.00, 0.20, 0.95, 0.20, 0.10],
            [0.20, 1.00, 0.10, 0.20, 0.10],
            [0.95, 0.10, 1.00, 0.30, 0.10],
            [0.20, 0.20, 0.30, 1.00, 0.10],
            [0.10, 0.10, 0.10, 0.10, 1.00],
        ],
        index=score_row.index,
        columns=score_row.index,
    )

    limited = sweep._select_target_weight_targets(
        score_row,
        prices,
        positions={},
        top_n=3,
        hold_rank_buffer=0,
        max_targets_per_sector=1,
        sector_map=sector_map,
        max_pairwise_correlation=0.80,
        correlation_matrix=correlation_matrix,
    )

    assert limited == ["AAA", "DDD", "EEE"]


def test_target_weight_correlation_matrix_uses_history_before_score_day():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    close_panel = pd.DataFrame(
        {
            "AAA": [100, 101, 102, 103, 300],
            "BBB": [50, 51, 52, 53, 10],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]),
    )

    corr = sweep._target_weight_correlation_matrix(
        close_panel,
        pd.Timestamp("2024-01-05"),
        ["AAA", "BBB"],
        lookback_days=3,
        min_periods=2,
    )

    assert corr.loc["AAA", "BBB"] > 0.99


def test_target_weight_correlation_score_penalty_pushes_crowded_names_down():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    score_row = pd.Series(
        [0.10, 0.095, 0.09, 0.085],
        index=["AAA", "BBB", "CCC", "DDD"],
    )
    correlation_matrix = pd.DataFrame(
        [
            [1.00, 0.92, 0.88, 0.10],
            [0.92, 1.00, 0.86, 0.10],
            [0.88, 0.86, 1.00, 0.10],
            [0.10, 0.10, 0.10, 1.00],
        ],
        index=score_row.index,
        columns=score_row.index,
    )

    adjusted = sweep._apply_target_weight_correlation_score_penalty(
        score_row,
        correlation_matrix,
        weight=0.08,
        mode="mean_positive",
    )

    assert adjusted.sort_values(ascending=False).index[0] == "DDD"
    assert adjusted["AAA"] < score_row["AAA"]
    assert adjusted["DDD"] > adjusted["AAA"]


def test_canonical_target_weight_specs_include_sectorcap_candidates():
    from tools.evaluate_and_promote import build_canonical_research_candidate_specs

    ids = {spec.candidate_id for spec in build_canonical_research_candidate_specs()}

    assert "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1" in ids
    assert "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk120_tol4_sectorcap2_pdd10_floor40_cd1" in ids
    assert "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_pdd10_floor40_cd1_volbudget60_cap35" in ids
    assert "target_weight_rotation_top5_60_120_floor0_exp75_rankrisk90_tol4_sectorcap2_pdd10_floor40_cd1_volbudget60_cap35" in ids


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
    assert benchmark["benchmark_coverage_complete"] is False


def test_buy_and_hold_benchmark_blocks_partial_symbol_coverage(monkeypatch):
    import pandas as pd
    import core.data_collector as data_collector
    from tools.research_candidate_sweep import buy_and_hold_benchmark_with_returns

    class PartialCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "005930":
                return pd.DataFrame(
                    {"close": [100.0, 110.0]},
                    index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
                )
            raise RuntimeError(f"missing {symbol}")

    monkeypatch.setattr(data_collector, "DataCollector", PartialCollector)

    benchmark, daily_returns = buy_and_hold_benchmark_with_returns(
        ["005930", "000660"],
        "2024-01-01",
        "2024-01-05",
        10_000_000,
    )

    assert daily_returns.empty
    assert benchmark["universe_size"] == 0
    assert benchmark["benchmark_symbols"] == ["005930"]
    assert benchmark["missing_benchmark_symbols"] == ["000660"]
    assert benchmark["benchmark_coverage_ratio"] == 50.0
    assert benchmark["benchmark_coverage_complete"] is False
    assert benchmark["benchmark_unusable_reason"] == "incomplete_benchmark_coverage"


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


def test_calculate_research_metrics_adds_exposure_matched_diagnostics():
    import pandas as pd
    from tools.research_candidate_sweep import calculate_research_metrics

    dates = pd.bdate_range("2025-01-01", periods=4)
    equity = pd.DataFrame(
        {
            "date": dates,
            "value": [100.0, 105.0, 105.0, 110.0],
            "cash": [50.0, 52.5, 105.0, 55.0],
            "n_positions": [1, 1, 0, 1],
        }
    )
    benchmark_returns = pd.Series([0.10, -0.05, 0.02], index=dates[1:])

    metrics = calculate_research_metrics(
        {"equity_curve": equity, "trades": []},
        capital=100.0,
        benchmark_daily_returns=benchmark_returns,
    )

    assert metrics["total_return"] == 10.0
    assert metrics["avg_exposure_pct"] == 37.5
    assert metrics["median_exposure_pct"] == 50.0
    assert metrics["avg_cash_pct"] == 62.5
    assert metrics["invested_days_pct"] == 75.0
    assert metrics["exposure_source"] == "cash_value"
    assert metrics["exposure_matched_bh_return"] == -2.5


def test_build_candidate_record_keeps_exposure_matched_excess_diagnostic():
    from tools.research_candidate_sweep import CandidateSpec, build_candidate_record

    rec = build_candidate_record(
        CandidateSpec("diagnostic", "relative_strength_rotation", {}, "diagnostic"),
        {
            "total_return": 10.0,
            "sharpe": 0.5,
            "profit_factor": 1.2,
            "mdd": -5.0,
            "total_trades": 30,
            "wf_positive_rate": 0.5,
            "wf_sharpe_positive_rate": 0.5,
            "wf_windows": 3,
            "wf_total_trades": 30,
            "ev_per_trade": 1,
            "cost_adjusted_cagr": 3.0,
            "turnover_per_year": 200.0,
            "exposure_matched_bh_return": 2.38,
            "exposure_matched_bh_sharpe": 0.1,
        },
        {"ew_bh_return": 20.0, "ew_bh_sharpe": 0.6},
    )

    assert rec["alpha_pass"] is False
    assert rec["metrics"]["benchmark_excess_return"] == -10.0
    assert rec["metrics"]["exposure_matched_excess_return"] == 7.62
    assert rec["metrics"]["exposure_matched_excess_sharpe"] == 0.4


def test_build_candidate_record_blocks_alpha_when_benchmark_incomplete():
    from tools.research_candidate_sweep import CandidateSpec, build_candidate_record

    rec = build_candidate_record(
        CandidateSpec("strong_but_untrusted", "relative_strength_rotation", {}, "strong"),
        {
            "total_return": 50.0,
            "sharpe": 1.2,
            "profit_factor": 2.0,
            "mdd": -5.0,
            "total_trades": 60,
            "wf_positive_rate": 1.0,
            "wf_sharpe_positive_rate": 1.0,
            "wf_windows": 5,
            "wf_total_trades": 60,
            "ev_per_trade": 10_000,
            "cost_adjusted_cagr": 12.0,
            "turnover_per_year": 300.0,
            "exposure_matched_bh_return": 20.0,
            "exposure_matched_bh_sharpe": 0.5,
        },
        {
            "ew_bh_return": 0,
            "ew_bh_sharpe": 0,
            "universe_size": 0,
            "benchmark_symbols": ["005930"],
            "input_universe_size": 2,
            "missing_benchmark_symbols": ["000660"],
            "benchmark_coverage_complete": False,
            "benchmark_unusable_reason": "incomplete_benchmark_coverage",
        },
    )

    assert rec["alpha_pass"] is False
    assert rec["metrics"]["benchmark_excess_return"] == 0
    assert rec["metrics"]["benchmark_excess_sharpe"] == 0
    assert "benchmark_data_incomplete=incomplete_benchmark_coverage" in rec["rejection_reasons"]


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


def test_decision_summary_blocks_incomplete_benchmark_coverage():
    from tools.research_candidate_sweep import build_decision_summary

    decision = build_decision_summary(
        [
            {
                "candidate_id": "looks_good",
                "alpha_pass": True,
                "promotion": {"status": "provisional_paper_candidate"},
            }
        ],
        walk_forward_enabled=True,
        benchmark={
            "ew_bh_return": 0,
            "ew_bh_sharpe": 0,
            "universe_size": 0,
            "benchmark_symbols": ["005930"],
            "input_universe_size": 2,
            "missing_benchmark_symbols": ["000660"],
            "benchmark_coverage_complete": False,
            "benchmark_unusable_reason": "incomplete_benchmark_coverage",
        },
    )

    assert decision["action"] == "INSUFFICIENT_BENCHMARK_DATA"
    assert decision["eligible_candidate_ids"] == []
    assert decision["alpha_candidate_ids"] == []
    assert "000660" in decision["reason"]


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


def test_summarize_rejection_reasons_counts_unique_candidate_blockers():
    from tools.research_candidate_sweep import summarize_rejection_reasons

    summary = summarize_rejection_reasons(
        [
            {
                "candidate_id": "candidate_a",
                "rejection_reasons": [
                    "mdd < -20",
                    "mdd < -20",
                    "turnover_per_year >= 1000",
                ],
            },
            {
                "candidate_id": "candidate_b",
                "rejection_reasons": ["mdd < -20"],
            },
        ]
    )

    assert summary[0]["reason"] == "mdd < -20"
    assert summary[0]["count"] == 2
    assert summary[0]["candidate_ids"] == ["candidate_a", "candidate_b"]
    assert summary[1]["reason"] == "turnover_per_year >= 1000"
    assert summary[1]["count"] == 1


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


def test_evaluate_candidate_routes_target_weight_rotation(monkeypatch):
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.bdate_range("2025-01-01", periods=3)
    sentinel_collector = object()
    seen = {}

    def fake_target_runner(**kwargs):
        seen["collector"] = kwargs.get("collector")
        return {
            "equity_curve": pd.DataFrame(
                {
                    "date": dates,
                    "value": [100.0, 104.0, 108.0],
                    "cash": [20.0, 22.0, 24.0],
                    "n_positions": [2, 2, 2],
                }
            ),
            "trades": [
                {"date": dates[1], "symbol": "005930", "action": "BUY", "price": 10, "quantity": 1, "pnl": 0},
                {"date": dates[2], "symbol": "005930", "action": "REBALANCE_SELL", "price": 11, "quantity": 1, "pnl": 1},
            ],
            "target_weight_metrics": {
                "target_top_n": 2,
                "rebalance_count": 1,
                "avg_slots_filled": 2.0,
                "slot_fill_rate_pct": 100.0,
            },
        }

    monkeypatch.setattr(sweep, "run_target_weight_rotation_backtest", fake_target_runner)

    metrics = sweep.evaluate_candidate(
        sweep.CandidateSpec(
            "target",
            "target_weight_rotation",
            {"target_top_n": 2},
            "target",
        ),
        ["005930"],
        "2025-01-01",
        "2025-01-03",
        100.0,
        pd.Series([0.01, 0.01], index=dates[1:]),
        target_weight_collector=sentinel_collector,
    )

    assert metrics["total_return"] == 8.0
    assert seen["collector"] is sentinel_collector
    assert metrics["target_top_n"] == 2
    assert metrics["rebalance_count"] == 1
    assert metrics["avg_slots_filled"] == 2.0


def test_target_weight_research_rebalances_at_next_open_not_same_day_close():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04"])

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "AAA":
                return pd.DataFrame(
                    {
                        "open": [100.0, 100.0, 50.0, 510.0],
                        "close": [100.0, 110.0, 500.0, 520.0],
                        "volume": [100.0, 100.0, 900.0, 900.0],
                    },
                    index=dates,
                )
            if symbol == "BBB":
                return pd.DataFrame(
                    {
                        "open": [100.0, 100.0, 100.0, 100.0],
                        "close": [100.0, 101.0, 100.0, 100.0],
                        "volume": [100.0, 100.0, 100.0, 100.0],
                    },
                    index=dates,
                )
            return pd.DataFrame(
                {
                    "open": [100.0, 100.0, 100.0, 100.0],
                    "close": [100.0, 100.0, 100.0, 100.0],
                    "volume": [100.0, 100.0, 100.0, 100.0],
                },
                index=dates,
            )

    class NoCostRiskManager:
        def calculate_transaction_costs(self, price, quantity, side, **kwargs):
            return {
                "execution_price": float(price),
                "commission": 0.0,
                "tax": 0.0,
                "slippage": 0.0,
                "slippage_multiplier": 1.0,
                "participation_rate": 0.0,
            }

    result = sweep.run_target_weight_rotation_backtest(
        ["AAA", "BBB"],
        start="2025-02-03",
        end="2025-02-04",
        capital=1_000.0,
        params={
            "target_top_n": 1,
            "target_exposure": 1.0,
            "short_lookback": 1,
            "long_lookback": 1,
            "short_weight": 1.0,
        },
        collector=FakeCollector(),
        risk_manager=NoCostRiskManager(),
    )

    trade = result["trades"][0]
    assert trade["symbol"] == "AAA"
    assert trade["action"] == "BUY"
    assert trade["price"] == 50.0
    assert trade["avg_daily_volume"] == 100.0
    assert trade["execution_price_mode"] == "next_open"
    assert result["target_weight_metrics"]["execution_price_mode"] == "next_open"
    assert result["target_weight_metrics"]["avg_volume_lookback_lag_days"] == 1


def test_target_weight_rank_penalty_can_move_high_downside_name_below_lower_risk_name():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(
        [
            "2025-01-27",
            "2025-01-28",
            "2025-01-29",
            "2025-01-30",
            "2025-01-31",
            "2025-02-03",
        ]
    )

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "AAA":
                close = [100.0, 50.0, 100.0, 100.0, 130.0, 130.0]
            elif symbol == "BBB":
                close = [100.0, 100.0, 100.0, 100.0, 120.0, 120.0]
            else:
                close = [100.0] * len(dates)
            return pd.DataFrame(
                {
                    "open": [100.0] * len(dates),
                    "close": close,
                    "volume": [100.0] * len(dates),
                },
                index=dates,
            )

    class NoCostRiskManager:
        def calculate_transaction_costs(self, price, quantity, side, **kwargs):
            return {
                "execution_price": float(price),
                "commission": 0.0,
                "tax": 0.0,
                "slippage": 0.0,
                "slippage_multiplier": 1.0,
                "participation_rate": 0.0,
            }

    result = sweep.run_target_weight_rotation_backtest(
        ["AAA", "BBB"],
        start="2025-02-03",
        end="2025-02-03",
        capital=1_000.0,
        params={
            "target_top_n": 1,
            "target_exposure": 1.0,
            "short_lookback": 1,
            "long_lookback": 1,
            "short_weight": 1.0,
            "rank_penalty_mode": "downside_risk",
            "rank_penalty_lookback": 4,
            "downside_vol_penalty_weight": 1.0,
            "drawdown_penalty_weight": 0.0,
        },
        collector=FakeCollector(),
        risk_manager=NoCostRiskManager(),
    )

    assert result["trades"][0]["symbol"] == "BBB"
    metrics = result["target_weight_metrics"]
    assert metrics["rank_penalty_mode"] == "downside_risk"
    assert metrics["rank_penalty_lookback"] == 4
    assert metrics["downside_vol_penalty_weight"] == 1.0


def test_target_weight_correlation_rank_penalty_adjusts_rebalance_ranking(monkeypatch):
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-03"])
    closes = {
        "AAA": [100.0, 110.0, 110.0],
        "BBB": [100.0, 105.0, 105.0],
        "CCC": [100.0, 104.0, 104.0],
        "DDD": [100.0, 109.0, 109.0],
    }
    correlation_matrix = pd.DataFrame(
        [
            [1.00, 0.92, 0.88, 0.10],
            [0.92, 1.00, 0.86, 0.10],
            [0.88, 0.86, 1.00, 0.10],
            [0.10, 0.10, 0.10, 1.00],
        ],
        index=list(closes),
        columns=list(closes),
    )

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            close = closes.get(symbol, [100.0, 100.0, 100.0])
            return pd.DataFrame(
                {
                    "open": [100.0, 100.0, 100.0],
                    "close": close,
                    "volume": [100.0, 100.0, 100.0],
                },
                index=dates,
            )

    class NoCostRiskManager:
        def calculate_transaction_costs(self, price, quantity, side, **kwargs):
            return {
                "execution_price": float(price),
                "commission": 0.0,
                "tax": 0.0,
                "slippage": 0.0,
                "slippage_multiplier": 1.0,
                "participation_rate": 0.0,
            }

    monkeypatch.setattr(
        sweep,
        "_target_weight_correlation_matrix",
        lambda close_panel, score_day, symbols, lookback_days, min_periods: correlation_matrix.loc[
            symbols,
            symbols,
        ],
    )

    result = sweep.run_target_weight_rotation_backtest(
        ["AAA", "BBB", "CCC", "DDD"],
        start="2025-02-03",
        end="2025-02-03",
        capital=1_000.0,
        params={
            "target_top_n": 1,
            "target_exposure": 1.0,
            "short_lookback": 1,
            "long_lookback": 1,
            "short_weight": 1.0,
            "correlation_rank_penalty_weight": 0.10,
            "correlation_rank_penalty_lookback_days": 4,
            "correlation_rank_penalty_min_periods": 2,
            "correlation_rank_penalty_mode": "mean_positive",
        },
        collector=FakeCollector(),
        risk_manager=NoCostRiskManager(),
    )

    assert result["trades"][0]["symbol"] == "DDD"
    metrics = result["target_weight_metrics"]
    assert metrics["correlation_rank_penalty_weight"] == 0.10
    assert metrics["correlation_rank_penalty_mode"] == "mean_positive"
    assert metrics["correlation_rank_penalty_lookback_days"] == 4
    assert metrics["max_correlation_rank_score_penalty"] > 0


def test_target_weight_rebalance_days_support_lower_frequency():
    import pandas as pd
    import pytest
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(
        [
            "2025-02-03",
            "2025-02-04",
            "2025-03-03",
            "2025-03-04",
            "2025-04-01",
            "2025-04-02",
            "2025-05-02",
        ]
    )

    assert sweep.target_weight_rebalance_days(dates, "monthly") == [
        pd.Timestamp("2025-02-03"),
        pd.Timestamp("2025-03-03"),
        pd.Timestamp("2025-04-01"),
        pd.Timestamp("2025-05-02"),
    ]
    assert sweep.target_weight_rebalance_days(dates, "bimonthly") == [
        pd.Timestamp("2025-02-03"),
        pd.Timestamp("2025-04-01"),
    ]
    assert sweep.target_weight_rebalance_days(dates, "quarterly") == [
        pd.Timestamp("2025-02-03"),
        pd.Timestamp("2025-05-02"),
    ]
    with pytest.raises(ValueError, match="unsupported_rebalance_frequency"):
        sweep.target_weight_rebalance_days(dates, "weekly")


def test_target_weight_exposure_supports_volatility_target_and_drawdown_brake():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    calm_benchmark = pd.Series(
        [100.0, 100.5, 101.0, 101.5],
        index=pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]),
    )
    volatile_benchmark = pd.Series(
        [100.0, 120.0, 90.0, 125.0],
        index=calm_benchmark.index,
    )
    drawdown_benchmark = pd.Series(
        [100.0, 100.0, 100.0, 82.0],
        index=calm_benchmark.index,
    )
    params = {
        "target_exposure": 0.8,
        "market_exposure_mode": "benchmark_vol_target",
        "benchmark_vol_target_pct": 10.0,
        "benchmark_vol_lookback": 3,
        "benchmark_drawdown_lookback": 4,
        "benchmark_drawdown_trigger_pct": 8.0,
        "bear_target_exposure": 0.25,
    }

    assert sweep._target_exposure_for_day(
        pd.Timestamp("2025-01-08"),
        calm_benchmark,
        params,
    ) == 0.8

    volatile_exposure = sweep._target_exposure_for_day(
        pd.Timestamp("2025-01-08"),
        volatile_benchmark,
        params,
    )
    assert 0.25 <= volatile_exposure < 0.8

    assert sweep._target_exposure_for_day(
        pd.Timestamp("2025-01-08"),
        drawdown_benchmark,
        params,
    ) == 0.25


def test_target_weight_research_warmup_includes_exposure_lookbacks():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    class RecordingCollector:
        quiet_ohlcv_log = False

        def __init__(self):
            self.calls = []

        def fetch_korean_stock(self, symbol, start, end):
            self.calls.append((symbol, start, end))
            return pd.DataFrame()

    collector = RecordingCollector()
    sweep.run_target_weight_rotation_backtest(
        ["AAA"],
        start="2025-05-01",
        end="2025-05-02",
        capital=1_000.0,
        params={
            "target_top_n": 1,
            "short_lookback": 1,
            "long_lookback": 1,
            "market_exposure_mode": "benchmark_vol_target",
            "benchmark_vol_lookback": 100,
            "benchmark_drawdown_lookback": 90,
            "market_ma_period": 80,
        },
        collector=collector,
    )

    assert collector.calls[0][1] == "2024-07-05"


def test_target_weight_research_honors_bimonthly_rebalance_frequency():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(
        [
            "2025-01-30",
            "2025-01-31",
            "2025-02-03",
            "2025-02-04",
            "2025-03-03",
            "2025-03-04",
            "2025-04-01",
        ]
    )

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "AAA":
                return pd.DataFrame(
                    {
                        "open": [100.0, 100.0, 50.0, 50.0, 55.0, 55.0, 60.0],
                        "close": [100.0, 110.0, 120.0, 121.0, 122.0, 123.0, 124.0],
                        "volume": [100.0] * 7,
                    },
                    index=dates,
                )
            return pd.DataFrame(
                {
                    "open": [100.0] * 7,
                    "close": [100.0] * 7,
                    "volume": [100.0] * 7,
                },
                index=dates,
            )

    class NoCostRiskManager:
        def calculate_transaction_costs(self, price, quantity, side, **kwargs):
            return {
                "execution_price": float(price),
                "commission": 0.0,
                "tax": 0.0,
                "slippage": 0.0,
                "slippage_multiplier": 1.0,
                "participation_rate": 0.0,
            }

    result = sweep.run_target_weight_rotation_backtest(
        ["AAA", "BBB"],
        start="2025-02-03",
        end="2025-04-01",
        capital=1_000.0,
        params={
            "target_top_n": 1,
            "target_exposure": 1.0,
            "rebalance_frequency": "bimonthly",
            "short_lookback": 1,
            "long_lookback": 1,
            "short_weight": 1.0,
        },
        collector=FakeCollector(),
        risk_manager=NoCostRiskManager(),
    )

    metrics = result["target_weight_metrics"]
    assert metrics["rebalance_frequency"] == "bimonthly"
    assert metrics["rebalance_count"] == 2


def test_target_weight_research_records_tolerance_skipped_rebalances():
    import pandas as pd
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04", "2025-03-03"])

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "AAA":
                return pd.DataFrame(
                    {
                        "open": [100.0, 100.0, 100.0, 100.0, 104.0],
                        "close": [100.0, 110.0, 100.0, 110.0, 104.0],
                        "volume": [100.0, 100.0, 100.0, 100.0, 100.0],
                    },
                    index=dates,
                )
            if symbol == "BBB":
                return pd.DataFrame(
                    {
                        "open": [100.0, 100.0, 100.0, 100.0, 96.0],
                        "close": [100.0, 105.0, 100.0, 105.0, 96.0],
                        "volume": [100.0, 100.0, 100.0, 100.0, 100.0],
                    },
                    index=dates,
                )
            return pd.DataFrame(
                {
                    "open": [100.0, 100.0, 100.0, 100.0, 100.0],
                    "close": [100.0, 100.0, 100.0, 100.0, 100.0],
                    "volume": [100.0, 100.0, 100.0, 100.0, 100.0],
                },
                index=dates,
            )

    class NoCostRiskManager:
        def calculate_transaction_costs(self, price, quantity, side, **kwargs):
            return {
                "execution_price": float(price),
                "commission": 0.0,
                "tax": 0.0,
                "slippage": 0.0,
                "slippage_multiplier": 1.0,
                "participation_rate": 0.0,
            }

    result = sweep.run_target_weight_rotation_backtest(
        ["AAA", "BBB"],
        start="2025-02-03",
        end="2025-03-03",
        capital=1_000.0,
        params={
            "target_top_n": 2,
            "target_exposure": 1.0,
            "target_tolerance_pct": 3.0,
            "short_lookback": 1,
            "long_lookback": 1,
            "short_weight": 1.0,
        },
        collector=FakeCollector(),
        risk_manager=NoCostRiskManager(),
    )

    metrics = result["target_weight_metrics"]
    assert len(result["trades"]) == 2
    assert metrics["rebalance_tolerance_pct"] == 3.0
    assert metrics["rebalance_tolerance_skipped_trades"] == 2
    assert metrics["rebalance_tolerance_skipped_sell_trades"] == 1
    assert metrics["rebalance_tolerance_skipped_buy_trades"] == 1
    assert metrics["rebalance_tolerance_skipped_notional"] == 40.0
    assert metrics["rebalance_tolerance_skipped_notional_pct_of_capital"] == 4.0


def test_target_weight_research_blocks_missing_rebalance_open_price():
    import pandas as pd
    import pytest
    import tools.research_candidate_sweep as sweep

    dates = pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-03"])

    class MissingOpenCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            if symbol == "AAA":
                return pd.DataFrame(
                    {
                        "close": [100.0, 110.0, 500.0],
                        "volume": [100.0, 100.0, 100.0],
                    },
                    index=dates,
                )
            return pd.DataFrame(
                {
                    "open": [100.0, 100.0, 100.0],
                    "close": [100.0, 100.0, 100.0],
                    "volume": [100.0, 100.0, 100.0],
                },
                index=dates,
            )

    with pytest.raises(ValueError, match="target_weight_research_execution_price_missing"):
        sweep.run_target_weight_rotation_backtest(
            ["AAA"],
            start="2025-02-03",
            end="2025-02-03",
            capital=1_000.0,
            params={
                "target_top_n": 1,
                "target_exposure": 1.0,
                "short_lookback": 1,
                "long_lookback": 1,
                "short_weight": 1.0,
            },
            collector=MissingOpenCollector(),
        )


def test_write_sweep_artifact_does_not_touch_promotion_dir(tmp_path):
    from tools.research_candidate_sweep import write_candidate_artifacts

    output_dir = tmp_path / "reports" / "research_sweeps"
    json_path, md_path = write_candidate_artifacts(_minimal_bundle(), output_dir)

    assert json_path.exists()
    assert md_path.exists()
    assert not (tmp_path / "reports" / "promotion").exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "research_candidate_sweep_bundle"
    assert "EM Excess" in md_path.read_text(encoding="utf-8")


def test_write_sweep_artifact_surfaces_rejection_reasons(tmp_path):
    from tools.research_candidate_sweep import write_candidate_artifacts

    bundle = _minimal_bundle(
        [
            {
                "candidate_id": "risk_candidate",
                "rank_score": 10.0,
                "promotion": {
                    "status": "paper_only",
                    "reason": "provisional 미달: MDD -25.79% < -20%, turnover 1097.1%/y >= 1000.0%/y",
                },
                "metrics": {
                    "total_return": 110.39,
                    "benchmark_excess_return": 78.5,
                    "exposure_matched_excess_return": 90.25,
                    "avg_exposure_pct": 66.2,
                    "sharpe": 0.85,
                    "profit_factor": 2.06,
                    "mdd": -25.79,
                    "total_trades": 107,
                },
                "rejection_reasons": [
                    "promotion_status=paper_only",
                    "mdd < -20",
                    "turnover_per_year >= 1000",
                ],
            }
        ]
    )
    bundle["data_fetch_cache"] = {
        "enabled": True,
        "unique_fetches": 3,
        "cache_hits": 7,
        "cached_items": 3,
    }

    _, md_path = write_candidate_artifacts(bundle, tmp_path)
    text = md_path.read_text(encoding="utf-8")

    assert "Data fetch cache: unique_fetches=3, cache_hits=7, cached_items=3" in text
    assert "## Rejection Reasons" in text
    assert "## Rejection Summary" in text
    assert "risk_candidate" in text
    assert "mdd < -20" in text
    assert "turnover 1097.1%/y >= 1000.0%/y" in text


def test_run_candidate_sweep_filters_universe_before_evaluation(monkeypatch):
    import pandas as pd
    import config.config_loader as config_loader
    import tools.research_candidate_sweep as sweep

    class _Config:
        yaml_hash = "yaml"
        resolved_hash = "resolved"
        risk_params = {
            "liquidity_filter": {
                "enabled": True,
                "min_avg_trading_value_20d_krw": 5_000_000_000,
                "strict": True,
            }
        }

    def fake_filter(symbols, config, *, as_of_end):
        assert symbols == ["AAA", "BBB"]
        assert as_of_end == "2025-01-01"
        return ["AAA"], {
            "enabled": True,
            "input_symbols": ["AAA", "BBB"],
            "passed_symbols": ["AAA"],
            "excluded_symbols": ["BBB"],
            "min_avg_trading_value_20d_krw": 5_000_000_000,
            "strict": True,
            "symbols": {
                "AAA": {"passed": True, "reason": "passed"},
                "BBB": {"passed": False, "reason": "below_min_avg_trading_value"},
            },
        }

    seen = {}

    def fake_benchmark(symbols, start, end, capital):
        seen["benchmark_symbols"] = list(symbols)
        return (
            {"ew_bh_return": 0, "ew_bh_sharpe": 0, "universe_size": len(symbols), "benchmark_symbols": symbols},
            pd.Series(dtype=float),
        )

    monkeypatch.setattr(config_loader.Config, "get", staticmethod(lambda: _Config()))
    monkeypatch.setattr(sweep, "apply_research_universe_liquidity_filter", fake_filter)
    monkeypatch.setattr(sweep, "buy_and_hold_benchmark_with_returns", fake_benchmark)
    monkeypatch.setattr(sweep, "build_candidate_specs", lambda family: [])

    bundle = sweep.run_candidate_sweep(
        symbols=["AAA", "BBB"],
        start="2025-01-01",
        end="2025-12-31",
        include_walk_forward=False,
    )

    assert seen["benchmark_symbols"] == ["AAA"]
    assert bundle["input_universe"] == ["AAA", "BBB"]
    assert bundle["universe"] == ["AAA"]
    assert bundle["universe_liquidity_filter"]["excluded_symbols"] == ["BBB"]


def test_run_candidate_sweep_skips_evaluation_when_liquidity_filter_removes_all(monkeypatch, tmp_path):
    import config.config_loader as config_loader
    import tools.research_candidate_sweep as sweep

    class _Config:
        yaml_hash = "yaml"
        resolved_hash = "resolved"
        risk_params = {
            "liquidity_filter": {
                "enabled": True,
                "min_avg_trading_value_20d_krw": 5_000_000_000,
                "strict": True,
            }
        }

    def fake_filter(symbols, config, *, as_of_end):
        return [], {
            "enabled": True,
            "input_symbols": list(symbols),
            "passed_symbols": [],
            "excluded_symbols": list(symbols),
            "min_avg_trading_value_20d_krw": 5_000_000_000,
            "strict": True,
            "symbols": {
                sym: {
                    "passed": False,
                    "avg_trading_value_20d_krw": None,
                    "reason": "missing_liquidity_data",
                }
                for sym in symbols
            },
        }

    def fail_benchmark(*args, **kwargs):
        raise AssertionError("benchmark must not run when research universe is empty")

    def fail_evaluate(*args, **kwargs):
        raise AssertionError("candidate evaluation must not run when research universe is empty")

    monkeypatch.setattr(config_loader.Config, "get", staticmethod(lambda: _Config()))
    monkeypatch.setattr(sweep, "apply_research_universe_liquidity_filter", fake_filter)
    monkeypatch.setattr(sweep, "buy_and_hold_benchmark_with_returns", fail_benchmark)
    monkeypatch.setattr(sweep, "evaluate_candidate", fail_evaluate)
    monkeypatch.setattr(
        sweep,
        "build_candidate_specs",
        lambda family: [
            sweep.CandidateSpec("candidate_a", "relative_strength_rotation", {}, "A"),
            sweep.CandidateSpec("candidate_b", "relative_strength_rotation", {}, "B"),
        ],
    )

    bundle = sweep.run_candidate_sweep(
        symbols=["AAA", "BBB"],
        start="2025-01-01",
        end="2025-12-31",
        include_walk_forward=True,
    )

    assert bundle["universe"] == []
    assert bundle["candidates"] == []
    assert bundle["candidate_ids_evaluated"] == []
    assert bundle["candidate_ids_skipped_due_to_data"] == ["candidate_a", "candidate_b"]
    assert bundle["benchmark"]["benchmark_unusable_reason"] == "all_symbols_missing_liquidity_data"
    assert bundle["decision"]["action"] == "INSUFFICIENT_BENCHMARK_DATA"
    assert bundle["summary"]["evaluated"] == 0
    assert bundle["summary"]["skipped_due_to_data"] == 2
    assert bundle["data_fetch_cache"] == {
        "enabled": False,
        "skipped": True,
        "reason": "all_symbols_missing_liquidity_data",
    }
    assert bundle["walk_forward"]["windows"] == []
    ok, reason = sweep.validate_sweep_artifact(bundle)
    assert ok, reason

    _, md_path = sweep.write_candidate_artifacts(bundle, tmp_path)
    text = md_path.read_text(encoding="utf-8")
    assert "Data fetch cache: skipped (all_symbols_missing_liquidity_data)" in text
    assert "Candidate evaluation: skipped_due_to_data=2" in text


def test_run_candidate_sweep_fail_closed_when_canonical_universe_selection_fails(monkeypatch, tmp_path):
    import config.config_loader as config_loader
    import tools.research_candidate_sweep as sweep

    class _Config:
        yaml_hash = "yaml"
        resolved_hash = "resolved"
        risk_params = {"liquidity_filter": {"enabled": True, "strict": True}}

    def fake_filter(symbols, config, *, as_of_end):
        assert symbols == []
        return [], {
            "enabled": True,
            "input_symbols": [],
            "passed_symbols": [],
            "excluded_symbols": [],
            "strict": True,
            "symbols": {},
        }

    def fail_benchmark(*args, **kwargs):
        raise AssertionError("benchmark must not run when universe selection fails")

    def fail_evaluate(*args, **kwargs):
        raise AssertionError("candidate evaluation must not run when universe selection fails")

    def fail_select_canonical_universe(*args, **kwargs):
        raise RuntimeError("KRX listing blocked")

    monkeypatch.setattr(config_loader.Config, "get", staticmethod(lambda: _Config()))
    monkeypatch.setattr(sweep, "select_canonical_universe", fail_select_canonical_universe)
    monkeypatch.setattr(sweep, "apply_research_universe_liquidity_filter", fake_filter)
    monkeypatch.setattr(sweep, "buy_and_hold_benchmark_with_returns", fail_benchmark)
    monkeypatch.setattr(sweep, "evaluate_candidate", fail_evaluate)
    monkeypatch.setattr(
        sweep,
        "build_candidate_specs",
        lambda family: [
            sweep.CandidateSpec("candidate_a", "relative_strength_rotation", {}, "A"),
        ],
    )

    bundle = sweep.run_candidate_sweep(
        symbols=None,
        top_n=200,
        universe_scan_limit=400,
        start="2025-01-01",
        end="2025-12-31",
        include_walk_forward=True,
    )

    assert bundle["input_universe"] == []
    assert bundle["universe"] == []
    assert bundle["candidates"] == []
    assert bundle["candidate_ids_skipped_due_to_data"] == ["candidate_a"]
    assert bundle["benchmark"]["benchmark_unusable_reason"] == "canonical_universe_selection_failed"
    assert bundle["decision"]["action"] == "INSUFFICIENT_BENCHMARK_DATA"
    assert bundle["universe_selection"]["source"] == "canonical_liquidity_universe"
    assert bundle["universe_selection"]["selection_error_type"] == "RuntimeError"
    assert bundle["universe_selection"]["empty_universe_reason"] == "canonical_universe_selection_failed"
    assert bundle["data_fetch_cache"]["reason"] == "canonical_universe_selection_failed"

    _, md_path = sweep.write_candidate_artifacts(bundle, tmp_path)
    text = md_path.read_text(encoding="utf-8")
    assert "Data fetch cache: skipped (canonical_universe_selection_failed)" in text
    assert "Candidate evaluation: skipped_due_to_data=1" in text


def test_run_candidate_sweep_limits_to_requested_candidate_ids(monkeypatch):
    import pandas as pd
    import config.config_loader as config_loader
    import tools.research_candidate_sweep as sweep

    class _Config:
        yaml_hash = "yaml"
        resolved_hash = "resolved"
        risk_params = {"liquidity_filter": {"enabled": False}}

    evaluated = []

    def fake_evaluate(
        spec,
        symbols,
        start,
        end,
        capital,
        benchmark_daily_returns,
        **kwargs,
    ):
        evaluated.append(spec.candidate_id)
        return {
            "total_return": 12.0,
            "sharpe": 0.7,
            "profit_factor": 1.4,
            "mdd": -8.0,
            "total_trades": 40,
            "ev_per_trade": 1000.0,
            "cost_adjusted_cagr": 5.0,
            "turnover_per_year": 300.0,
        }

    monkeypatch.setattr(config_loader.Config, "get", staticmethod(lambda: _Config()))
    monkeypatch.setattr(
        sweep,
        "apply_research_universe_liquidity_filter",
        lambda symbols, config, *, as_of_end: (
            symbols,
            {
                "enabled": False,
                "input_symbols": symbols,
                "passed_symbols": symbols,
                "excluded_symbols": [],
            },
        ),
    )
    monkeypatch.setattr(
        sweep,
        "buy_and_hold_benchmark_with_returns",
        lambda symbols, start, end, capital: (
            {
                "ew_bh_return": 2.0,
                "ew_bh_sharpe": 0.2,
                "universe_size": len(symbols),
                "benchmark_symbols": symbols,
                "benchmark_coverage_complete": True,
            },
            pd.Series(dtype=float),
        ),
    )
    monkeypatch.setattr(
        sweep,
        "build_candidate_specs",
        lambda family: [
            sweep.CandidateSpec("candidate_a", "relative_strength_rotation", {}, "A"),
            sweep.CandidateSpec("candidate_b", "relative_strength_rotation", {}, "B"),
        ],
    )
    monkeypatch.setattr(sweep, "evaluate_candidate", fake_evaluate)

    bundle = sweep.run_candidate_sweep(
        symbols=["AAA", "BBB"],
        start="2025-01-01",
        end="2025-12-31",
        include_walk_forward=False,
        candidate_ids=["candidate_b"],
    )

    assert evaluated == ["candidate_b"]
    assert bundle["candidate_ids_requested"] == ["candidate_b"]
    assert bundle["candidate_ids_evaluated"] == ["candidate_b"]
    assert [record["candidate_id"] for record in bundle["candidates"]] == ["candidate_b"]


def test_run_candidate_sweep_reports_target_weight_fetch_cache(monkeypatch):
    import pandas as pd
    import config.config_loader as config_loader
    import core.data_collector as data_collector
    import tools.research_candidate_sweep as sweep

    class _Config:
        yaml_hash = "yaml"
        resolved_hash = "resolved"
        risk_params = {"liquidity_filter": {"enabled": False}}

    class FakeCollector:
        quiet_ohlcv_log = False

        def fetch_korean_stock(self, symbol, start, end):
            return pd.DataFrame(
                {"close": [100.0, 101.0], "volume": [10.0, 10.0]},
                index=pd.bdate_range("2025-01-01", periods=2),
            )

    def fake_target_runner(**kwargs):
        kwargs["collector"].fetch_korean_stock("AAA", "2024-01-01", "2025-01-31")
        return {
            "equity_curve": pd.DataFrame(
                {
                    "date": pd.bdate_range("2025-01-01", periods=2),
                    "value": [100.0, 110.0],
                    "cash": [50.0, 60.0],
                    "n_positions": [1, 1],
                }
            ),
            "trades": [
                {
                    "date": pd.Timestamp("2025-01-02"),
                    "symbol": "AAA",
                    "action": "BUY",
                    "price": 10.0,
                    "quantity": 1.0,
                    "pnl": 0,
                }
            ],
            "target_weight_metrics": {
                "target_top_n": 1,
                "turnover_per_year": 200.0,
            },
        }

    monkeypatch.setattr(config_loader.Config, "get", staticmethod(lambda: _Config()))
    monkeypatch.setattr(data_collector, "DataCollector", FakeCollector)
    monkeypatch.setattr(
        sweep,
        "apply_research_universe_liquidity_filter",
        lambda symbols, config, *, as_of_end: (
            symbols,
            {"enabled": False, "input_symbols": symbols, "passed_symbols": symbols},
        ),
    )
    monkeypatch.setattr(
        sweep,
        "buy_and_hold_benchmark_with_returns",
        lambda symbols, start, end, capital: (
            {
                "ew_bh_return": 2.0,
                "ew_bh_sharpe": 0.2,
                "universe_size": len(symbols),
                "benchmark_symbols": symbols,
                "benchmark_coverage_complete": True,
            },
            pd.Series(dtype=float),
        ),
    )
    monkeypatch.setattr(
        sweep,
        "build_candidate_specs",
        lambda family: [
            sweep.CandidateSpec("candidate_a", "target_weight_rotation", {"target_top_n": 1}, "A"),
            sweep.CandidateSpec("candidate_b", "target_weight_rotation", {"target_top_n": 1}, "B"),
        ],
    )
    monkeypatch.setattr(sweep, "run_target_weight_rotation_backtest", fake_target_runner)

    bundle = sweep.run_candidate_sweep(
        symbols=["AAA"],
        start="2025-01-01",
        end="2025-01-31",
        include_walk_forward=False,
    )

    assert bundle["data_fetch_cache"] == {
        "enabled": True,
        "unique_fetches": 1,
        "cache_hits": 1,
        "cached_items": 1,
    }


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
