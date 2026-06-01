"""BasketRebalancer 단위 테스트."""

import sys
import os
from types import SimpleNamespace
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

    def test_paper_buy_execute_uses_current_capital_api(self, rebalancer, monkeypatch):
        """paper BUY 리밸런싱은 존재하는 포트폴리오 자본 API로 주문 수량을 넘긴다."""
        from core.basket_rebalancer import RebalanceOrder

        rebalancer.portfolio_mgr = SimpleNamespace(
            get_available_cash=MagicMock(return_value=5_000_000),
            get_current_capital=MagicMock(return_value=100_000_000),
        )
        fake_executor = MagicMock()
        fake_executor.execute_buy_quantity.return_value = {"success": True}
        executor_cls = MagicMock(return_value=fake_executor)
        monkeypatch.setattr("core.order_executor.OrderExecutor", executor_cls)

        result = rebalancer.execute([
            RebalanceOrder("005930", "BUY", 10, 70_000, "테스트"),
        ])

        assert result["executed"] == 1
        assert result["failed"] == 0
        rebalancer.portfolio_mgr.get_available_cash.assert_called_once()
        rebalancer.portfolio_mgr.get_current_capital.assert_called_once()
        fake_executor.execute_buy_quantity.assert_called_once()
        kwargs = fake_executor.execute_buy_quantity.call_args.kwargs
        assert kwargs["capital"] == 100_000_000
        assert kwargs["available_cash"] == 5_000_000
        assert kwargs["strategy"] == "basket_rebalance"

    def test_live_execute_requires_confirmed_gate(self, rebalancer, monkeypatch):
        """live 리밸런싱은 확인 게이트 없이 주문 실행부에 도달하면 안 된다."""
        from core.basket_rebalancer import RebalanceOrder

        rebalancer.config.trading["mode"] = "live"
        executor_cls = MagicMock()
        monkeypatch.setattr("core.order_executor.OrderExecutor", executor_cls)

        result = rebalancer.execute([
            RebalanceOrder("005930", "SELL", 3, 70000, "테스트"),
        ])

        assert result["blocked"] is True
        assert result["failed"] == 1
        assert result["details"][0]["status"] == "blocked"
        executor_cls.assert_not_called()

    def test_live_execute_allows_confirmed_gate(self, rebalancer, monkeypatch):
        """확인 게이트를 통과한 live 리밸런싱만 주문 실행부로 넘긴다."""
        from core.basket_rebalancer import RebalanceOrder

        rebalancer.config.trading["mode"] = "live"
        rebalancer.account_key = "basket_rebalance:test_basket"
        rebalancer.execution_strategy = "basket_rebalance:test_basket"
        fake_executor = MagicMock()
        fake_executor.execute_sell.return_value = {"success": True}
        executor_cls = MagicMock(return_value=fake_executor)
        monkeypatch.setattr("core.order_executor.OrderExecutor", executor_cls)

        result = rebalancer.execute(
            [RebalanceOrder("005930", "SELL", 3, 70000, "테스트")],
            live_confirmed=True,
        )

        assert result["executed"] == 1
        assert result["failed"] == 0
        executor_cls.assert_called_once_with(
            rebalancer.config,
            account_key="basket_rebalance:test_basket",
            live_gate_validated=True,
        )
        fake_executor.execute_sell.assert_called_once()
        assert fake_executor.execute_sell.call_args.kwargs["strategy"] == (
            "basket_rebalance:test_basket"
        )

    def test_live_execute_blocks_scope_mismatch(self, rebalancer, monkeypatch):
        """live 리밸런싱은 gate/account/order 전략 단위가 일치해야 한다."""
        from core.basket_rebalancer import RebalanceOrder

        rebalancer.config.trading["mode"] = "live"
        rebalancer.account_key = "basket_rebalance:test_basket"
        rebalancer.execution_strategy = "basket_rebalance:other"
        executor_cls = MagicMock()
        monkeypatch.setattr("core.order_executor.OrderExecutor", executor_cls)

        result = rebalancer.execute(
            [RebalanceOrder("005930", "SELL", 3, 70000, "테스트")],
            live_confirmed=True,
        )

        assert result["blocked"] is True
        assert result["failed"] == 1
        assert "승인 단위 불일치" in result["reason"]
        executor_cls.assert_not_called()


class TestShippedBasketsConfig:
    """config/baskets.yaml에 실제로 배포되는 바스켓 정의가 올바른지 검증."""

    @staticmethod
    def _load():
        import yaml
        path = Path(__file__).parent.parent / "config" / "baskets.yaml"
        with open(path, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("baskets", {})

    def test_all_baskets_weights_sum_to_one(self):
        baskets = self._load()
        assert baskets, "baskets.yaml에 바스켓 정의가 없음"
        for name, cfg in baskets.items():
            holdings = cfg.get("holdings", {})
            total = sum(float(w) for w in holdings.values())
            assert abs(total - 1.0) < 1e-6, f"{name} 비중 합={total} (1.0 이어야 함)"

    def test_diversified_hold_basket_is_low_turnover(self):
        """수익성 결론 반영 바스켓: 저회전 buy&hold 설정인지 확인."""
        baskets = self._load()
        assert "kr_diversified_hold" in baskets, "분산 보유 바스켓 누락"
        b = baskets["kr_diversified_hold"]
        # 10종목 균등(각 10%)
        assert len(b["holdings"]) == 10
        assert all(abs(float(w) - 0.10) < 1e-9 for w in b["holdings"].values())
        # 저회전: 넓은 드리프트 임계 + 낮은 회전 상한
        rb = b["rebalance"]
        assert rb["drift_threshold"] >= 0.08
        assert rb["max_turnover_ratio"] <= 0.15
        # 기본 비활성(운영자가 paper 검증 후 켠다)
        assert b["enabled"] is False

    def test_all_basket_symbols_are_6digit_kr_codes(self):
        baskets = self._load()
        for name, cfg in baskets.items():
            for sym in cfg.get("holdings", {}):
                assert isinstance(sym, str) and sym.isdigit() and len(sym) == 6, \
                    f"{name}의 종목코드 {sym!r}가 6자리 숫자가 아님"
