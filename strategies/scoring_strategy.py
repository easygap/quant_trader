"""
멀티 지표 스코어링 전략
- RSI, MACD, 볼린저밴드, 거래량, 이동평균 점수 합산
"""

import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from core.signal_generator import SignalGenerator
from config.config_loader import Config


class ScoringStrategy(BaseStrategy):
    """
    멀티 지표 스코어링 전략

    각 기술 지표를 점수화하여 합산 점수로 매수/매도 결정:
    - 총점 ≥ buy_threshold (기본 2) → 매수
    - 총점 ≤ sell_threshold (기본 -2) → 매도
    - 히스터리시스 활성화 시 진입/청산 임계값 분리로 과매매 방지
    """

    def __init__(self, config: Config = None):
        super().__init__(
            name="scoring",
            description="멀티 지표 스코어링 전략 — 여러 지표를 점수화하여 매수/매도 결정",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.signal_generator = SignalGenerator(self.config)
        logger.info("ScoringStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """기술 지표 계산 후 신호 생성"""
        # 모든 지표 계산
        df = self.indicator_engine.calculate_all(df)
        # 신호 생성
        df = self.signal_generator.generate(df)
        if "total_score" in df.columns:
            df["strategy_score"] = df["total_score"]
        return df

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 매매 신호 반환"""
        df = self.analyze(df)
        return self.signal_generator.get_latest_signal(df)
