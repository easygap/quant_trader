"""RiskManager 단위 테스트 — 포지션 사이징 엣지 케이스"""
import pytest

from core.risk_manager import RiskManager, _get_tick_size


class _MockConfig:
    risk_params = {
        "position_sizing": {"max_risk_per_trade": 0.01},
        "diversification": {"max_position_ratio": 0.20, "min_cash_ratio": 0.20},
        "transaction_costs": {"slippage": 0.0005, "slippage_ticks": 2},
    }


@pytest.fixture
def risk_manager():
    return RiskManager(_MockConfig())


def test_position_size_entry_price_zero(risk_manager):
    """진입가 0이면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, 0, 9_000) == 0


def test_position_size_entry_price_negative(risk_manager):
    """진입가 음수면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, -100, 90) == 0


def test_position_size_risk_per_share_zero(risk_manager):
    """손절가 == 진입가면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, 50_000, 50_000) == 0


def test_position_size_capital_zero(risk_manager):
    """자본 0이면 0 반환"""
    assert risk_manager.calculate_position_size(0, 50_000, 49_000) == 0


def test_position_size_normal(risk_manager):
    """정상 케이스: 양수 수량 반환"""
    qty = risk_manager.calculate_position_size(10_000_000, 50_000, 49_000)
    assert qty > 0
    assert isinstance(qty, int)


def test_tick_size():
    """호가 단위 헬퍼 (KRX: 5만원 미만 50원, 이상 100원 등)"""
    assert _get_tick_size(1000) == 1
    assert _get_tick_size(2000) == 5
    assert _get_tick_size(49999) == 50
    assert _get_tick_size(50000) == 100
    assert _get_tick_size(499999) == 500
    assert _get_tick_size(500000) == 1000
    assert _get_tick_size(0) == 1


def test_diversification_blocks_when_remaining_cash_too_low(risk_manager):
    """주문 후 남는 현금 비중이 설정값보다 낮으면 차단 (단일 종목 비중은 20% 이하로 두어 해당 검사 통과)"""
    result = risk_manager.check_diversification(
        current_positions=1,
        position_value=50_000,   # 5% of total → max_position_ratio 통과
        total_value=1_000_000,
        available_cash=100_000,  # 매수 후 잔여 현금 50k → 5% < min_cash_ratio 20%
    )
    assert result["can_buy"] is False
    assert "최소 현금 비중" in result["reason"]


def test_diversification_allows_when_cash_ratio_is_safe(risk_manager):
    """남는 현금 비중이 충분하면 통과"""
    result = risk_manager.check_diversification(
        current_positions=1,
        position_value=150_000,
        total_value=1_000_000,
        available_cash=450_000,
    )
    assert result["can_buy"] is True
