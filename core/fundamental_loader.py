"""
펀더멘털(재무) 지표 조회 모듈.
- PER, 부채비율 등 기본 재무 지표를 정상 범위 필터에 사용
- yfinance 기반 (한국 종목: 005930 → 005930.KS)
"""

from typing import Optional

from loguru import logger

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


def _korean_ticker(symbol: str) -> str:
    """한국 종목코드 → yfinance 티커 (005930 → 005930.KS)."""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s == "KS11" or s.endswith(".KS") or s.endswith(".KQ"):
        return s if "." in s else f"{s}.KS"
    return f"{s}.KS"


def get_fundamentals(symbol: str) -> dict:
    """
    종목의 기본 펀더멘털 지표 조회.

    Args:
        symbol: 종목코드 (예: "005930")

    Returns:
        {
            "per": float | None,        # PER (trailing). None = 미제공/적자
            "debt_ratio": float | None, # 부채비율(%). 총부채/자본총계*100 또는 debtToEquity*100
            "available": bool,           # 필수 항목 조회 성공 여부
        }
    """
    result = {"per": None, "debt_ratio": None, "available": False}
    if not HAS_YF:
        logger.debug("yfinance 미설치 — 펀더멘털 조회 스킵")
        return result

    ticker_str = _korean_ticker(symbol)
    if not ticker_str:
        return result

    try:
        t = yf.Ticker(ticker_str)
        info = t.info or {}
        # PER: trailingPE. 음수(적자)면 None으로 처리해 필터에서 제외 가능
        per = info.get("trailingPE")
        if per is not None:
            try:
                per = float(per)
                if per < 0:
                    per = None
            except (TypeError, ValueError):
                per = None
        result["per"] = per

        # 부채비율: Yahoo는 debtToEquity (부채/자본 비율, 소수). 0.5 = 50%
        # 한국식 부채비율(%) = (총부채/자본총계)*100 → debtToEquity * 100
        debt_eq = info.get("debtToEquity")
        if debt_eq is not None:
            try:
                debt_eq = float(debt_eq)
                result["debt_ratio"] = debt_eq * 100.0
            except (TypeError, ValueError):
                pass
        # 일부 종목은 totalDebt, totalStockholderEquity만 제공
        if result["debt_ratio"] is None:
            td = info.get("totalDebt")
            te = info.get("totalStockholderEquity")
            if td is not None and te is not None and float(te) != 0:
                try:
                    result["debt_ratio"] = float(td) / float(te) * 100.0
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

        result["available"] = result["per"] is not None or result["debt_ratio"] is not None
    except Exception as e:
        logger.debug("펀더멘털 조회 실패 {}: {}", symbol, e)

    return result


def check_fundamental_filter(
    symbol: str,
    per_min: Optional[float] = 0,
    per_max: Optional[float] = 50,
    debt_ratio_max: Optional[float] = 200,
) -> tuple[bool, str]:
    """
    PER·부채비율이 정상 범위인지 확인.

    Args:
        symbol: 종목코드
        per_min: PER 하한 (None이면 검사 생략). 0이면 적자(PER 음수) 제외
        per_max: PER 상한 (None이면 검사 생략)
        debt_ratio_max: 부채비율(%) 상한 (None이면 검사 생략)

    Returns:
        (통과 여부, 사유 문자열)
    """
    data = get_fundamentals(symbol)
    reasons = []

    if per_min is not None or per_max is not None:
        per = data.get("per")
        if per is None:
            if data.get("available") is False:
                # 조회 실패 시 보수적으로 필터 통과로 할지 불통과로 할지: 설정에 따라 다름.
                # 여기서는 "데이터 없음 = 필터 스킵(통과)"로 두고, 상위에서 strict 모드 시 불통과 처리 가능.
                reasons.append("PER 미제공")
            else:
                reasons.append("PER 적자/미제공")
        else:
            if per_min is not None and per < per_min:
                reasons.append(f"PER {per:.1f} < {per_min}")
            if per_max is not None and per > per_max:
                reasons.append(f"PER {per:.1f} > {per_max}")

    if debt_ratio_max is not None:
        dr = data.get("debt_ratio")
        if dr is None:
            reasons.append("부채비율 미제공")
        elif dr > debt_ratio_max:
            reasons.append(f"부채비율 {dr:.1f}% > {debt_ratio_max}%")

    if reasons:
        return False, "; ".join(reasons)
    return True, "OK"
