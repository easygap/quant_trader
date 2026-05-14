from datetime import datetime, timedelta
from types import SimpleNamespace


def _config(policy="block", dart_enabled=False):
    return SimpleNamespace(
        trading={"earnings_filter_unknown_policy": policy},
        dart={"enabled": dart_enabled, "api_key": ""},
    )


def test_earnings_lookup_unknown_blocks_by_default(monkeypatch):
    import core.earnings_filter as ef

    monkeypatch.setattr(
        ef,
        "_lookup_next_earnings_date_yfinance",
        lambda symbol: ef.EarningsLookupResult(source="yfinance", reason="calendar missing"),
    )
    monkeypatch.setattr(
        ef,
        "_lookup_next_earnings_date_dart",
        lambda symbol, config=None: ef.EarningsLookupResult(source="dart", reason="dart api key missing"),
    )

    blocked, reason = ef.is_near_earnings("005930", skip_days=3, config=_config())

    assert blocked is True
    assert "실적일 조회 불가" in reason
    assert "dart api key missing" in reason


def test_earnings_lookup_unknown_can_be_explicitly_allowed(monkeypatch):
    import core.earnings_filter as ef

    monkeypatch.setattr(
        ef,
        "_lookup_next_earnings_date_yfinance",
        lambda symbol: ef.EarningsLookupResult(source="yfinance", reason="calendar missing"),
    )
    monkeypatch.setattr(
        ef,
        "_lookup_next_earnings_date_dart",
        lambda symbol, config=None: ef.EarningsLookupResult(source="dart", reason="dart disabled"),
    )

    blocked, reason = ef.is_near_earnings("005930", skip_days=3, config=_config(policy="allow"))

    assert blocked is False
    assert reason == ""


def test_earnings_lookup_found_near_date_blocks(monkeypatch):
    import core.earnings_filter as ef

    earnings_date = datetime(2026, 5, 15)
    monkeypatch.setattr(
        ef,
        "lookup_next_earnings_date",
        lambda symbol, config=None: ef.EarningsLookupResult(
            date=earnings_date,
            status="found",
            source="yfinance",
        ),
    )

    blocked, reason = ef.is_near_earnings(
        "005930",
        skip_days=3,
        reference_date=datetime(2026, 5, 14),
        config=_config(),
    )

    assert blocked is True
    assert "2026-05-15" in reason


def test_earnings_lookup_found_outside_window_allows(monkeypatch):
    import core.earnings_filter as ef

    earnings_date = datetime(2026, 6, 30)
    monkeypatch.setattr(
        ef,
        "lookup_next_earnings_date",
        lambda symbol, config=None: ef.EarningsLookupResult(
            date=earnings_date,
            status="found",
            source="dart",
        ),
    )

    blocked, reason = ef.is_near_earnings(
        "005930",
        skip_days=3,
        reference_date=earnings_date - timedelta(days=10),
        config=_config(),
    )

    assert blocked is False
    assert reason == ""


def test_get_next_earnings_date_keeps_optional_datetime_api(monkeypatch):
    import core.earnings_filter as ef

    earnings_date = datetime(2026, 5, 20)
    monkeypatch.setattr(
        ef,
        "lookup_next_earnings_date",
        lambda symbol, config=None: ef.EarningsLookupResult(
            date=earnings_date,
            status="found",
            source="dart",
        ),
    )

    assert ef.get_next_earnings_date("005930", config=_config()) == earnings_date
