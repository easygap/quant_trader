"""
추세 눌림목(Trend Pullback) 전략

장기 상승추세 종목에서 단기 과매도 눌림 구간에 진입.
- 진입: close > SMA200 AND RSI < rsi_entry AND ADX > adx_min
- 청산: close < SMA200 OR RSI > rsi_exit
- ATR trailing stop은 backtester 기존 로직에 위임.

신호는 edge-triggered: 조건 "전환" 시에만 BUY/SELL 발생.
"""

import pandas as pd
from loguru import logger

from core.indicator_engine import IndicatorEngine
from strategies.base_strategy import BaseStrategy


class TrendPullbackStrategy(BaseStrategy):

    def __init__(self, config):
        super().__init__(name="trend_pullback", description="추세 눌림목 진입 전략")
        self.config = config
        self.params = (config.strategies or {}).get("trend_pullback", {})
        self.indicator_engine = IndicatorEngine(config=config)

        # 진입 파라미터
        self.rsi_entry = self.params.get("rsi_entry", 35)
        self.adx_min = self.params.get("adx_min", 20)
        # 청산 파라미터
        self.rsi_exit = self.params.get("rsi_exit", 70)

        logger.info(
            "TrendPullbackStrategy 초기화 완료 "
            "(rsi_entry={}, adx_min={}, rsi_exit={})",
            self.rsi_entry, self.adx_min, self.rsi_exit,
        )

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # 지표 계산 (IndicatorEngine 100% 재사용)
        df = self.indicator_engine.calculate_all(df)

        # 필수 컬럼 존재 확인
        required = ["sma_200", "rsi", "adx"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            df["signal"] = "HOLD"
            df["total_score"] = 0.0
            return df

        # ── 진입/청산 조건 (bool) ──
        entry_cond = (
            (df["close"] > df["sma_200"])
            & (df["rsi"] < self.rsi_entry)
            & (df["adx"] > self.adx_min)
        )
        exit_cond = (
            (df["close"] < df["sma_200"])
            | (df["rsi"] > self.rsi_exit)
        )

        # ── Edge-trigger: 전일 false → 당일 true 전환만 신호 ──
        entry_prev = entry_cond.shift(1, fill_value=False)
        exit_prev = exit_cond.shift(1, fill_value=False)

        entry_trigger = entry_cond & ~entry_prev   # 진입 전환
        exit_trigger = exit_cond & ~exit_prev       # 청산 전환

        # ── 상태 머신: BUY → HOLD/SELL 순서 강제 ──
        signals = pd.Series("HOLD", index=df.index)
        in_position = False

        for i in range(len(df)):
            if not in_position:
                if entry_trigger.iloc[i]:
                    signals.iloc[i] = "BUY"
                    in_position = True
            else:
                if exit_trigger.iloc[i]:
                    signals.iloc[i] = "SELL"
                    in_position = False

        df["signal"] = signals

        # ── Portfolio 호환: total_score ──
        # BUY 신호 시 1.0, SELL 시 -1.0, 그 외 0.0
        df["total_score"] = 0.0
        df.loc[df["signal"] == "BUY", "total_score"] = 1.0
        df.loc[df["signal"] == "SELL", "total_score"] = -1.0

        # ── 디버그 컬럼 ──
        df["entry_condition"] = entry_cond
        df["exit_condition"] = exit_cond
        df["entry_trigger"] = entry_trigger
        df["exit_trigger"] = exit_trigger

        return df

    def generate_signal(self, df: pd.DataFrame) -> dict:
        result = self.analyze(df)
        if result.empty:
            return {"signal": "HOLD", "score": 0.0, "details": {}}

        last = result.iloc[-1]
        return {
            "signal": last.get("signal", "HOLD"),
            "score": float(last.get("total_score", 0.0)),
            "details": {
                "rsi": float(last.get("rsi", 0)),
                "adx": float(last.get("adx", 0)),
                "close_vs_sma200": float(last["close"] - last["sma_200"]) if "sma_200" in last.index else 0,
                "entry_condition": bool(last.get("entry_condition", False)),
                "exit_condition": bool(last.get("exit_condition", False)),
            },
        }
