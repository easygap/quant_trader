"""live 게이트 데이터 소스 신선도 회귀 테스트 (감사: runtime-live-readiness-datasource-check-fixed-window).

예전 게이트는 005930의 고정 구간(2026-01-01~03-26)만 받아 봐서, 과거 이력은 주지만
갱신이 멈춘 피드와 바스켓 자신의 종목(069500/357870) 문제를 잡지 못했다. 이제 보유
종목 전부 + 벤치마크의 최근 봉이 직전 거래일에서 1거래일 이내인지 본다.
네트워크는 호출하지 않는다(가짜 DataCollector). 거래일 달력은 테스트가 고정한다.
"""

from datetime import date, datetime

import pandas as pd
import pytest
from loguru import logger

from config.config_loader import Config
from core.live_readiness import check_data_source_freshness, check_live_readiness_gate

CHUSEOK_2026_WITH_0928 = {
    "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-28",
    "2026-10-03", "2026-10-05", "2026-10-09",
}
POCKET = {"kr_pocket": {"enabled": True, "holdings": {"069500": 0.5, "357870": 0.5}}}


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    monkeypatch.setattr("core.trading_hours._load_holidays", lambda: set(CHUSEOK_2026_WITH_0928))


@pytest.fixture
def pocket_config(monkeypatch):
    monkeypatch.setattr(
        "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
        staticmethod(lambda: POCKET),
    )


class FakeCollector:
    """종목별 마지막 봉 날짜를 지정할 수 있는 가짜 수집기."""

    def __init__(self, last_bar_by_symbol=None, default_last_bar=None, source="FinanceDataReader"):
        self.last_bar_by_symbol = last_bar_by_symbol or {}
        self.default_last_bar = default_last_bar
        self.source = source
        self.requested = []

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        self.requested.append((symbol, start_date, end_date))
        last = self.last_bar_by_symbol.get(symbol, self.default_last_bar)
        if last is None:
            return pd.DataFrame()
        index = pd.bdate_range(end=pd.Timestamp(last), periods=5)
        return pd.DataFrame({"close": [100.0] * len(index)}, index=index)

    def get_last_source_info(self):
        return {
            "source": self.source,
            "history": {symbol: self.source for symbol, _s, _e in self.requested},
        }


def test_stale_fixed_window_feed_is_rejected(pocket_config):
    """2026-03-26에서 멈춘 피드는 모든 종목·벤치마크에서 거부된다."""
    collector = FakeCollector(default_last_bar="2026-03-26")

    issues = check_data_source_freshness(
        Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 23), collector=collector,
    )

    assert [s for s, _a, _b in collector.requested] == ["069500", "357870", "KS11"]
    assert len(issues) == 3
    assert all("갱신 멈춤 의심" in i and "2026-03-26" in i for i in issues)


def test_fresh_feed_on_previous_trading_day_passes(pocket_config):
    collector = FakeCollector(default_last_bar="2026-09-22")
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        issues = check_data_source_freshness(
            Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 23), collector=collector,
        )
    finally:
        logger.remove(sink)

    assert issues == []
    # 최근 약 10일 구간만 요청한다(고정 과거 구간이 아님).
    assert all(start >= "2026-09-10" and end == "2026-09-23" for _s, start, end in collector.requested)
    # 실제 마지막 봉 날짜를 로그로 남긴다.
    assert any("069500" in m and "2026-09-22" in m for m in messages)


def test_one_trading_day_lag_is_tolerated_but_two_is_not(pocket_config):
    collector = FakeCollector(
        last_bar_by_symbol={"069500": "2026-09-21", "357870": "2026-09-18"},
        default_last_bar="2026-09-22",
    )

    issues = check_data_source_freshness(
        Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 23), collector=collector,
    )

    assert len(issues) == 1
    assert "357870" in issues[0] and "2026-09-18" in issues[0]


def test_after_chuseok_previous_trading_day_comes_from_calendar(pocket_config):
    """9/29 점검: 달력상 직전 거래일은 9/23(9/24~28 휴장) — 9/23 봉이면 통과."""
    collector = FakeCollector(default_last_bar="2026-09-23")

    issues = check_data_source_freshness(
        Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 29), collector=collector,
    )

    assert issues == []


def test_signal_strategy_checks_representative_symbol_and_benchmark():
    collector = FakeCollector(default_last_bar="2026-09-22")

    issues = check_data_source_freshness(
        Config.get(), "scoring", today=date(2026, 9, 23), collector=collector,
    )

    assert issues == []
    assert [s for s, _a, _b in collector.requested] == ["005930", "KS11"]


def test_kis_unadjusted_source_is_flagged(pocket_config):
    collector = FakeCollector(default_last_bar="2026-09-22", source="KIS")

    issues = check_data_source_freshness(
        Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 23), collector=collector,
    )

    assert len(issues) == 1 and "KIS(비수정주가)" in issues[0]


def test_empty_feed_fails_closed(pocket_config):
    issues = check_data_source_freshness(
        Config.get(), "basket_rebalance:kr_pocket", today=date(2026, 9, 23),
        collector=FakeCollector(default_last_bar=None),
    )
    assert len(issues) == 3 and all("최근 데이터 없음" in i for i in issues)


def test_gate_wires_freshness_check_with_basket_holdings(pocket_config, monkeypatch):
    """배선: 바스켓 게이트 통과 후 게이트가 실제로 보유 종목 신선도 점검을 돈다."""
    today = datetime.now().date()
    created = []

    class _Collector(FakeCollector):
        def __init__(self):
            super().__init__(default_last_bar=today.isoformat())
            created.append(self)

    monkeypatch.setattr("core.live_readiness.check_basket_live_readiness", lambda cfg, name: [])
    monkeypatch.setattr("core.data_collector.DataCollector", _Collector)

    issues = check_live_readiness_gate(Config.get(), "basket_rebalance:kr_pocket")

    assert issues == []
    assert [s for s, _a, _b in created[0].requested] == ["069500", "357870", "KS11"]
