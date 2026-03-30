"""
눌림목(Trend Pullback) 전략 — C-3A 구조 재설계

중기 상승 추세 + 일시적 과매도 + 추세 강도 확인으로 진입.
SMA200 → SMA60, MACD → RSI 로 교체하여 entry 희소성 문제를 해결.

Entry (edge-trigger): close > SMA(sma_period) AND RSI < rsi_entry AND ADX > adx_min
Exit:  close < SMA(sma_period) OR RSI > rsi_exit OR 기존 ATR trailing stop
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from config.config_loader import Config


class TrendPullbackStrategy(BaseStrategy):
    """
    눌림목 전략 (C-3A)

    조건:
    1. close > SMA(sma_period) → 중기 상승 추세
    2. RSI < rsi_entry → 일시적 과매도 (눌림목)
    3. ADX > adx_min → 추세 존재 확인

    세 조건 동시 충족 시점에만 BUY (edge-trigger).
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="trend_pullback",
            description="SMA + RSI 눌림목 + ADX 추세 확인 전략",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("trend_pullback", {})
        logger.info("TrendPullbackStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """모든 지표 계산 + edge-trigger 기반 signal 컬럼 추가."""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        # 파라미터
        sma_period = self.params.get("sma_period", 60)
        rsi_entry = self.params.get("rsi_entry", 45)
        adx_min = self.params.get("adx_min", 20)
        rsi_exit = self.params.get("rsi_exit", 70)

        # 지표 추출
        close = analyzed.get("close", pd.Series(np.nan, index=analyzed.index))
        rsi = analyzed.get("rsi", pd.Series(np.nan, index=analyzed.index))
        adx = analyzed.get("adx", pd.Series(np.nan, index=analyzed.index))

        # SMA: indicator_engine이 sma_60을 이미 계산. 다른 period면 직접 계산.
        sma_col = f"sma_{sma_period}"
        if sma_col in analyzed.columns:
            sma = analyzed[sma_col]
        else:
            sma = close.rolling(window=sma_period, min_periods=sma_period).mean()
            analyzed[sma_col] = sma

        # ── 조건 ──
        above_sma = sma.notna() & (sma > 0) & (close > sma)
        rsi_low = rsi.notna() & (rsi < rsi_entry)
        has_trend = adx.notna() & (adx > adx_min)

        # 복합 entry 조건
        entry_cond = above_sma & rsi_low & has_trend

        # Edge-trigger: 이전 봉에서는 조건 미충족 → 현재 봉에서 충족
        entry_prev = entry_cond.shift(1, fill_value=False)
        entry_edge = entry_cond & (~entry_prev)

        # Exit 조건: close < SMA OR RSI > rsi_exit
        below_sma = sma.notna() & (sma > 0) & (close < sma)
        rsi_high = rsi.notna() & (rsi > rsi_exit)
        exit_cond = below_sma | rsi_high

        # Exit edge-trigger
        exit_prev = exit_cond.shift(1, fill_value=False)
        exit_edge = exit_cond & (~exit_prev)

        # ── strategy_score (디버그용, 0~4점) ──
        analyzed["strategy_score"] = (
            above_sma.astype(float)
            + rsi_low.astype(float)
            + has_trend.astype(float)
            + entry_edge.astype(float)  # edge 보너스
        )

        # ── 디버그 컬럼 ──
        analyzed["_pb_above_sma"] = above_sma
        analyzed["_pb_rsi_low"] = rsi_low
        analyzed["_pb_has_trend"] = has_trend
        analyzed["_pb_entry_edge"] = entry_edge
        analyzed["_pb_exit_edge"] = exit_edge

        # ── signal 생성 ──
        analyzed["signal"] = self.HOLD
        analyzed.loc[entry_edge.fillna(False), "signal"] = self.BUY
        analyzed.loc[exit_edge.fillna(False), "signal"] = self.SELL
        analyzed.loc[analyzed["signal"] == self.SELL, "strategy_score"] *= -1

        return analyzed

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 매매 신호 생성."""
        sma_period = self.params.get("sma_period", 60)
        analyzed = self.analyze(df)

        if analyzed.empty or len(analyzed) < sma_period:
            return {
                "signal": self.HOLD,
                "score": 0,
                "details": {"이유": f"데이터 부족({sma_period}일 필요)"},
            }

        last = analyzed.iloc[-1]
        signal = last.get("signal", self.HOLD)
        score = last.get("strategy_score", 0)

        return {
            "signal": signal,
            "score": score,
            "details": {
                "ADX": round(last.get("adx", 0), 2) if pd.notna(last.get("adx")) else 0,
                "RSI": round(last.get("rsi", 0), 2) if pd.notna(last.get("rsi")) else 0,
                f"SMA{sma_period}": round(last.get(f"sma_{sma_period}", 0), 0) if pd.notna(last.get(f"sma_{sma_period}")) else 0,
                "종가": last.get("close", 0),
                "above_sma": bool(last.get("_pb_above_sma", False)),
                "rsi_low": bool(last.get("_pb_rsi_low", False)),
                "has_trend": bool(last.get("_pb_has_trend", False)),
                "entry_edge": bool(last.get("_pb_entry_edge", False)),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
