"""
기술적 지표 계산 엔진
- pandas-ta 기반 모든 기술 지표 계산
- RSI, MACD, 볼린저 밴드, MA(SMA/EMA), 스토캐스틱, ADX, ATR, OBV
- 하나의 DataFrame에 모든 지표 컬럼을 추가하여 반환
"""

import pandas as pd
import numpy as np
from loguru import logger

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False
    logger.warning("pandas-ta가 설치되지 않았습니다: pip install pandas-ta")

from config.config_loader import Config


class IndicatorEngine:
    """
    기술적 지표 계산기

    사용법:
        engine = IndicatorEngine()
        df = engine.calculate_all(ohlcv_dataframe)
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.params = self.config.indicators
        logger.info("IndicatorEngine 초기화 완료")

    def calculate_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        모든 기술 지표를 계산하여 DataFrame에 추가

        Args:
            df: OHLCV 데이터프레임 (컬럼: open, high, low, close, volume)

        Returns:
            지표가 추가된 DataFrame
        """
        if df.empty or len(df) < 30:
            logger.warning("데이터가 부족합니다 (최소 30일 필요, 현재 {}일)", len(df))
            return df

        result = df.copy()

        # 각 지표 계산
        result = self.add_rsi(result)
        result = self.add_macd(result)
        result = self.add_bollinger_bands(result)
        result = self.add_moving_averages(result)
        result = self.add_stochastic(result)
        result = self.add_adx(result)
        result = self.add_atr(result)
        result = self.add_obv(result)
        result = self.add_volume_ratio(result)

        logger.info("모든 기술 지표 계산 완료 (총 {} 컬럼)", len(result.columns))
        return result

    # =============================================================
    # 개별 지표 계산
    # =============================================================

    def add_rsi(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        RSI (상대강도지수) 계산

        추가 컬럼: rsi
        """
        period = self.params.get("rsi", {}).get("period", 14)

        if HAS_PANDAS_TA:
            df["rsi"] = ta.rsi(df["close"], length=period)
        else:
            df["rsi"] = self._calc_rsi(df["close"], period)

        return df

    def add_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        MACD (이동평균 수렴·확산) 계산

        추가 컬럼: macd, macd_signal, macd_histogram
        """
        macd_params = self.params.get("macd", {})
        fast = macd_params.get("fast_period", 12)
        slow = macd_params.get("slow_period", 26)
        signal = macd_params.get("signal_period", 9)

        if HAS_PANDAS_TA:
            macd_result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
            if macd_result is not None and not macd_result.empty:
                df["macd"] = macd_result.iloc[:, 0]             # MACD선
                df["macd_histogram"] = macd_result.iloc[:, 1]   # 히스토그램
                df["macd_signal"] = macd_result.iloc[:, 2]      # 시그널선
        else:
            ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
            ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
            df["macd"] = ema_fast - ema_slow
            df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
            df["macd_histogram"] = df["macd"] - df["macd_signal"]

        return df

    def add_bollinger_bands(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        볼린저 밴드 계산

        추가 컬럼: bb_upper, bb_middle, bb_lower, bb_bandwidth, bb_percent
        """
        bb_params = self.params.get("bollinger", {})
        period = bb_params.get("period", 20)
        std_dev = bb_params.get("std_dev", 2.0)

        if HAS_PANDAS_TA:
            bb_result = ta.bbands(df["close"], length=period, std=std_dev)
            if bb_result is not None and not bb_result.empty:
                df["bb_lower"] = bb_result.iloc[:, 0]      # 하단
                df["bb_middle"] = bb_result.iloc[:, 1]      # 중심선
                df["bb_upper"] = bb_result.iloc[:, 2]       # 상단
                df["bb_bandwidth"] = bb_result.iloc[:, 3]   # 밴드폭
                df["bb_percent"] = bb_result.iloc[:, 4]     # %B
        else:
            df["bb_middle"] = df["close"].rolling(window=period).mean()
            rolling_std = df["close"].rolling(window=period).std()
            df["bb_upper"] = df["bb_middle"] + (rolling_std * std_dev)
            df["bb_lower"] = df["bb_middle"] - (rolling_std * std_dev)
            df["bb_bandwidth"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]
            df["bb_percent"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

        return df

    def add_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        이동평균선 (SMA, EMA) 계산

        추가 컬럼: sma_5, sma_20, sma_60, sma_200, ema_5, ema_20, ema_60
        """
        ma_params = self.params.get("moving_average", {})
        short = ma_params.get("short_period", 5)
        mid = ma_params.get("mid_period", 20)
        long_p = ma_params.get("long_period", 60)
        trend = ma_params.get("trend_period", 200)

        # SMA (단순 이동평균)
        df[f"sma_{short}"] = df["close"].rolling(window=short).mean()
        df[f"sma_{mid}"] = df["close"].rolling(window=mid).mean()
        df[f"sma_{long_p}"] = df["close"].rolling(window=long_p).mean()
        if len(df) >= trend:
            df[f"sma_{trend}"] = df["close"].rolling(window=trend).mean()

        # EMA (지수 이동평균) — 반응 속도가 빠름
        df[f"ema_{short}"] = df["close"].ewm(span=short, adjust=False).mean()
        df[f"ema_{mid}"] = df["close"].ewm(span=mid, adjust=False).mean()
        df[f"ema_{long_p}"] = df["close"].ewm(span=long_p, adjust=False).mean()

        return df

    def add_stochastic(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        스토캐스틱 오실레이터 계산

        추가 컬럼: stoch_k, stoch_d
        """
        stoch_params = self.params.get("stochastic", {})
        k_period = stoch_params.get("k_period", 5)
        d_period = stoch_params.get("d_period", 3)
        smooth = stoch_params.get("smooth", 3)

        if HAS_PANDAS_TA:
            stoch_result = ta.stoch(
                df["high"], df["low"], df["close"],
                k=k_period, d=d_period, smooth_k=smooth
            )
            if stoch_result is not None and not stoch_result.empty:
                df["stoch_k"] = stoch_result.iloc[:, 0]
                df["stoch_d"] = stoch_result.iloc[:, 1]
        else:
            low_min = df["low"].rolling(window=k_period).min()
            high_max = df["high"].rolling(window=k_period).max()
            df["stoch_k"] = ((df["close"] - low_min) / (high_max - low_min)) * 100
            df["stoch_k"] = df["stoch_k"].rolling(window=smooth).mean()
            df["stoch_d"] = df["stoch_k"].rolling(window=d_period).mean()

        return df

    def add_adx(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        ADX (평균 방향 지수) 계산

        추가 컬럼: adx, di_plus, di_minus
        """
        period = self.params.get("adx", {}).get("period", 14)

        if HAS_PANDAS_TA:
            adx_result = ta.adx(df["high"], df["low"], df["close"], length=period)
            if adx_result is not None and not adx_result.empty:
                df["adx"] = adx_result.iloc[:, 0]        # ADX
                df["di_plus"] = adx_result.iloc[:, 1]    # +DI
                df["di_minus"] = adx_result.iloc[:, 2]   # -DI
        else:
            df["adx"] = self._calc_adx(df, period)

        return df

    def add_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        ATR (평균 실질 범위) 계산

        추가 컬럼: atr
        """
        period = self.params.get("atr", {}).get("period", 14)

        if HAS_PANDAS_TA:
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=period)
        else:
            high_low = df["high"] - df["low"]
            high_close = (df["high"] - df["close"].shift(1)).abs()
            low_close = (df["low"] - df["close"].shift(1)).abs()
            true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df["atr"] = true_range.rolling(window=period).mean()

        return df

    def add_obv(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OBV (On Balance Volume) 계산

        추가 컬럼: obv
        """
        if HAS_PANDAS_TA:
            df["obv"] = ta.obv(df["close"], df["volume"])
        else:
            obv = [0]
            for i in range(1, len(df)):
                if df["close"].iloc[i] > df["close"].iloc[i - 1]:
                    obv.append(obv[-1] + df["volume"].iloc[i])
                elif df["close"].iloc[i] < df["close"].iloc[i - 1]:
                    obv.append(obv[-1] - df["volume"].iloc[i])
                else:
                    obv.append(obv[-1])
            df["obv"] = obv

        return df

    def add_volume_ratio(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        거래량 비율 계산 (현재 거래량 / 평균 거래량)

        추가 컬럼: volume_ratio
        """
        avg_period = self.params.get("volume", {}).get("avg_period", 20)
        df["volume_avg"] = df["volume"].rolling(window=avg_period).mean()
        df["volume_ratio"] = df["volume"] / df["volume_avg"]

        return df

    # =============================================================
    # pandas-ta 없을 때 대체 계산
    # =============================================================

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """RSI 수동 계산"""
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()

        # Wilder's smoothing
        for i in range(period, len(series)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def _calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ADX 수동 계산 (간략 버전)"""
        high = df["high"]
        low = df["low"]
        close = df["close"]

        plus_dm = high.diff()
        minus_dm = -low.diff()

        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

        high_low = high - low
        high_close = (high - close.shift(1)).abs()
        low_close = (low - close.shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

        atr = tr.rolling(window=period).mean()
        plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)

        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
        adx = dx.rolling(window=period).mean()

        return adx
