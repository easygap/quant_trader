"""
펀더멘털 팩터 전략
- 가격(OHLC)이 아닌 재무 지표만으로 신호 산출 (pykrx 우선 → yfinance 폴백).
- 백테스트 시 DataFrame에 종목코드를 넣으려면 df.attrs['symbol'] = '005930' 설정.
- Point-in-time 안전장치: as_of_date가 주어지면 해당 시점 기준 공시 가용 데이터만 사용.
  한국 상장사는 분기 종료 후 통상 45일 내 실적 공시. 안전 마진으로 60일 적용.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy
from config.config_loader import Config

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

# 모듈 캐시: symbol -> (만료 epoch, bundle dict)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _ticker_norm(ix: Any) -> str:
    return "".join(c for c in str(ix) if c.isdigit()).zfill(6)[-6:]


def _yf_ticker_candidates(symbol: str) -> list[str]:
    s = "".join(c for c in str(symbol) if c.isdigit()).zfill(6)[-6:]
    if len(s) != 6:
        return []
    return [f"{s}.KS", f"{s}.KQ"]


def _clean_per(x) -> Optional[float]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        v = float(x)
        if v <= 0 or v > 5000:
            return None
        return v
    except (TypeError, ValueError):
        return None


def _try_dates_for_fundamental_table(
    markets: tuple[str, ...],
    as_of_date: datetime = None,
) -> tuple[Optional[pd.DataFrame], str, str]:
    """최근 영업일 후보로 get_market_fundamental_by_ticker 조회.
    as_of_date가 주어지면 해당 날짜 이전 영업일에서만 조회 (look-ahead 방지).
    """
    if not HAS_PYKRX:
        return None, "", ""
    ref = as_of_date.date() if as_of_date else datetime.now().date()
    for delta in range(0, 15):
        d = (ref - timedelta(days=delta)).strftime("%Y%m%d")
        for mkt in markets:
            try:
                df = pykrx_stock.get_market_fundamental_by_ticker(d, market=mkt, alternative=True)
                if df is None or df.empty:
                    continue
                if "PER" not in df.columns:
                    continue
                return df, d, mkt
            except Exception:
                continue
    return None, "", ""


def _market_fundamental_for_symbol(
    symbol: str,
    as_of_date: datetime = None,
) -> tuple[Optional[pd.DataFrame], str, str]:
    """종목이 포함된 시장(KOSPI/KOSDAQ) 스냅샷 우선, 없으면 첫 유효 테이블."""
    s6 = "".join(c for c in str(symbol) if c.isdigit()).zfill(6)[-6:]
    if len(s6) != 6:
        return _try_dates_for_fundamental_table(("KOSPI", "KOSDAQ"), as_of_date=as_of_date)
    for mkt in ("KOSPI", "KOSDAQ"):
        mdf, dstr, mkt_out = _try_dates_for_fundamental_table((mkt,), as_of_date=as_of_date)
        if mdf is None or mdf.empty:
            continue
        for ix in mdf.index:
            if _ticker_norm(ix) == s6:
                return mdf, dstr, mkt_out
    return _try_dates_for_fundamental_table(("KOSPI", "KOSDAQ"), as_of_date=as_of_date)


def _per_from_mdf(market_df: pd.DataFrame, t6: str) -> Optional[float]:
    for ix in market_df.index:
        if _ticker_norm(ix) == t6:
            return _clean_per(market_df.loc[ix, "PER"])
    return None


def _stock_per_pykrx(symbol: str, as_of_date: datetime = None) -> Optional[float]:
    """일별 시계열에서 최근 PER. as_of_date가 주어지면 해당 시점까지만 조회."""
    if not HAS_PYKRX:
        return None
    s = "".join(c for c in str(symbol) if c.isdigit()).zfill(6)[-6:]
    if len(s) != 6:
        return None
    try:
        ref = as_of_date or datetime.now()
        today = ref.strftime("%Y%m%d")
        start = (ref - timedelta(days=30)).strftime("%Y%m%d")
        per_df = pykrx_stock.get_market_fundamental(start, today, s)
        if per_df is None or per_df.empty or "PER" not in per_df.columns:
            return None
        per_val = per_df["PER"].dropna()
        if per_val.empty:
            return None
        return _clean_per(per_val.iloc[-1])
    except Exception as e:
        logger.debug("pykrx 종목 PER 조회 실패 {}: {}", symbol, e)
        return None


def _sector_median_per_pykrx(market_df: pd.DataFrame) -> Optional[float]:
    """동일 시장 스냅샷에서 유효 PER 중앙값(업종 실패 시 시장 대비 프록시)."""
    if market_df is None or market_df.empty or "PER" not in market_df.columns:
        return None
    series = pd.to_numeric(market_df["PER"], errors="coerce")
    series = series[(series > 0) & (series < 500)]
    if series.empty:
        return None
    return float(series.median())


def _industry_avg_per_pykrx(
    symbol: str, date_str: str, market: str, market_df: pd.DataFrame,
) -> Optional[float]:
    """업종 분류가 되면 동일 업종 PER 평균, 실패 시 None."""
    if not HAS_PYKRX or market_df is None or market_df.empty:
        return None
    s = "".join(c for c in str(symbol) if c.isdigit()).zfill(6)[-6:]
    for delta in range(0, 10):
        dtry = date_str
        if delta > 0:
            try:
                d0 = datetime.strptime(date_str, "%Y%m%d").date() - timedelta(days=delta)
                dtry = d0.strftime("%Y%m%d")
            except Exception:
                continue
        try:
            sec = pykrx_stock.get_market_sector_classifications(dtry, market)
            if sec is None or sec.empty:
                continue
            code_col = next((c for c in sec.columns if "코드" in str(c) or "ticker" in str(c).lower()), None)
            ind_col = next((c for c in sec.columns if "업종" in str(c) or "산업" in str(c)), None)
            if not code_col or not ind_col:
                continue
            sec = sec.copy()
            sec["_t"] = sec[code_col].astype(str).str.replace(" ", "").str.zfill(6).str[-6:]
            row = sec.loc[sec["_t"] == s]
            if row.empty:
                continue
            industry = row.iloc[0][ind_col]
            peers = sec.loc[sec[ind_col] == industry, "_t"].tolist()
            pers = [_per_from_mdf(market_df, p) for p in peers]
            pers = [x for x in pers if x is not None]
            if not pers:
                continue
            return float(sum(pers) / len(pers))
        except Exception as e:
            logger.debug("업종 PER 평균 계산 실패 {} ({}): {}", symbol, dtry, e)
            continue
    return None


def _yf_first_ticker(symbol: str) -> Optional[Any]:
    """KOSPI(.KS) 우선, 데이터 없으면 코스닥(.KQ) 티커."""
    if not HAS_YF:
        return None
    cands = _yf_ticker_candidates(symbol)
    if not cands:
        return None
    for tkr in cands:
        try:
            t = yf.Ticker(tkr)
            info = t.info or {}
            if (
                info.get("trailingPE") is not None
                or info.get("returnOnEquity") is not None
                or info.get("longName")
                or info.get("shortName")
            ):
                return t
        except Exception:
            continue
    try:
        return yf.Ticker(cands[0])
    except Exception:
        return None


def _yf_roe_debt_op_yoy(symbol: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """yfinance: ROE(%), 부채비율(%), 영업이익 YoY(%).

    주의: yfinance debtToEquity는 totalDebt/equity로, 한국 회계의 부채비율(totalLiabilities/equity)과
    다릅니다. 차입금 비율에 가까우며 한국 기업은 이 값이 높게 나올 수 있습니다.
    이 값을 그대로 사용하면 건전한 기업도 부채 항목에서 탈락할 수 있으므로,
    debt_ratio_max 설정을 yfinance 기준에 맞게 조정하거나 이 필드를 skip하세요.
    """
    if not HAS_YF:
        return None, None, None
    try:
        t = _yf_first_ticker(symbol)
        if t is None:
            return None, None, None
        info = t.info or {}
        roe = info.get("returnOnEquity")
        if roe is not None:
            try:
                roe = float(roe) * 100.0 if abs(float(roe)) <= 1.5 else float(roe)
            except (TypeError, ValueError):
                roe = None

        debt_ratio = None
        de = info.get("debtToEquity")
        if de is not None:
            try:
                debt_ratio = float(de) * 100.0
            except (TypeError, ValueError):
                pass
        if debt_ratio is None:
            td, te = info.get("totalDebt"), info.get("totalStockholderEquity")
            if td is not None and te is not None and float(te) != 0:
                try:
                    debt_ratio = float(td) / float(te) * 100.0
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

        yoy = _yf_operating_income_yoy(t)
        return roe, debt_ratio, yoy
    except Exception as e:
        logger.debug("yfinance 펀더멘털 조회 실패 {}: {}", symbol, e)
        return None, None, None


def _yf_operating_income_yoy(t: "yf.Ticker") -> Optional[float]:
    """분기 재무제표에서 영업이익 전년 동기 대비 증가율(%)."""
    try:
        qf = t.quarterly_financials
        if qf is None or qf.empty:
            return None
        op_row = None
        for idx in qf.index:
            lab = str(idx).lower()
            if "operating income" in lab or "영업이익" in str(idx):
                op_row = qf.loc[idx]
                break
        if op_row is None:
            return None
        vals = pd.to_numeric(op_row, errors="coerce").dropna()
        if len(vals) < 5:
            return None
        cur = float(vals.iloc[0])
        prev_y = float(vals.iloc[4])
        if prev_y == 0 or abs(prev_y) < 1e-6:
            return None
        return (cur / prev_y - 1.0) * 100.0
    except Exception:
        return None


def _yf_per(symbol: str) -> Optional[float]:
    """yfinance PER: trailingPE 우선, 없으면 forwardPE 폴백."""
    if not HAS_YF:
        return None
    try:
        t = _yf_first_ticker(symbol)
        if t is None:
            return None
        info = t.info or {}
        per = info.get("trailingPE")
        if per is None:
            per = info.get("forwardPE")
        return _clean_per(per)
    except Exception:
        return None


def _fetch_fundamental_bundle(symbol: str, as_of_date: datetime = None) -> dict[str, Any]:
    """pykrx + yfinance 병합 펀더멘털 스냅샷.
    as_of_date: 백테스트 시점. 주어지면 해당 시점 기준 공시 가용 데이터만 사용.
    """
    out: dict[str, Any] = {
        "per": None,
        "sector_benchmark_per": None,
        "used_sector_detail": False,
        "roe": None,
        "debt_ratio": None,
        "op_income_yoy": None,
        "source": "",
    }
    per_pykrx = _stock_per_pykrx(symbol, as_of_date=as_of_date)
    mdf, dstr, mkt = _market_fundamental_for_symbol(symbol, as_of_date=as_of_date)
    bench: Optional[float] = None
    if mdf is not None and not mdf.empty:
        bench_ind = _industry_avg_per_pykrx(symbol, dstr, mkt, mdf)
        if bench_ind is not None:
            bench = bench_ind
            out["used_sector_detail"] = True
        else:
            bench = _sector_median_per_pykrx(mdf)
    per = per_pykrx
    if per is None:
        per = _yf_per(symbol)
    roe, debt, yoy = _yf_roe_debt_op_yoy(symbol)
    out["per"] = per
    out["sector_benchmark_per"] = bench
    out["roe"] = roe
    out["debt_ratio"] = debt
    out["op_income_yoy"] = yoy
    parts: list[str] = []
    if per_pykrx is not None or mdf is not None:
        parts.append("pykrx")
    if per is not None and per_pykrx is None:
        parts.append("yfinance_per")
    if roe is not None or debt is not None or yoy is not None:
        parts.append("yfinance_financials")
    out["source"] = "+".join(dict.fromkeys(parts)) if parts else "none"
    return out


class FundamentalFactorStrategy(BaseStrategy):
    """
    재무 팩터만 사용하는 신호.
    - PER(시장/업종 대비), ROE, 부채비율, 영업이익 YoY 네 축으로 0~4점.
    - 총점 >= 3: BUY, <= 1: SELL, 그 외 HOLD.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    # Point-in-time 안전 마진: 분기 종료 후 실적 공시까지 최소 일수.
    # 한국 상장사 통상 45일 이내 공시, 안전 마진 60일.
    PIT_SAFE_MARGIN_DAYS = 60

    def __init__(self, config: Config = None, as_of_date: datetime = None):
        super().__init__(
            name="fundamental_factor",
            description="펀더멘털 팩터 — PER·ROE·부채·영업이익 성장 (가격과 독립)",
        )
        self.config = config or Config.get()
        self.params = self.config.strategies.get("fundamental_factor", {})
        self._as_of_date = as_of_date

    def _cached_bundle(self, symbol: str, as_of_date: datetime = None) -> dict[str, Any]:
        hours = float(self.params.get("data_cache_hours", 24))
        now = time.time()
        sym = "".join(c for c in str(symbol) if c.isdigit()).zfill(6)[-6:]
        if not sym:
            return {}

        ref_date = as_of_date or self._as_of_date
        # Point-in-time: 백테스트 시 공시 가용 시점으로 조회 시점을 후퇴
        pit_date = None
        if ref_date is not None:
            pit_date = ref_date - timedelta(days=self.PIT_SAFE_MARGIN_DAYS)

        cache_key = f"{sym}_{pit_date.strftime('%Y%m%d') if pit_date else 'live'}"
        ent = _CACHE.get(cache_key)
        if ent and ent[0] > now:
            return ent[1]
        b = _fetch_fundamental_bundle(sym, as_of_date=pit_date)
        _CACHE[cache_key] = (now + hours * 3600.0, b)
        return b

    # 신뢰할 수 있는 데이터 소스 목록. 이 소스가 아니면 해당 필드를 점수 계산에서 제외.
    _TRUSTED_DEBT_SOURCES = ("pykrx", "dart")

    def _score_bundle(self, bundle: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        per_max = float(self.params.get("per_max_absolute", 30))
        use_sector = bool(self.params.get("per_sector_relative", True))
        roe_min = float(self.params.get("roe_min", 10))
        debt_max = float(self.params.get("debt_ratio_max", 200))
        growth_min = float(self.params.get("earnings_growth_min", 5))

        score = 0
        max_score = 0  # 유효한 팩터 수 (데이터 부재/신뢰도 부족 시 분모에서 제외)
        details: dict[str, Any] = {}

        per = bundle.get("per")
        bench = bundle.get("sector_benchmark_per")

        if per is not None:
            max_score += 1
            if per <= per_max:
                if use_sector and bench is not None and bench > 0:
                    if per < bench:
                        score += 1
                        details["per_vs_benchmark"] = f"{per:.2f} < 벤치마크 {bench:.2f}"
                elif not use_sector:
                    score += 1
                    details["per_absolute"] = f"{per:.2f} (≤ {per_max})"

        roe = bundle.get("roe")
        if roe is not None:
            max_score += 1
            if roe >= roe_min:
                score += 1
                details["roe"] = round(roe, 2)

        # 부채비율: provider 신뢰도 검증. yfinance debtToEquity는 한국 회계 기준과
        # 다르므로(차입금/자본 vs 총부채/자본) 신뢰 소스가 아니면 점수 계산에서 제외.
        dr = bundle.get("debt_ratio")
        source = bundle.get("source", "")
        debt_source_trusted = any(ts in source for ts in self._TRUSTED_DEBT_SOURCES)
        if dr is not None and debt_source_trusted:
            max_score += 1
            if dr <= debt_max:
                score += 1
                details["debt_ratio"] = round(dr, 2)
        elif dr is not None and not debt_source_trusted:
            details["debt_ratio_skipped"] = f"{round(dr, 1)}% (소스 '{source}' — 한국 기준 불일치로 점수 제외)"
            logger.debug("부채비율 점수 제외: 소스 '{}' 미신뢰 (값={:.1f}%)", source, dr)

        yoy = bundle.get("op_income_yoy")
        if yoy is not None:
            max_score += 1
            if yoy >= growth_min:
                score += 1
                details["op_income_yoy_pct"] = round(yoy, 2)

        details["_max_score"] = max_score
        return score, details

    def _signal_from_score_adaptive(self, score: int, max_score: int) -> str:
        """유효 팩터 수에 비례하여 신호 임계값을 동적 조정."""
        if max_score <= 1:
            return self.HOLD  # 데이터 1개 이하면 판단 불가
        # 유효 팩터의 75% 이상 충족 시 BUY, 25% 이하 시 SELL
        buy_threshold = max(2, int(max_score * 0.75))
        sell_threshold = max(0, int(max_score * 0.25))
        if score >= buy_threshold:
            return self.BUY
        if score <= sell_threshold:
            return self.SELL
        return self.HOLD

    def _signal_from_score(self, score: int, max_score: int = 4) -> str:
        """유효 팩터 수에 비례하여 신호 결정. max_score가 줄면 임계값도 내려감."""
        return self._signal_from_score_adaptive(score, max_score)

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        if result.empty:
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            result["ensemble_skip"] = True
            return result

        sym = ""
        if hasattr(result, "attrs") and isinstance(result.attrs, dict):
            sym = str(result.attrs.get("symbol") or "").strip()

        if not sym:
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            result["fundamental_score"] = 0
            result["ensemble_skip"] = True
            return result

        # 백테스트: DataFrame의 마지막 날짜를 as_of_date로 사용
        row_date = None
        if self._as_of_date:
            row_date = self._as_of_date
        elif hasattr(result.index, 'max') and len(result) > 0:
            try:
                last_idx = result.index[-1]
                if isinstance(last_idx, (datetime, pd.Timestamp)):
                    row_date = last_idx.to_pydatetime() if hasattr(last_idx, 'to_pydatetime') else last_idx
            except Exception:
                pass

        bundle = self._cached_bundle(sym, as_of_date=row_date)
        if not any(
            bundle.get(k) is not None
            for k in ("per", "sector_benchmark_per", "roe", "debt_ratio", "op_income_yoy")
        ):
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            result["fundamental_score"] = 0
            result["ensemble_skip"] = True
            return result

        sc, det = self._score_bundle(bundle)
        max_sc = det.get("_max_score", 4)
        sig = self._signal_from_score(sc, max_sc)
        result["signal"] = sig
        result["strategy_score"] = float(sc)
        result["fundamental_score"] = sc
        result["ensemble_skip"] = False
        return result

    def generate_signal(self, df: pd.DataFrame, symbol: str = None, **kwargs) -> dict:
        sym = symbol or kwargs.get("symbol") or (
            df.attrs.get("symbol") if hasattr(df, "attrs") and isinstance(df.attrs, dict) else None
        )
        if not sym:
            return {
                "signal": self.HOLD,
                "score": 0,
                "details": {"사유": "종목코드 없음 — df.attrs['symbol'] 또는 symbol 인자 필요"},
            }

        bundle = self._cached_bundle(sym)
        if not any(
            bundle.get(k) is not None
            for k in ("per", "roe", "debt_ratio", "op_income_yoy")
        ) and bundle.get("per") is None:
            return {
                "signal": self.HOLD,
                "score": 0,
                "details": {"사유": "펀더멘털 데이터 조회 실패"},
                "source": bundle.get("source", "none"),
            }

        sc, det = self._score_bundle(bundle)
        max_sc = det.get("_max_score", 4)
        sig = self._signal_from_score(sc, max_sc)
        det["_raw"] = {k: bundle.get(k) for k in ("per", "sector_benchmark_per", "roe", "debt_ratio", "op_income_yoy")}
        return {
            "signal": sig,
            "score": sc,
            "details": det,
            "close": float(df["close"].iloc[-1]) if not df.empty and "close" in df.columns else 0,
            "date": df.index[-1] if not df.empty else None,
            "source": bundle.get("source", ""),
        }
