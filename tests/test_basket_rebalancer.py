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


class TestTargetStockWeight:
    """target_stock_weight(주식/현금 정적 배분) 기능 — docs/STATIC_ALLOCATION.md 실행 구현."""

    @staticmethod
    def _baskets():
        return {
            "full": {  # target 없음 → 기존 동작
                "name": "풀 인베스트",
                "enabled": True,
                "rebalance": {"trigger": "drift", "drift_threshold": 0.05,
                              "min_trade_amount": 50000, "max_turnover_ratio": 1.0},
                "holdings": {"005930": 0.5, "000660": 0.5},
            },
            "balanced": {  # 주식 50%, 현금 50%
                "name": "균형",
                "enabled": True,
                "target_stock_weight": 0.5,
                "rebalance": {"trigger": "drift", "drift_threshold": 0.05,
                              "min_trade_amount": 50000, "max_turnover_ratio": 1.0},
                "holdings": {"005930": 0.5, "000660": 0.5},
            },
        }

    def _make(self, name):
        with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                   return_value=self._baskets()):
            with patch("core.basket_rebalancer.PortfolioManager"), \
                 patch("core.basket_rebalancer.DataCollector"):
                from core.basket_rebalancer import BasketRebalancer
                return BasketRebalancer(basket_name=name, config=_MockConfig())

    def test_default_basket_uses_min_cash_ratio(self):
        rb = self._make("full")
        assert rb._target_stock_weight is None
        # _MockConfig min_cash_ratio=0.20 → 주식 0.80
        assert rb._stock_fraction() == pytest.approx(0.80, abs=1e-9)

    def test_balanced_basket_holds_half_stock(self):
        rb = self._make("balanced")
        assert rb._target_stock_weight == 0.5
        assert rb._stock_fraction() == pytest.approx(0.50, abs=1e-9)

    def test_target_stock_weight_capped_by_min_cash(self):
        """target_stock_weight가 1 - min_cash_ratio보다 크면 현금 하한이 우선한다."""
        baskets = self._baskets()
        baskets["balanced"]["target_stock_weight"] = 0.95  # min_cash 0.20 → 최대 0.80
        with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config",
                   return_value=baskets):
            with patch("core.basket_rebalancer.PortfolioManager"), \
                 patch("core.basket_rebalancer.DataCollector"):
                from core.basket_rebalancer import BasketRebalancer
                rb = BasketRebalancer(basket_name="balanced", config=_MockConfig())
        assert rb._stock_fraction() == pytest.approx(0.80, abs=1e-9)

    def test_balanced_plan_invests_less_than_full(self):
        """같은 자본에서 balanced(50%)는 full(80%)보다 적게 매수해야 한다."""
        prices = {"005930": 70000, "000660": 70000}
        full = self._make("full")
        bal = self._make("balanced")
        for rb in (full, bal):
            rb._fetch_current_prices = MagicMock(return_value=prices)
            rb.portfolio_mgr.get_portfolio_summary = MagicMock(return_value={"total_value": 100_000_000})
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            full_orders = full.plan_rebalance(prices=prices)
            bal_orders = bal.plan_rebalance(prices=prices)
        full_notional = sum(o.quantity * prices[o.symbol] for o in full_orders)
        bal_notional = sum(o.quantity * prices[o.symbol] for o in bal_orders)
        assert bal_notional < full_notional
        # 대략 50/80 비율
        assert bal_notional == pytest.approx(full_notional * 0.5 / 0.8, rel=0.1)


class _RealSignatureCollector:
    """실제 DataCollector.fetch_korean_stock 시그니처만 받는 fake.

    기존 테스트는 DataCollector를 MagicMock으로 패치해 `days=` 같은 잘못된 인자도
    조용히 받아들여, 운영 경로가 깨진 걸 못 잡았다. 이 fake는 실제 시그니처
    (symbol, start_date, end_date)만 받으므로 days= 를 주면 TypeError가 난다.
    """

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        import pandas as pd
        assert start_date and end_date, "start_date/end_date 누락"
        return pd.DataFrame({"close": [100.0, 101.0, 102.0]})


class TestPriceFetchUsesDateRange:
    """현재가 조회가 days= 가 아니라 start_date/end_date 로 호출되는지(운영 경로 회귀)."""

    def test_fetch_current_prices_uses_date_range_not_days(self, rebalancer):
        rebalancer.data_collector = _RealSignatureCollector()
        prices = rebalancer._fetch_current_prices()
        # days= 였다면 TypeError로 전부 실패해 빈 dict가 된다.
        assert set(prices) == set(rebalancer.holdings)
        assert all(v == 102.0 for v in prices.values())

    def test_recent_range_returns_start_before_end(self):
        from core.basket_rebalancer import BasketRebalancer
        start, end = BasketRebalancer._recent_range(15)
        assert start < end


