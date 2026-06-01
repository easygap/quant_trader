"""target-weight pilot의 KRX 거래일·날짜 계산 헬퍼.

target_weight_rotation_pilot.py(13,500줄 모놀리스)에서 순수 날짜/달력 로직만 분리한
모듈이다. 외부 상태에 의존하지 않고(휴장일은 core.trading_hours에서 lazy load) stdlib만
쓰므로 단독 테스트가 쉽다. 기존 동작과 100% 동일하며, 모놀리스는 이 모듈을 re-import해
하위 호환을 유지한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from loguru import logger

# 한국 표준시(KST). pilot 실행일·세션ID 계산 기준.
KST = timezone(timedelta(hours=9))

# pilot 윈도우 기본 길이(영업일). _pilot_valid_to 기본값.
TARGET_WEIGHT_PILOT_TARGET_DAYS = 60


def split_symbols(raw: str | None) -> list[str] | None:
    """쉼표/개행 구분 종목 문자열을 리스트로. None이면 None."""
    if raw is None:
        return None
    symbols = [part.strip() for part in raw.replace("\n", ",").split(",")]
    return [symbol for symbol in symbols if symbol]


def date_range(start_date: str, end_date: str) -> list[str]:
    """start~end(포함) 사이의 평일(월~금) 날짜 문자열 리스트."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("shadow end date must be on or after start date")

    dates: list[str] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    if not dates:
        raise ValueError("shadow date range contains no weekdays")
    return dates


def load_kr_market_holidays() -> set[str]:
    """KRX 휴장일 집합(ISO 날짜 문자열). 조회 실패 시 빈 집합."""
    try:
        from core.trading_hours import _load_holidays

        return {str(day).strip() for day in _load_holidays() if str(day).strip()}
    except Exception as exc:
        logger.debug("KRX holiday lookup skipped for pilot window calculation: {}", exc)
        return set()


def is_kr_market_business_day(day: date, holidays: set[str]) -> bool:
    """평일이면서 휴장일이 아니면 True."""
    return day.weekday() < 5 and day.isoformat() not in holidays


def pilot_valid_to(
    valid_from: str,
    target_pilot_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
) -> str:
    """valid_from부터 target_pilot_days 영업일째 되는 날(포함)을 ISO로 반환."""
    if target_pilot_days <= 0:
        raise ValueError("target_pilot_days must be positive")

    current = datetime.strptime(valid_from, "%Y-%m-%d").date()
    holidays = load_kr_market_holidays()
    counted_days = 0
    while True:
        if is_kr_market_business_day(current, holidays):
            counted_days += 1
            if counted_days >= target_pilot_days:
                return current.isoformat()
        current += timedelta(days=1)


def next_kr_market_business_day(day: str) -> str:
    """주어진 날의 다음 KRX 영업일을 ISO로 반환."""
    current = datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)
    holidays = load_kr_market_holidays()
    while not is_kr_market_business_day(current, holidays):
        current += timedelta(days=1)
    return current.isoformat()


def coerce_kst_datetime(now: datetime | None = None) -> datetime:
    """now를 KST naive datetime으로 정규화(미지정 시 현재 KST)."""
    current = now or datetime.now(KST)
    if current.tzinfo is not None:
        current = current.astimezone(KST).replace(tzinfo=None)
    return current


def execution_day(now: datetime | None = None) -> str:
    """KST 기준 실행일(YYYY-MM-DD)."""
    current = coerce_kst_datetime(now)
    return current.date().strftime("%Y-%m-%d")


# ── 하위 호환: 모놀리스가 쓰던 _접두어 이름을 별칭으로 노출 ──
_split_symbols = split_symbols
_date_range = date_range
_load_kr_market_holidays = load_kr_market_holidays
_is_kr_market_business_day = is_kr_market_business_day
_pilot_valid_to = pilot_valid_to
_next_kr_market_business_day = next_kr_market_business_day
_coerce_kst_datetime = coerce_kst_datetime
_execution_day = execution_day
