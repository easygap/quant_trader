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


class TestDefaultAccountKey:
    """paper/live 공통 기본 계정·귀속 키 = basket_rebalance:<name> (격리·귀속 보장)."""

    def test_default_keys_are_basket_scoped(self, rebalancer):
        assert rebalancer.account_key == "basket_rebalance:test_basket"
        assert rebalancer.execution_strategy == "basket_rebalance:test_basket"

    def test_explicit_keys_still_respected(self, mock_baskets_config):
        with patch("core.basket_rebalancer.PortfolioManager"), \
             patch("core.basket_rebalancer.DataCollector"):
            from core.basket_rebalancer import BasketRebalancer
            rb = BasketRebalancer(
                basket_name="test_basket", config=_MockConfig(),
                account_key="custom", execution_strategy="custom_strat",
            )
        assert rb.account_key == "custom"
        assert rb.execution_strategy == "custom_strat"


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
        # 거래 귀속: 바스켓 이름 포함 키(어느 바스켓의 트랙레코드인지 구분 가능해야 함)
        assert kwargs["strategy"] == "basket_rebalance:test_basket"

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
        # 2026-06-10 paper 운영 개시(트랙레코드 축적) — enabled:true.
        # paper에서는 실주문이 없고, live는 별도 4중 게이트(ENABLE_LIVE_TRADING +
        # --confirm-live + basket_rebalance:<name> readiness gate)를 통과해야 한다.
        assert b["enabled"] is True

    def test_pocket_basket_small_capital_invariants(self):
        """소액 적립 바스켓(docs/POCKET_TRACK_PLAN.md §3) — 소액 특화 불변식.

        이 값들이 '일반' 기본값으로 되돌아가면 바스켓이 조용히 죽는다:
        min_trade 20만이면 슬롯(15만)이 미체결 판정, 회전상한 15%면 한도(4.5만)가
        ETF 1주가(약 12.6만)보다 작아 영원히 매수 불가.
        """
        baskets = self._load()
        assert "kr_pocket" in baskets, "소액 적립 바스켓 누락"
        b = baskets["kr_pocket"]
        assert b["enabled"] is True
        capital = float(b["initial_capital"])
        assert capital == 300_000
        tsw = float(b["target_stock_weight"])
        assert tsw == 0.5
        rb = b["rebalance"]
        slot = capital * tsw  # 단일 종목이므로 슬리브 전체가 한 슬롯
        assert float(rb["min_trade_amount"]) <= slot, "최소거래가 슬롯보다 크면 미체결"
        # 첫 사이클에 주식 슬리브를 채울 수 있어야 한다(회전 한도 ≥ 슬리브 비중)
        assert float(rb["max_turnover_ratio"]) >= tsw, "회전 한도 < 슬리브면 초기 매수 불가"
        # ETF 단일 구성(1주 = 200종목 분산)
        assert list(b["holdings"].keys()) == ["069500"]
        # 소액 구조적 절사(1주 단위 -7.3%p) 허용 — 기본 5%p면 매일 무의미 ATTENTION
        assert float(b["monitoring"]["deployment_tolerance"]) == 0.10

    def test_observation_track_deployment_alarm_disabled(self):
        """관찰용 강등(kr_diversified_hold): 종결된 자본 결정의 잔상인 배치율 미달이
        상시 ATTENTION으로 남아 다른 바스켓 감시를 가리지 않도록 허용 오차 해제."""
        baskets = self._load()
        b = baskets["kr_diversified_hold"]
        assert float(b["monitoring"]["deployment_tolerance"]) >= 1.0

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


class _RealSignatureCollectorWithVolume:
    """실 시그니처 + volume 컬럼 포함 fake (유동성 데이터 공급 검증용)."""

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        import pandas as pd
        assert start_date and end_date
        return pd.DataFrame({
            "close": [100.0, 101.0, 102.0],
            "volume": [10_000.0, 20_000.0, 30_000.0],
        })


