"""
관심 종목 자동 구성 모듈.
- 수동 목록 사용
- 시가총액 상위 N개 자동 선정 (top_market_cap)
- 코스피200 구성 종목과 유사한 시총 상위 200개 (kospi200)
- 모멘텀 팩터: 12개월 수익률 상위 종목 (momentum_top)
- 저변동성 팩터: 60일 실현변동성 하위 = 저변동성 상위 (low_vol_top)
- 모멘텀+저변동성 복합: 12개월 수익률 상위이면서 저변동성 필터 (momentum_lowvol)
"""

from datetime import datetime, timedelta
import numpy as np
from loguru import logger

from config.config_loader import Config
from core.data_collector import DataCollector


class WatchlistManager:
    """watchlist 설정을 실제 종목 리스트로 해석한다."""

    def __init__(self, config: Config = None):
        self.config = config or Config.get()

    def resolve(self) -> list[str]:
        """설정 기준으로 관심 종목 리스트를 생성한다."""
        settings = self.config.watchlist_settings
        mode = str(settings.get("mode", "manual")).lower()
        manual_symbols = self._normalize_symbols(settings.get("symbols", []))

        if mode == "manual" and manual_symbols:
            return manual_symbols

        if mode == "top_market_cap":
            auto_symbols = self._build_top_market_cap_watchlist(settings)
            if auto_symbols:
                return auto_symbols

        # 코스피200: 시총 상위 200개 (KRX 코스피200 구성과 유사)
        if mode == "kospi200":
            kospi_settings = {**settings, "market": "KOSPI", "top_n": settings.get("kospi200_top_n", 200)}
            auto_symbols = self._build_top_market_cap_watchlist(kospi_settings)
            if auto_symbols:
                logger.info(
                    "watchlist 자동 생성 완료: mode=kospi200 (시총 상위 {}개)",
                    len(auto_symbols),
                )
                return auto_symbols

        # 모멘텀 팩터: 12개월 수익률 상위 종목 매수
        if mode == "momentum_top":
            auto_symbols = self._build_momentum_top_watchlist(settings)
            if auto_symbols:
                return auto_symbols

        # 저변동성 팩터: 60일 실현변동성 하위 = 저변동성 상위 종목
        if mode == "low_vol_top":
            auto_symbols = self._build_low_vol_top_watchlist(settings)
            if auto_symbols:
                return auto_symbols

        # 모멘텀 + 저변동성: 12개월 수익률 상위이면서 저변동성 필터 통과
        if mode == "momentum_lowvol":
            auto_symbols = self._build_momentum_lowvol_watchlist(settings)
            if auto_symbols:
                return auto_symbols

        if manual_symbols:
            return manual_symbols

        logger.warning("watchlist 설정이 비어 있어 기본 종목 005930을 사용합니다.")
        return ["005930"]

    def _build_top_market_cap_watchlist(self, settings: dict) -> list[str]:
        market = str(settings.get("market", "KOSPI")).upper()
        top_n = max(1, int(settings.get("top_n", 20)))

        try:
            stocks = DataCollector.get_krx_stock_list()
        except Exception as exc:
            logger.warning("KRX 종목 리스트 조회 실패 — 수동 watchlist로 대체: {}", exc)
            return []

        if stocks.empty:
            return []

        df = stocks.copy()

        market_col = self._pick_column(df, "Market", "market")
        code_col = self._pick_column(df, "Code", "code", "Symbol", "symbol")
        marcap_col = self._pick_column(df, "Marcap", "marcap", "Amount", "amount", "Close", "close")

        if code_col is None or marcap_col is None:
            logger.warning("watchlist 자동 생성 실패 — 필수 컬럼(Code/Marcap)을 찾을 수 없습니다.")
            return []

        if market_col is not None:
            df = df[df[market_col].astype(str).str.upper() == market]

        df = df.dropna(subset=[code_col, marcap_col]).copy()
        if df.empty:
            return []

        df[marcap_col] = df[marcap_col].astype(float)
        df = df.sort_values(marcap_col, ascending=False)
        symbols = self._normalize_symbols(df[code_col].head(top_n).tolist())

        logger.info(
            "watchlist 자동 생성 완료: mode=top_market_cap market={} top_n={} 실제={}개",
            market,
            top_n,
            len(symbols),
        )
        return symbols

    def _get_candidate_symbols(self, settings: dict, max_candidates: int) -> list[str]:
        """팩터 계산용 후보 종목 리스트 (시총 상위 N개)."""
        s = {**settings, "market": settings.get("market", "KOSPI"), "top_n": max_candidates}
        return self._build_top_market_cap_watchlist(s)

    def _build_momentum_top_watchlist(self, settings: dict) -> list[str]:
        """모멘텀 팩터: 12개월 수익률 상위 종목. 후보 풀에서 1년 수익률 계산 후 상위 top_n 반환."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n + 20, 60)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_1y = (end_d - timedelta(days=400)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        results = []
        for sym in candidates:
            ret_12m = self._compute_12m_return(collector, sym, start_1y, end_str)
            if ret_12m is not None:
                results.append((sym, ret_12m))

        if not results:
            logger.warning("모멘텀 팩터: 12개월 수익률 계산 가능한 종목 없음.")
            return []

        results.sort(key=lambda x: x[1], reverse=True)
        symbols = self._normalize_symbols([r[0] for r in results[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=momentum_top (12개월 수익률 상위 {}개)",
            len(symbols),
        )
        return symbols

    def _build_low_vol_top_watchlist(self, settings: dict) -> list[str]:
        """저변동성 팩터: 60일 실현변동성(연율화) 하위 = 저변동성 상위 종목."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n + 20, 60)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_d = (end_d - timedelta(days=130)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        results = []
        for sym in candidates:
            vol = self._compute_60d_vol(collector, sym, start_d, end_str)
            if vol is not None:
                results.append((sym, vol))

        if not results:
            logger.warning("저변동성 팩터: 60일 변동성 계산 가능한 종목 없음.")
            return []

        results.sort(key=lambda x: x[1])
        symbols = self._normalize_symbols([r[0] for r in results[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=low_vol_top (60일 저변동성 상위 {}개)",
            len(symbols),
        )
        return symbols

    def _build_momentum_lowvol_watchlist(self, settings: dict) -> list[str]:
        """모멘텀 + 저변동성: 저변동성 필터 통과 종목 중 12개월 수익률 상위."""
        top_n = max(1, int(settings.get("top_n", 20)))
        pool = max(top_n * 3, 80)
        candidates = self._get_candidate_symbols(settings, pool)
        if not candidates:
            return []

        collector = DataCollector()
        end_d = datetime.now()
        start_1y = (end_d - timedelta(days=400)).strftime("%Y-%m-%d")
        start_60 = (end_d - timedelta(days=130)).strftime("%Y-%m-%d")
        end_str = end_d.strftime("%Y-%m-%d")

        rows = []
        for sym in candidates:
            ret_12m = self._compute_12m_return(collector, sym, start_1y, end_str)
            vol = self._compute_60d_vol(collector, sym, start_60, end_str)
            if ret_12m is not None and vol is not None:
                rows.append({"symbol": sym, "ret_12m": ret_12m, "vol_60d": vol})

        if not rows:
            logger.warning("모멘텀+저변동성: 계산 가능한 종목 없음.")
            return []

        vol_median = float(np.median([r["vol_60d"] for r in rows]))
        filtered = [r for r in rows if r["vol_60d"] <= vol_median]
        if not filtered:
            filtered = rows
        filtered.sort(key=lambda x: x["ret_12m"], reverse=True)
        symbols = self._normalize_symbols([r["symbol"] for r in filtered[:top_n]])
        logger.info(
            "watchlist 자동 생성 완료: mode=momentum_lowvol (저변동성 필터 후 모멘텀 상위 {}개)",
            len(symbols),
        )
        return symbols

    @staticmethod
    def _compute_12m_return(collector: DataCollector, symbol: str, start_date: str, end_date: str):
        """12개월 수익률(%). 데이터 부족 시 None."""
        try:
            df = collector.fetch_korean_stock(symbol, start_date, end_date)
            if df.empty or len(df) < 120:
                return None
            close = df["close"].astype(float).dropna()
            if len(close) < 120:
                return None
            return float((close.iloc[-1] / close.iloc[0] - 1) * 100)
        except Exception as e:
            logger.debug("12m return 계산 실패 {}: {}", symbol, e)
            return None

    @staticmethod
    def _compute_60d_vol(collector: DataCollector, symbol: str, start_date: str, end_date: str):
        """60일 실현변동성(연율화). 일일 수익률 표준편차 * sqrt(252). 데이터 부족 시 None."""
        try:
            df = collector.fetch_korean_stock(symbol, start_date, end_date)
            if df.empty or len(df) < 65:
                return None
            close = df["close"].astype(float).dropna()
            if len(close) < 65:
                return None
            ret = close.pct_change().dropna()
            vol_60 = ret.tail(60).std()
            if vol_60 is None or np.isnan(vol_60) or vol_60 <= 0:
                return None
            return float(vol_60 * np.sqrt(252))
        except Exception as e:
            logger.debug("60d vol 계산 실패 {}: {}", symbol, e)
            return None

    @staticmethod
    def _normalize_symbols(symbols) -> list[str]:
        unique = []
        seen = set()
        for symbol in symbols or []:
            value = str(symbol).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique

    @staticmethod
    def _pick_column(df, *candidates):
        for candidate in candidates:
            if candidate in df.columns:
                return candidate
        return None
