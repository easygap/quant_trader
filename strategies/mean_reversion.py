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
        """지표 계산 + Z-Score + 전략 signal 컬럼 추가"""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        lookback = self.params.get("lookback_period", 20)
        z_buy = self.params.get("z_score_buy", -2.0)
        z_sell = self.params.get("z_score_sell", 2.0)
        adx_filter = self.params.get("adx_filter", 20)
        volume_spike_filter = self.params.get("volume_spike_filter", 3.0)

        analyzed["z_mean"] = analyzed["close"].rolling(window=lookback).mean()
        analyzed["z_std"] = analyzed["close"].rolling(window=lookback).std()
        analyzed["z_score"] = (analyzed["close"] - analyzed["z_mean"]) / analyzed["z_std"]
        analyzed["strategy_score"] = analyzed["z_score"].fillna(0.0)

        adx_ok = analyzed.get("adx", pd.Series(np.nan, index=analyzed.index)) < adx_filter
        rsi_buy = analyzed.get("rsi", pd.Series(np.nan, index=analyzed.index)) < 40
        rsi_sell = analyzed.get("rsi", pd.Series(np.nan, index=analyzed.index)) > 60
        volume_ratio = analyzed.get("volume_ratio", pd.Series(np.nan, index=analyzed.index))

        buy_signal = adx_ok & (analyzed["z_score"] <= z_buy) & rsi_buy
        sell_signal = adx_ok & (analyzed["z_score"] >= z_sell) & rsi_sell

        # 거래량 급변 시 평균회귀 매수는 차단
        buy_signal = buy_signal & ~((volume_ratio > volume_spike_filter).fillna(False))

        analyzed["signal"] = self.HOLD
        analyzed.loc[buy_signal.fillna(False), "signal"] = self.BUY
        analyzed.loc[sell_signal.fillna(False), "signal"] = self.SELL

        return analyzed

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """Z-Score 기반 신호 생성"""
        analyzed = self.analyze(df)

        if analyzed.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}

        last = analyzed.iloc[-1]
        z_score = last.get("z_score", 0)
        adx = last.get("adx", 50)
        rsi = last.get("rsi", 50)

        adx_filter = self.params.get("adx_filter", 20)
        volume_ratio = last.get("volume_ratio")
        signal = last.get("signal", self.HOLD)
        score = last.get("strategy_score", z_score)

        return {
            "signal": signal,
            "score": round(score, 2),
            "details": {
                "Z-Score": round(z_score, 2),
                "ADX": round(adx, 2) if pd.notna(adx) else 0,
                "RSI": round(rsi, 2) if pd.notna(rsi) else 0,
                "ADX필터": f"< {adx_filter}",
                "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
