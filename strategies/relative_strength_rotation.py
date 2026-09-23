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
from strategies.index_cache import IndexCloseCache
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
        # KS11 > SMA 시장 필터: 날짜별 통과 여부(T-1 기준). NA = 판단 불가(데이터·SMA 없음)
        self._mf_series = None
        self._mf_cache: IndexCloseCache | None = None
        self._mf_cache_period = None
        self._mf_series_key = None
        # 시장 필터 상태 — 조회 실패 시 ok=False. 연구 리포트가 '필터 미적용' 실행을 걸러낼 수 있게 남긴다.
        self.market_filter_status: dict = {"ok": None, "reason": None}
        # 벤치마크 지수 캐시: (지수, 워밍업 일수) → IndexCloseCache
        self._benchmark_index_caches: dict[tuple[str, int], IndexCloseCache] = {}
        # 파생 복합 모멘텀 캐시: (지수, short, long, weight) → (캐시 version, 시리즈)
        self._benchmark_composite_cache: dict[tuple, tuple[int, pd.Series]] = {}
        logger.info("RelativeStrengthRotationStrategy 초기화 완료")

    def _ensure_market_filter(self, dates_index):
        """KS11 > SMA(N) 시장 필터 계산 (지수는 인스턴스당 넓게 한 번 받아 캐시).

        예전에는 첫 analyze 호출의 날짜 범위로 한 번만 받아 캐시했다. strict 백테스트의
        첫 호출은 df.iloc[:1]이라 1일치 상태가 전 기간에 ffill되어 필터가 한 번도
        작동하지 않았다. 이제 넓은 구간을 받아 두고 필터 시리즈를 그 전체로 한 번
        계산한 뒤, analyze()가 요청 끝 날짜 이하로 잘라 쓴다. 값은 전일 종가·전일 SMA로만
        정해지므로(T-1) 넓게 계산해도 각 날짜의 값은 그날 이전 데이터만 반영한다.
        """
        if len(dates_index) == 0:
            return
        mf_period = int(self.params.get("market_filter_ma_period", 200))
        if self._mf_cache is None or self._mf_cache_period != mf_period:
            # 거래일 mf_period개를 확보하려면 달력일로 약 1.7배 + 여유가 필요하다.
            self._mf_cache = IndexCloseCache(
                "KS11", warmup_days=int(mf_period * 1.7) + 30, label="market_filter",
            )
            self._mf_cache_period = mf_period
            self._mf_series_key = None

        closes = self._mf_cache.ensure(dates_index.min(), dates_index.max())
        key = (mf_period, self._mf_cache.version)
        if key == self._mf_series_key:
            return
        self._mf_series_key = key

        if closes is None or len(closes) < mf_period:
            self._mf_series = None
            reason = self._mf_cache.last_error or (
                f"KS11 데이터 부족({0 if closes is None else len(closes)}/{mf_period})"
            )
            self.market_filter_status = {"ok": False, "reason": reason}
            logger.warning(
                "market_filter: {} — 필터가 적용되지 않습니다 "
                "(market_filter_active=False로 기록, 이 실행은 필터 없는 결과)",
                reason,
            )
            return

        sma = closes.rolling(mf_period, min_periods=mf_period).mean()
        # SMA가 아직 없는 날은 '아래'가 아니라 '판단 불가'(NA)로 둔다.
        above = (closes > sma).astype("boolean")
        above[sma.isna()] = pd.NA
        # T-1 기준: 전일 종가 > 전일 SMA → 당일 신규 진입 허용
        self._mf_series = above.shift(1)
        self.market_filter_status = {"ok": True, "reason": None}

    def _benchmark_composite(
        self,
        index: pd.Index,
        short_lb: int,
        long_lb: int,
        short_w: float,
        benchmark_symbol: str,
    ) -> pd.Series:
        """Return benchmark composite momentum aligned to the input index.

        예전에는 (시작, 끝) 날짜를 캐시 키로 써서 strict 백테스트가 봉마다 지수를 새로
        받았다. 이제 지수를 인스턴스당 넓게 한 번 받아 복합 모멘텀을 한 번 계산하고,
        호출의 끝 날짜 이하로 잘라 맞춘다. 조회 실패는 IndexCloseCache가 사유와 함께
        경고하고, 여기서는 NaN(진입 차단)을 준다.
        """
        if len(index) == 0:
            return pd.Series(dtype=float, index=index)

        dates = pd.to_datetime(index)
        margin_days = max(int(long_lb) * 3, 180)
        index_key = (benchmark_symbol, margin_days)
        cache = self._benchmark_index_caches.get(index_key)
        if cache is None:
            cache = IndexCloseCache(
                benchmark_symbol, warmup_days=margin_days, label="benchmark-aware rotation",
            )
            self._benchmark_index_caches[index_key] = cache

        closes = cache.ensure(dates.min(), dates.max())
        if closes is None or closes.empty:
            return pd.Series(np.nan, index=index)

        cache_key = (benchmark_symbol, int(short_lb), int(long_lb), float(short_w))
        cached = self._benchmark_composite_cache.get(cache_key)
        if cached is None or cached[0] != cache.version:
            ret_short = closes.pct_change(short_lb)
            ret_long = closes.pct_change(long_lb)
            benchmark_composite = short_w * ret_short + (1.0 - short_w) * ret_long
            cached = (cache.version, benchmark_composite)
            self._benchmark_composite_cache[cache_key] = cached
        # 호출 끝 날짜 이후 봉은 잘라 낸다 (strict 백테스트에서 벤치마크 미래 정보 차단)
        aligned = cached[1].loc[: dates.max()].reindex(dates, method="ffill")
        return pd.Series(aligned.to_numpy(), index=index)

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
        score_mode = str(self.params.get("score_mode", "absolute")).lower().strip()
        rank_entry_mode = str(
            self.params.get("rank_entry_mode", "absolute_trend")
        ).lower().strip()
        use_positive_momentum_filter = bool(
            self.params.get("use_positive_momentum_filter", True)
        )
        use_trend_filter = bool(self.params.get("use_trend_filter", True))
        exit_trend_edge_enabled = bool(self.params.get("exit_trend_edge", True))
        exit_rebalance_mode = str(
            self.params.get("exit_rebalance_mode", "absolute_trend")
        ).lower().strip()
        sell_score_floor_pct = float(self.params.get("sell_score_floor_pct", -8.0))

        close = analyzed["close"]

        # ── 모멘텀 수익률 ──
        ret_short = close.pct_change(short_lb)
        ret_long = close.pct_change(long_lb)

        # ── 추세 필터 ──
        sma = close.rolling(sma_period, min_periods=sma_period).mean()

        # ── 복합 모멘텀 점수 ──
        composite = short_w * ret_short + long_w * ret_long
        score_composite = composite.copy()
        benchmark_composite = pd.Series(np.nan, index=analyzed.index)
        benchmark_excess = pd.Series(np.nan, index=analyzed.index)
        benchmark_score_available = pd.Series(True, index=analyzed.index)
        if score_mode == "benchmark_excess":
            benchmark_symbol = str(self.params.get("benchmark_symbol", "KS11"))
            benchmark_composite = self._benchmark_composite(
                analyzed.index,
                short_lb,
                long_lb,
                short_w,
                benchmark_symbol,
            )
            benchmark_excess = composite - benchmark_composite
            score_composite = benchmark_excess
            benchmark_score_available = benchmark_excess.notna()
        elif score_mode != "absolute":
            logger.warning(
                "relative_strength_rotation: unknown score_mode={} fallback to absolute",
                score_mode,
            )
            score_mode = "absolute"

        # ── 리밸런싱일 (매월 첫 거래일) ──
        months = analyzed.index.to_series().dt.to_period("M")
        rebalance = (months != months.shift(1)).fillna(False)
        rebalance.iloc[:long_lb] = False  # 수익률 히스토리 확보

        # ── 조건 ──
        above_trend = sma.notna() & (close > sma)
        positive_momentum = composite.notna() & (composite > 0)

        # Entry: 기본은 기존 절대 모멘텀+추세, 연구 후보는 dense ranking 허용.
        if rank_entry_mode == "dense_ranked":
            entry_cond = rebalance & score_composite.notna()
            if use_positive_momentum_filter:
                entry_cond = entry_cond & positive_momentum
            if use_trend_filter:
                entry_cond = entry_cond & above_trend
        else:
            entry_cond = rebalance & positive_momentum & above_trend
        if score_mode == "benchmark_excess":
            entry_cond = entry_cond & benchmark_score_available
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
        # 판단 불가(지수 조회 실패·SMA 미형성) 날은 진입을 막지도, 청산을 강제하지도 않고
        # market_filter_active=False로 기록한다 — 필터가 켜진 설정인데 실제로는 적용되지
        # 않은 실행을 연구 리포트가 걸러낼 수 있게.
        if self.params.get("market_filter_sma200", False):
            self._ensure_market_filter(analyzed.index)
            market_filter_active = pd.Series(False, index=analyzed.index)
            if self._mf_series is not None:
                mf = self._mf_series.loc[: analyzed.index.max()]
                mf_aligned = mf.reindex(analyzed.index, method="ffill").astype("boolean")
                market_filter_active = mf_aligned.notna().astype(bool)
                market_filter_pass = mf_aligned.fillna(True).astype(bool)
                entry_cond = entry_cond & market_filter_pass
                analyzed["market_filter_pass"] = market_filter_pass
            else:
                analyzed["market_filter_pass"] = True
            analyzed["market_filter_active"] = market_filter_active
        market_filter_exit = (
            self.params.get("market_filter_sma200", False)
            and self.params.get("market_filter_exit", False)
            and ~market_filter_pass
        )
        analyzed["market_filter_exit"] = market_filter_exit

        # Exit (리밸런싱): 기본은 기존 절대 모멘텀/추세, 연구 후보는 score floor 허용.
        if exit_rebalance_mode == "score_floor":
            exit_rebalance = (
                rebalance
                & (
                    score_composite.isna()
                    | ((score_composite * 100) <= sell_score_floor_pct)
                )
            )
        elif exit_rebalance_mode == "none":
            exit_rebalance = pd.Series(False, index=analyzed.index)
        else:
            exit_rebalance = rebalance & (~positive_momentum | ~above_trend)

        # Exit (비리밸런싱): SMA 하향 이탈 edge-trigger
        prev_above = above_trend.shift(1, fill_value=True)
        if exit_trend_edge_enabled:
            exit_trend_edge = ~rebalance & ~above_trend & prev_above
        else:
            exit_trend_edge = pd.Series(False, index=analyzed.index)

        exit_cond = exit_rebalance | exit_trend_edge | market_filter_exit

        # ── 디버그 컬럼 ──
        analyzed["ret_60d"] = ret_short
        analyzed["ret_120d"] = ret_long
        analyzed["composite_score"] = composite
        analyzed["score_mode"] = score_mode
        analyzed["ranking_score"] = score_composite
        analyzed["benchmark_composite_score"] = benchmark_composite
        analyzed["benchmark_excess_score"] = benchmark_excess
        analyzed["sma_trend"] = sma
        analyzed["rebalance_day"] = rebalance
        analyzed["above_trend"] = above_trend

        # ── Score ──
        raw_score = score_composite.fillna(0) * 100  # 백분율
        analyzed["strategy_score"] = raw_score

        # total_score: signal_scaling [2,5] 호환.
        # composite 0% → 3.0, 10% → 4.0, 20% → 5.0, -10% → 2.0
        analyzed["total_score"] = (3.0 + score_composite.fillna(0) * 10).clip(
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
                "ranking_score": round(last.get("ranking_score", 0), 4),
                "score_mode": last.get("score_mode", "absolute"),
                "benchmark_composite_score": round(
                    last.get("benchmark_composite_score", 0), 4
                ),
                "benchmark_excess_score": round(
                    last.get("benchmark_excess_score", 0), 4
                ),
                "ret_60d": round(last.get("ret_60d", 0), 4),
                "ret_120d": round(last.get("ret_120d", 0), 4),
                "rebalance_day": bool(last.get("rebalance_day", False)),
                "above_trend": bool(last.get("above_trend", False)),
                "market_filter_pass": bool(last.get("market_filter_pass", True)),
                "market_filter_active": bool(last.get("market_filter_active", False)),
                "market_filter_exit": bool(last.get("market_filter_exit", False)),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
