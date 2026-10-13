from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from core import fundamental_loader as loader
from strategies import fundamental_factor as factor


@pytest.mark.parametrize("info,expected", [
    ({"debtToEquity": 145.03}, 145.03),
    ({"debtToEquity": 0.7}, 0.7),
    ({"totalDebt": 145.03, "totalStockholderEquity": 100}, 145.03),
    ({"debtToEquity": float("inf")}, None),
    ({"debtToEquity": float("nan")}, None),
    ({"debtToEquity": -5}, None),
    ({"totalDebt": 100, "totalStockholderEquity": -20}, None),
    ({"totalDebt": 100, "totalStockholderEquity": 0}, None),
])
def test_yahoo_debt_percentage_units_and_invalid_equity(info, expected):
    assert loader.yahoo_debt_to_equity_pct(info) == expected


def test_successful_per_does_not_prevent_loading_debt_and_preserves_provenance(monkeypatch):
    monkeypatch.setattr(loader, "_get_fundamentals_pykrx", lambda symbol: {"per": 8., "debt_ratio": None, "available": True, "source": "pykrx"})
    monkeypatch.setattr(loader, "_get_fundamentals_yfinance", lambda symbol: {"per": 10., "debt_ratio": 35., "available": True, "source": "yfinance"})
    output = loader.get_fundamentals("005930")
    assert output["per"] == 8
    assert output["debt_ratio"] == 35
    assert output["field_sources"] == {"per": "pykrx", "debt_ratio": "yfinance"}
    assert output["debt_ratio_basis"] == "interest_bearing_debt_to_equity_pct"
    passed, reason = loader.check_fundamental_filter("005930", debt_ratio_max=200)
    assert passed is False and "요구 총부채/자본, 제공 차입금/자본" in reason
    assert loader.check_fundamental_filter("005930", debt_ratio_max=200,
                                            debt_ratio_basis="interest_bearing_debt_to_equity_pct")[0] is True


def test_yahoo_unavailable_does_not_relabel_available_pykrx_per_as_none(monkeypatch):
    monkeypatch.setattr(loader, "_get_fundamentals_pykrx", lambda symbol: {"per": 8., "debt_ratio": None, "available": True, "source": "pykrx"})
    monkeypatch.setattr(loader, "_get_fundamentals_yfinance", lambda symbol: {"per": None, "debt_ratio": None, "available": False, "source": "yfinance"})
    assert loader.get_fundamentals("005930")["source"] == "pykrx"


@pytest.mark.parametrize("basis,passed", [(None, False), ("unknown", False),
                                         ("interest_bearing_debt_to_equity_pct", False),
                                         ("total_liabilities_to_equity_pct", True)])
def test_default_debt_filter_requires_verified_total_liabilities_basis(monkeypatch, basis, passed):
    monkeypatch.setattr(loader, "get_fundamentals", lambda symbol: {"per": 10., "debt_ratio": 35., "debt_ratio_basis": basis})
    actual, reason = loader.check_fundamental_filter("005930")
    assert actual is passed
    assert ("기준 불일치" in reason) is (not passed)


def test_explicit_interest_debt_basis_uses_its_own_threshold_and_label(monkeypatch):
    monkeypatch.setattr(loader, "get_fundamentals", lambda symbol: {"per": 10., "debt_ratio": 250.,
                                                                 "debt_ratio_basis": "interest_bearing_debt_to_equity_pct"})
    passed, reason = loader.check_fundamental_filter("005930", debt_ratio_max=200,
                                                     debt_ratio_basis="interest_bearing_debt_to_equity_pct")
    assert passed is False and "차입금/자본 250.0% > 200%" in reason


def test_historical_bundle_never_queries_current_yahoo_financials(monkeypatch):
    seen = []
    monkeypatch.setattr(factor, "_stock_per_pykrx", lambda symbol, as_of_date=None: seen.append(as_of_date) or 12.)
    monkeypatch.setattr(factor, "_market_fundamental_for_symbol", lambda *args, **kwargs: (None, "", ""))
    def forbidden(*args, **kwargs):
        pytest.fail("current Yahoo values must not enter a historical bundle")
    monkeypatch.setattr(factor, "_yf_per", forbidden)
    monkeypatch.setattr(factor, "_yf_roe_debt_op_yoy", forbidden)
    date = datetime(2023, 1, 3)
    output = factor._fetch_fundamental_bundle("005930", date)
    assert seen == [date]
    assert output["roe"] is None and output["debt_ratio"] is None and output["op_income_yoy"] is None
    assert output["financial_history_available"] is False
    assert output["per"] == 12


def test_historical_missing_per_never_falls_back_to_current_yahoo(monkeypatch):
    monkeypatch.setattr(factor, "_stock_per_pykrx", lambda *args, **kwargs: None)
    monkeypatch.setattr(factor, "_market_fundamental_for_symbol", lambda *args, **kwargs: (None, "", ""))
    monkeypatch.setattr(factor, "_yf_per", lambda *args: pytest.fail("current PER fallback"))
    output = factor._fetch_fundamental_bundle("005930", datetime(2023, 1, 3))
    assert output["per"] is None and output["source"] == "none"


def _strategy():
    return factor.FundamentalFactorStrategy(SimpleNamespace(strategies={"fundamental_factor": {"per_sector_relative": False}}))


