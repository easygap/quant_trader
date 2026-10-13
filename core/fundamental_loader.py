"""
펀더멘털(재무) 지표 조회 모듈.
- PER, 부채비율 등 기본 재무 지표를 정상 범위 필터에 사용
- 한국 종목: pykrx(우선) → yfinance(폴백) 순서로 조회.
  yfinance는 한국 종목 재무 데이터 업데이트가 느리고 누락이 많아 pykrx를 우선 사용.
"""

from typing import Optional
import math

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


def yahoo_debt_to_equity_pct(info: dict) -> Optional[float]:
    """Yahoo debtToEquity is already expressed in percent (145.03 means 145.03%).

    This measures interest-bearing debt/equity, not Korean total liabilities/equity.
    Keep the basis explicit at callers; negative equity is not a healthy low ratio.
    """
    value = info.get("debtToEquity")
    try:
        if value is not None:
            ratio = float(value)
            return ratio if math.isfinite(ratio) and ratio >= 0 else None
        debt, equity = float(info["totalDebt"]), float(info["totalStockholderEquity"])
        if math.isfinite(debt) and math.isfinite(equity) and debt >= 0 and equity > 0:
            return debt / equity * 100.0
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


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
                result["per"] = per if math.isfinite(per) and per > 0 else None

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
                if not math.isfinite(per) or per <= 0:
                    per = None
            except (TypeError, ValueError):
                per = None
        result["per"] = per

        result["debt_ratio"] = yahoo_debt_to_equity_pct(info)
        result["debt_ratio_basis"] = "interest_bearing_debt_to_equity_pct"
        result["field_sources"] = {key: "yfinance" for key in ("per", "debt_ratio") if result[key] is not None}

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
    result["field_sources"] = {key: "pykrx" for key in ("per", "debt_ratio") if result.get(key) is not None}
    # pykrx provides PER but no debt field. A successful PER lookup must not
    # short-circuit the separate debt lookup and reject every PER-successful stock.

    # 2차: yfinance (폴백)
    yf_result = _get_fundamentals_yfinance(symbol)
    if yf_result["available"]:
        # pykrx에서 못 가져온 필드를 yfinance로 보충
        if result["per"] is None and yf_result["per"] is not None:
            result["per"] = yf_result["per"]
            result["field_sources"]["per"] = "yfinance"
        if result["debt_ratio"] is None and yf_result["debt_ratio"] is not None:
            result["debt_ratio"] = yf_result["debt_ratio"]
            result["field_sources"]["debt_ratio"] = "yfinance"
            result["debt_ratio_basis"] = yf_result.get("debt_ratio_basis", "interest_bearing_debt_to_equity_pct")
        result["available"] = result["per"] is not None or result["debt_ratio"] is not None
        result["source"] = "+".join(dict.fromkeys(result["field_sources"].values())) or "none"
        return result

    result["source"] = "+".join(dict.fromkeys(result["field_sources"].values())) or "none"
    return result


def check_fundamental_filter(
    symbol: str,
    per_min: Optional[float] = 0,
    per_max: Optional[float] = 50,
    debt_ratio_max: Optional[float] = 200,
    debt_ratio_basis: str = "total_liabilities_to_equity_pct",
) -> tuple[bool, str]:
    """
    PER·부채비율이 정상 범위인지 확인.

    Args:
        symbol: 종목코드
        per_min: PER 하한 (None이면 검사 생략). 0이면 적자(PER 음수) 제외
        per_max: PER 상한 (None이면 검사 생략)
        debt_ratio_max: 부채비율(%) 상한 (None이면 검사 생략)
        debt_ratio_basis: 기본 총부채/자본. Yahoo의 차입금/자본을 사용하려면
            interest_bearing_debt_to_equity_pct를 명시해야 한다.

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
        basis_labels = {
            "total_liabilities_to_equity_pct": "총부채/자본",
            "interest_bearing_debt_to_equity_pct": "차입금/자본",
        }
        dr = data.get("debt_ratio")
        actual_basis = data.get("debt_ratio_basis")
        if debt_ratio_basis not in basis_labels:
            reasons.append("펀더멘털 필터 설정 오류: 알 수 없는 부채비율 기준")
        elif dr is None:
            reasons.append("부채비율 미제공")
        elif actual_basis != debt_ratio_basis:
            reasons.append(
                f"부채비율 기준 불일치: 요구 {basis_labels[debt_ratio_basis]}, "
                f"제공 {basis_labels.get(actual_basis, '미확인')}"
            )
        elif dr > debt_ratio_max:
            label = basis_labels[debt_ratio_basis]
            reasons.append(f"{label} {dr:.1f}% > {debt_ratio_max}%")

    if reasons:
        return False, "; ".join(reasons)
    return True, "OK"
