"""
전략 추상 클래스
- 모든 매매 전략의 기반 인터페이스
"""

from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """
    매매 전략 추상 클래스

    모든 전략은 이 클래스를 상속하여 구현합니다:
        - analyze(): 데이터를 분석하여 지표를 계산
        - generate_signal(): 매매 신호를 생성
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description

    @abstractmethod
    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        주가 데이터를 분석하여 필요한 지표들과 전략 신호를 추가.
        반환 DataFrame은 최소한 `signal` 컬럼(BUY/SELL/HOLD)을 포함해야 합니다.

        Args:
            df: OHLCV 데이터프레임

        Returns:
            지표 및 signal 컬럼이 추가된 DataFrame
        """
        pass

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> dict:
        """
        최신 매매 신호를 생성

        Args:
            df: 분석된 데이터프레임

        Returns:
            {"signal": "BUY"/"SELL"/"HOLD", "score": 점수, "details": 상세}
        """
        pass

    def __repr__(self):
        return f"<Strategy({self.name})>"