def test_mixed_source_string_cannot_make_yahoo_debt_trusted():
    strategy = _strategy()
    bundle = {"debt_ratio": 35, "source": "pykrx+yfinance_financials", "field_sources": {"debt_ratio": "yfinance"},
              "debt_ratio_basis": "interest_bearing_debt_to_equity_pct"}
    score, details = strategy._score_bundle(bundle)
    assert score == 0 and details["_max_score"] == 0
    assert "debt_ratio_skipped" in details
    bundle["field_sources"]["debt_ratio"] = "dart"
    bundle["debt_ratio_basis"] = "total_liabilities_to_equity_pct"
    assert strategy._score_bundle(bundle)[0] == 1


def test_historical_analyzer_does_no_io_when_required_pit_financials_are_unavailable(monkeypatch):
    strategy = _strategy()
    dates = pd.to_datetime(["2023-01-02", "2023-01-03", "2023-01-04"])
    prices = pd.DataFrame({"close": [10, 11, 12]}, index=dates)
    prices.attrs["symbol"] = "005930"
    monkeypatch.setattr(strategy, "_cached_bundle", lambda *args, **kwargs: pytest.fail("no pointless historical network calls"))
    result = strategy.analyze(prices)
    assert result["signal"].tolist() == ["HOLD", "HOLD", "HOLD"]
    assert result["ensemble_skip"].all()
    assert result["fundamental_data_status"].eq("point_in_time_financial_history_unavailable").all()


def test_one_valid_factor_is_not_treated_as_complete_ensemble_vote(monkeypatch):
    strategy = _strategy()
    prices = pd.DataFrame({"close": [10]}, index=pd.to_datetime(["2023-01-02"]))
    prices.attrs["symbol"] = "005930"
    monkeypatch.setattr(strategy, "_cached_bundle", lambda *args, **kwargs: {"per": 10})
    row = strategy.analyze_current(prices).iloc[-1]
    assert row["signal"] == "HOLD" and row["ensemble_skip"]


def test_fixed_sixty_day_lag_is_not_invented_as_filing_date(monkeypatch):
    factor._CACHE.clear()
    strategy = _strategy()
    seen = []
    monkeypatch.setattr(factor, "_fetch_fundamental_bundle", lambda symbol, as_of_date=None: seen.append(as_of_date) or {})
    reference = datetime(2023, 1, 3)
    strategy._cached_bundle("005930", as_of_date=reference)
    assert seen == [reference]


def test_current_and_historical_cache_namespaces_cannot_mix(monkeypatch):
    factor._CACHE.clear()
    strategy = _strategy()
    seen = []
    def fetch(symbol, as_of_date=None):
        seen.append(as_of_date)
        return {"roe": 999 if as_of_date is None else None}
    monkeypatch.setattr(factor, "_fetch_fundamental_bundle", fetch)
    assert strategy._cached_bundle("005930")["roe"] == 999
    reference = datetime(2023, 1, 3)
    assert strategy._cached_bundle("005930", reference)["roe"] is None
    assert strategy._cached_bundle("005930", reference)["roe"] is None
    assert seen == [None, reference]


def test_explicit_historical_single_signal_uses_dated_bundle(monkeypatch):
    strategy = _strategy()
    seen = []
    monkeypatch.setattr(strategy, "_cached_bundle", lambda symbol, as_of_date=None: seen.append(as_of_date) or {})
    reference = datetime(2023, 1, 3)
    strategy.generate_signal(pd.DataFrame(), symbol="005930", as_of_date=reference)
    assert seen == [reference]


def test_current_fundamental_only_updates_last_row_and_historical_call_stays_pure(monkeypatch):
    strategy = _strategy()
    prices = pd.DataFrame({"close": [10, 11, 12]}, index=pd.date_range("2023-01-02", periods=3))
    prices.attrs["symbol"] = "005930"
    seen = []
    monkeypatch.setattr(strategy, "_cached_bundle", lambda symbol, as_of_date=None: seen.append(as_of_date) or {"per": 10, "roe": 20})
    current = strategy.analyze_current(prices)
    assert seen == [None]
    assert current["signal"].tolist() == ["HOLD", "HOLD", "BUY"]
    assert current["ensemble_skip"].tolist() == [True, True, False]
    historical = strategy.analyze(prices)
    assert historical["ensemble_skip"].all() and seen == [None]


def test_live_ensemble_includes_current_fundamentals_without_historical_contamination(monkeypatch):
    from core.strategy_ensemble import StrategyEnsemble
    strategy = _strategy()
    cfg = SimpleNamespace(strategies={"ensemble": {"components": [{"name": "unknown", "enabled": True}], "auto_downgrade": False}})
    ensemble = StrategyEnsemble(cfg, skip_independence_check=True)
    ensemble._strategies = [("fundamental_factor", strategy, 1.)]
    prices = pd.DataFrame({"close": [10, 11, 12]}, index=pd.date_range("2023-01-02", periods=3))
    seen = []
    monkeypatch.setattr(strategy, "_cached_bundle", lambda symbol, as_of_date=None: seen.append((symbol, as_of_date)) or {"per": 10, "roe": 20})
    assert ensemble.analyze(prices)["signal"].tolist() == ["HOLD", "HOLD", "HOLD"]
    assert seen == []
    live = ensemble.generate_signal(prices, symbol="005930")
    assert live["signal"] == "BUY"
    assert not live["details"].get("fundamental_factor_skipped")
    assert seen == [("005930", None)]
    historical = ensemble.generate_signal(prices, symbol="005930", as_of_date=datetime(2023, 1, 4))
    assert historical["signal"] == "HOLD"
    assert historical["details"]["fundamental_factor_skipped"] is True
    assert seen == [("005930", None)]
