"""
실적 발표일(어닝) 필터.
- 실적 발표일 전후 N일 동안 해당 종목의 신규 매수를 금지한다.
- 데이터 소스: yfinance earningsDate (Yahoo Finance 기반, 한국 종목은 누락 가능).
  yfinance 미설치·조회 실패·날짜 미제공 시 필터를 통과(매수 허용)시킨다.
  향후 pykrx / KRX OPEN API(공시) 연동으로 정확도 향상 가능.
"""

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


def _korean_ticker(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith(".KS") or s.endswith(".KQ"):
        return s
    return f"{s}.KS"


def get_next_earnings_date(symbol: str) -> Optional[datetime]:
    """
    종목의 다음 실적 발표 예정일을 조회한다.
    yfinance의 earningsDate(리스트)에서 현재 이후 가장 가까운 날짜를 반환.
    조회 실패·미제공 시 None.
    """
    if not HAS_YF:
        return None
    ticker_str = _korean_ticker(symbol)
    if not ticker_str:
        return None
    try:
        t = yf.Ticker(ticker_str)
        cal = t.calendar
        if cal is None:
            return None
        # yfinance >= 0.2: calendar는 dict. earningsDate는 list[Timestamp] 또는 단일 Timestamp
        dates = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("earningsDate")
        elif hasattr(cal, "get"):
            dates = cal.get("Earnings Date")

        if dates is None:
            return None

        now = datetime.now()
        if not isinstance(dates, (list, tuple)):
            dates = [dates]

        candidates = []
        for d in dates:
            try:
                dt = d.to_pydatetime() if hasattr(d, "to_pydatetime") else datetime.fromisoformat(str(d)[:10])
                candidates.append(dt)
            except Exception:
                continue

        future = [d for d in candidates if d >= now - timedelta(days=30)]
        if not future:
            return None
        return min(future)
    except Exception as e:
        logger.debug("실적 발표일 조회 실패 {}: {}", symbol, e)
        return None


def is_near_earnings(
    symbol: str,
    skip_days: int = 3,
    reference_date: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    실적 발표일 전후 skip_days 이내인지 판별.

    Returns:
        (True이면 진입 금지, 사유 문자열)
        조회 실패·날짜 없음 시 (False, "") → 매수 허용
    """
    if skip_days <= 0:
        return False, ""

    earnings_dt = get_next_earnings_date(symbol)
    if earnings_dt is None:
        return False, ""

    ref = reference_date or datetime.now()
    diff = (earnings_dt.date() - ref.date()).days

    if -skip_days <= diff <= skip_days:
        direction = "후" if diff < 0 else ("당일" if diff == 0 else "전")
        logger.info(
            "실적 필터: {} — 실적 발표일 {} ({}일 {}), 신규 매수 금지 구간(±{}일)",
            symbol, earnings_dt.strftime("%Y-%m-%d"),
            abs(diff), direction, skip_days,
        )
        return True, f"실적 발표일 {earnings_dt.strftime('%Y-%m-%d')} 전후 {skip_days}일 이내"

    return False, ""
