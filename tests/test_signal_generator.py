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
