"""SignalGenerator 단위 테스트 — 가중치 필수 시 KeyError"""
import pytest
import pandas as pd

from core.signal_generator import SignalGenerator


class _MockConfigWeightsMissing:
    strategies = {"scoring": {}}  # weights 없음
    indicators = {}


class _MockConfigWeightsComplete:
    strategies = {
        "scoring": {
            "weights": {
                "rsi_oversold": 2,
                "rsi_overbought": -2,
                "macd_golden_cross": 2,
                "macd_dead_cross": -2,
                "bollinger_lower": 1,
                "bollinger_upper": -1,
                "volume_surge": 1,
                "ma_golden_cross": 1,
                "ma_dead_cross": -1,
            }
        }
    }
    indicators = {"rsi": {"oversold": 30, "overbought": 70}}


def test_weights_missing_raises():
    """scoring.weights 없으면 KeyError"""
    gen = SignalGenerator(_MockConfigWeightsMissing())
    with pytest.raises(KeyError, match="scoring.weights"):
        gen._get_weights()


def test_weights_complete_returns_dict():
    """필수 키 모두 있으면 dict 반환"""
    gen = SignalGenerator(_MockConfigWeightsComplete())
    w = gen._get_weights()
    assert w["rsi_oversold"] == 2
    assert w["volume_surge"] == 1


# ── MACD 점수 체계 회귀 테스트 ──

import numpy as np


def _make_macd_df(n=10):
    """MACD > Signal 유지 상태 DataFrame (크로스 없음)."""
    dates = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({
        "macd": [1.0] * n,           # 항상 signal 위
        "macd_signal": [0.5] * n,
        "macd_histogram": [0.5] * n,
        "close": [10000.0] * n,
    }, index=dates)


def _make_macd_cross_df():
    """골든크로스 → 유지 → 데드크로스 시나리오."""
    dates = pd.bdate_range("2024-01-01", periods=6)
    return pd.DataFrame({
        # day0: below, day1: cross up, day2-3: maintain, day4: cross down, day5: below
        "macd":        [-1.0,  1.0,  1.5,  1.2, -0.5, -1.0],
        "macd_signal": [ 0.0,  0.0,  0.0,  0.0,  0.0,  0.0],
        "macd_histogram": [-1.0,  1.0,  1.5,  1.2, -0.5, -1.0],
        "close": [10000.0] * 6,
    }, index=dates)


class TestMacdScoreThreeTier:
    """MACD 3단계 점수 체계 회귀 테스트 (현행 동작 보호)."""

    def test_maintenance_score_is_half_weight(self):
        """MACD > Signal 유지 중 점수 = buy_weight × 0.5 = +1."""
        gen = SignalGenerator(_MockConfigWeightsComplete())
        df = _make_macd_df(n=10)
        scores = gen._score_macd(df)
        # day2~9: 유지 구간 (day0-1은 shift 경계)
        maintenance_scores = scores.iloc[2:]
        assert (maintenance_scores == 1.0).all(), (
            f"유지 구간 점수가 +1이 아님: {maintenance_scores.tolist()}"
        )

    def test_golden_cross_scores_full_weight(self):
        """골든크로스 당일 = buy_weight(+2)."""
        gen = SignalGenerator(_MockConfigWeightsComplete())
        df = _make_macd_cross_df()
        scores = gen._score_macd(df)
        assert scores.iloc[1] == 2.0, f"골든크로스 점수: {scores.iloc[1]} (expected 2.0)"

    def test_dead_cross_scores_full_weight(self):
        """데드크로스 당일 = sell_weight(-2)."""
        gen = SignalGenerator(_MockConfigWeightsComplete())
        df = _make_macd_cross_df()
        scores = gen._score_macd(df)
        assert scores.iloc[4] == -2.0, f"데드크로스 점수: {scores.iloc[4]} (expected -2.0)"

    def test_maintenance_after_cross_has_half_weight(self):
        """크로스 이후 유지 구간 = buy_weight × 0.5 (+ 히스토그램 보너스 가능)."""
        gen = SignalGenerator(_MockConfigWeightsComplete())
        df = _make_macd_cross_df()
        scores = gen._score_macd(df)
        # day2, day3 = MACD > Signal 유지: base +1, 히스토그램 보너스 ±0.5 가능
        for i in [2, 3]:
            assert 0.5 <= scores.iloc[i] <= 1.5, (
                f"day{i} 유지점수 {scores.iloc[i]} 범위 밖 (expected 0.5~1.5)"
            )
