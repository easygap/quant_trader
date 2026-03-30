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
    """테스트용 DataFrame. IndicatorEngine 계산을 우회하고 직접 지표 주입."""
    dates = pd.bdate_range("2023-01-01", periods=n)
    c = np.array(close, dtype=float)
    df = pd.DataFrame({
        "open": c,
        "high": c + 100,
        "low": c - 100,
        "close": c,
        "volume": [5_000_000] * n,
        "sma_200": np.array(sma_200, dtype=float),
        "rsi": np.array(rsi, dtype=float),
        "adx": np.array(adx, dtype=float),
    }, index=dates)
    return df


class TestEdgeTrigger:
    """신호가 edge-triggered인지 검증."""

    def test_buy_fires_only_on_transition(self):
        """진입 조건이 연속 true여도 BUY는 전환 시 1회만."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 6
        close   = [60000, 60000, 60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000, 50000, 50000]  # close > sma_200 전체
        rsi     = [50,    50,    30,    30,    30,    50]      # RSI < 35: row 2,3,4
        adx     = [25,    25,    25,    25,    25,    25]      # ADX > 20 전체

        df = _make_df(n, close, rsi, adx, sma_200)
        # IndicatorEngine 계산을 우회하기 위해 analyze 내부 calculate_all을 모킹
        strat.indicator_engine.calculate_all = lambda d: d  # 이미 지표 있으므로 패스스루
        result = strat.analyze(df)

        signals = result["signal"].tolist()
        # row 2: 진입 조건 전환 → BUY
        # row 3,4: 이미 in_position → BUY 아님
        assert signals[2] == "BUY", f"row 2 should be BUY, got {signals[2]}"
        assert signals[3] == "HOLD", f"row 3 should be HOLD (already in position), got {signals[3]}"
        assert signals[4] == "HOLD", f"row 4 should be HOLD, got {signals[4]}"

    def test_sell_fires_only_on_transition(self):
        """청산 조건도 전환 시 1회만."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 6
        close   = [60000, 60000, 60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000, 50000, 50000]
        rsi     = [50,    30,    50,    75,    75,    50]  # entry at 1, exit at 3
        adx     = [25,    25,    25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        signals = result["signal"].tolist()
        assert signals[1] == "BUY"
        assert signals[3] == "SELL", f"row 3 should be SELL, got {signals[3]}"
        assert signals[4] == "HOLD", f"row 4 should be HOLD (already exited), got {signals[4]}"

    def test_no_sell_without_buy(self):
        """포지션 없으면 SELL 안 남."""
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 4
        close   = [60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000]
        rsi     = [50,    75,    75,    50]   # exit 조건 있지만 포지션 없음
        adx     = [25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        assert "SELL" not in result["signal"].tolist()


class TestTotalScore:
    """total_score 포트폴리오 호환 검증."""

    def test_total_score_on_buy(self):
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 4
        close   = [60000, 60000, 60000, 60000]
        sma_200 = [50000, 50000, 50000, 50000]
        rsi     = [50,    30,    50,    50]
        adx     = [25,    25,    25,    25]

        df = _make_df(n, close, rsi, adx, sma_200)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        assert result.loc[result["signal"] == "BUY", "total_score"].iloc[0] == 1.0
        assert (result.loc[result["signal"] == "HOLD", "total_score"] == 0.0).all()


class TestDebugColumns:
    """디버그 컬럼 존재 확인."""

    def test_debug_columns_present(self):
        from strategies.trend_pullback import TrendPullbackStrategy

        config = Config.get()
        strat = TrendPullbackStrategy(config)

        n = 3
        df = _make_df(n, [60000]*n, [50]*n, [25]*n, [50000]*n)
        strat.indicator_engine.calculate_all = lambda d: d
        result = strat.analyze(df)

        for col in ["entry_condition", "exit_condition", "entry_trigger", "exit_trigger"]:
            assert col in result.columns, f"missing debug column: {col}"
