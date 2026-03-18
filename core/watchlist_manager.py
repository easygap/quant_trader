"""
관심 종목 자동 구성 모듈.
- 수동 목록 사용
- 시가총액 상위 N개 자동 선정 (top_market_cap)
- 코스피200 구성 종목과 유사한 시총 상위 200개 (kospi200)
"""

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
