"""
실적 발표일(어닝) 필터.
- 실적 발표일 전후 N일 동안 해당 종목의 신규 매수를 금지한다.
- 데이터 소스: 1) yfinance earningsDate 2) 실패·None 시 DART Open API(정기공시 접수일 기반 추정).
  둘 다 없으면 기본 fail-closed로 신규 매수를 차단한다.
  settings.trading.earnings_filter_unknown_policy=allow인 경우에만 조회 불가를 통과시킨다.
"""

from dataclasses import dataclass
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


@dataclass(frozen=True)
class EarningsLookupResult:
    date: Optional[datetime] = None
    status: str = "unknown"  # "found" | "unknown"
    source: str = ""
    reason: str = ""


def _korean_ticker(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith(".KS") or s.endswith(".KQ"):
        return s
    return f"{s}.KS"


def _get_next_earnings_date_yfinance(symbol: str) -> Optional[datetime]:
    """yfinance 캘린더에서 가장 가까운 실적일(최근 30일 이내 과거 포함)."""
    return _lookup_next_earnings_date_yfinance(symbol).date


def _lookup_next_earnings_date_yfinance(symbol: str) -> EarningsLookupResult:
    """yfinance 캘린더에서 가장 가까운 실적일과 조회 상태를 반환."""
    if not HAS_YF:
        return EarningsLookupResult(source="yfinance", reason="yfinance unavailable")
    ticker_str = _korean_ticker(symbol)
    if not ticker_str:
        return EarningsLookupResult(source="yfinance", reason="symbol missing")
    try:
        t = yf.Ticker(ticker_str)
        cal = t.calendar
        if cal is None:
            return EarningsLookupResult(source="yfinance", reason="calendar missing")
        dates = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("earningsDate")
        elif hasattr(cal, "get"):
            dates = cal.get("Earnings Date")

        if dates is None:
            return EarningsLookupResult(source="yfinance", reason="earnings date missing")

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
            return EarningsLookupResult(source="yfinance", reason="no recent or future earnings date")
        return EarningsLookupResult(date=min(future), status="found", source="yfinance")
    except Exception as e:
        logger.debug("yfinance 실적일 조회 실패 {}: {}", symbol, e)
        return EarningsLookupResult(source="yfinance", reason=f"lookup failed: {e}")


def _get_next_earnings_date_dart(symbol: str, config: Optional["Config"] = None) -> Optional[datetime]:
    """DART 정기공시 접수 이력으로 차기 분기 공시 시점 추정."""
    return _lookup_next_earnings_date_dart(symbol, config=config).date


def _lookup_next_earnings_date_dart(
    symbol: str,
    config: Optional["Config"] = None,
) -> EarningsLookupResult:
    """DART 정기공시 접수 이력으로 차기 분기 공시 시점과 조회 상태를 반환."""
    try:
        if config is None:
            from config.config_loader import Config
            config = Config.get()
    except Exception as e:
        return EarningsLookupResult(source="dart", reason=f"config unavailable: {e}")

    dart_cfg = config.dart or {}
    if not dart_cfg.get("enabled", False):
        return EarningsLookupResult(source="dart", reason="dart disabled")
    api_key = (dart_cfg.get("api_key") or "").strip()
    if not api_key:
        return EarningsLookupResult(source="dart", reason="dart api key missing")

    try:
        from core.dart_loader import DartEarningsLoader
        loader = DartEarningsLoader(api_key)
        corp = loader.get_corp_code(symbol)
        if not corp:
            logger.debug("DART 고유번호 없음: {}", symbol)
            return EarningsLookupResult(source="dart", reason="corp code missing")
        dt = loader.get_next_earnings_date(corp)
        if dt is None:
            return EarningsLookupResult(source="dart", reason="earnings date missing")
        return EarningsLookupResult(date=dt, status="found", source="dart")
    except Exception as e:
        logger.debug("DART 실적일 추정 실패 {}: {}", symbol, e)
        return EarningsLookupResult(source="dart", reason=f"lookup failed: {e}")


def get_next_earnings_date(
    symbol: str,
    config: Optional["Config"] = None,
) -> Optional[datetime]:
    """
    종목의 다음 실적(공시) 기준일 조회.
    yfinance 우선, 실패·None이면 DART 추정.
    """
    return lookup_next_earnings_date(symbol, config=config).date


def lookup_next_earnings_date(
    symbol: str,
    config: Optional["Config"] = None,
) -> EarningsLookupResult:
    """
    종목의 다음 실적(공시) 기준일과 조회 상태를 반환.
    yfinance 우선, 실패·None이면 DART 추정.
    """
    yf_result = _lookup_next_earnings_date_yfinance(symbol)
    if yf_result.status == "found":
        return yf_result

    dart_result = _lookup_next_earnings_date_dart(symbol, config=config)
    if dart_result.status == "found":
        return dart_result

    reasons = "; ".join(
        r for r in (yf_result.reason, dart_result.reason) if r
    )
    sources = ",".join(
        s for s in (yf_result.source, dart_result.source) if s
    )
    return EarningsLookupResult(source=sources, reason=reasons or "earnings date unknown")


def _unknown_policy(config: Optional["Config"]) -> str:
    trading = {}
    if config is not None:
        trading = getattr(config, "trading", {}) or {}
    policy = str(trading.get("earnings_filter_unknown_policy", "block")).strip().lower()
    if policy in {"allow", "pass", "ignore"}:
        return "allow"
    return "block"


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
        조회 실패·날짜 없음 시 기본 (True, 사유) → 매수 차단
        settings.trading.earnings_filter_unknown_policy=allow일 때만 통과
    """
    if skip_days <= 0:
        return False, ""

    lookup = lookup_next_earnings_date(symbol, config=config)
    earnings_dt = lookup.date
    if earnings_dt is None:
        reason = f"실적일 조회 불가: {lookup.reason or 'unknown'}"
        if _unknown_policy(config) == "allow":
            logger.warning("실적 필터 조회 불가이나 설정상 통과: {} — {}", symbol, reason)
            return False, ""
        logger.warning("실적 필터: {} — {}, 신규 매수 차단", symbol, reason)
        return True, reason

    if lookup.status != "found":
        reason = f"실적일 조회 상태 불명확: {lookup.reason or 'unknown'}"
        if _unknown_policy(config) == "allow":
            logger.warning("실적 필터 상태 불명확이나 설정상 통과: {} — {}", symbol, reason)
            return False, ""
        logger.warning("실적 필터: {} — {}, 신규 매수 차단", symbol, reason)
        return True, reason

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
