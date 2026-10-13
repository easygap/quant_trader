from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.market_research import (build_market_research, collect_market_research, expected_session,
                                  load_research_config, read_market_research, save_market_research)


def config():
    return {"benchmark": {"symbol": "069500", "name": "Index", "cohort": "benchmark", "role": "benchmark"},
            "horizons": [20, 60, 120], "ranking_horizon": 60,
            "universe": [{"symbol": "005930", "name": "A", "cohort": "stocks", "role": "basket_target"},
                         {"symbol": "091160", "name": "ETF", "cohort": "etfs", "role": "observation_only"}]}


def frame(rate=0.002, end="2026-10-12", periods=150):
    dates = pd.bdate_range(end=end, periods=periods)
    return pd.DataFrame({"close": 100 * (1 + rate) ** np.arange(periods), "volume": 1000.}, index=dates)


def report(prices=None, sources=None, **kwargs):
    return build_market_research(config(), prices or {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)},
                                 sources=sources or {s: "FinanceDataReader" for s in ("069500", "005930", "091160")},
                                 generated_at="2026-10-13T11:00:00+09:00", holidays=[], **kwargs)


def test_completed_session_excludes_holiday_weekend_and_current_even_after_close():
    assert expected_session("2026-10-12T11:00:00+09:00", holidays=["2026-10-09"]) == "2026-10-08"
    assert expected_session("2026-10-13T19:00:00+09:00", holidays=[]) == "2026-10-12"
    assert expected_session("2026-10-12T23:59:00+00:00", holidays=[]) == "2026-10-12"


def test_weekend_reader_uses_same_verified_session_without_wall_clock_expiry(tmp_path):
    prices = {symbol: frame(end="2026-10-16") for symbol in ("069500", "005930", "091160")}
    output = build_market_research(config(), prices, sources={symbol: "FinanceDataReader" for symbol in prices},
                                  generated_at="2026-10-17T08:30:00+09:00", holidays=[])
    paths = save_market_research(output, tmp_path)
    monday = read_market_research(paths["latest"], now="2026-10-19T08:00:00+09:00")
    assert monday["status"] == "ready"
    assert monday["read_expected_session"] == monday["as_of"] == "2026-10-16"
    assert read_market_research(paths["latest"], now="2026-10-20T08:00:00+09:00")["status"] == "stale"


def test_exact_horizon_and_volume_baseline_excludes_latest():
    prices = {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)}
    prices["005930"].iloc[-1, prices["005930"].columns.get_loc("volume")] = 2000
    output = report(prices)
    row = output["observations"][0]
    assert output["status"] == "ready"
    assert row["horizons"]["20"]["return_pct"] == pytest.approx(((1.002**20) - 1)*100, abs=1e-6)
    assert row["horizons"]["20"]["excess_pct"] == pytest.approx(((1.002**20) - (1.001**20))*100, abs=1e-6)
    assert row["volume_ratio_20d"] == 2
    assert row["drawdown_120d_pct"] == 0
    assert row["above_sma60"] is True
    assert output["breadth"][0]["total"] == 1
    assert all(row["rank"] == 1 for row in output["observations"])  # stocks never compete with ETF


def test_current_and_future_bars_cannot_change_report_metrics():
    prices = {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)}
    expected = report(prices)
    for data in prices.values():
        data.loc[pd.Timestamp("2026-10-13")] = [9999999, 9999999]
        data.loc[pd.Timestamp("2026-10-14")] = [0.01, 9999999]
    assert report(prices) == expected


def test_stale_asset_is_never_compared_over_its_own_endpoint():
    prices = {"069500": frame(.001), "005930": frame(.002).iloc[:-1], "091160": frame(.003)}
    result = report(prices)
    row = result["observations"][0]
    assert row["as_of"] == "2026-10-09"
    assert row["rank"] is None
    assert row["horizons"]["20"] is None
    assert row["missing_sessions"] == 1
    assert result["coverage"]["ranked"] == 1


def test_stale_benchmark_prevents_all_rankings():
    result = report({"069500": frame(.001).iloc[:-1], "005930": frame(.002), "091160": frame(.003)})
    assert result["status"] == "unavailable"
    assert result["as_of"] is None
    assert result["leaders"] == []


def test_halt_or_gap_inside_window_is_not_silently_forward_filled():
    prices = {"069500": frame(.001), "005930": frame(.002).drop(frame().index[-8]), "091160": frame(.003)}
    row = report(prices)["observations"][0]
    assert row["horizons"]["20"] is None
    assert row["volatility_20d_pct"] is None
    assert row["rank"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_invalid_price_remains_unknown(bad):
    prices = {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)}
    prices["005930"].iloc[-1, 0] = bad
    row = report(prices)["observations"][0]
    assert row["rank"] is None
    assert row["horizons"]["20"] is None
    json.dumps(row, allow_nan=False)


def test_duplicate_dates_are_ambiguous_not_arbitrarily_selected():
    prices = {"069500": frame(.001), "005930": pd.concat([frame(), frame().tail(1)]), "091160": frame(.003)}
    row = report(prices)["observations"][0]
    assert row["status"] == "unavailable"
    assert "duplicate_session" in row["issues"]


def test_insufficient_history_stays_missing_instead_of_annualized_or_shortened():
    row = report({"069500": frame(.001), "005930": frame(.002, periods=35), "091160": frame(.003)})["observations"][0]
    assert row["horizons"]["20"] is not None
    assert row["horizons"]["60"] is None
    assert row["horizons"]["120"] is None
    assert row["rank"] is None


