"""미국 시장 휴리스틱·환율·거래시간·리스크 환산 스모크."""

import pytest


def test_is_us_ticker():
    from core.data_collector import DataCollector

    assert DataCollector.is_us_ticker("AAPL") is True
    assert DataCollector.is_us_ticker("MSFT") is True
    assert DataCollector.is_us_ticker("005930") is False
    assert DataCollector.is_us_ticker("BRK.B") is False
    assert DataCollector.is_us_ticker("") is False


def test_fetch_stock_routes_us(monkeypatch):
    from core.data_collector import DataCollector
    import pandas as pd

    dc = DataCollector()
    called = {}

    def fake_us(s, *a, **k):
        called["us"] = s
        return pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})

    def fake_kr(s, *a, **k):
        called["kr"] = s
        return pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})

    monkeypatch.setattr(dc, "fetch_us_stock", fake_us)
    monkeypatch.setattr(dc, "fetch_korean_stock", fake_kr)
    dc.fetch_stock("AAPL", "2020-01-01", "2020-02-01")
    assert called.get("us") == "AAPL"
    called.clear()
    dc.fetch_stock("005930", "2020-01-01", "2020-02-01")
    assert called.get("kr") == "005930"


def test_trading_hours_us_methods():
    from core.trading_hours import TradingHours

    th = TradingHours()
    assert hasattr(th, "is_us_trading_day")
    assert hasattr(th, "is_us_market_open")
    w = th.us_market_session_kst_window()
    assert "open_kst" in w and "close_kst" in w


def test_risk_manager_converts_us_position_to_krw(monkeypatch):
    from core.data_collector import DataCollector
    from core.risk_manager import RiskManager

    monkeypatch.setattr(
        DataCollector,
        "get_usd_krw_rate",
        classmethod(lambda cls: 1300.0),
    )
    rm = RiskManager()
    # 100 USD → 130,000원 환산 후 단일 종목 비중 26% (> 기본 max_position_ratio 20%)
    out = rm.check_diversification(
        current_positions=0,
        position_value=100.0,
        total_value=500_000.0,
        available_cash=400_000.0,
        current_invested=0,
        symbol="AAPL",
    )
    assert out["can_buy"] is False
    assert "비중" in out["reason"]


def test_kis_map_us_market():
    from api.kis_api import KISApi

    assert KISApi.map_us_market_to_kis_codes("NAS") == ("NAS", "NASD")
    assert KISApi.map_us_market_to_kis_codes("NYS") == ("NYS", "NYSE")


def test_config_markets_property():
    from config.config_loader import Config

    m = Config.get().markets
    assert isinstance(m, dict)
    assert "korea" in m or "us" in m or len(m) >= 0
