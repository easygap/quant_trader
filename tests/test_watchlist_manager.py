import pandas as pd


def test_watchlist_manager_returns_manual_symbols():
    from core.watchlist_manager import WatchlistManager

    class _Config:
        watchlist_settings = {"mode": "manual", "symbols": ["005930", "000660"]}

    symbols = WatchlistManager(_Config()).resolve()
    assert symbols == ["005930", "000660"]


def test_watchlist_manager_builds_top_market_cap(monkeypatch):
    from core.watchlist_manager import WatchlistManager

    class _Config:
        watchlist_settings = {"mode": "top_market_cap", "market": "KOSPI", "top_n": 2, "symbols": []}
        risk_params = {}

    sample = pd.DataFrame(
        {
            "Code": ["000001", "000002", "000003"],
            "Market": ["KOSPI", "KOSPI", "KOSDAQ"],
            "Marcap": [100, 300, 500],
        }
    )

    monkeypatch.setattr(
        "core.data_collector.DataCollector.get_krx_stock_list",
        staticmethod(lambda *a, **kw: sample),
    )
    symbols = WatchlistManager(_Config()).resolve()
    assert symbols == ["000002", "000001"]


def test_watchlist_liquidity_report_records_exclusion_reasons(monkeypatch):
    from core.watchlist_manager import WatchlistManager

    class _Config:
        risk_params = {
            "liquidity_filter": {
                "enabled": True,
                "min_avg_trading_value_20d_krw": 5_000_000_000,
                "strict": True,
            }
        }

    values = {
        "PASS": 6_000_000_000,
        "LOW": 1_000_000_000,
        "MISS": None,
    }

    monkeypatch.setattr(
        WatchlistManager,
        "_compute_avg_trading_value_20d",
        staticmethod(lambda collector, symbol, as_of_end=None: values[symbol]),
    )

    report = WatchlistManager(_Config()).liquidity_filter_report(
        ["PASS", "LOW", "MISS"],
        as_of_end="2025-01-01",
        data_collector=object(),
    )

    assert report["passed_symbols"] == ["PASS"]
    assert report["excluded_symbols"] == ["LOW", "MISS"]
    assert report["symbols"]["LOW"]["reason"] == "below_min_avg_trading_value"
    assert report["symbols"]["MISS"]["reason"] == "missing_liquidity_data"
