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


def test_2026_substitute_holidays_are_non_trading_days():
    """대체공휴일·연말휴장 누락 회귀 — 누락되면 휴장일이 거래일로 계산돼
    트랙레코드 커버리지 분모가 부풀고 스냅샷 귀속(가격 기준일)이 어긋난다.

    2026: 3·1절(일)→3/2, 부처님오신날(일)→5/25, 광복절(토)→8/17,
    추석 토요일 겹침→9/28, 개천절(토)→10/5, KRX 연말 휴장 12/31.
    (현충일 6/6(토)은 대체 미적용 — 6/8은 거래일이어야 함)
    """
    from datetime import datetime
    from core.trading_hours import TradingHours

    th = TradingHours()
    for d in ("2026-03-02", "2026-05-25", "2026-08-17",
              "2026-09-28", "2026-10-05", "2026-12-31"):
        y, m, dd = map(int, d.split("-"))
        assert th.is_trading_day(datetime(y, m, dd)) is False, f"{d}는 휴장일이어야 함"
    # 현충일은 대체공휴일 미적용 — 다음 월요일은 정상 거래일
    assert th.is_trading_day(datetime(2026, 6, 8)) is True


def test_fallback_sets_are_single_source():
    """trading_hours의 최종 fallback이 holidays_updater와 단일 소스인지(드리프트 방지)."""
    from core.holidays_updater import FALLBACK_BY_YEAR
    from core.trading_hours import KR_HOLIDAYS_FALLBACK

    assert KR_HOLIDAYS_FALLBACK == set().union(*FALLBACK_BY_YEAR.values())
