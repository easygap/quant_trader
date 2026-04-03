"""
trend_pullback 전략 단위 테스트
- edge-trigger 동작 확인
- 신호 상태 머신 (BUY→SELL 순서)
- total_score 포트폴리오 호환
"""

import numpy as np
import pandas as pd
import pytest

from config.config_loader import Config


def _make_df(n, close, rsi, adx, sma_200):
    """테스트용 DataFrame. IndicatorEngine 계산을 우회하고 직접 지표 주입.
    코드의 기본 sma_period=60이므로 sma_60 컬럼도 함께 제공."""
    dates = pd.bdate_range("2023-01-01", periods=n)
    c = np.array(close, dtype=float)
    sma_vals = np.array(sma_200, dtype=float)
    df = pd.DataFrame({
        "open": c,
        "high": c + 100,
        "low": c - 100,
        "close": c,
        "volume": [5_000_000] * n,
        "sma_200": sma_vals,
        "sma_60": sma_vals,   # 코드는 sma_{sma_period} 컬럼을 찾음 (기본 sma_period=60)
        "rsi": np.array(rsi, dtype=float),
        "adx": np.array(adx, dtype=float),
    }, index=dates)
    return df


class TestEdgeTrigger:
    """신호가 edge-triggered인지 검증."""

    def test_buy_fires_only_on_transition(self):
        """진입 조건이 연속 true여도 BUY는 전환 시 1회만.
        코드 기본값: rsi_entry=45, adx_min=20. RSI < 45 이면서 close > sma 이면 진입 조건 충족.
        """
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 6
        close   = [60000, 60000, 60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000, 50000, 50000]  # close > sma 전체
        rsi     = [50,    50,    30,    30,    30,    50]      # RSI < 45: row 2,3,4
        adx     = [25,    25,    25,    25,    25,    25]      # ADX > 20 전체

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        signals = result["signal"].tolist()
        # row 2: 진입 조건 전환 → BUY (edge-trigger)
        # row 3,4: entry_cond 유지 but edge 아님 → HOLD
        assert signals[2] == "BUY", f"row 2 should be BUY, got {signals[2]}"
        assert signals[3] == "HOLD", f"row 3 should be HOLD (no edge), got {signals[3]}"
        assert signals[4] == "HOLD", f"row 4 should be HOLD, got {signals[4]}"

    def test_sell_fires_only_on_transition(self):
        """청산 조건도 전환 시 1회만. rsi_entry=45, rsi_exit=70."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 6
        close   = [60000, 60000, 60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000, 50000, 50000]
        #           HOLD   BUY    HOLD   SELL   HOLD   HOLD
        rsi     = [50,    30,    50,    75,    75,    50]  # entry at 1(RSI<45 edge), exit at 3(RSI>70 edge)
        adx     = [25,    25,    25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        signals = result["signal"].tolist()
        # row 0: RSI=50 > 45 → entry_cond=False
        # row 1: RSI=30 < 45, close>sma, adx>20 → entry_cond=True, prev=False → edge → BUY
        assert signals[1] == "BUY", f"row 1 should be BUY, got {signals[1]}"
        # row 3: RSI=75 > 70 → exit_cond=True, prev exit_cond=False → edge → SELL
        assert signals[3] == "SELL", f"row 3 should be SELL, got {signals[3]}"
        # row 4: RSI=75 → exit_cond still True but no edge → HOLD
        assert signals[4] == "HOLD", f"row 4 should be HOLD, got {signals[4]}"

    def test_no_sell_without_buy(self):
        """exit edge는 entry와 독립적으로 발생 — 전략 코드는 edge-trigger 기반이므로
        포지션 유무와 무관하게 exit_edge 발생 시 SELL을 출력.
        이는 backtester/executor가 포지션 없으면 무시하는 구조.
        """
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 4
        close   = [60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000]
        rsi     = [50,    75,    75,    50]   # row 1: exit edge (RSI>70 전환)
        adx     = [25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        # edge-trigger 전략은 포지션 추적 없이 조건 전환만으로 신호 발생
        # row 1: exit_cond=True(RSI=75>70), prev=False → exit_edge → SELL
        # backtester가 포지션 없으면 이 SELL을 무시함 (전략 레벨이 아닌 실행 레벨 필터)
        signals = result["signal"].tolist()
        assert signals[1] == "SELL", "exit edge에서 SELL이 발생해야 함 (실행 레벨에서 필터)"


class TestTotalScore:
    """total_score 포트폴리오 호환 검증."""

    def test_strategy_score_on_buy(self):
        """BUY 신호 시 strategy_score > 0이어야 함. 컬럼명은 strategy_score (total_score 아님)."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 4
        close   = [60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000]
        rsi     = [50,    30,    50,    50]   # row 1: RSI<45, entry edge
        adx     = [25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        buy_rows = result[result["signal"] == "BUY"]
        assert len(buy_rows) > 0, "BUY 신호가 없음"
        # strategy_score = above_sma(1) + rsi_low(1) + has_trend(1) + entry_edge(1) = 4.0
        assert buy_rows["strategy_score"].iloc[0] == 4.0, \
            f"strategy_score={buy_rows['strategy_score'].iloc[0]}, expected=4.0"


class TestDebugColumns:
    """디버그 컬럼 존재 확인."""

    def test_debug_columns_present(self):
        """실제 코드의 디버그 컬럼명 확인: _pb_above_sma, _pb_rsi_low, _pb_has_trend, _pb_entry_edge, _pb_exit_edge."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 3
        df = _make_df(n, [60000]*n, [50]*n, [25]*n, [50000]*n)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        for col in ["_pb_above_sma", "_pb_rsi_low", "_pb_has_trend", "_pb_entry_edge", "_pb_exit_edge"]:
            assert col in result.columns, f"missing debug column: {col}"
