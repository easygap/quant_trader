"""BasketRebalancer 단위 테스트."""

import sys
import os
from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# --- Mock Config ---

class _MockConfig:
    """테스트용 Config."""

    def __init__(self):
        self.trading = {"mode": "paper", "initial_capital": 100_000_000}
        self.risk_params = {
            "diversification": {
                "max_position_ratio": 0.20,
                "max_investment_ratio": 0.70,
                "min_cash_ratio": 0.20,
            },
        }
        self.strategies = {"scoring": {"buy_threshold": 2, "sell_threshold": -2}}

    @staticmethod
    def get():
        return _MockConfig()


SAMPLE_BASKETS_YAML = {
    "baskets": {
        "test_basket": {
            "name": "테스트 바스켓",
            "enabled": True,
            "rebalance": {
                "trigger": "drift",
                "drift_threshold": 0.05,
                "min_trade_amount": 50000,
                "max_turnover_ratio": 0.30,
            },
            "holdings": {
                "005930": 0.40,
                "000660": 0.35,
                "035420": 0.25,
            },
        },
        "disabled_basket": {
            "name": "비활성 바스켓",
            "enabled": False,
            "rebalance": {"trigger": "weekly"},
            "holdings": {"005930": 1.0},
        },
    }
}


@pytest.fixture
def mock_baskets_config():
    with patch(
        "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
        return_value=SAMPLE_BASKETS_YAML["baskets"],
    ):
        yield


@pytest.fixture
def rebalancer(mock_baskets_config):
    with patch("core.basket_rebalancer.PortfolioManager"):
        with patch("core.basket_rebalancer.DataCollector"):
            from core.basket_rebalancer import BasketRebalancer
            rb = BasketRebalancer(
                basket_name="test_basket",
                config=_MockConfig(),
            )
            return rb


class TestBasketConfig:

    def test_get_enabled_baskets(self, mock_baskets_config):
        from core.basket_rebalancer import BasketRebalancer
        enabled = BasketRebalancer.get_enabled_baskets()
        assert "test_basket" in enabled
        assert "disabled_basket" not in enabled

    def test_invalid_basket_name(self, mock_baskets_config):
        from core.basket_rebalancer import BasketRebalancer
        with patch("core.basket_rebalancer.PortfolioManager"):
            with patch("core.basket_rebalancer.DataCollector"):
                with pytest.raises(ValueError, match="바스켓"):
                    BasketRebalancer(basket_name="nonexistent", config=_MockConfig())


class TestTargetWeights:

    def test_target_weights_sum_to_one(self, rebalancer):
        weights = rebalancer.get_target_weights()
        assert abs(sum(weights.values()) - 1.0) < 0.001

    def test_target_weights_correct(self, rebalancer):
        weights = rebalancer.get_target_weights()
        assert weights["005930"] == pytest.approx(0.40, abs=0.01)
        assert weights["000660"] == pytest.approx(0.35, abs=0.01)


class TestDriftCalculation:

    def test_drift_all_zero_when_no_positions(self, rebalancer):
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.0, "000660": 0.0, "035420": 0.0,
        })
        drifts = rebalancer.calculate_drift()
        for symbol, d in drifts.items():
            assert d["drift"] > 0
            assert d["actual"] == 0.0

    def test_drift_zero_when_balanced(self, rebalancer):
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.40, "000660": 0.35, "035420": 0.25,
        })
        drifts = rebalancer.calculate_drift()
        for d in drifts.values():
            assert abs(d["drift"]) < 0.01


class TestTrigger:

    def test_drift_trigger_fires(self, rebalancer):
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.30, "000660": 0.35, "035420": 0.25,
        })
        should, reason = rebalancer.should_rebalance()
        assert should is True
        assert "드리프트" in reason

    def test_drift_trigger_skips(self, rebalancer):
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.39, "000660": 0.35, "035420": 0.25,
        })
        should, reason = rebalancer.should_rebalance()
        assert should is False


class TestPlanRebalance:

    def test_plan_generates_orders(self, rebalancer):
        rebalancer._fetch_current_prices = MagicMock(return_value={
            "005930": 70000, "000660": 150000, "035420": 300000,
        })
        rebalancer.portfolio_mgr.get_portfolio_summary = MagicMock(return_value={
            "total_value": 100_000_000,
        })
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rebalancer.plan_rebalance()

        assert len(orders) > 0
        assert all(o.action == "BUY" for o in orders)

    def test_plan_respects_min_trade_amount(self, rebalancer):
        rebalancer._fetch_current_prices = MagicMock(return_value={
            "005930": 70000, "000660": 150000, "035420": 300000,
        })
        rebalancer.portfolio_mgr.get_portfolio_summary = MagicMock(return_value={
            "total_value": 100_000_000,
        })
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.399, "000660": 0.349, "035420": 0.249,
        })
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rebalancer.plan_rebalance()

        for o in orders:
            trade_val = o.price * o.quantity
            assert trade_val >= rebalancer.rebalance_cfg["min_trade_amount"]


class TestExecute:

    def test_dry_run_no_actual_orders(self, rebalancer):
        from core.basket_rebalancer import RebalanceOrder
        orders = [
            RebalanceOrder("005930", "BUY", 10, 70000, "테스트"),
        ]
        result = rebalancer.execute(orders, dry_run=True)
        assert result["skipped"] == 1
        assert result["executed"] == 0