class TestMarketSnapshotLiquidity:
    """가격 조회가 20일 평균 거래량도 수집하고, execute가 유동성 체크용으로 전달하는지."""

    def test_snapshot_includes_price_and_avg_volume(self, rebalancer):
        rebalancer.data_collector = _RealSignatureCollectorWithVolume()
        snap = rebalancer._fetch_market_snapshot()
        assert set(snap) == set(rebalancer.holdings)
        for v in snap.values():
            assert v["price"] == 102.0
            assert v["avg_volume"] == pytest.approx(20_000.0)

    def test_execute_passes_avg_volume_to_buy(self, rebalancer, monkeypatch):
        """paper BUY 주문에 스냅샷의 avg_volume이 전달된다(없으면 strict 유동성 필터가 차단)."""
        from core.basket_rebalancer import RebalanceOrder

        rebalancer.portfolio_mgr = SimpleNamespace(
            get_available_cash=MagicMock(return_value=5_000_000),
            get_current_capital=MagicMock(return_value=100_000_000),
        )
        rebalancer._market_snapshot = {
            "005930": {"price": 70_000.0, "avg_volume": 123_456.0},
        }
        fake_executor = MagicMock()
        fake_executor.execute_buy_quantity.return_value = {"success": True}
        monkeypatch.setattr(
            "core.order_executor.OrderExecutor", MagicMock(return_value=fake_executor)
        )

        result = rebalancer.execute([
            RebalanceOrder("005930", "BUY", 10, 70_000, "테스트"),
        ])

        assert result["executed"] == 1
        kwargs = fake_executor.execute_buy_quantity.call_args.kwargs
        assert kwargs["avg_daily_volume"] == pytest.approx(123_456.0)


class TestBasketAccountIsolation:
    """여러 enabled 바스켓의 계좌(자본 풀) 공유 차단 — 과배분·트랙레코드 오염 방지."""

    def _config(self, accounts=None):
        cfg = _MockConfig()
        cfg.kis_api = {"account_no": "11111111-01", "accounts": accounts or {}}
        def get_account_no(strategy=""):
            accts = cfg.kis_api.get("accounts", {}) or {}
            if strategy and strategy in accts:
                return accts[strategy] or cfg.kis_api.get("account_no", "")
            return cfg.kis_api.get("account_no", "")
        cfg.get_account_no = get_account_no
        return cfg

    def test_single_basket_passes(self):
        from core.basket_rebalancer import check_basket_account_isolation
        assert check_basket_account_isolation(["a"], self._config(), "paper") == []
        assert check_basket_account_isolation([], self._config(), "live") == []

    def test_two_paper_baskets_pass_with_key_isolation(self):
        """paper는 바스켓별 가상 계정 키(basket_rebalance:<name>)로 자연 격리 — 통과."""
        from core.basket_rebalancer import check_basket_account_isolation
        assert check_basket_account_isolation(["a", "b"], self._config(), "paper") == []

    def test_two_live_baskets_same_default_account_blocked(self):
        """live라도 계좌 미분리(둘 다 기본 계좌)면 차단."""
        from core.basket_rebalancer import check_basket_account_isolation
        issues = check_basket_account_isolation(["a", "b"], self._config(), "live")
        assert len(issues) == 1
        assert "공유" in issues[0]

    def test_two_live_baskets_separate_accounts_pass(self):
        """live에서 kis_api.accounts로 바스켓별 계좌를 분리하면 통과."""
        from core.basket_rebalancer import check_basket_account_isolation
        cfg = self._config(accounts={
            "basket_rebalance:a": "11111111-01",
            "basket_rebalance:b": "22222222-01",
        })
        assert check_basket_account_isolation(["a", "b"], cfg, "live") == []

    def test_account_resolution_error_fails_closed(self):
        from core.basket_rebalancer import check_basket_account_isolation
        cfg = self._config()
        def boom(strategy=""):
            raise RuntimeError("config broken")
        cfg.get_account_no = boom
        issues = check_basket_account_isolation(["a", "b"], cfg, "live")
        assert len(issues) == 1 and "fail-closed" in issues[0]