class TestTurnoverBudgetOrdering:
    """회전율 예산이 SELL(자금원) 우선·거래액 큰 순으로 배분되고, 넘치는 거래는 부분 실행되는지.

    이전 버그: dict(YAML) 순서대로 예산을 소진해 BUY가 먼저 예산을 다 쓰면 자금원이 될
    SELL이 통째로 누락됐고(break), 이후 작은 거래도 모두 버려졌다.
    """

    def _prepare(self, rebalancer, actual_weights, positions, budget_ratio):
        rebalancer._fetch_current_prices = MagicMock(return_value={
            "005930": 70_000, "000660": 100_000, "035420": 300_000,
        })
        rebalancer.portfolio_mgr.get_portfolio_summary = MagicMock(return_value={
            "total_value": 100_000_000,
        })
        rebalancer.get_current_weights = MagicMock(return_value=actual_weights)
        rebalancer.rebalance_cfg["max_turnover_ratio"] = budget_ratio
        return positions

    def test_sell_gets_budget_priority_over_buy(self, rebalancer):
        """BUY가 dict 순서상 먼저라도 예산은 SELL이 먼저 가져간다."""
        # 005930(첫 키): 0.40 목표 vs 0.25 → BUY 12M / 000660: 0.35 vs 0.50 → SELL 12M
        positions = [SimpleNamespace(symbol="000660", quantity=200, avg_price=100_000)]
        self._prepare(
            rebalancer,
            {"005930": 0.25, "000660": 0.50, "035420": 0.25},
            positions,
            budget_ratio=0.13,  # 예산 13M — 12M 거래 하나만 온전히 들어감
        )
        with patch("core.basket_rebalancer.get_all_positions", return_value=positions):
            orders = rebalancer.plan_rebalance()

        sells = [o for o in orders if o.action == "SELL"]
        buys = [o for o in orders if o.action == "BUY"]
        # SELL이 예산을 먼저 받아 전량(120주=12M) 계획된다.
        assert len(sells) == 1 and sells[0].symbol == "000660"
        assert sells[0].quantity == 120
        # BUY는 잔여 예산(1M)만큼 부분 실행으로 축소된다(이전엔 BUY가 전량, SELL이 잘림).
        if buys:
            assert buys[0].quantity * buys[0].price <= 1_000_000
            assert "부분 실행" in buys[0].reason
        # SELL이 리스트 앞(현금 확보 먼저).
        assert orders[0].action == "SELL"

    def test_single_oversized_trade_partially_executes(self, rebalancer):
        """예산보다 큰 단일 드리프트는 영원히 스킵되지 않고 예산만큼 부분 실행된다."""
        self._prepare(
            rebalancer,
            {"005930": 0.0, "000660": 0.35, "035420": 0.25},  # 005930만 32M 부족
            [],
            budget_ratio=0.13,
        )
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rebalancer.plan_rebalance()

        assert len(orders) == 1
        o = orders[0]
        assert o.action == "BUY" and o.symbol == "005930"
        notional = o.quantity * o.price
        assert notional <= 13_000_000
        assert notional >= 12_000_000  # 예산을 거의 다 사용 (영(0)건이 아님)
        assert "부분 실행" in o.reason

    def test_smaller_trades_still_fit_after_oversized_one(self, rebalancer):
        """큰 거래가 예산을 못 맞춰도 뒤의 작은 거래는 계속 검토된다(continue, break 아님)."""
        # 000660 SELL 24M(예산 초과→부분), 035420 SELL 4M(온전히 들어가야 함)
        positions = [
            SimpleNamespace(symbol="000660", quantity=400, avg_price=100_000),
            SimpleNamespace(symbol="035420", quantity=40, avg_price=300_000),
        ]
        self._prepare(
            rebalancer,
            {"005930": 0.40, "000660": 0.65, "035420": 0.30},
            positions,
            budget_ratio=0.28,  # 예산 28M
        )
        with patch("core.basket_rebalancer.get_all_positions", return_value=positions):
            orders = rebalancer.plan_rebalance()

        symbols = {o.symbol for o in orders}
        assert "000660" in symbols  # 큰 SELL (전량 24M ≤ 28M)
        assert "035420" in symbols  # 작은 SELL도 잔여 예산(4M)에 들어감


class TestLivePlanFailClosed:
    """live에서 KIS 잔고 미확인(broker_balance_ok=False) 시 stale 자본으로 사이징하지 않는다."""

    def _prepare(self, rebalancer, broker_ok):
        rebalancer._fetch_current_prices = MagicMock(return_value={
            "005930": 70_000, "000660": 100_000, "035420": 300_000,
        })
        summary = {"total_value": 100_000_000}
        if broker_ok is not None:
            summary["broker_balance_ok"] = broker_ok
        rebalancer.portfolio_mgr.get_portfolio_summary = MagicMock(return_value=summary)
        rebalancer.get_current_weights = MagicMock(return_value={
            "005930": 0.0, "000660": 0.0, "035420": 0.0,
        })

    def test_live_plan_blocked_when_broker_balance_unconfirmed(self, rebalancer):
        rebalancer.config.trading["mode"] = "live"
        self._prepare(rebalancer, broker_ok=False)
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rebalancer.plan_rebalance()
        assert orders == []

    def test_paper_plan_unaffected_by_broker_flag(self, rebalancer):
        rebalancer.config.trading["mode"] = "paper"
        self._prepare(rebalancer, broker_ok=False)  # paper에선 의미 없는 플래그
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rebalancer.plan_rebalance()
        assert len(orders) > 0  # 정상 계획 생성
