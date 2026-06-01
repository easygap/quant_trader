"""tools/pilot_calendar.py — KRX 거래일·날짜 헬퍼 단위 테스트.

모놀리스(target_weight_rotation_pilot.py)에서 분리한 순수 함수들. 분리 전에는 전용
테스트가 없었으므로 이번에 커버리지를 추가한다. 휴장일은 주입해 결정론적으로 검증.
"""
from datetime import datetime

import pytest

from tools import pilot_calendar as pc


class TestSplitSymbols:
    def test_none_returns_none(self):
        assert pc.split_symbols(None) is None

    def test_comma_and_newline(self):
        assert pc.split_symbols("005930, 000660\n035420") == ["005930", "000660", "035420"]

    def test_empty_filtered(self):
        assert pc.split_symbols("005930,,  ,000660") == ["005930", "000660"]


class TestDateRange:
    def test_weekdays_only(self):
        # 2024-01-01(월)~01-07(일): 평일 5개(1,2,3,4,5)
        out = pc.date_range("2024-01-01", "2024-01-07")
        assert out == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError):
            pc.date_range("2024-01-10", "2024-01-01")

    def test_only_weekend_raises(self):
        # 2024-01-06(토)~01-07(일): 평일 없음
        with pytest.raises(ValueError):
            pc.date_range("2024-01-06", "2024-01-07")


class TestBusinessDay:
    def test_weekday_no_holiday(self):
        from datetime import date
        assert pc.is_kr_market_business_day(date(2024, 1, 2), set()) is True

    def test_weekend_is_not_business_day(self):
        from datetime import date
        assert pc.is_kr_market_business_day(date(2024, 1, 6), set()) is False  # 토

    def test_holiday_is_not_business_day(self):
        from datetime import date
        assert pc.is_kr_market_business_day(date(2024, 1, 2), {"2024-01-02"}) is False


class TestPilotValidTo:
    def test_counts_business_days(self, monkeypatch):
        # 휴장일 없음 가정 → 2024-01-02(화)부터 5영업일째
        monkeypatch.setattr(pc, "load_kr_market_holidays", lambda: set())
        # 1/2(1), 1/3(2), 1/4(3), 1/5(4), 주말, 1/8(5)
        assert pc.pilot_valid_to("2024-01-02", 5) == "2024-01-08"

    def test_skips_holiday(self, monkeypatch):
        monkeypatch.setattr(pc, "load_kr_market_holidays", lambda: {"2024-01-03"})
        # 1/2(1), 1/3 휴장, 1/4(2), 1/5(3), 주말, 1/8(4), 1/9(5)
        assert pc.pilot_valid_to("2024-01-02", 5) == "2024-01-09"

    def test_non_positive_raises(self):
        with pytest.raises(ValueError):
            pc.pilot_valid_to("2024-01-02", 0)


class TestNextBusinessDay:
    def test_skips_weekend(self, monkeypatch):
        monkeypatch.setattr(pc, "load_kr_market_holidays", lambda: set())
        # 금(1/5) 다음 영업일 = 월(1/8)
        assert pc.next_kr_market_business_day("2024-01-05") == "2024-01-08"

    def test_skips_holiday(self, monkeypatch):
        monkeypatch.setattr(pc, "load_kr_market_holidays", lambda: {"2024-01-03"})
        # 화(1/2) 다음 = 수(1/3) 휴장 → 목(1/4)
        assert pc.next_kr_market_business_day("2024-01-02") == "2024-01-04"


class TestKstHelpers:
    def test_coerce_naive_kept(self):
        dt = datetime(2024, 1, 2, 10, 0, 0)
        assert pc.coerce_kst_datetime(dt) == dt

    def test_coerce_aware_converted_to_kst_naive(self):
        from datetime import timezone as tz, timedelta
        utc_dt = datetime(2024, 1, 2, 1, 0, 0, tzinfo=tz.utc)  # UTC 01:00 = KST 10:00
        out = pc.coerce_kst_datetime(utc_dt)
        assert out.tzinfo is None
        assert out.hour == 10

    def test_execution_day_format(self):
        assert pc.execution_day(datetime(2024, 3, 5, 15, 30)) == "2024-03-05"


def test_backcompat_aliases_exist():
    """모놀리스가 쓰던 _접두어 별칭이 그대로 노출되는지."""
    for name in ("_split_symbols", "_date_range", "_load_kr_market_holidays",
                 "_is_kr_market_business_day", "_pilot_valid_to",
                 "_next_kr_market_business_day", "_coerce_kst_datetime", "_execution_day"):
        assert hasattr(pc, name)
