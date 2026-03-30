"""
데이터 수집 모듈
- FinanceDataReader로 한국 주식 과거 데이터 수집 (우선)
- yfinance로 미국 주식 데이터 수집
- KIS API 일봉 조회 (FDR 실패 시 폴백)

수정주가(배당·액면분할 반영) 처리:
- FinanceDataReader: 기본적으로 수정주가 제공. 백테스트·실전 동일 소스 권장.
- yfinance: auto_adjust=True 로 수정주가 사용.
- KIS API: 비수정(원시) 데이터를 반환하는 경우가 많음. FDR/yfinance와 혼용 시 지표값·신호가 달라질 수 있으므로,
  백테스트와 실전에서 동일 소스를 쓰도록 FDR 설치·우선 사용을 권장하고, KIS fallback 시 로그로 경고.
"""

import re
import time as time_module
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

try:
    import FinanceDataReader as fdr
    HAS_FDR = True
except ImportError:
    HAS_FDR = False
    logger.warning("FinanceDataReader가 설치되지 않았습니다. pip install FinanceDataReader")

try:
    from pykrx import stock as _pykrx_stock
    HAS_PYKRX = True
except ImportError:
    HAS_PYKRX = False
    _pykrx_stock = None

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    logger.warning("yfinance가 설치되지 않았습니다. pip install yfinance")

from database.repositories import save_stock_prices, get_stock_prices


# 시장별 FDR StockListing 전체 테이블(시총순) — 백테스트 중 반복 호출 방지
_FDR_FULL_LISTING_CACHE: dict[str, pd.DataFrame] = {}


def clear_fdr_listing_cache() -> None:
    """FDR 상장 목록 메모리 캐시 비우기 (테스트·장기 백테스트 세션 구분용)."""
    _FDR_FULL_LISTING_CACHE.clear()


def _fdr_stock_listing_table(market: str, top_n: int | None) -> pd.DataFrame:
    """FDR StockListing을 Code, Name, Marcap 컬럼 DataFrame으로 정규화. 시총 내림차순.

    동일 시장은 전체 테이블을 1회만 로드해 메모리에 캐시한다(top_n 슬라이스만 복사).
    """
    import FinanceDataReader as fdr

    empty = pd.DataFrame(columns=["Code", "Name", "Marcap"])
    if not HAS_FDR:
        return empty

    if market not in _FDR_FULL_LISTING_CACHE:
        try:
            df = fdr.StockListing(market)
        except Exception as e:
            logger.warning("FDR StockListing('{}') 실패: {}", market, e)
            return empty
        if df is None or df.empty:
            _FDR_FULL_LISTING_CACHE[market] = empty
            return empty.copy()

        code_col = None
        for col in ("Code", "code", "Symbol", "symbol"):
            if col in df.columns:
                code_col = col
                break
        if code_col is None:
            logger.warning("FDR {} 목록에 Code 계열 컬럼이 없습니다.", market)
            _FDR_FULL_LISTING_CACHE[market] = empty
            return empty.copy()

        marcap_col = None
        for col in ("Marcap", "MarketCap", "marcap"):
            if col in df.columns:
                marcap_col = col
                break
        name_col = None
        for col in ("Name", "name"):
            if col in df.columns:
                name_col = col
                break

        codes = []
        names = []
        marcaps = []
        for _, row in df.iterrows():
            c = str(row[code_col]).strip()
            if not c or c.lower() == "nan":
                continue
            codes.append(c.zfill(6))
            names.append(str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else "")
            if marcap_col and pd.notna(row.get(marcap_col)):
                try:
                    marcaps.append(float(row[marcap_col]))
                except (TypeError, ValueError):
                    marcaps.append(0.0)
            else:
                marcaps.append(0.0)

        out = pd.DataFrame({"Code": codes, "Name": names, "Marcap": marcaps})
        if marcap_col and out["Marcap"].sum() > 0:
            out = out.sort_values("Marcap", ascending=False)
        out = out.reset_index(drop=True)
        _FDR_FULL_LISTING_CACHE[market] = out

    full = _FDR_FULL_LISTING_CACHE[market]
    if full.empty:
        return empty.copy()
    if top_n is None:
        return full.copy()
    return full.head(int(top_n)).copy().reset_index(drop=True)


