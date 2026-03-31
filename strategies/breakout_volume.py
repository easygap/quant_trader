"""
거래량 동반 돌파 전략 (Breakout + Volume) — C-4 MVP

가설: 전고점 돌파 + 거래량 급증이 동반되면 한국 대형주에서 유효한 모멘텀 시그널.

Entry (edge-trigger, long only):
  1. breakout_ref = rolling_max(high, breakout_period).shift(1)
  2. avg_vol_ref  = rolling_mean(volume, breakout_period).shift(1)
  3. close > breakout_ref
     AND volume > avg_vol_ref * surge_ratio
     AND ADX > adx_min
  → 전일 조건 미충족, 당일 충족 시에만 BUY (edge-trigger)

Exit:
  전략 레벨 최소 실패 신호만 사용: close < breakout_ref
  나머지 손절/트레일링은 기존 backtester risk layer (ATR 2.5) 위임.

Look-ahead 방지:
  breakout_ref, avg_vol_ref 모두 .shift(1) 적용 → 현재 봉의 high/volume 제외.
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from config.config_loader import Config


class BreakoutVolumeStrategy(BaseStrategy):
    """거래량 동반 돌파 전략 (C-4)"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="breakout_volume",
            description="전고점 돌파 + 거래량 급증 전략",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("breakout_volume", {})
        logger.info("BreakoutVolumeStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """모든 지표 계산 + edge-trigger 기반 signal 컬럼 추가."""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        # 파라미터
        breakout_period = self.params.get("breakout_period", 20)
        surge_ratio = self.params.get("surge_ratio", 2.0)
        adx_min = self.params.get("adx_min", 20)

        close = analyzed["close"]
        high = analyzed["high"]
        volume = analyzed["volume"]
        adx = analyzed.get("adx", pd.Series(np.nan, index=analyzed.index))

        # ── 커스텀 지표 (look-ahead 방지: shift(1)) ──
        # breakout_ref: 직전 breakout_period 봉의 고가 최대값 (현재 봉 제외)
        breakout_ref = high.rolling(window=breakout_period, min_periods=breakout_period).max().shift(1)
        # avg_vol_ref: 직전 breakout_period 봉의 평균 거래량 (현재 봉 제외)
        avg_vol_ref = volume.rolling(window=breakout_period, min_periods=breakout_period).mean().shift(1)

        # ── 조건 ──
        above_breakout = breakout_ref.notna() & (close > breakout_ref)
        volume_surge = avg_vol_ref.notna() & (avg_vol_ref > 0) & (volume > avg_vol_ref * surge_ratio)
        has_trend = adx.notna() & (adx > adx_min)

        # 복합 entry 조건
        entry_cond = above_breakout & volume_surge & has_trend

        # Edge-trigger: 전일 false → 당일 true
        entry_prev = entry_cond.shift(1, fill_value=False)
        entry_edge = entry_cond & (~entry_prev)

        # ── Exit: 최소 실패 신호 (close < breakout_ref) ──
        exit_cond = breakout_ref.notna() & (close < breakout_ref)
        exit_prev = exit_cond.shift(1, fill_value=False)
        exit_edge = exit_cond & (~exit_prev)

        # ── 디버그 컬럼 ──
        volume_surge_ratio = pd.Series(np.nan, index=analyzed.index)
        valid_vol = avg_vol_ref.notna() & (avg_vol_ref > 0)
        volume_surge_ratio[valid_vol] = volume[valid_vol] / avg_vol_ref[valid_vol]

        analyzed["breakout_ref"] = breakout_ref
        analyzed["avg_vol_ref"] = avg_vol_ref
        analyzed["volume_surge_ratio"] = volume_surge_ratio
        analyzed["entry_condition"] = entry_cond
        analyzed["exit_condition"] = exit_cond
        analyzed["entry_trigger"] = entry_edge
        analyzed["exit_trigger"] = exit_edge

        # ── strategy_score: 연속 ranking 점수 (동시 BUY 후보 정렬용) ──
        # 돌파 강도 + 거래량 급증도 + 추세 강도의 도메인 고정 스케일링 가중합.
        # entry 조건 자체는 변경하지 않음 — BUY trigger 발생 시에만 의미 있는 값.
        # 정규화: 각 요소를 실측 도메인 범위의 고정 상수로 나눠 [0, ~1.5]로 정규화.
        # look-ahead 없음: 스케일 상수는 하드코딩된 도메인 지식.
        #
        # breakout_strength: (close/breakout_ref - 1) 실측 0.005~0.10 → /0.05 → 0.1~2.0
        # volume_surge:      (vol/avg_vol - 1) 실측 0.5~4.0           → /3.0  → 0.17~1.33
        # trend (ADX):       (adx - adx_min) 실측 0~40                → /30   → 0.0~1.33
        breakout_strength = ((close / breakout_ref - 1).clip(lower=0) / 0.05).clip(upper=1.5)
        volume_strength = (((volume / avg_vol_ref) - 1).clip(lower=0) / 3.0).clip(upper=1.5)
        adx_safe = adx.fillna(0)
        trend_strength = ((adx_safe - adx_min) / 30.0).clip(lower=0, upper=1.5)

        analyzed["breakout_strength"] = breakout_strength
        analyzed["volume_strength"] = volume_strength
        analyzed["trend_strength"] = trend_strength

        # 가중치: volume(0.45) > breakout(0.35) > trend(0.20)
        # 이 전략의 핵심 엣지는 거래량 동반 돌파이므로 volume surge에 최대 가중.
        rank_score = (
            0.35 * breakout_strength
            + 0.45 * volume_strength
            + 0.20 * trend_strength
        )
        analyzed["strategy_score"] = rank_score

        # total_score: signal_scaling score_range [2,5]에 매핑.
        # rank_score 범위 [0, 1.5] → total_score [2.6, 5.0].
        # offset=2.6, scale=1.6 → median rank_score(~0.45)가 total_score ~3.3에 위치.
        # signal_scaling에서 3.3은 scale≈0.93 → 평균 신호에 근접한 neutral sizing 유지.
        # 포트폴리오 backtester가 total_score를 우선 읽어 ranking + sizing 양쪽에 사용.
        analyzed["total_score"] = 2.6 + 1.6 * rank_score

        # ── signal 생성 ──
        analyzed["signal"] = self.HOLD
        analyzed.loc[entry_edge.fillna(False), "signal"] = self.BUY
        analyzed.loc[exit_edge.fillna(False), "signal"] = self.SELL
        analyzed.loc[analyzed["signal"] == self.SELL, "total_score"] *= -1
        analyzed.loc[analyzed["signal"] == self.SELL, "strategy_score"] *= -1

        return analyzed

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 매매 신호 생성."""
        breakout_period = self.params.get("breakout_period", 20)
        analyzed = self.analyze(df)

        if analyzed.empty or len(analyzed) < breakout_period + 1:
            return {
                "signal": self.HOLD,
                "score": 0,
                "details": {"이유": f"데이터 부족({breakout_period + 1}일 필요)"},
            }

        last = analyzed.iloc[-1]
        signal = last.get("signal", self.HOLD)
        score = last.get("total_score", last.get("strategy_score", 0))

        return {
            "signal": signal,
            "score": score,
            "details": {
                "ADX": round(last.get("adx", 0), 2) if pd.notna(last.get("adx")) else 0,
                "breakout_ref": round(last.get("breakout_ref", 0), 0) if pd.notna(last.get("breakout_ref")) else 0,
                "avg_vol_ref": round(last.get("avg_vol_ref", 0), 0) if pd.notna(last.get("avg_vol_ref")) else 0,
                "volume_surge_ratio": round(last.get("volume_surge_ratio", 0), 2) if pd.notna(last.get("volume_surge_ratio")) else 0,
                "close": last.get("close", 0),
                "entry_trigger": bool(last.get("entry_trigger", False)),
                "exit_trigger": bool(last.get("exit_trigger", False)),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
