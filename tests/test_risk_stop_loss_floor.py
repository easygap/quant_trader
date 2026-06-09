"""손절가 하한 클램프 회귀 테스트.

ATR이 지나치게 크거나 고정 손절폭이 100% 이상이면 손절가가 0 이하/매수가 이상이 되어
손절이 영원히 발동하지 않는다(무방비 포지션). 이 비정상 케이스만 최대 손실폭 기준으로
폴백하고, 정상 손절가는 그대로 유지하는지 검증한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk_manager import RiskManager
from config.config_loader import Config


def _rm_with_stop_loss(sl_config):
    Config._instance = None
    rm = RiskManager()
    rm.risk_params = dict(rm.risk_params)
    rm.risk_params["stop_loss"] = sl_config
    return rm


def test_atr_stop_below_zero_falls_back_to_max_loss_floor():
    """ATR*배수가 매수가를 초과하면 음수 손절가 대신 최대손실폭(50%)으로 폴백."""
    rm = _rm_with_stop_loss({"type": "atr", "atr_multiplier": 2.0, "max_loss_pct": 0.5})
    entry = 1000.0
    # atr 800 * 2 = 1600 > entry → 순진한 손절가 = -600 (영구 비발동)
    stop = rm.calculate_stop_loss(entry, atr=800.0)
    assert 0 < stop < entry, f"손절가가 (0, entry) 범위 밖: {stop}"
    assert stop == 500.0  # entry * (1 - 0.5)


def test_normal_atr_stop_is_unchanged():
    """정상 범위 손절가는 클램프하지 않고 그대로 둔다."""
    rm = _rm_with_stop_loss({"type": "atr", "atr_multiplier": 2.0, "max_loss_pct": 0.5})
    entry = 50000.0
    stop = rm.calculate_stop_loss(entry, atr=1000.0)  # 50000 - 2000 = 48000
    assert stop == 48000.0


def test_fixed_stop_rate_over_100pct_falls_back():
    """고정 손절폭 * 국면배수 >= 1 이면 stop >= entry(즉시 청산) 대신 폴백."""
    rm = _rm_with_stop_loss({"type": "fixed", "fixed_rate": 0.6, "max_loss_pct": 0.4})
    entry = 10000.0
    # fixed_rate 0.6 * regime 2.0 = 1.2 → stop = entry*(1-1.2) = -2000
    stop = rm.calculate_stop_loss(entry, regime_multiplier=2.0)
    assert 0 < stop < entry
    assert stop == 6000.0  # entry * (1 - 0.4)


def test_normal_fixed_stop_is_unchanged():
    """정상 고정 손절가는 유지."""
    rm = _rm_with_stop_loss({"type": "fixed", "fixed_rate": 0.03})
    entry = 10000.0
    stop = rm.calculate_stop_loss(entry)
    assert stop == 9700.0
