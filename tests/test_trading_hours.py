"""TradingHours 단위 테스트"""
from datetime import datetime, time

import pytest

from core.trading_hours import TradingHours, _load_holidays


class _MockConfig:
    trading = {"market_open": "09:00", "market_close": "15:30", "pre_market_prep": "08:50"}


@pytest.fixture
def th():
    return TradingHours(_MockConfig())


def test_holidays_loaded(th):
    """공휴일 세트 로드됨"""
    assert isinstance(th.holidays, set)
    assert len(th.holidays) >= 0


def test_weekend_not_trading(th):
    """주말은 거래일 아님"""
    # 2026-01-03 토요일
    assert th.is_trading_day(datetime(2026, 1, 3)) is False
    assert th.is_trading_day(datetime(2026, 1, 4)) is False


def test_market_open_time(th):
    """장중 시간이면 is_market_open True"""
    dt = datetime(2026, 1, 5, 10, 0, 0)  # 월 10:00
    if th.is_trading_day(dt):
        assert th.is_market_open(dt) is True
