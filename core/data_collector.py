"""
데이터 수집 모듈
- FinanceDataReader로 한국 주식 과거 데이터 수집
- yfinance로 미국 주식 데이터 수집
- KIS API 일봉 조회 (FDR 미지원 환경 폴백)
- 데이터 정규화 및 DB 저장
"""

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
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    logger.warning("yfinance가 설치되지 않았습니다. pip install yfinance")

from database.repositories import save_stock_prices, get_stock_prices


class DataCollector:
    """
    주가 데이터 수집기
    - 한국 주식: FinanceDataReader
    - 미국 주식: yfinance
    """

    def __init__(self):
        logger.info("DataCollector 초기화 완료")

    def fetch_korean_stock(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """
        한국 주식 일봉 데이터 수집 (FinanceDataReader)

        Args:
            symbol: 종목 코드 (예: "005930" 삼성전자)
            start_date: 시작일 (YYYY-MM-DD). 기본값: 3년 전
            end_date: 종료일 (YYYY-MM-DD). 기본값: 오늘

        Returns:
            정규화된 OHLCV DataFrame
        """
        if not HAS_FDR:
            # FDR 미지원 시 yfinance(한국 티커 .KS) 시도 후 KIS API 폴백
            if HAS_YF:
                df = self._fetch_korean_stock_via_yfinance(symbol, start_date, end_date)
                if not df.empty:
                    return df
            logger.info("FDR/yfinance 미사용 — KIS API 일봉 조회로 대체")
            return self.fetch_korean_stock_via_kis(symbol)

        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("한국 주식 데이터 수집 (FDR): {} ({} ~ {})", symbol, start_date, end_date)

        try:
            df = fdr.DataReader(symbol, start_date, end_date)
            df = self._normalize_dataframe(df)
            
            # 수신 데이터 정합성 검증 추가
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)

            logger.info("종목 {} 데이터 수집 완료: {}건", symbol, len(df))
            return df
        except Exception as e:
            logger.warning("FDR 수집 실패 → KIS API 폴백: {}", e)
            return self.fetch_korean_stock_via_kis(symbol)

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

        logger.info("미국 주식 데이터 수집: {} ({} ~ {})", symbol, start_date, end_date)

        try:
            ticker = yf.Ticker(symbol)
            df = yf.download(symbol, start=start_date, end=end_date, progress=False)
            df = self._normalize_dataframe(df, is_us=True)
            
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
            df = self.fetch_and_save(symbol, "KR", start_date, end_date)

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
    def get_krx_stock_list() -> pd.DataFrame:
        """
        KRX 전 종목 리스트 조회

        Returns:
            종목 리스트 DataFrame (Code, Name, Market 등)
        """
        if not HAS_FDR:
            raise ImportError("FinanceDataReader가 필요합니다")

        logger.info("KRX 종목 리스트 조회 중...")
        stocks = fdr.StockListing("KRX")
        logger.info("KRX 종목 총 {}개 조회 완료", len(stocks))
        return stocks

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
        ticker = f"{symbol}.KS"
        logger.info("한국 주식 데이터 수집 (yfinance): {} ({} ~ {})", ticker, start_date, end_date)
        try:
            df = yf.download(ticker, start=start_date, end=end_date, progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = self._normalize_dataframe(df)
            from core.data_validator import DataValidator
            df = DataValidator.clean_dataframe(df, symbol)
            logger.info("종목 {} 데이터 수집 완료 (yfinance): {}건", symbol, len(df))
            return df
        except Exception as e:
            logger.warning("yfinance 한국 주식 수집 실패 ({}): {}", ticker, e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def fetch_korean_stock_via_kis(self, symbol: str) -> pd.DataFrame:
        """
        KIS API를 이용한 한국 주식 일봉 데이터 수집
        (FinanceDataReader 미지원 환경에서 폴백으로 사용)

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

            logger.info("KIS API 일봉 수집 완료: {} {}건", symbol, len(df))
            return df

        except Exception as e:
            logger.error("KIS API 일봉 수집 실패 ({}): {}", symbol, e)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
