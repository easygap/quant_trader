import types

import pandas as pd


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
