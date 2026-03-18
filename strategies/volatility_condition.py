"""
변동성 조건 전략
- 실현변동성(과거 N일 수익률 표준편차)만 사용. 기술적 지표·모멘텀과 정보 소스 분리.
- 저변동성 구간에서 매수, 고변동성 구간에서 매도(리스크 축소) 또는 역으로 설정 가능.
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from config.config_loader import Config


class VolatilityConditionStrategy(BaseStrategy):
    """
    변동성 조건 전략 (정보 소스: 실현변동성만)

    - 60일 실현변동성(연율화)이 low_vol_max 이하 → 매수 (저변동성 = 유리한 환경)
    - 60일 실현변동성이 high_vol_min 이상 → 매도 (고변동성 = 리스크 오프)
    - 그 외 HOLD
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="volatility_condition",
            description="변동성 조건 — 실현변동성 구간만 사용, 기술·모멘텀과 독립",
        )
        self.config = config or Config.get()
        self.params = self.config.strategies.get("volatility_condition", {})

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """N일 실현변동성(연율화) 계산 후 신호 부여"""
        result = df.copy()
        if result.empty or len(result) < 2:
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            return result

        lookback = max(5, int(self.params.get("lookback_days", 60)))
        low_max = float(self.params.get("low_vol_max_pct", 25.0))   # 연율화 변동성 % 이하 → 매수
        high_min = float(self.params.get("high_vol_min_pct", 45.0)) # 연율화 변동성 % 이상 → 매도

        close = result["close"].astype(float)
        ret = close.pct_change().dropna()
        # 롤링 표준편차 * sqrt(252) = 연율화 변동성 (% 단위로 하려면 *100)
        vol = ret.rolling(lookback, min_periods=min(10, lookback)).std() * np.sqrt(252) * 100
        result["realized_vol_pct"] = vol

        # 스코어: 낮은 변동성 = 양수, 높은 변동성 = 음수 (정규화 -1~1 수준)
        mid = (low_max + high_min) / 2
        result["strategy_score"] = np.clip((mid - vol) / max(mid, 1), -2, 2)

        result["signal"] = self.HOLD
        result.loc[vol <= low_max, "signal"] = self.BUY
        result.loc[vol >= high_min, "signal"] = self.SELL
        return result

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 변동성 조건 신호 반환"""
        analyzed = self.analyze(df)
        if analyzed.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}
        last = analyzed.iloc[-1]
        vol = last.get("realized_vol_pct")
        signal = last.get("signal", self.HOLD)
        return {
            "signal": signal,
            "score": round(last.get("strategy_score", 0), 2),
            "details": {"실현변동성(연%, N일)": round(vol, 2) if pd.notna(vol) else None},
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
            "date": last.name if hasattr(last, "name") else None,
        }
