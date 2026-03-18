"""BlackSwanDetector 단위 테스트"""
from datetime import datetime, timedelta

import pytest

from core.blackswan_detector import BlackSwanDetector


class _MockConfig:
    risk_params = {}
    settings = {}


@pytest.fixture
def detector():
    return BlackSwanDetector(_MockConfig())


def test_can_trade_default_allowed(detector):
    """쿨다운 아닐 때 can_trade allowed True"""
    r = detector.can_trade()
    assert r["allowed"] is True


def test_check_stock_no_trigger(detector):
    """급락 아닐 때 triggered False"""
    r = detector.check_stock("005930", 50_000, 49_000)
    assert r["triggered"] is False