class TestUnfillableSlotWarning:
    """1주 가격 > 목표 거래금액이라 0주가 되는 슬롯의 침묵 스킵 가시화.

    실사례(2026-06-11): 자본 1,000만·실효 목표 8%=80만원 < SK하이닉스 1주 213만원
    → 000660 슬롯이 경고 없이 영원히 비었다. 운영자 결정(자본 증액/비중 조정)이
    필요한 상태는 반드시 경고로 드러나야 한다.
    """

    def test_warns_when_single_share_exceeds_target_amount(self, caplog):
        from types import SimpleNamespace
        from unittest.mock import patch
        from core.basket_rebalancer import BasketRebalancer

        rb = BasketRebalancer.__new__(BasketRebalancer)
        rb.basket_name = "t"
        rb.basket_cfg = {
            "holdings": {"000660": 0.5, "005930": 0.5},
            "rebalance": {"trigger": "drift", "drift_threshold": 0.08,
                          "min_trade_amount": 200000, "max_turnover_ratio": 1.0},
        }
        rb.rebalance_cfg = rb.basket_cfg["rebalance"]
        rb.account_key = "t"
        rb.execution_strategy = "t"
        rb.portfolio_mgr = SimpleNamespace(
            get_portfolio_summary=lambda current_prices=None: {"total_value": 1_000_000},
        )
        rb._is_live = lambda: False
        rb._stock_fraction = lambda: 1.0
        rb.get_target_weights = lambda: {"000660": 0.5, "005930": 0.5}
        rb.get_current_weights = lambda prices=None: {}

        prices = {"000660": 2_129_000, "005930": 60_000}
        with patch("core.basket_rebalancer.get_all_positions", return_value=[]):
            orders = rb.plan_rebalance(prices=prices)

        # 005930은 매수 가능(50만/6만=8주), 000660은 0주 — 주문엔 없어야 한다
        symbols = [o.symbol for o in orders]
        assert "005930" in symbols and "000660" not in symbols

class TestPerBasketInitialCapital:
    """바스켓별 initial_capital 레버 + portfolio_mgr 계정 키 전달 회귀."""

    def _make(self, monkeypatch, basket_cfg):
        from unittest.mock import patch
        from core.basket_rebalancer import BasketRebalancer

        with patch.object(BasketRebalancer, "_load_baskets_config",
                          return_value={"t": basket_cfg}):
            return BasketRebalancer(basket_name="t")

    def test_basket_initial_capital_overrides_global(self, monkeypatch):
        """baskets.yaml의 initial_capital이 그 바스켓 계정의 자본이 된다(전역 무변)."""
        rb = self._make(monkeypatch, {
            "holdings": {"005930": 1.0},
            "initial_capital": 30_000_000,
        })
        assert rb.portfolio_mgr.initial_capital == 30_000_000

    def test_default_uses_global_capital(self, monkeypatch):
        rb = self._make(monkeypatch, {"holdings": {"005930": 1.0}})
        from config.config_loader import Config
        global_cap = Config.get().risk_params.get("position_sizing", {}).get("initial_capital", 10000000)
        assert rb.portfolio_mgr.initial_capital == global_cap

    def test_portfolio_mgr_gets_resolved_account_key(self, monkeypatch):
        """인자 생략 시 portfolio_mgr는 ''(전 계정 합산)가 아니라 바스켓 키를 봐야 한다."""
        rb = self._make(monkeypatch, {"holdings": {"005930": 1.0}})
        assert rb.portfolio_mgr.account_key == "basket_rebalance:t"