def get_kospi_tickers_fdr(top_n: int | None = 200) -> list[str]:
    """FDR KOSPI 상장 목록에서 시총 상위 종목 코드.

    pykrx ``get_market_ticker_list`` 가 동작하지 않는 환경에서 유니버스·후보 풀 폴백용.
    ``top_n`` 이 None이면 정렬된 전체 KOSPI 코드를 반환한다.
    """
    tab = _fdr_stock_listing_table("KOSPI", top_n)
    if tab.empty:
        return []
    return tab["Code"].tolist()


class DataCollectionError(RuntimeError):
    """주가 데이터 수집 실패 예외."""


_USD_KRW_CACHE: tuple[float, float] = (0.0, 0.0)
_USD_KRW_TTL_SEC = 300.0


class DataCollector:
    """
    주가 데이터 수집기
    - 한국 주식: FinanceDataReader (수정주가, 우선) → yfinance (수정주가) → KIS API (비수정 가능)
    - 미국 주식: yfinance (auto_adjust=True, 수정주가)

    데이터 소스 추적: 마지막 수집 시 사용된 소스와 수정주가 여부를 기록.
    백테스트↔실전 간 소스 불일치 시 지표·신호가 달라질 수 있으므로 동일 소스 사용 권장.
    """

    # 소스별 수정주가 보장 여부
    SOURCE_ADJUSTED_MAP = {
        "FinanceDataReader": True,
        "yfinance": True,       # auto_adjust=True
        "KIS": False,           # 비수정(원시) 반환 가능
    }

    def __init__(self, config=None):
        if config is None:
            from config.config_loader import Config
            config = Config.get()
        self._config = config
        ds_cfg = (config.settings or {}).get("data_source") or {}
        self._preferred_source = ds_cfg.get("preferred", "auto")
        self._allow_kis_fallback = ds_cfg.get("allow_kis_fallback", True)
        self._warn_on_source_mismatch = ds_cfg.get("warn_on_source_mismatch", True)

        self._last_source: str | None = None
        self._last_adjusted: bool | None = None
        self._source_history: dict[str, str] = {}
        # 종목별 (wide_start, wide_end, df) — fetch_korean_stock 구간 캐시·병합
        self._krx_ohlcv_ranges: dict[str, tuple[str, str, pd.DataFrame]] = {}
        self.quiet_ohlcv_log = False
        logger.info(
            "DataCollector 초기화 완료 (preferred={}, allow_kis_fallback={})",
            self._preferred_source, self._allow_kis_fallback,
        )

    def clear_krx_ohlcv_range_cache(self) -> None:
        """한국 주가 구간 캐시 비우기."""
        self._krx_ohlcv_ranges.clear()

    def _record_source(self, symbol: str, source: str):
        """수집에 사용된 소스를 기록하고 수정주가 여부를 갱신."""
        self._last_source = source
        self._last_adjusted = self.SOURCE_ADJUSTED_MAP.get(source, None)
        self._source_history[symbol] = source

    def _log_source_usage(self, symbol: str, source: str):
        """종목 수집 시 사용 소스/수정주가 여부를 표준 포맷으로 로그 출력."""
        adjusted = bool(self.SOURCE_ADJUSTED_MAP.get(source, False))
        logger.info(
            "[DataCollector] {} 소스={}, 수정주가={}",
            symbol,
            source,
            adjusted,
        )

    def get_last_source_info(self) -> dict:
        """마지막 수집 시 사용된 소스 정보."""
        return {
            "source": self._last_source,
            "adjusted": self._last_adjusted,
            "history": dict(self._source_history),
        }

    @staticmethod
    def is_us_ticker(symbol: str) -> bool:
        """
        미국 주식 티커 휴리스틱: '.' 없이 알파벳만 (예: AAPL, MSFT).
        BRK.B 등은 제외되며 한국 6자리 숫자 코드와 구분됩니다.
        """
        s = str(symbol).strip()
        if not s or "." in s:
            return False
        return bool(re.fullmatch(r"[A-Za-z]+", s))

    @classmethod
    def get_usd_krw_rate(cls) -> float:
        """
        USD→KRW 환율 (1 USD당 원화). yfinance 티커 KRW=X.
        짧은 TTL 캐시로 호출 부담을 줄입니다.
        """
        global _USD_KRW_CACHE  # noqa: PLW0603
        now = time_module.monotonic()
        rate, ts = _USD_KRW_CACHE
        if rate > 0 and (now - ts) < _USD_KRW_TTL_SEC:
            return rate
        if not HAS_YF:
            logger.warning("yfinance 미설치 — USD/KRW 환율 0 반환")
            return 0.0
        try:
            t = yf.Ticker("KRW=X")
            last = t.fast_info.get("last_price") or t.fast_info.get("regular_market_price")
            if last is None or float(last) <= 0:
                hist = t.history(period="5d", auto_adjust=True)
                if hist is not None and not hist.empty and "Close" in hist.columns:
                    last = float(hist["Close"].iloc[-1])
            fx = float(last)
            if fx <= 0:
                return 0.0
            _USD_KRW_CACHE = (fx, now)
            logger.debug("USD/KRW 환율 갱신: {:.2f}", fx)
            return fx
        except Exception as e:
            logger.warning("USD/KRW 환율 조회 실패: {}", e)
            return rate if rate > 0 else 0.0

    def fetch_stock(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """티커 규칙에 따라 미국(yfinance) 또는 한국(FDR→yfinance→KIS) 경로로 수집."""
        if self.is_us_ticker(symbol):
            return self.fetch_us_stock(symbol, start_date, end_date)
        return self.fetch_korean_stock(symbol, start_date, end_date)

    def check_source_consistency(self, reference_source: str = "FinanceDataReader") -> list[str]:
        """수집 이력에서 reference_source와 다른 소스를 사용한 종목 반환."""
        mismatched = []
        for symbol, src in self._source_history.items():
            if src != reference_source:
                mismatched.append(f"{symbol}({src})")
        if mismatched and self._warn_on_source_mismatch:
            logger.warning(
                "데이터 소스 불일치 감지(reference={}): {}",
                reference_source,
                mismatched,
            )
        return mismatched

    @staticmethod
    def _slice_krx_df_by_date(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        s = pd.Timestamp(start_date).normalize()
        e = pd.Timestamp(end_date).normalize()
        part = df.sort_index().loc[s:e]
        return part.copy()

    def fetch_korean_stock(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        한국 주식 일봉 데이터 수집.

        우선순위: FinanceDataReader(수정주가) → yfinance(수정주가) → KIS API(비수정 가능).
        동일 인스턴스에서 종목별로 요청 구간을 병합해 네트워크 호출을 줄인다(멀티 리밸런스 백테스트용).

        Args:
            symbol: 종목 코드 (예: "005930" 삼성전자)
            start_date: 시작일 (YYYY-MM-DD). 기본값: 3년 전
            end_date: 종료일 (YYYY-MM-DD). 기본값: 오늘

        Returns:
            정규화된 OHLCV DataFrame
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        sym = str(symbol).strip()
        ent = self._krx_ohlcv_ranges.get(sym)
        if ent:
            s0, e0, df0 = ent
            if s0 <= start_date and e0 >= end_date and df0 is not None and not df0.empty:
                return self._slice_krx_df_by_date(df0, start_date, end_date)
            fetch_s = min(s0, start_date)
            fetch_e = max(e0, end_date)
        else:
            fetch_s, fetch_e = start_date, end_date

        df_full = self._fetch_korean_stock_uncached(sym, fetch_s, fetch_e)
        if df_full is not None and not df_full.empty:
            self._krx_ohlcv_ranges[sym] = (fetch_s, fetch_e, df_full)
        elif sym in self._krx_ohlcv_ranges:
            del self._krx_ohlcv_ranges[sym]

        if df_full is None or df_full.empty:
            return pd.DataFrame()
        return self._slice_krx_df_by_date(df_full, start_date, end_date)

    def _fetch_korean_stock_uncached(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """fetch_korean_stock의 실제 수집(캐시 미사용)."""
        # 1) FDR
        if HAS_FDR and self._preferred_source in ("auto", "fdr"):
            df = self._try_fdr(symbol, start_date, end_date)
            if df is not None and not df.empty:
                self._record_source(symbol, "FinanceDataReader")
                if not self.quiet_ohlcv_log:
                    self._log_source_usage(symbol, "FinanceDataReader")
                return df

        # 2) yfinance
        if HAS_YF and self._preferred_source in ("auto", "fdr", "yfinance"):
            df = self._fetch_korean_stock_via_yfinance(symbol, start_date, end_date)
            if not df.empty:
                self._record_source(symbol, "yfinance")
                if not self.quiet_ohlcv_log:
                    self._log_source_usage(symbol, "yfinance")
                return df

        # 3) KIS API (비수정주가 가능)
        if not self._allow_kis_fallback:
            raise DataCollectionError(
                f"KIS fallback 비활성화 상태. FDR/yfinance 실패로 수집 중단: {symbol}"
            )

        logger.warning(
            "⚠️ [데이터 소스 불일치] {} → KIS API 폴백. "
            "KIS는 비수정주가(원시)를 반환할 수 있습니다. "
            "백테스트를 FDR/yfinance(수정주가)로 했다면 지표·신호가 달라질 수 있습니다. "
            "pip install FinanceDataReader 를 강력히 권장합니다.",
            symbol,
        )
        df = self.fetch_korean_stock_via_kis(symbol)
        if not df.empty:
            self._record_source(symbol, "KIS")
            if not self.quiet_ohlcv_log:
                self._log_source_usage(symbol, "KIS")
        return df

    def _try_fdr(
        self, symbol: str, start_date: str, end_date: str,
    ) -> pd.DataFrame | None:
        """FDR 수집 시도. 실패 시 None 반환."""
        log = logger.debug if self.quiet_ohlcv_log else logger.info
        log("한국 주식 데이터 수집 (FDR): {} ({} ~ {})", symbol, start_date, end_date)
        try:
            df = fdr.DataReader(symbol, start_date, end_date)
            df = self._normalize_dataframe(df)
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)
            log(
                "종목 {} 수집 완료 (소스=FinanceDataReader, 수정주가=Yes): {}건",
                symbol, len(df),
            )
            return df
        except Exception as e:
            logger.warning("FDR 수집 실패 ({}): yfinance 폴백 시도", e)
            return None

    def fetch_us_stock(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        미국 주식 일봉 데이터 수집 (yfinance)

        Args:
            symbol: 티커 (예: "AAPL", "GOOGL")
            start_date: 시작일 (YYYY-MM-DD)
            end_date: 종료일 (YYYY-MM-DD)

        Returns:
            정규화된 OHLCV DataFrame
        """
        if not HAS_YF:
            raise ImportError("yfinance가 필요합니다: pip install yfinance")

        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("미국 주식 데이터 수집 (yfinance, auto_adjust=True): {} ({} ~ {})", symbol, start_date, end_date)

        try:
            df = yf.download(
                symbol,
                start=start_date,
                end=end_date,
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if isinstance(df.columns, pd.MultiIndex):
                df = df.copy()
                df.columns = [str(c[0]).lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
            df = self._normalize_dataframe(df)
            
            # 수신 데이터 정합성 검증 추가
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)

            logger.info("종목 {} 데이터 수집 완료: {}건", symbol, len(df))
            return df
        except Exception as e:
            logger.error("종목 {} 데이터 수집 실패: {}", symbol, e)
            raise

    def fetch_and_save(
        self,
        symbol: str,
        market: str = "KR",
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        데이터 수집 후 DB에 저장

        Args:
            symbol: 종목 코드
            market: "KR" (한국) 또는 "US" (미국)
            start_date: 시작일
            end_date: 종료일

        Returns:
            수집된 OHLCV DataFrame
        """
        if market.upper() == "US":
            df = self.fetch_us_stock(symbol, start_date, end_date)
        else:
            df = self.fetch_korean_stock(symbol, start_date, end_date)

        # DB 저장을 위해 인덱스를 컬럼으로 변환
        df_save = df.reset_index()
        save_stock_prices(symbol, df_save)

        return df

    def get_cached_data(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        DB에서 캐시된 데이터를 조회, 없으면 새로 수집

        Args:
            symbol: 종목 코드
            start_date: 시작일
            end_date: 종료일

        Returns:
            OHLCV DataFrame
        """
        start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

        df = get_stock_prices(symbol, start, end)

        if df.empty:
            logger.info("종목 {} 캐시 없음 — 새로 수집합니다", symbol)
            mkt = "US" if self.is_us_ticker(symbol) else "KR"
            df = self.fetch_and_save(symbol, mkt, start_date, end_date)

        return df

    @staticmethod
    def _normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        데이터프레임 컬럼명을 통일된 소문자로 정규화

        다양한 데이터 소스의 컬럼명을 표준화:
        Open/open → open, High/high → high, ...
        """
        # 인덱스가 날짜인 경우 유지
        if df.index.name and df.index.name.lower() in ("date", "datetime"):
            df.index.name = "date"
        elif not isinstance(df.index, pd.DatetimeIndex):
            # 일반 인덱스인 경우 날짜 컬럼 탐색
            for col in df.columns:
                if col.lower() in ("date", "datetime"):
                    df.set_index(col, inplace=True)
                    df.index.name = "date"
                    break

        # 컬럼명 소문자 변환
        df.columns = [c.lower() for c in df.columns]

        # 필수 컬럼 확인 및 매핑
        column_map = {
            "adj close": "close",  # yfinance의 adjusted close
        }
        df.rename(columns=column_map, inplace=True)

        # 필수 컬럼만 유지
        required = ["open", "high", "low", "close", "volume"]
        available = [c for c in required if c in df.columns]
        df = df[available]

        # 결측치 처리 (forward fill) — pandas 3.x 호환
        df = df.ffill()
        df = df.dropna()

        return df

    @staticmethod
    def get_krx_stock_list(
        as_of_date: Optional[str] = None,
        exclude_administrative: bool = True,
        universe_mode: str = "current",
    ) -> pd.DataFrame:
        """
        KRX 종목 리스트 조회 (생존자 편향 완화 옵션 지원).

        Args:
            as_of_date: 기준일 (YYYY-MM-DD).
                - "historical" 모드: 해당 일자에 상장되어 있던 전체 종목 조회 (pykrx).
                - "kospi200" 모드: 해당 일자 코스피200 구성종목 조회 (pykrx).
                - "current" 모드: 무시됨 (현재 상장 종목 FDR).
            exclude_administrative: True면 관리종목(투자주의·투자위험 등) 제외.
            universe_mode:
                - "current": 현재 상장 종목 (FDR). 생존자 편향 있음.
                - "historical": 과거 시점(as_of_date) 전체 상장 종목 (pykrx). 생존자 편향 완화.
                - "kospi200": 과거 시점(as_of_date) 코스피200 구성종목 (pykrx). 대형주 한정.

        Returns:
            종목 리스트 DataFrame (Code, Name, Market, Marcap 등).
        """
        mode = (universe_mode or "current").strip().lower()
        if mode == "kospi200":
            return DataCollector._get_kospi200_constituents(as_of_date)
        if mode == "historical":
            return DataCollector._get_historical_krx_list(as_of_date, exclude_administrative)
        if not HAS_FDR:
            raise ImportError("FinanceDataReader가 필요합니다")
        logger.info("KRX 종목 리스트 조회 중... (mode=current, 생존자 편향 주의)")
        stocks = fdr.StockListing("KRX")
        if stocks.empty:
            logger.warning("KRX 종목 리스트가 비어 있습니다.")
            return stocks
        if exclude_administrative:
            stocks = DataCollector._exclude_administrative(stocks)
        logger.info("KRX 종목 총 {}개 조회 완료", len(stocks))
        return stocks

    @staticmethod
    def _exclude_administrative(stocks: pd.DataFrame) -> pd.DataFrame:
        """FDR KRX-ADMINISTRATIVE 목록으로 관리종목 제외."""
        if not HAS_FDR:
            return stocks
        try:
            admin = fdr.StockListing("KRX-ADMINISTRATIVE")
            if not admin.empty:
                code_col = next((c for c in ["Code", "code", "Symbol", "symbol"] if c in admin.columns), None)
                if code_col:
                    admin_codes = set(admin[code_col].astype(str).str.strip().str.zfill(6))
                    sc = next((c for c in ["Code", "code", "Symbol", "symbol"] if c in stocks.columns), None)
                    if sc:
                        before = len(stocks)
                        stocks = stocks[~stocks[sc].astype(str).str.strip().str.zfill(6).isin(admin_codes)]
                        logger.info("관리종목 제외: {} → {} 종목", before, len(stocks))
        except Exception as e:
            logger.warning("관리종목 목록 조회 실패 — 제외 생략: {}", e)
        return stocks

    @staticmethod
    def _get_historical_krx_list(
        as_of_date: Optional[str], exclude_administrative: bool = True,
    ) -> pd.DataFrame:
        """
        pykrx로 과거 특정 날짜에 상장되어 있던 전체 종목 리스트 조회.
        상장폐지 종목 포함 → 생존자 편향 완화.
        ``get_market_ticker_list`` 실패·빈 목록 시 FDR KOSPI/KOSDAQ 시총순 목록으로 폴백한다.
        """
        date_str = (as_of_date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
        rows: list[dict] = []

        for market_name in ("KOSPI", "KOSDAQ"):
            tickers: list = []
            if HAS_PYKRX and _pykrx_stock is not None:
                try:
                    tickers = _pykrx_stock.get_market_ticker_list(date_str, market=market_name) or []
                except Exception as e:
                    logger.warning(
                        "pykrx get_market_ticker_list 실패 (market={}, as_of={}): {} — FDR 폴백",
                        market_name, as_of_date, e,
                    )
                    tickers = []

            if tickers:
                for t in tickers:
                    code = str(t).strip().zfill(6)
                    try:
                        name = _pykrx_stock.get_market_ticker_name(t) or ""
                    except Exception:
                        name = ""
                    rows.append({"Code": code, "Name": name, "Market": market_name, "Marcap": 0})
                continue

            tab = _fdr_stock_listing_table(market_name, None)
            logger.warning(
                "pykrx {} 티커 목록이 비어 있거나 미사용 — FDR {} 시총순 {}개로 대체 "
                "(현재 스냅샷·과거 시점 불일치·생존자 편향 주의, as_of={})",
                market_name, market_name, len(tab), as_of_date,
            )
            for _, r in tab.iterrows():
                rows.append({
                    "Code": str(r["Code"]).strip().zfill(6),
                    "Name": str(r.get("Name") or ""),
                    "Market": market_name,
                    "Marcap": float(r.get("Marcap") or 0),
                })

        if not rows:
            logger.warning("historical 종목 리스트 조회 결과 없음 (기준일: {})", as_of_date)
            return pd.DataFrame(columns=["Code", "Name", "Market", "Marcap"])

        df = pd.DataFrame(rows)
        logger.info(
            "historical 종목 리스트 {}개 (기준일: {}, KOSPI+KOSDAQ, pykrx 또는 FDR 폴백)",
            len(df), as_of_date,
        )
        if exclude_administrative:
            df = DataCollector._exclude_administrative(df)
        return df

    @staticmethod
    def get_sector_map() -> dict[str, str]:
        """
        KRX 종목코드 → 업종(Sector) 매핑 딕셔너리 반환.
        FDR StockListing('KRX')의 Sector 컬럼을 사용한다.
        FDR 미설치·조회 실패 시 빈 dict 반환.
        """
        if not HAS_FDR:
            logger.debug("FDR 미설치 — 업종 매핑 불가")
            return {}
        try:
            stocks = fdr.StockListing("KRX")
            if stocks.empty:
                return {}
            code_col = next((c for c in ["Code", "code", "Symbol", "symbol"] if c in stocks.columns), None)
            sector_col = next((c for c in ["Sector", "sector"] if c in stocks.columns), None)
            if code_col is None or sector_col is None:
                logger.debug("KRX 리스트에 Code/Sector 컬럼 없음")
                return {}
            mapping = {}
            for _, row in stocks.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                sector = str(row[sector_col]).strip() if pd.notna(row[sector_col]) else ""
                if code and sector:
                    mapping[code] = sector
            logger.debug("업종 매핑 {}개 종목 로드", len(mapping))
            return mapping
        except Exception as e:
            logger.warning("업종 매핑 조회 실패: {}", e)
            return {}

    @staticmethod
    def _get_kospi200_constituents(as_of_date: Optional[str]) -> pd.DataFrame:
        """pykrx로 해당 일자 코스피200 구성종목 조회 (생존자 편향 완화용). 실패 시 FDR 시총 상위 200 폴백."""
        date_str = (as_of_date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
        tickers: list | None = None

        if HAS_PYKRX and _pykrx_stock is not None:
            try:
                try:
                    tickers = _pykrx_stock.get_index_portfolio_deposit_file("1028", date_str)
                except TypeError:
                    tickers = _pykrx_stock.get_index_portfolio_deposit_file("1028")
            except Exception as e:
                logger.warning(
                    "pykrx 코스피200 구성 조회 실패 (as_of={}): {} — get_kospi_tickers_fdr(200) 폴백",
                    as_of_date, e,
                )
                tickers = None

        if tickers:
            rows = []
            for t in tickers:
                code = str(t).strip().zfill(6)
                try:
                    name = _pykrx_stock.get_market_ticker_name(t, date_str) or ""
                except (TypeError, Exception):
                    try:
                        name = _pykrx_stock.get_market_ticker_name(t) or ""
                    except Exception:
                        name = ""
                rows.append({"Code": code, "Name": name, "Market": "KOSPI", "Marcap": 0})
            df = pd.DataFrame(rows)
            logger.info("코스피200 구성종목 {}개 조회 완료 (기준일: {})", len(df), as_of_date)
            return df

        fb = get_kospi_tickers_fdr(200)
        logger.warning(
            "코스피200 구성종목이 비어 있거나 pykrx 미사용 — get_kospi_tickers_fdr(200)으로 "
            "{}개 대체 (지수 구성과 다를 수 있음, as_of={})",
            len(fb), as_of_date,
        )
        rows = [{"Code": c, "Name": "", "Market": "KOSPI", "Marcap": 0} for c in fb]
        return pd.DataFrame(rows)

    def _fetch_korean_stock_via_yfinance(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        yfinance로 한국 주식 일봉 수집 (티커: 005930.KS).
        FDR 미설치 환경에서 백테스트용 폴백.
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        ticker = "^KS11" if symbol.upper() == "KS11" else f"{symbol}.KS"
        logger.info("한국 주식 데이터 수집 (yfinance): {} ({} ~ {})", ticker, start_date, end_date)
        try:
            df = yf.download(ticker, start=start_date, end=end_date, progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = self._normalize_dataframe(df)
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)
            logger.info(
                "종목 {} 데이터 수집 완료 (소스=yfinance, 수정주가=auto_adjust=True): {}건",
                symbol, len(df),
            )
            return df
        except Exception as e:
            logger.warning("yfinance 한국 주식 수집 실패 ({}): {}", ticker, e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def fetch_korean_stock_via_kis(self, symbol: str) -> pd.DataFrame:
        """
        KIS API를 이용한 한국 주식 일봉 데이터 수집 (FDR/yfinance 실패 시 폴백).

        주의: KIS API는 비수정주가(원시)를 반환하는 경우가 많습니다. 백테스트를 FDR/yfinance(수정주가)로
        했다면 실전에서 KIS만 쓰면 지표값·신호가 달라질 수 있으므로, 가능하면 FinanceDataReader를 설치해
        백테스트와 실전에서 동일 소스를 사용하세요.

        Args:
            symbol: 종목 코드

        Returns:
            OHLCV DataFrame (최근 100일)
        """
        try:
            from api.kis_api import KISApi
            kis = KISApi()

            if not kis._is_configured():
                logger.warning("KIS API 미설정 — 빈 DataFrame 반환")
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

            logger.info(
                "한국 주식 데이터 수집 (소스=KIS API, 수정주가=비수정(원시) 가능성 — 백테스트와 소스 불일치 시 지표 차이 주의)"
            )
            kis.authenticate()
            raw_data = kis.get_daily_prices(symbol, period="D", count=100)

            if not raw_data:
                logger.warning("KIS API 일봉 데이터 없음: {}", symbol)
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

            # KIS API 응답을 DataFrame으로 변환
            rows = []
            for item in raw_data:
                try:
                    rows.append({
                        "date": pd.to_datetime(item.get("stck_bsop_date", "")),
                        "open": float(item.get("stck_oprc", 0)),
                        "high": float(item.get("stck_hgpr", 0)),
                        "low": float(item.get("stck_lwpr", 0)),
                        "close": float(item.get("stck_clpr", 0)),
                        "volume": int(item.get("acml_vol", 0)),
                    })
                except (ValueError, TypeError):
                    continue

            if not rows:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

            df = pd.DataFrame(rows)
            df.set_index("date", inplace=True)
            df.sort_index(inplace=True)

            # 수신 데이터 정합성 검증 추가
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)

            logger.info(
                "KIS API 일봉 수집 완료: {} {}건 (소스=KIS_API, 수정주가=No/불확실)",
                symbol, len(df),
            )
            return df

        except Exception as e:
            logger.error("KIS API 일봉 수집 실패 ({}): {}", symbol, e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
