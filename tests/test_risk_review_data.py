"""서로 다른 자료의 마지막 날짜와 중간 누락을 숨기지 않는다."""

import pandas as pd
import pytest

from tools.risk_review import align_report_inputs


def sources():
    dates = pd.to_datetime(["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"])
    return {
        symbol: pd.Series([100.0, 101.0, 102.0, 103.0], index=dates)
        for symbol in ("069500", "357870", "KS200")
    }


def test_lagging_index_limits_all_comparisons_and_records_exclusions():
    inputs = sources()
    inputs["KS200"] = inputs["KS200"].iloc[:2]
    aligned, panel, audit = align_report_inputs(inputs, "2026-09-22")
    assert all(
        values.index[-1] == pd.Timestamp("2026-09-17") for values in aligned.values()
    )
    assert not panel.isna().any().any()
    assert audit["latest_available"]["069500"] == "2026-09-21"
    assert audit["excluded_after_common_bar"] == {"069500": 2, "357870": 2, "KS200": 0}
    assert len(inputs["069500"]) == 4


def test_incomplete_today_and_future_bars_are_never_used():
    aligned, _, audit = align_report_inputs(sources(), "2026-09-18")
    assert all(len(values) == 2 for values in aligned.values())
    assert audit["common_last_bar"] == "2026-09-17"


def test_missing_internal_bar_stops_report_instead_of_forward_filling():
    inputs = sources()
    inputs["KS200"] = inputs["KS200"].drop(pd.Timestamp("2026-09-17"))
    with pytest.raises(ValueError, match="종가 누락: 2026-09-17"):
        align_report_inputs(inputs, "2026-09-22")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_invalid_prices_stop_report(bad):
    inputs = sources()
    inputs["069500"].iloc[1] = bad
    with pytest.raises(ValueError, match="종가와 날짜"):
        align_report_inputs(inputs, "2026-09-22")


def test_nonoverlapping_sources_stop_with_explanation():
    inputs = sources()
    inputs["KS200"].index = inputs["KS200"].index - pd.Timedelta(days=30)
    with pytest.raises(ValueError, match="겹치는 비교 기간"):
        align_report_inputs(inputs, "2026-09-22")
