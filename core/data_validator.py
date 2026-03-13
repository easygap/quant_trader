"""
데이터 정합성 검증 모듈
- 외부 API나 웹소켓에서 수신한 OHLCV 데이터의 이상 유무 판단
- Null, NaN, 음수 주가, 거래량 오류 등 필터링
- 타임스탬프 역전 방지
"""

import math
from datetime import datetime
import pandas as pd
from loguru import logger


class DataValidator:
    """
    들어오는 시장 데이터의 유효성을 검사합니다.
    """

    @staticmethod
    def is_valid_price(price: float) -> bool:
        """단일 주가 데이터가 유효한지 검사"""
        if price is None or math.isnan(price) or math.isinf(price):
            return False
        if price <= 0:  # 주식은 0원 또는 음수일 수 없음
            return False
        return True

    @staticmethod
    def is_valid_volume(volume: int) -> bool:
        """거래량이 유효한지 검사"""
        if volume is None or math.isnan(volume) or math.isinf(volume):
            return False
        if volume < 0:
            return False
        return True

    @classmethod
    def validate_realtime_data(cls, data: dict) -> bool:
        """
        웹소켓으로 수신한 틱 데이터 검증
        data = {"symbol": str, "price": float, "volume": int, ...}

        Returns:
            유효하면 True, 이상 데이터면 False
        """
        try:
            if not data or not isinstance(data, dict):
                return False

            if "symbol" not in data or not data["symbol"]:
                return False

            if not cls.is_valid_price(data.get("price")):
                logger.warning(f"유효하지 않은 주가 수신: {data}")
                return False

            if not cls.is_valid_volume(data.get("volume", 0)):
                logger.warning(f"유효하지 않은 거래량 수신: {data}")
                return False

            return True
            
        except Exception as e:
            logger.error(f"실시간 데이터 검증 실패: {e}")
            return False

    @classmethod
    def clean_dataframe(cls, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """
        수집된 일봉/분봉 DataFrame 정제
        - NaN 제거 또는 채우기
        - 음수 데이터 필터링
        - 중복 날짜 제거 및 시간순 정렬
        
        Args:
            df: 검증할 DataFrame
            symbol: 로깅용 종목 코드
            
        Returns:
            정제 완료된 DataFrame
        """
        if df.empty:
            return df

        initial_len = len(df)
        
        # 1. 필수 컬럼 확인
        required_cols = {"open", "high", "low", "close"}
        if not required_cols.issubset(df.columns):
            logger.error(f"[{symbol}] 필수 컬럼 누락. (현재: {df.columns})")
            return pd.DataFrame()

        # 2. NaN 결측치 처리 (이전 값으로 채움)
        df = df.ffill().bfill()
        
        # 3. 비정상 주가(음수 또는 0) 필터링
        mask = (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)
        df = df[mask]

        if "volume" in df.columns:
            df = df[df["volume"] >= 0]

        # 4. 고가 >= 시가,종가,저가 및 저가 <= 시가,종가,고가 정합성
        # 만약 고가나 저가가 논리적으로 맞지 않는 쓰레기 데이터라면 필터링
        logic_mask = (df["high"] >= df["open"]) & (df["high"] >= df["close"]) & (df["high"] >= df["low"]) & \
                     (df["low"] <= df["open"]) & (df["low"] <= df["close"])
        df = df[logic_mask]

        # 5. 인덱스(날짜) 중복 제거 및 시간순 정렬
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        cleaned_len = len(df)
        if initial_len != cleaned_len:
            logger.debug(f"[{symbol}] 이상 데이터 정제 완료: {initial_len} -> {cleaned_len}")

        return df

