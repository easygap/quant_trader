"""
모멘텀 팩터 전략
- 가격 모멘텀(과거 N일 수익률)만 사용. 기술적 지표(RSI/MACD 등)와 정보 소스 분리.
- 학술적 모멘텀 효과: "좋은 주식이 일정 기간 계속 좋다"에 기반.
"""

import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy
from strategies.index_cache import IndexCloseCache
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
        # 지수 종가 캐시: (지수, 워밍업 일수) → IndexCloseCache
        self._benchmark_index_caches: dict[tuple[str, int], IndexCloseCache] = {}
        # 파생 수익률 캐시: (지수, lookback) → (캐시 version, 수익률 시리즈)
        self._benchmark_return_cache: dict[tuple[str, int], tuple[int, pd.Series]] = {}

    def _benchmark_return(
        self,
        index: pd.Index,
        lookback: int,
        benchmark_symbol: str,
    ) -> pd.Series:
        """Return benchmark N-day momentum aligned to the input index.

        예전에는 (시작, 끝) 날짜를 캐시 키로 써서 strict 백테스트(봉마다 df.iloc[:i+1])가
        봉마다 지수를 새로 받았다(호출마다 새 DataCollector). 이제 지수를 인스턴스당
        넓게 한 번 받아 수익률을 한 번 계산하고, 호출의 끝 날짜 이하로 잘라 맞춘다.
        조회 실패는 IndexCloseCache가 사유와 함께 경고하고, 여기서는 NaN(매수 없음)을 준다.
        """
        if len(index) == 0:
            return pd.Series(dtype=float, index=index)

        lookback = int(lookback)
        dates = pd.to_datetime(index)
        margin_days = max(lookback * 3, 120)
        cache_key = (benchmark_symbol, margin_days)
        cache = self._benchmark_index_caches.get(cache_key)
        if cache is None:
            cache = IndexCloseCache(
                benchmark_symbol, warmup_days=margin_days, label="benchmark-relative momentum",
            )
            self._benchmark_index_caches[cache_key] = cache

        closes = cache.ensure(dates.min(), dates.max())
        if closes is None or closes.empty:
            return pd.Series(np.nan, index=index)

        ret_key = (benchmark_symbol, lookback)
        cached = self._benchmark_return_cache.get(ret_key)
        if cached is None or cached[0] != cache.version:
            benchmark_return = (closes / closes.shift(lookback) - 1) * 100
            cached = (cache.version, benchmark_return)
            self._benchmark_return_cache[ret_key] = cached
        # 호출 끝 날짜 이후 봉은 잘라 낸다 (strict 백테스트에서 벤치마크 미래 정보 차단)
        aligned = cached[1].loc[: dates.max()].reindex(dates, method="ffill")
        return pd.Series(aligned.to_numpy(), index=index)

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
        benchmark_relative = bool(self.params.get("benchmark_relative", False))

        close = result["close"].astype(float)
        ret = (close / close.shift(lookback) - 1) * 100  # N일 수익률 %
        signal_metric = ret

        result["momentum_return"] = ret
        if benchmark_relative:
            benchmark_symbol = str(self.params.get("benchmark_symbol", "KS11"))
            benchmark_return = self._benchmark_return(result.index, lookback, benchmark_symbol)
            excess_return = ret - benchmark_return
            vol_lookback = max(5, int(self.params.get("volatility_lookback_days", lookback)))
            realized_vol = (
                close.pct_change()
                .rolling(vol_lookback, min_periods=min(10, vol_lookback))
                .std()
                * np.sqrt(252)
                * 100
            )
            score_scale = (realized_vol / 20.0).clip(lower=1.0).fillna(1.0)

            result["benchmark_return"] = benchmark_return
            result["benchmark_excess_return"] = excess_return
            result["realized_vol_pct"] = realized_vol
            signal_metric = excess_return
            result["strategy_score"] = (excess_return / score_scale).fillna(0)
        else:
            result["strategy_score"] = ret.fillna(0) / 10.0  # 스케일 (대략 -3~+3)

        signal = self.HOLD
        result["signal"] = self.HOLD
        buy_mask = signal_metric >= buy_th
        sell_mask = signal_metric <= sell_th
        if benchmark_relative and self.params.get("max_realized_vol_pct") is not None:
            max_vol = float(self.params["max_realized_vol_pct"])
            buy_mask = buy_mask & (result["realized_vol_pct"] <= max_vol)
            if bool(self.params.get("sell_on_high_vol", False)):
                sell_mask = sell_mask | (result["realized_vol_pct"] > max_vol)
        result.loc[buy_mask.fillna(False), "signal"] = self.BUY
        result.loc[sell_mask.fillna(False), "signal"] = self.SELL
        return result

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """최신 모멘텀 신호 반환"""
        analyzed = self.analyze(df)
        if analyzed.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}
        last = analyzed.iloc[-1]
        mom = last.get("momentum_return", 0)
        signal = last.get("signal", self.HOLD)
        details = {"모멘텀(N일수익률%)": round(mom, 2) if pd.notna(mom) else None}
        if self.params.get("benchmark_relative", False):
            details.update({
                "벤치마크수익률(%)": round(last.get("benchmark_return", 0), 2)
                if pd.notna(last.get("benchmark_return"))
                else None,
                "초과모멘텀(%)": round(last.get("benchmark_excess_return", 0), 2)
                if pd.notna(last.get("benchmark_excess_return"))
                else None,
                "실현변동성(연%)": round(last.get("realized_vol_pct", 0), 2)
                if pd.notna(last.get("realized_vol_pct"))
                else None,
            })
        return {
            "signal": signal,
            "score": round(last.get("strategy_score", 0), 2),
            "details": details,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
            "date": last.name if hasattr(last, "name") else None,
        }
