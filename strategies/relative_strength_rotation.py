"""
상대강도 회전 전략 (Relative Strength Rotation) — C-5 MVP

목표: 유니버스 내 모멘텀 상위 종목을 월간 회전 보유하여
      breakout_volume의 SIGNAL_SPARSE 구간을 보완.

Entry (monthly rebalance, long only):
  1. 리밸런싱일 (매월 첫 거래일)
  2. composite_score = 0.6 * ret_60d + 0.4 * ret_120d > 0
  3. close > SMA(60)  (추세 필터)
  → 조건 충족 시 BUY. total_score = composite 기반 랭킹.
     portfolio_backtester가 total_score 상위 max_positions개 선택.

Exit:
  - 리밸런싱일: 모멘텀 음수 또는 SMA 하회 → SELL
  - 비리밸런싱일: close가 SMA60 하향 이탈 시 edge-trigger SELL
  - 나머지: 기존 backtester risk layer (ATR trailing stop 등) 위임

Look-ahead 방지:
  - ret_60d/ret_120d: pct_change(N) → 과거 N일 수익률. 미래 미참조.
  - SMA: rolling mean → 현재까지만 참조.
  - 리밸런싱 판정: 전일과 당일의 월 비교 → 미래 미참조.
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from config.config_loader import Config


class RelativeStrengthRotationStrategy(BaseStrategy):
    """상대강도 회전 전략 (C-5)"""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="relative_strength_rotation",
            description="상대강도 상위 종목 월간 회전 보유",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("relative_strength_rotation", {})
        self._mf_series = None  # KS11 > SMA200 market filter cache
        logger.info("RelativeStrengthRotationStrategy 초기화 완료")

    def _ensure_market_filter(self, dates_index):
        """KS11 > SMA(200) 시장 필터 사전 계산 (lazy cache, 인스턴스당 1회)."""
        if self._mf_series is not None:
            return
        try:
            from core.data_collector import DataCollector

            mf_period = self.params.get("market_filter_ma_period", 200)
            collector = DataCollector()
            first = dates_index.min()
            last = dates_index.max()
            margin = mf_period + 100
            start = (first - pd.Timedelta(days=margin)).strftime("%Y-%m-%d")
            end = last.strftime("%Y-%m-%d")
            ks11 = collector.fetch_korean_stock(
                "KS11", start_date=start, end_date=end
            )
            if ks11 is None or ks11.empty or len(ks11) < mf_period:
                logger.warning(
                    "market_filter: KS11 데이터 부족({}/{}) — 필터 비활성화 fallback",
                    len(ks11) if ks11 is not None else 0,
                    mf_period,
                )
                return
            close_ks = ks11["close"].astype(float)
            sma = close_ks.rolling(mf_period, min_periods=mf_period).mean()
            # T-1 기준: 전일 종가 > 전일 SMA200 → 당일 신규 진입 허용
            self._mf_series = (close_ks > sma).shift(1, fill_value=True).astype(bool)
        except Exception as e:
            logger.warning("market_filter: KS11 로드 실패 — 필터 비활성화: {}", e)

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """모멘텀 지표 계산 + 월간 리밸런싱 signal 생성."""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        # 파라미터
        short_lb = self.params.get("short_lookback", 60)
        long_lb = self.params.get("long_lookback", 120)
        sma_period = self.params.get("sma_period", 60)
        short_w = self.params.get("short_weight", 0.6)
        long_w = 1.0 - short_w

        close = analyzed["close"]

        # ── 모멘텀 수익률 ──
        ret_short = close.pct_change(short_lb)
        ret_long = close.pct_change(long_lb)

        # ── 추세 필터 ──
        sma = close.rolling(sma_period, min_periods=sma_period).mean()

        # ── 복합 모멘텀 점수 ──
        composite = short_w * ret_short + long_w * ret_long

        # ── 리밸런싱일 (매월 첫 거래일) ──
        months = analyzed.index.to_series().dt.to_period("M")
        rebalance = (months != months.shift(1)).fillna(False)
        rebalance.iloc[:long_lb] = False  # 수익률 히스토리 확보

        # ── 조건 ──
        above_trend = sma.notna() & (close > sma)
        positive_momentum = composite.notna() & (composite > 0)

        # Entry: 리밸런싱 + 양수 모멘텀 + 추세 유지
        entry_cond = rebalance & positive_momentum & above_trend
        market_filter_pass = pd.Series(True, index=analyzed.index)

        # ── 종목 절대모멘텀 필터 (T-1 기준) ──
        abs_mom = self.params.get("abs_momentum_filter", "none")
        if abs_mom == "A":
            # 전일 120일 수익률 > 0
            abs_pass = (ret_long.shift(1) > 0).fillna(False)
            entry_cond = entry_cond & abs_pass
            analyzed["abs_mom_pass"] = abs_pass
        elif abs_mom == "B":
            # 전일 60일 AND 120일 수익률 모두 > 0
            abs_pass = (
                (ret_short.shift(1) > 0) & (ret_long.shift(1) > 0)
            ).fillna(False)
            entry_cond = entry_cond & abs_pass
            analyzed["abs_mom_pass"] = abs_pass

        # ── 시장 필터: KS11 > SMA200 (T-1 기준) ──
        if self.params.get("market_filter_sma200", False):
            self._ensure_market_filter(analyzed.index)
            if self._mf_series is not None:
                mf_aligned = self._mf_series.reindex(analyzed.index, method="ffill")
                market_filter_pass = mf_aligned.astype("boolean").fillna(True).astype(bool)
                entry_cond = entry_cond & market_filter_pass
                analyzed["market_filter_pass"] = market_filter_pass
            else:
                analyzed["market_filter_pass"] = True
        market_filter_exit = (
            self.params.get("market_filter_sma200", False)
            and self.params.get("market_filter_exit", False)
            and ~market_filter_pass
        )
        analyzed["market_filter_exit"] = market_filter_exit

        # Exit (리밸런싱): 모멘텀 음수 또는 추세 붕괴
        exit_rebalance = rebalance & (~positive_momentum | ~above_trend)

        # Exit (비리밸런싱): SMA 하향 이탈 edge-trigger
        prev_above = above_trend.shift(1, fill_value=True)
        exit_trend_edge = ~rebalance & ~above_trend & prev_above

        exit_cond = exit_rebalance | exit_trend_edge | market_filter_exit

        # ── 디버그 컬럼 ──
        analyzed["ret_60d"] = ret_short
        analyzed["ret_120d"] = ret_long
        analyzed["composite_score"] = composite
        analyzed["sma_trend"] = sma
        analyzed["rebalance_day"] = rebalance
        analyzed["above_trend"] = above_trend

        # ── Score ──
        raw_score = composite.fillna(0) * 100  # 백분율
        analyzed["strategy_score"] = raw_score

        # total_score: signal_scaling [2,5] 호환.
        # composite 0% → 3.0, 10% → 4.0, 20% → 5.0, -10% → 2.0
        analyzed["total_score"] = (3.0 + composite.fillna(0) * 10).clip(
            lower=2.0, upper=5.0
        )

        # ── 신호 생성 ──
        analyzed["signal"] = self.HOLD
        analyzed.loc[entry_cond.fillna(False), "signal"] = self.BUY
        analyzed.loc[exit_cond.fillna(False), "signal"] = self.SELL
        analyzed.loc[analyzed["signal"] == self.SELL, "total_score"] *= -1
        analyzed.loc[analyzed["signal"] == self.SELL, "strategy_score"] *= -1

        return analyzed

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 매매 신호 생성."""
        long_lb = self.params.get("long_lookback", 120)
        analyzed = self.analyze(df)

        if analyzed.empty or len(analyzed) < long_lb + 1:
            return {
                "signal": self.HOLD,
                "score": 0,
                "details": {"이유": f"데이터 부족({long_lb + 1}일 필요)"},
            }

        last = analyzed.iloc[-1]
        signal = last.get("signal", self.HOLD)
        score = last.get("total_score", 0)

        return {
            "signal": signal,
            "score": score,
            "details": {
                "composite_score": round(last.get("composite_score", 0), 4),
                "ret_60d": round(last.get("ret_60d", 0), 4),
                "ret_120d": round(last.get("ret_120d", 0), 4),
                "rebalance_day": bool(last.get("rebalance_day", False)),
                "above_trend": bool(last.get("above_trend", False)),
                "market_filter_pass": bool(last.get("market_filter_pass", True)),
                "market_filter_exit": bool(last.get("market_filter_exit", False)),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