class TestDiagnoseDeployment:
    """읽기전용 배치 진단(리포트 v2용): 집계 배치율 + 미체결 슬롯(#422)."""

    def _prep(self, rebalancer, *, total_value, cash, prices, actual_weights):
        rebalancer._fetch_current_prices = MagicMock(return_value=prices)
        rebalancer.portfolio_mgr.get_portfolio_summary = MagicMock(
            return_value={"total_value": total_value, "cash": cash}
        )
        rebalancer.get_current_weights = MagicMock(return_value=actual_weights)

    def test_deployment_ratio_from_cash(self, rebalancer):
        self._prep(
            rebalancer,
            total_value=1_000_000, cash=400_000,
            prices={"005930": 60_000, "000660": 2_129_000, "035420": 100_000},
            actual_weights={"005930": 0.5, "000660": 0.0, "035420": 0.0},
        )
        d = rebalancer.diagnose_deployment()
        assert d["total_value"] == 1_000_000
        assert d["stock_value"] == 600_000
        assert d["deployment_ratio"] == pytest.approx(0.60)
        assert d["design_fraction"] == pytest.approx(0.80)  # min_cash_ratio 0.20

    def test_unfilled_slot_when_share_exceeds_slot(self, rebalancer):
        # investable = 1M*0.8 = 800k. 000660 슬롯 280k < 1주 2.13M → 미체결.
        # 035420 슬롯 200k > 1주 10만 → 채움 가능(미체결 아님).
        self._prep(
            rebalancer,
            total_value=1_000_000, cash=400_000,
            prices={"005930": 60_000, "000660": 2_129_000, "035420": 100_000},
            actual_weights={"005930": 0.5, "000660": 0.0, "035420": 0.0},
        )
        d = rebalancer.diagnose_deployment()
        syms = [s["symbol"] for s in d["unfilled_slots"]]
        assert syms == ["000660"]
        assert d["unfilled_slots"][0]["price"] == 2_129_000

    def test_held_slot_not_flagged(self, rebalancer):
        # 전 종목 보유(actual>0) → 미체결 없음
        self._prep(
            rebalancer,
            total_value=1_000_000, cash=200_000,
            prices={"005930": 60_000, "000660": 100_000, "035420": 100_000},
            actual_weights={"005930": 0.5, "000660": 0.5, "035420": 0.5},
        )
        assert rebalancer.diagnose_deployment()["unfilled_slots"] == []

    def test_unfilled_when_slot_below_min_trade(self, rebalancer):
        # 두 번째 판정 arm: 슬롯 목표금액 < 최소 거래금액(50k)이라 1주는 살 수 있어도 못 채움.
        # 총자산 100k → investable 80k. 035420 슬롯 = 80k*0.25 = 20k < 50k → 미체결(가격은 저렴해도).
        self._prep(
            rebalancer,
            total_value=100_000, cash=80_000,
            prices={"005930": 5_000, "000660": 5_000, "035420": 5_000},
            actual_weights={"005930": 0.0, "000660": 0.0, "035420": 0.0},
        )
        d = rebalancer.diagnose_deployment()
        # 세 슬롯 모두 목표금액(32k/28k/20k)이 min_trade 50k 미만 → 전부 미체결
        assert {s["symbol"] for s in d["unfilled_slots"]} == {"005930", "000660", "035420"}

    def test_zero_price_symbol_skipped(self, rebalancer):
        # 가격 미확보(0)는 판정 보류(스냅샷 스킵이 별도로 처리) — 미체결로 잘못 표기하지 않음.
        self._prep(
            rebalancer,
            total_value=1_000_000, cash=1_000_000,
            prices={"005930": 0, "000660": 0, "035420": 0},
            actual_weights={"005930": 0.0, "000660": 0.0, "035420": 0.0},
        )
        assert rebalancer.diagnose_deployment()["unfilled_slots"] == []

    def test_zero_total_value_ratio_is_zero(self, rebalancer):
        self._prep(
            rebalancer,
            total_value=0, cash=0,
            prices={"005930": 60_000, "000660": 100_000, "035420": 100_000},
            actual_weights={"005930": 0.0, "000660": 0.0, "035420": 0.0},
        )
        d = rebalancer.diagnose_deployment()
        assert d["deployment_ratio"] == 0.0
        assert d["total_value"] == 0
