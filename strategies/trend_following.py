"""
추세 추종 전략
- 강한 추세 방향으로 따라가는 전략
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from config.config_loader import Config


class TrendFollowingStrategy(BaseStrategy):
    """
    추세 추종 전략 (중급)

    조건:
    1. ADX > 25 → 강한 추세 존재
    2. 가격 > 200일선 → 상승 추세
    3. MACD 골든크로스 → 매수 진입
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="trend_following",
            description="ADX + 200일선 + MACD 추세 추종 전략",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("trend_following", {})
        logger.info("TrendFollowingStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """모든 지표 계산 + 전략 signal 컬럼 추가"""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        adx_threshold = self.params.get("adx_threshold", 25)

        adx = analyzed.get("adx", pd.Series(np.nan, index=analyzed.index))
        close = analyzed.get("close", pd.Series(np.nan, index=analyzed.index))
        sma_200 = analyzed.get("sma_200", pd.Series(np.nan, index=analyzed.index))
        macd = analyzed.get("macd", pd.Series(np.nan, index=analyzed.index))
        macd_signal = analyzed.get("macd_signal", pd.Series(np.nan, index=analyzed.index))

        has_trend = adx > adx_threshold
        above_200 = sma_200.notna() & (sma_200 > 0) & (close > sma_200)
        below_200 = sma_200.notna() & (sma_200 > 0) & (close < sma_200)

        macd_golden = (
            macd.notna() & macd_signal.notna()
            & (macd > macd_signal)
            & (macd.shift(1) <= macd_signal.shift(1))
        )
        macd_dead = (
            macd.notna() & macd_signal.notna()
            & (macd < macd_signal)
            & (macd.shift(1) >= macd_signal.shift(1))
        )

        analyzed["strategy_score"] = (
            has_trend.astype(int)
            + above_200.astype(int)
            + (macd_golden.astype(int) * 2)
        ).astype(float)

        analyzed["signal"] = self.HOLD
        analyzed.loc[(has_trend & above_200 & macd_golden).fillna(False), "signal"] = self.BUY
        analyzed.loc[(macd_dead | (below_200 & has_trend)).fillna(False), "signal"] = self.SELL
        analyzed.loc[analyzed["signal"] == self.SELL, "strategy_score"] *= -1

        return analyzed

    def generate_signal(self, df: pd.DataFrame) -> dict:
        """추세 추종 신호 생성"""
        analyzed = self.analyze(df)

        if analyzed.empty or len(analyzed) < 200:
            return {"signal": self.HOLD, "score": 0, "details": {"이유": "데이터 부족(200일 필요)"}}

        last = analyzed.iloc[-1]
        prev = analyzed.iloc[-2] if len(analyzed) >= 2 else last

        adx = last.get("adx", 0)
        close = last.get("close", 0)
        sma_200 = last.get("sma_200", 0)
        macd = last.get("macd", 0)
        macd_signal = last.get("macd_signal", 0)
        prev_macd = prev.get("macd", 0)
        prev_signal = prev.get("macd_signal", 0)

        adx_threshold = self.params.get("adx_threshold", 25)

        signal = last.get("signal", self.HOLD)
        score = last.get("strategy_score", 0)
        reasons = []

        # 조건 1: 강한 추세 존재
        has_trend = pd.notna(adx) and adx > adx_threshold
        if has_trend:
            reasons.append(f"ADX={adx:.1f} > {adx_threshold}")
            score += 1

        # 조건 2: 상승 추세 (200일선 위)
        above_200 = pd.notna(sma_200) and sma_200 > 0 and close > sma_200
        if above_200:
            reasons.append("종가 > 200일선")
            score += 1

        # 조건 3: MACD 골든크로스
        macd_golden = (
            pd.notna(macd) and pd.notna(macd_signal) and
            macd > macd_signal and prev_macd <= prev_signal
        )
        if macd_golden:
            reasons.append("MACD 골든크로스")
            score += 2

        below_200 = pd.notna(sma_200) and sma_200 > 0 and close < sma_200
        macd_dead = (
            pd.notna(macd) and pd.notna(macd_signal) and
            macd < macd_signal and prev_macd >= prev_signal
        )
        if macd_dead:
            reasons.append("MACD 데드크로스")
        if below_200:
            reasons.append("종가 < 200일선")

        return {
            "signal": signal,
            "score": score,
            "details": {
                "ADX": round(adx, 2) if pd.notna(adx) else 0,
                "종가": close,
                "200일선": round(sma_200, 0) if pd.notna(sma_200) else 0,
                "MACD": round(macd, 2) if pd.notna(macd) else 0,
                "조건": ", ".join(reasons) if reasons else "없음",
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": close,
            "atr": last.get("atr", 0),
        }
