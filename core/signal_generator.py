"""
매매 신호 생성기
- 기술 지표 DataFrame을 입력받아 매수/매도/홀드 신호 생성
- 멀티 지표 스코어링 시스템
"""

import pandas as pd
import numpy as np
from loguru import logger

from config.config_loader import Config


class SignalGenerator:
    """
    매매 신호 생성기

    사용법:
        generator = SignalGenerator()
        signals = generator.generate(indicator_dataframe)
    """

    # 신호 상수
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.strategy_params = self.config.strategies
        self.indicator_params = self.config.indicators
        logger.info("SignalGenerator 초기화 완료")

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        멀티 지표 스코어링 방식으로 매매 신호 생성

        Args:
            df: 기술 지표가 계산된 DataFrame

        Returns:
            신호 컬럼(signal, score, score_details)이 추가된 DataFrame
        """
        if df.empty:
            return df

        result = df.copy()

        # 각 개별 점수 계산
        result["score_rsi"] = self._score_rsi(result)
        result["score_macd"] = self._score_macd(result)
        result["score_bollinger"] = self._score_bollinger(result)
        result["score_volume"] = self._score_volume(result)
        result["score_ma"] = self._score_ma(result)

        # 총점 합산
        score_columns = [
            "score_rsi", "score_macd", "score_bollinger",
            "score_volume", "score_ma"
        ]
        result["total_score"] = result[score_columns].sum(axis=1)

        # 매수/매도 임계값
        scoring = self.strategy_params.get("scoring", {})
        buy_threshold = scoring.get("buy_threshold", 5)
        sell_threshold = scoring.get("sell_threshold", -4)

        # 신호 생성
        result["signal"] = self.HOLD
        result.loc[result["total_score"] >= buy_threshold, "signal"] = self.BUY
        result.loc[result["total_score"] <= sell_threshold, "signal"] = self.SELL

        logger.info(
            "신호 생성 완료 — 매수: {}건, 매도: {}건, 홀드: {}건",
            (result["signal"] == self.BUY).sum(),
            (result["signal"] == self.SELL).sum(),
            (result["signal"] == self.HOLD).sum(),
        )

        return result

    def get_latest_signal(self, df: pd.DataFrame) -> dict:
        """
        최신(마지막 행) 신호 정보 반환

        Returns:
            {
                "signal": "BUY" / "SELL" / "HOLD",
                "score": 총점,
                "details": 개별 점수 딕셔너리,
                "date": 날짜,
                "close": 종가,
            }
        """
        if df.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}

        last = df.iloc[-1]

        return {
            "signal": last.get("signal", self.HOLD),
            "score": last.get("total_score", 0),
            "details": {
                "RSI": last.get("score_rsi", 0),
                "MACD": last.get("score_macd", 0),
                "볼린저": last.get("score_bollinger", 0),
                "거래량": last.get("score_volume", 0),
                "이동평균": last.get("score_ma", 0),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "rsi": last.get("rsi", 0),
            "adx": last.get("adx", 0),
        }

    # =============================================================
    # 개별 지표 점수 계산
    # =============================================================

    def _score_rsi(self, df: pd.DataFrame) -> pd.Series:
        """
        RSI 점수 계산
        - RSI < 30 (과매도) → +2점
        - RSI > 70 (과매수) → -2점
        - 중간 → 0점
        """
        rsi_params = self.indicator_params.get("rsi", {})
        oversold = rsi_params.get("oversold", 30)
        overbought = rsi_params.get("overbought", 70)

        weights = self.strategy_params.get("scoring", {}).get("weights", {})
        buy_weight = weights.get("rsi_oversold", 2)
        sell_weight = weights.get("rsi_overbought", -2)

        score = pd.Series(0.0, index=df.index)

        if "rsi" in df.columns:
            score = score.where(~(df["rsi"] < oversold), buy_weight)
            score = score.where(~(df["rsi"] > overbought), sell_weight)

        return score

    def _score_macd(self, df: pd.DataFrame) -> pd.Series:
        """
        MACD 점수 계산
        - MACD > Signal 이고 이전에 MACD < Signal (골든크로스) → +2점
        - MACD < Signal 이고 이전에 MACD > Signal (데드크로스) → -2점
        """
        weights = self.strategy_params.get("scoring", {}).get("weights", {})
        buy_weight = weights.get("macd_golden_cross", 2)
        sell_weight = weights.get("macd_dead_cross", -2)

        score = pd.Series(0.0, index=df.index)

        if "macd" in df.columns and "macd_signal" in df.columns:
            # 골든크로스: MACD가 시그널선을 상향 돌파
            macd_above = df["macd"] > df["macd_signal"]
            golden_cross = macd_above & (~macd_above.shift(1).fillna(False))

            # 데드크로스: MACD가 시그널선을 하향 돌파
            dead_cross = (~macd_above) & macd_above.shift(1).fillna(False)

            score[golden_cross] = buy_weight
            score[dead_cross] = sell_weight

            # 히스토그램 방향 보너스 (약한 신호)
            if "macd_histogram" in df.columns:
                hist_positive = df["macd_histogram"] > 0
                hist_turning_up = (
                    df["macd_histogram"] > df["macd_histogram"].shift(1)
                ) & hist_positive
                hist_turning_down = (
                    df["macd_histogram"] < df["macd_histogram"].shift(1)
                ) & (~hist_positive)

                # 골든/데드크로스 없는 날에만 약한 보너스
                no_cross = (score == 0)
                score = score.where(~(no_cross & hist_turning_up), 0.5)
                score = score.where(~(no_cross & hist_turning_down), -0.5)

        return score

    def _score_bollinger(self, df: pd.DataFrame) -> pd.Series:
        """
        볼린저 밴드 점수 계산
        - 종가 < 하단 밴드 → +1점 (과매도)
        - 종가 > 상단 밴드 → -1점 (과매수)
        """
        weights = self.strategy_params.get("scoring", {}).get("weights", {})
        buy_weight = weights.get("bollinger_lower", 1)
        sell_weight = weights.get("bollinger_upper", -1)

        score = pd.Series(0.0, index=df.index)

        if "bb_lower" in df.columns and "bb_upper" in df.columns:
            score = score.where(~(df["close"] < df["bb_lower"]), buy_weight)
            score = score.where(~(df["close"] > df["bb_upper"]), sell_weight)

        return score

    def _score_volume(self, df: pd.DataFrame) -> pd.Series:
        """
        거래량 점수 계산
        - 거래량이 평균 대비 150% 이상이면 추세 확인 신호
        - 가격 상승 + 거래량 급증 → +1점
        - 가격 하락 + 거래량 급증 → -1점
        """
        surge_ratio = self.indicator_params.get("volume", {}).get("surge_ratio", 1.5)
        weight = self.strategy_params.get("scoring", {}).get("weights", {}).get("volume_surge", 1)

        score = pd.Series(0.0, index=df.index)

        if "volume_ratio" in df.columns:
            volume_surge = df["volume_ratio"] > surge_ratio
            price_up = df["close"] > df["close"].shift(1)

            score = score.where(~(volume_surge & price_up), weight)
            score = score.where(~(volume_surge & ~price_up), -weight)

        return score

    def _score_ma(self, df: pd.DataFrame) -> pd.Series:
        """
        이동평균 점수 계산
        - 5일선이 20일선을 상향 돌파 (골든크로스) → +1점
        - 5일선이 20일선을 하향 돌파 (데드크로스) → -1점
        """
        weights = self.strategy_params.get("scoring", {}).get("weights", {})
        buy_weight = weights.get("ma_golden_cross", 1)
        sell_weight = weights.get("ma_dead_cross", -1)

        score = pd.Series(0.0, index=df.index)

        sma_short = None
        sma_mid = None

        # SMA 컬럼 찾기
        for col in df.columns:
            if col.startswith("sma_5") or col.startswith("ema_5"):
                sma_short = col
            if col.startswith("sma_20") or col.startswith("ema_20"):
                sma_mid = col

        if sma_short and sma_mid and sma_short in df.columns and sma_mid in df.columns:
            short_above = df[sma_short] > df[sma_mid]
            golden_cross = short_above & (~short_above.shift(1).fillna(False))
            dead_cross = (~short_above) & short_above.shift(1).fillna(False)

            score[golden_cross] = buy_weight
            score[dead_cross] = sell_weight

        return score
