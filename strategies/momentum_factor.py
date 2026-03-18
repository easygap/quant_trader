"""
모멘텀 팩터 전략
- 가격 모멘텀(과거 N일 수익률)만 사용. 기술적 지표(RSI/MACD 등)와 정보 소스 분리.
- 학술적 모멘텀 효과: "좋은 주식이 일정 기간 계속 좋다"에 기반.
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from config.config_loader import Config


class MomentumFactorStrategy(BaseStrategy):
    """
    모멘텀 팩터 전략 (정보 소스: 가격 수익률만)

    - lookback 일 수익률 > buy_threshold(%) → 매수
    - lookback 일 수익률 < sell_threshold(%) → 매도
    - 그 외 HOLD
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="momentum_factor",
            description="모멘텀 팩터 — 과거 N일 수익률 기반, 기술지표와 독립",
        )
        self.config = config or Config.get()
        self.params = self.config.strategies.get("momentum_factor", {})

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """lookback 일 수익률 계산 후 신호 부여"""
        result = df.copy()
        if result.empty or len(result) < 2:
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            return result

        lookback = max(2, int(self.params.get("lookback_days", 20)))
        buy_th = float(self.params.get("buy_threshold_pct", 2.0))
        sell_th = float(self.params.get("sell_threshold_pct", -2.0))

        close = result["close"].astype(float)
        ret = (close / close.shift(lookback) - 1) * 100  # N일 수익률 %

        result["momentum_return"] = ret
        result["strategy_score"] = ret.fillna(0) / 10.0  # 스케일 (대략 -3~+3)

        signal = self.HOLD
        result["signal"] = self.HOLD
        result.loc[ret >= buy_th, "signal"] = self.BUY
        result.loc[ret <= sell_th, "signal"] = self.SELL
        return result

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 모멘텀 신호 반환"""
        analyzed = self.analyze(df)
        if analyzed.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}
        last = analyzed.iloc[-1]
        mom = last.get("momentum_return", 0)
        signal = last.get("signal", self.HOLD)
        return {
            "signal": signal,
            "score": round(last.get("strategy_score", 0), 2),
            "details": {"모멘텀(N일수익률%)": round(mom, 2) if pd.notna(mom) else None},
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
            "date": last.name if hasattr(last, "name") else None,
        }
