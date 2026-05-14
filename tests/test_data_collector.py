import types

import pandas as pd
import pytest


def _price_frame():
    return pd.DataFrame(
        {
            "open": [1000.0],
            "high": [1010.0],
            "low": [990.0],
            "close": [1005.0],
            "volume": [10000],
        },
        index=pd.to_datetime(["2026-01-02"]),
    )


def test_kis_fallback_is_disabled_when_data_source_setting_is_missing(monkeypatch):
    import core.data_collector as data_collector

    monkeypatch.setattr(data_collector, "HAS_FDR", False)
    monkeypatch.setattr(data_collector, "HAS_YF", False)

    collector = data_collector.DataCollector(types.SimpleNamespace(settings={}))
    monkeypatch.setattr(
        collector,
        "fetch_korean_stock_via_kis",
        lambda _symbol: pytest.fail("KIS fallback must require explicit opt-in"),
    )

    with pytest.raises(data_collector.DataCollectionError, match="KIS fallback 비활성화"):
        collector._fetch_korean_stock_uncached("005930", "2026-01-01", "2026-01-03")


def test_kis_fallback_runs_only_when_explicitly_enabled(monkeypatch):
    import core.data_collector as data_collector

    monkeypatch.setattr(data_collector, "HAS_FDR", False)
    monkeypatch.setattr(data_collector, "HAS_YF", False)

    collector = data_collector.DataCollector(
        types.SimpleNamespace(settings={"data_source": {"allow_kis_fallback": True}})
    )
    expected = _price_frame()
    monkeypatch.setattr(collector, "fetch_korean_stock_via_kis", lambda _symbol: expected)

    result = collector._fetch_korean_stock_uncached("005930", "2026-01-01", "2026-01-03")

    assert result is expected
    assert collector.has_kis_fallback_symbols() == ["005930"]


def test_get_sector_map_uses_fdr_sector_column(monkeypatch):
    import core.data_collector as data_collector

    fake_fdr = types.SimpleNamespace(
        StockListing=lambda market: pd.DataFrame(
            {
                "Code": ["5930", "000660"],
                "Sector": ["반도체", "반도체"],
            }
        )
    )

    monkeypatch.setattr(data_collector, "HAS_FDR", True)
    monkeypatch.setattr(data_collector, "fdr", fake_fdr)
    monkeypatch.setattr(data_collector, "HAS_PYKRX", False)
    monkeypatch.setattr(data_collector, "_pykrx_stock", None)

    mapping = data_collector.DataCollector.get_sector_map()

    assert mapping == {"005930": "반도체", "000660": "반도체"}


def test_get_sector_map_falls_back_to_pykrx_when_fdr_has_no_sector(monkeypatch):
    import core.data_collector as data_collector

    fake_fdr = types.SimpleNamespace(
        StockListing=lambda market: pd.DataFrame(
            {
                "Code": ["005930", "035720"],
                "Name": ["삼성전자", "카카오"],
            }
        )
    )

    class FakePykrxStock:
        @staticmethod
        def get_market_sector_classifications(date, market):
            if market == "KOSPI":
                return pd.DataFrame(
                    {
                        "티커": ["005930"],
                        "업종명": ["전기전자"],
                    }
                )
            return pd.DataFrame(
                {
                    "종목코드": ["035720"],
                    "산업명": ["서비스업"],
                }
            )

    monkeypatch.setattr(data_collector, "HAS_FDR", True)
    monkeypatch.setattr(data_collector, "fdr", fake_fdr)
    monkeypatch.setattr(data_collector.DataCollector, "_get_sector_map_from_kind", staticmethod(lambda: {}))
    monkeypatch.setattr(data_collector, "HAS_PYKRX", True)
    monkeypatch.setattr(data_collector, "_pykrx_stock", FakePykrxStock)

    mapping = data_collector.DataCollector.get_sector_map()

    assert mapping == {"005930": "전기전자", "035720": "서비스업"}


def test_get_sector_map_uses_kind_listing_when_fdr_has_no_sector(monkeypatch):
    import sys

    import core.data_collector as data_collector

    fake_fdr = types.SimpleNamespace(
        StockListing=lambda market: pd.DataFrame(
            {
                "Code": ["005930"],
                "Name": ["삼성전자"],
            }
        )
    )

    class FakeResponse:
        text = """
        <table>
          <tr><th>회사명</th><th>종목코드</th><th>업종</th></tr>
          <tr><td>삼성전자</td><td>005930</td><td>통신 및 방송 장비 제조업</td></tr>
          <tr><td>카카오</td><td>035720</td><td>소프트웨어 개발 및 공급업</td></tr>
        </table>
        """
        encoding = "utf-8"

        @staticmethod
        def raise_for_status():
            return None

    fake_requests = types.SimpleNamespace(get=lambda url, timeout: FakeResponse())

    monkeypatch.setattr(data_collector, "HAS_FDR", True)
    monkeypatch.setattr(data_collector, "fdr", fake_fdr)
    monkeypatch.setattr(data_collector, "HAS_PYKRX", False)
    monkeypatch.setattr(data_collector, "_pykrx_stock", None)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    mapping = data_collector.DataCollector.get_sector_map()

    assert mapping == {
        "005930": "통신 및 방송 장비 제조업",
        "035720": "소프트웨어 개발 및 공급업",
    }
