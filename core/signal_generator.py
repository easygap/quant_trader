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

    # 스코어링 가중치 필수 키 (YAML에 반드시 정의, 코드 내 기본값 없음)
    REQUIRED_WEIGHT_KEYS = (
        "rsi_oversold", "rsi_overbought",
        "macd_golden_cross", "macd_dead_cross",
        "bollinger_lower", "bollinger_upper",
        "volume_surge", "ma_golden_cross", "ma_dead_cross",
    )

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.strategy_params = self.config.strategies
        self.indicator_params = self.config.indicators
        logger.info("SignalGenerator 초기화 완료")

    def _get_weights(self) -> dict:
        """스코어링 가중치 dict 반환. 미설정 또는 필수 키 누락 시 KeyError."""
        weights = (self.strategy_params.get("scoring") or {}).get("weights")
        if not weights:
            raise KeyError(
                "config/strategies.yaml에 scoring.weights 섹션이 없습니다. "
                "가중치를 완전히 외부화하려면 해당 섹션을 정의하세요."
            )
        missing = [k for k in self.REQUIRED_WEIGHT_KEYS if k not in weights]
        if missing:
            raise KeyError(
                f"scoring.weights에 필수 키가 없습니다: {missing}. "
                "strategies.yaml의 scoring.weights를 확인하세요."
            )
        return weights

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        멀티 지표 스코어링 방식으로 매매 신호 생성.

        다중공선성 완화: RSI/MACD/볼린저/MA는 모두 "가격 강도"를 다르게 표현하므로
        합산하지 않고, 매수 방향 점수(양수 중 최대) + 매도 방향 점수(음수 중 최대)만
        사용해 동일 신호의 중복 가산을 막는다. 거래량은 독립 정보로 그대로 가산.

        Args:
            df: 기술 지표가 계산된 DataFrame

        Returns:
            신호 컬럼(signal, score, score_price_group, score_volume 등)이 추가된 DataFrame
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

        # 다중공선성 완화: 가격 강도 그룹(RSI/MACD/볼린저/MA)은 같은 정보를 반복하므로
        # 방향별로 최대 1개만 반영. 매수 쪽은 양수 중 최대, 매도 쪽은 음수 중 최대만 사용.
        price_columns = ["score_rsi", "score_macd", "score_bollinger", "score_ma"]
        price_df = result[price_columns]
        buy_side = price_df.clip(lower=0).max(axis=1)   # 양수만 취해 최대
        sell_side = price_df.clip(upper=0).min(axis=1)  # 음수만 취해 최소
        result["score_price_group"] = buy_side + sell_side

        # 총점 = 가격 그룹 점수(중복 제거) + 거래량(독립 정보)
        result["total_score"] = result["score_price_group"] + result["score_volume"]

        # 매수/매도 임계값
        scoring = self.strategy_params.get("scoring", {})
        buy_threshold = scoring.get("buy_threshold", 3)
        sell_threshold = scoring.get("sell_threshold", -3)

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
                "가격그룹": last.get("score_price_group", 0),  # RSI/MACD/볼린저/MA 방향별 최대 1개
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
            "atr": last.get("atr", 0),
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

        weights = self._get_weights()
        buy_weight = weights["rsi_oversold"]
        sell_weight = weights["rsi_overbought"]

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
        weights = self._get_weights()
        buy_weight = weights["macd_golden_cross"]
        sell_weight = weights["macd_dead_cross"]

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
        weights = self._get_weights()
        buy_weight = weights["bollinger_lower"]
        sell_weight = weights["bollinger_upper"]

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
        weight = self._get_weights()["volume_surge"]

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
        weights = self._get_weights()
        buy_weight = weights["ma_golden_cross"]
        sell_weight = weights["ma_dead_cross"]

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