def test_unknown_adjustment_basis_and_raw_kis_data_not_ranked():
    output = report(sources={"069500": "FinanceDataReader", "005930": "KIS", "091160": "offline_csv"})
    assert output["status"] == "unavailable"
    assert all(row["rank"] is None and "adjustment_unverified" in row["issues"] for row in output["observations"])


def test_zero_volume_baseline_is_unknown_not_infinite_activity():
    prices = {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)}
    prices["005930"]["volume"] = 0
    row = report(prices)["observations"][0]
    assert row["volume_ratio_20d"] is None
    assert "volume_unavailable" in row["issues"]


def test_lead_changes_require_same_config_and_same_eligible_members():
    cfg = config()
    cfg["universe"][1]["cohort"] = "stocks"
    prices = {"069500": frame(.001), "005930": frame(.002), "091160": frame(.003)}
    kwargs = {"sources": {s: "FinanceDataReader" for s in prices}, "generated_at": "2026-10-13", "holidays": []}
    previous = build_market_research(cfg, prices, **kwargs)
    previous["as_of"] = "2026-10-09"
    previous["observations"][0]["rank"], previous["observations"][1]["rank"] = 1, 2
    assert len(build_market_research(cfg, prices, previous=previous, **kwargs)["lead_changes"]) == 2
    previous["observations"][1]["rank"] = None
    assert build_market_research(cfg, prices, previous=previous, **kwargs)["lead_changes"] == []
    previous["config_hash"] = "other"
    assert build_market_research(cfg, prices, previous=previous, **kwargs)["lead_changes"] == []


def test_read_recomputes_freshness_without_modifying_saved_file(tmp_path, monkeypatch):
    import core.market_research as module
    original = report()
    monkeypatch.setattr(module, "expected_session", lambda now=None, **kwargs: str(now)[:10])
    paths = save_market_research(original, tmp_path)
    text = (tmp_path / "latest.json").read_text(encoding="utf-8")
    output = read_market_research(paths["latest"], now="2026-10-14T11:00:00+09:00")
    assert output["status"] == "stale"
    assert output["is_stale"] is True
    assert (tmp_path / "latest.json").read_text(encoding="utf-8") == text
    save_market_research(original, tmp_path)
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 1


@pytest.mark.parametrize("payload", [[], {}, {"schema_version": 99}, {"schema_version": 1, "research_only": False}])
def test_reader_invalid_schema_is_unavailable(tmp_path, payload):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_market_research(path)["status"] == "unavailable"


def test_collector_retains_safe_error_class_only_and_has_no_write_api(monkeypatch):
    import core.market_research as module
    monkeypatch.setattr(module, "expected_session", lambda *args, **kwargs: "2026-10-12")
    monkeypatch.setattr(module, "_calendar", lambda *args: set())
    class FakeCollector:
        def fetch_korean_stock(self, symbol, start_date, end_date):
            assert end_date == "2026-10-12"
            if symbol == "005930":
                raise RuntimeError("secret_api_key=private")
            return frame()
        def get_last_source_info(self):
            return {"source": "FinanceDataReader"}
    output = collect_market_research(config(), FakeCollector(), now="2026-10-13")
    assert output["collection_failures"] == {"005930": "RuntimeError"}
    assert "private" not in json.dumps(output)


def test_invalid_universe_bounded_and_default_config_is_observation_only():
    cfg = config()
    cfg["universe"] *= 51
    with pytest.raises(ValueError):
        build_market_research(cfg, {}, holidays=[])
    default = load_research_config()
    assert all(row["role"] in {"basket_target", "observation_only"} for row in default["universe"])


def test_mixed_provider_tail_without_scale_proof_has_no_return_or_rank():
    result = report(sources={"069500": "FinanceDataReader", "005930": "FinanceDataReader+yfinance", "091160": "FinanceDataReader"})
    row = result["observations"][0]
    assert "mixed_price_sources" in row["issues"]
    assert row["rank"] is None and row["horizons"]["20"] is None
    assert row["above_sma60"] is None


def test_mixed_benchmark_does_not_leak_uncomparable_excess_returns():
    result = report(sources={"069500": "FinanceDataReader+yfinance", "005930": "FinanceDataReader", "091160": "FinanceDataReader"})
    assert result["status"] == "unavailable"
    assert all(row["horizons"]["60"] is None for row in result["observations"])


def test_offline_builder_marks_replay_and_keeps_generation_separate_from_cutoff():
    assert report()["replay"] is True
    output = build_market_research(config(), {}, generated_at="2026-10-14T11:00:00+09:00",
                                   evaluation_at="2026-10-13T11:00:00+09:00", holidays=[])
    assert output["expected_session"] == "2026-10-12"
    assert output["generated_at"].startswith("2026-10-14")
    assert output["evaluation_at"].startswith("2026-10-13")


def test_backdated_live_source_provenance_is_rejected(tmp_path):
    output = report()
    output["replay"] = False
    output["observations"][0]["source"]["observed_at"] = "2026-10-14T11:00:00+09:00"
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(output), encoding="utf-8")
    assert read_market_research(path, now="2026-10-15")["status"] == "unavailable"


def test_default_reader_detects_changed_universe_even_same_session(tmp_path, monkeypatch):
    import core.market_research as module
    output = report()
    save_market_research(output, tmp_path)
    monkeypatch.setenv("QUANT_MARKET_RESEARCH_DIR", str(tmp_path))
    changed = config()
    changed["name"] = "changed configuration"
    monkeypatch.setattr(module, "load_research_config", lambda: changed)
    monkeypatch.setattr(module, "expected_session", lambda now=None: "2026-10-12")
    actual = read_market_research(now="2026-10-13T12:00:00+09:00")
    assert actual["config_changed"] is True and actual["status"] == "stale"
