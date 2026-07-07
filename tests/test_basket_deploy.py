"""바스켓 배포 헬퍼(core.basket_deploy) 단위 테스트."""

from types import SimpleNamespace

import pytest

from core.basket_deploy import (
    effective_stock_fraction,
    estimate_order_costs,
    summarize_basket_deployment,
    DEFAULT_COMMISSION_RATE,
    DEFAULT_TAX_RATE,
)


def _order(action, symbol, quantity):
    return SimpleNamespace(action=action, symbol=symbol, quantity=quantity)


class TestEstimateOrderCosts:
    def test_buy_only_costs(self):
        orders = [_order("BUY", "005930", 10)]
        prices = {"005930": 70000}
        out = estimate_order_costs(orders, prices)
        assert out["buy_amount"] == 700000
        assert out["sell_amount"] == 0
        # 수수료만 (매수엔 세금 없음)
        assert out["est_commission"] == round(700000 * DEFAULT_COMMISSION_RATE, 0)
        assert out["est_tax"] == 0

    def test_sell_incurs_tax(self):
        orders = [_order("SELL", "005930", 10)]
        prices = {"005930": 70000}
        out = estimate_order_costs(orders, prices)
        assert out["est_tax"] == round(700000 * DEFAULT_TAX_RATE, 0)

    def test_mixed_orders_total(self):
        orders = [_order("BUY", "A", 10), _order("SELL", "B", 5)]
        prices = {"A": 10000, "B": 20000}
        out = estimate_order_costs(orders, prices)
        assert out["buy_amount"] == 100000
        assert out["sell_amount"] == 100000
        assert out["total_trade_amount"] == 200000
        # 수수료 = 양방향, 세금 = 매도(10만)만
        assert out["est_commission"] == round(200000 * DEFAULT_COMMISSION_RATE, 0)
        assert out["est_tax"] == round(100000 * DEFAULT_TAX_RATE, 0)
        assert out["est_total_cost"] == out["est_commission"] + out["est_tax"]

    def test_missing_price_skips(self):
        orders = [_order("BUY", "A", 10), _order("BUY", "B", 10)]
        prices = {"A": 10000}  # B 가격 없음 → 0 취급
        out = estimate_order_costs(orders, prices)
        assert out["buy_amount"] == 100000

    def test_empty_orders(self):
        out = estimate_order_costs([], {})
        assert out["order_count"] == 0
        assert out["est_total_cost"] == 0
        assert out["cost_bps_of_trade"] == 0.0


class TestSummarizeBasketDeployment:
    def _cfg(self, enabled=False):
        return {
            "name": "분산 대형주",
            "enabled": enabled,
            "rebalance": {"trigger": "drift", "drift_threshold": 0.08, "max_turnover_ratio": 0.15},
            "holdings": {"005930": 0.5, "000660": 0.5},
        }

    def test_basic_summary_fields(self):
        orders = [_order("BUY", "005930", 10), _order("BUY", "000660", 5)]
        prices = {"005930": 70000, "000660": 130000}
        out = summarize_basket_deployment("kr_test", self._cfg(), orders, prices,
                                          portfolio_value=10_000_000)
        assert out["basket"] == "kr_test"
        assert out["holdings_count"] == 2
        assert out["weights_sum_ok"] is True
        assert out["drift_threshold_pct"] == 8.0
        assert out["max_turnover_ratio_pct"] == 15.0
        assert len(out["plan"]) == 2
        assert out["turnover_pct_of_portfolio"] is not None
        assert out["ready_to_validate"] is True

    def test_disabled_basket_has_enable_step(self):
        out = summarize_basket_deployment("kr_test", self._cfg(enabled=False), [], {"005930": 1, "000660": 1})
        assert out["enabled"] is False
        assert any("enabled를 true" in s for s in out["next_steps"])

    def test_enabled_basket_no_enable_step(self):
        out = summarize_basket_deployment("kr_test", self._cfg(enabled=True), [], {"005930": 1, "000660": 1})
        assert out["enabled"] is True
        assert not any("enabled를 true" in s for s in out["next_steps"])

    def test_missing_prices_flagged(self):
        # holdings 2개인데 가격 1개 → ready_to_validate False + 안내
        out = summarize_basket_deployment("kr_test", self._cfg(), [], {"005930": 70000})
        assert out["ready_to_validate"] is False
        assert any("가격 조회 일부 실패" in s for s in out["next_steps"])

    def test_bad_weights_not_ready(self):
        cfg = self._cfg()
        cfg["holdings"] = {"005930": 0.5, "000660": 0.3}  # 합 0.8
        out = summarize_basket_deployment("kr_test", cfg, [], {"005930": 1, "000660": 1})
        assert out["weights_sum_ok"] is False
        assert out["ready_to_validate"] is False

    def test_next_steps_always_include_dryrun_and_live(self):
        out = summarize_basket_deployment("kr_test", self._cfg(), [], {"005930": 1, "000660": 1})
        joined = " ".join(out["next_steps"])
        assert "--dry-run" in joined
        assert "live" in joined.lower()


class TestEffectiveStockFraction:
    """유효 투자 비중 — 리밸런서·평가·헬스가 공유하는 단일 규칙."""

    RISK = {"diversification": {"min_cash_ratio": 0.20}}

    def test_no_target_uses_global_cash_floor(self):
        assert effective_stock_fraction({}, self.RISK) == pytest.approx(0.80)

    def test_target_clamped_by_global_floor(self):
        # 오버라이드 없으면 전역 20% 하한이 그대로 상한을 만든다 (기존 동작 회귀 pin)
        cfg = {"target_stock_weight": 0.95}
        assert effective_stock_fraction(cfg, self.RISK) == pytest.approx(0.80)

    def test_legacy_5050_unchanged(self):
        cfg = {"target_stock_weight": 0.5}
        assert effective_stock_fraction(cfg, self.RISK) == pytest.approx(0.5)

    def test_basket_override_allows_higher_deployment(self):
        # kr_pocket v2: 보유분 절반이 현금성(파킹 ETF)이라 현금 하한 5%로 낮춰 95% 배치
        cfg = {"target_stock_weight": 0.95, "min_cash_ratio": 0.05}
        assert effective_stock_fraction(cfg, self.RISK) == pytest.approx(0.95)

    def test_override_still_floors_target(self):
        cfg = {"target_stock_weight": 0.99, "min_cash_ratio": 0.05}
        assert effective_stock_fraction(cfg, self.RISK) == pytest.approx(0.95)

    def test_invalid_override_falls_back_to_global(self):
        cfg = {"target_stock_weight": 0.95, "min_cash_ratio": "잘못된값"}
        assert effective_stock_fraction(cfg, self.RISK) == pytest.approx(0.80)

    def test_out_of_range_override_clamped(self):
        assert effective_stock_fraction({"min_cash_ratio": -0.5}, self.RISK) == pytest.approx(1.0)
        assert effective_stock_fraction({"min_cash_ratio": 1.5}, self.RISK) == pytest.approx(0.0)

    def test_empty_risk_params_uses_default(self):
        assert effective_stock_fraction({}, {}) == pytest.approx(0.80)
