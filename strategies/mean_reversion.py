"""
평균 회귀 전략
- Z-Score 기반 가격 이탈 시 되돌아오는 특성 활용
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from config.config_loader import Config


class MeanReversionStrategy(BaseStrategy):
    """
    평균 회귀 전략 (중급)

    - Z-Score < -2 → 과도한 하락 → 매수
    - Z-Score > +2 → 과도한 상승 → 매도
    - ADX < 20 일 때만 활성화 (횡보장에서 유효)
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="mean_reversion",
            description="Z-Score 기반 평균 회귀 전략 — 횡보장에서 유효",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("mean_reversion", {})
        logger.info("MeanReversionStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """지표 계산 + Z-Score 추가"""
        df = self.indicator_engine.calculate_all(df)

        lookback = self.params.get("lookback_period", 20)
        df["z_mean"] = df["close"].rolling(window=lookback).mean()
        df["z_std"] = df["close"].rolling(window=lookback).std()
        df["z_score"] = (df["close"] - df["z_mean"]) / df["z_std"]

        return df

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """Z-Score 기반 신호 생성"""
        df = self.analyze(df)

        if df.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}

        last = df.iloc[-1]
        z_score = last.get("z_score", 0)
        adx = last.get("adx", 50)
        rsi = last.get("rsi", 50)

        z_buy = self.params.get("z_score_buy", -2.0)
        z_sell = self.params.get("z_score_sell", 2.0)
        adx_filter = self.params.get("adx_filter", 20)

        signal = self.HOLD
        score = z_score

        # ADX 필터: 추세가 약할 때만 평균 회귀 유효
        if pd.notna(adx) and adx < adx_filter:
            if z_score <= z_buy and rsi < 40:
                signal = self.BUY
            elif z_score >= z_sell and rsi > 60:
                signal = self.SELL

        return {
            "signal": signal,
            "score": round(score, 2),
            "details": {
                "Z-Score": round(z_score, 2),
                "ADX": round(adx, 2) if pd.notna(adx) else 0,
                "RSI": round(rsi, 2) if pd.notna(rsi) else 0,
                "ADX필터": f"< {adx_filter}",
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
        }
