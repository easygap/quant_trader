"""
실적 발표일(어닝) 필터.
- 실적 발표일 전후 N일 동안 해당 종목의 신규 매수를 금지한다.
- 데이터 소스: 1) yfinance earningsDate 2) 실패·None 시 DART Open API(정기공시 접수일 기반 추정).
  둘 다 없으면 필터 통과(매수 허용). DART는 settings.dart.enabled 및 API 키 필요.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from config.config_loader import Config

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


def _get_next_earnings_date_yfinance(symbol: str) -> Optional[datetime]:
    """yfinance 캘린더에서 가장 가까운 실적일(최근 30일 이내 과거 포함)."""
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
        logger.debug("yfinance 실적일 조회 실패 {}: {}", symbol, e)
        return None


def _get_next_earnings_date_dart(symbol: str, config: Optional["Config"] = None) -> Optional[datetime]:
    """DART 정기공시 접수 이력으로 차기 분기 공시 시점 추정."""
    try:
        if config is None:
            from config.config_loader import Config
            config = Config.get()
    except Exception:
        return None

    dart_cfg = config.dart or {}
    if not dart_cfg.get("enabled", False):
        return None
    api_key = (dart_cfg.get("api_key") or "").strip()
    if not api_key:
        return None

    try:
        from core.dart_loader import DartEarningsLoader
        loader = DartEarningsLoader(api_key)
        corp = loader.get_corp_code(symbol)
        if not corp:
            logger.debug("DART 고유번호 없음: {}", symbol)
            return None
        return loader.get_next_earnings_date(corp)
    except Exception as e:
        logger.debug("DART 실적일 추정 실패 {}: {}", symbol, e)
        return None


def get_next_earnings_date(
    symbol: str,
    config: Optional["Config"] = None,
) -> Optional[datetime]:
    """
    종목의 다음 실적(공시) 기준일 조회.
    yfinance 우선, 실패·None이면 DART 추정.
    """
    dt = _get_next_earnings_date_yfinance(symbol)
    if dt is not None:
        return dt
    return _get_next_earnings_date_dart(symbol, config=config)


def is_near_earnings(
    symbol: str,
    skip_days: int = 3,
    reference_date: Optional[datetime] = None,
    config: Optional["Config"] = None,
) -> tuple[bool, str]:
    """
    실적 발표일 전후 skip_days 이내인지 판별.

    Returns:
        (True이면 진입 금지, 사유 문자열)
        조회 실패·날짜 없음 시 (False, "") → 매수 허용
    """
    if skip_days <= 0:
        return False, ""

    earnings_dt = get_next_earnings_date(symbol, config=config)
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
