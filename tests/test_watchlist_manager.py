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

    sample = pd.DataFrame(
        {
            "Code": ["000001", "000002", "000003"],
            "Market": ["KOSPI", "KOSPI", "KOSDAQ"],
            "Marcap": [100, 300, 500],
        }
    )

    monkeypatch.setattr("core.data_collector.DataCollector.get_krx_stock_list", staticmethod(lambda: sample))
    symbols = WatchlistManager(_Config()).resolve()
    assert symbols == ["000002", "000001"]
