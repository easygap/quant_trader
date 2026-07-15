"""BlackSwanDetector 단위 테스트"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.blackswan_detector import BlackSwanDetector


class _MockConfig:
    risk_params = {}
    settings = {}
    trading = {}


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


def test_detector_reads_all_blackswan_risk_params():
    """risk_params.blackswan이 감지·cooldown·recovery의 실제 운영값이다."""
    config = SimpleNamespace(
        risk_params={
            "blackswan": {
                "single_stock_threshold": -0.04,
                "portfolio_threshold": -0.025,
                "consecutive_days": 4,
                "consecutive_threshold": -0.015,
                "cooldown_minutes": 7,
                "recovery_minutes": 11,
                "recovery_scale": 0.25,
            }
        },
        # risk_params가 있으면 기존 settings 값보다 우선해야 한다.
        trading={
            "blackswan_recovery_minutes": 99,
            "blackswan_recovery_scale": 0.9,
        },
    )

    configured = BlackSwanDetector(config)

    assert configured.single_stock_threshold == -0.04
    assert configured.portfolio_threshold == -0.025
    assert configured.consecutive_days == 4
    assert configured.consecutive_threshold == -0.015
    assert configured.cooldown_minutes == 7
    assert configured.recovery_minutes == 11
    assert configured.recovery_scale == 0.25

    before = datetime.now()
    result = configured.check_stock("005930", 95_000, 100_000)
    assert result["triggered"] is True
    assert configured.is_on_cooldown() is True
    remaining = configured._cooldown_until - before
    assert timedelta(minutes=6, seconds=55) <= remaining <= timedelta(minutes=7, seconds=5)


def test_detector_keeps_legacy_recovery_fallback_when_risk_keys_missing():
    config = SimpleNamespace(
        risk_params={"blackswan": {}},
        trading={
            "blackswan_recovery_minutes": 33,
            "blackswan_recovery_scale": 0.4,
        },
    )

    configured = BlackSwanDetector(config)

    assert configured.recovery_minutes == 33
    assert configured.recovery_scale == 0.4
