from datetime import datetime
from types import SimpleNamespace

import pytest
import requests

from core.observation_benchmark import BenchmarkCollector, BenchmarkUnavailable


class Transport:
    def __init__(self, rows, status=200):
        self.rows, self.status, self.calls = rows, status, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status, json=lambda: self.rows)


def collector(rows, at="2026-10-13T13:00:00+09:00", status=200):
    transport = Transport(rows, status)
    return BenchmarkCollector(transport=transport, clock=lambda: datetime.fromisoformat(at)), transport


def row(day="2026-10-12", price="6,384.24"):
    return {"localTradedAt": day, "closePrice": price}


def test_exact_previous_session_ignores_current_provisional_row():
    source, transport = collector([row("2026-10-13", "6,500.00"), row()])
    result = source.fetch_korean_stock("KS11", "2026-10-12", "2026-10-12")
    assert result["close"].iloc[0] == 6384.24
    assert list(result.index.strftime("%Y-%m-%d")) == ["2026-10-12"]
    info = source.get_last_source_info()
    assert info["source"] == "NAVERPublicIndex" and info["price_basis"] == "price_index"
    assert info["direct_krx_feed"] is False and info["publication_timestamp"] is None
    assert info["observed_at"] == "2026-10-13T13:00:00+09:00"
    assert info["replay"] is True
    assert transport.calls[0][0].endswith("/KOSPI/price")
    assert transport.calls[0][1]["timeout"] == 10
    assert transport.calls[0][1]["params"] == {"pageSize": 60, "page": 1}


@pytest.mark.parametrize("at,allowed", [("15:59:59", False), ("16:00:00", True)])
def test_current_day_is_not_available_until_publication_buffer(at, allowed):
    source, transport = collector([row("2026-10-13")], at=f"2026-10-13T{at}+09:00")
    if allowed:
        assert not source.fetch_korean_stock("KS11", "2026-10-13", "2026-10-13").empty
    else:
        with pytest.raises(BenchmarkUnavailable, match="not_completed"):
            source.fetch_korean_stock("KS11", "2026-10-13", "2026-10-13")
        assert not transport.calls


@pytest.mark.parametrize("price", [None, True, "NaN", float("nan"), float("inf"), "0", "-3", "6,38.24"])
def test_invalid_close_never_becomes_a_zero_or_filled_price(price):
    source, _ = collector([row(price=price)])
    with pytest.raises(BenchmarkUnavailable, match="invalid_benchmark_price"):
        source.fetch_korean_stock("KS11", "2026-10-12", "2026-10-12")
    assert source.get_last_source_info()["status"] == "unavailable"


@pytest.mark.parametrize("rows,reason", [([row(), row()], "duplicate"),
                                        ([row("2026-10-08")], "exact_benchmark_session_missing"),
                                        ([row("2026-99-12")], "invalid_benchmark_date"),
                                        ({"error": "private response"}, "invalid_benchmark_schema")])
def test_duplicate_missing_or_malformed_dates_fail_closed(rows, reason):
    source, _ = collector(rows)
    with pytest.raises(BenchmarkUnavailable, match=reason):
        source.fetch_korean_stock("KS11", "2026-10-12", "2026-10-12")


def test_holiday_is_not_fabricated_in_exact_multisession_window():
    source, _ = collector([row("2026-10-12"), row("2026-10-08", "6,625.93")])
    result = source.fetch_korean_stock("KS11", "2026-10-08", "2026-10-12")
    assert list(result.index.strftime("%Y-%m-%d")) == ["2026-10-08", "2026-10-12"]


def test_series_mapping_and_failure_do_not_reuse_previous_success():
    source, transport = collector([row(price="1,004.92")])
    assert source.fetch_korean_stock("KS200", "2026-10-12", "2026-10-12")["close"].iloc[0] == 1004.92
    assert transport.calls[0][0].endswith("/KPI200/price")
    transport.rows = []
    with pytest.raises(BenchmarkUnavailable):
        source.fetch_korean_stock("KS200", "2026-10-12", "2026-10-12")
    assert source.get_last_source_info()["status"] == "unavailable"
    assert "quote_dates" not in source.get_last_source_info()


def test_future_and_unbounded_history_never_request_network():
    source, transport = collector([row()])
    for day in ("2026-10-14", "2020-01-01"):
        with pytest.raises(BenchmarkUnavailable):
            source.fetch_korean_stock("KS11", day, day)
    assert not transport.calls


def test_request_errors_and_body_are_not_exposed():
    source, transport = collector([row()])
    def fail(*args, **kwargs):
        raise requests.Timeout("https://example?private=SECRET")
    transport.get = fail
    with pytest.raises(BenchmarkUnavailable) as error:
        source.fetch_korean_stock("KS11", "2026-10-12", "2026-10-12")
    assert "SECRET" not in str(error.value)
    assert str(error.value) == "benchmark_request_failed:Timeout"
