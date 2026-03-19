"""
펀더멘털(재무) 지표 조회 모듈.
- PER, 부채비율 등 기본 재무 지표를 정상 범위 필터에 사용
- 한국 종목: pykrx(우선) → yfinance(폴백) 순서로 조회.
  yfinance는 한국 종목 재무 데이터 업데이트가 느리고 누락이 많아 pykrx를 우선 사용.
"""

from typing import Optional

from loguru import logger

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

try:
    from pykrx import stock as pykrx_stock
    HAS_PYKRX = True
except ImportError:
    HAS_PYKRX = False


def _korean_ticker(symbol: str) -> str:
    """한국 종목코드 → yfinance 티커 (005930 → 005930.KS)."""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if s == "KS11" or s.endswith(".KS") or s.endswith(".KQ"):
        return s if "." in s else f"{s}.KS"
    return f"{s}.KS"


def _get_fundamentals_pykrx(symbol: str) -> dict:
    """pykrx로 한국 종목 PER·부채비율 조회. 설치되지 않았거나 실패하면 빈 결과."""
    result = {"per": None, "debt_ratio": None, "available": False, "source": "pykrx"}
    if not HAS_PYKRX:
        return result

    s = (symbol or "").strip().replace(".KS", "").replace(".KQ", "")
    if not s or not s.isdigit():
        return result

    try:
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

        # PER: pykrx의 일별 PER 데이터 (가장 최근 거래일)
        import pandas as pd
        per_df = pykrx_stock.get_market_fundamental(start, today, s)
        if per_df is not None and not per_df.empty and "PER" in per_df.columns:
            per_val = per_df["PER"].dropna()
            if not per_val.empty:
                per = float(per_val.iloc[-1])
                result["per"] = per if per > 0 else None

        result["available"] = result["per"] is not None
        if result["available"]:
            logger.debug("pykrx 펀더멘털 조회 성공: {} PER={}", symbol, result["per"])
    except Exception as e:
        logger.debug("pykrx 펀더멘털 조회 실패 {}: {}", symbol, e)

    return result


def _get_fundamentals_yfinance(symbol: str) -> dict:
    """yfinance로 펀더멘털 조회 (폴백용)."""
    result = {"per": None, "debt_ratio": None, "available": False, "source": "yfinance"}
    if not HAS_YF:
        return result

    ticker_str = _korean_ticker(symbol)
    if not ticker_str:
        return result

    try:
        t = yf.Ticker(ticker_str)
        info = t.info or {}
        per = info.get("trailingPE")
        if per is not None:
            try:
                per = float(per)
                if per < 0:
                    per = None
            except (TypeError, ValueError):
                per = None
        result["per"] = per

        debt_eq = info.get("debtToEquity")
        if debt_eq is not None:
            try:
                result["debt_ratio"] = float(debt_eq) * 100.0
            except (TypeError, ValueError):
                pass
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
        logger.debug("yfinance 펀더멘털 조회 실패 {}: {}", symbol, e)

    return result


def get_fundamentals(symbol: str) -> dict:
    """
    종목의 기본 펀더멘털 지표 조회. pykrx(우선) → yfinance(폴백) 순서.

    Returns:
        {"per": float|None, "debt_ratio": float|None, "available": bool, "source": str}
    """
    # 1차: pykrx (한국 종목 정확도 높음)
    result = _get_fundamentals_pykrx(symbol)
    if result["available"]:
        return result

    # 2차: yfinance (폴백)
    yf_result = _get_fundamentals_yfinance(symbol)
    if yf_result["available"]:
        # pykrx에서 못 가져온 필드를 yfinance로 보충
        if result["per"] is None and yf_result["per"] is not None:
            result["per"] = yf_result["per"]
        if result["debt_ratio"] is None and yf_result["debt_ratio"] is not None:
            result["debt_ratio"] = yf_result["debt_ratio"]
        result["available"] = result["per"] is not None or result["debt_ratio"] is not None
        result["source"] = "pykrx+yfinance" if result["source"] == "pykrx" else "yfinance"
        return result

    result["source"] = "none"
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
