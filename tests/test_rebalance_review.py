"""주문 계획 비교에서 미래 가격·적립금이 수익으로 섞이지 않는지 확인한다."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from core import basket_rebalancer
from tools.rebalance_review import replay


@pytest.fixture
def basket():
    path = Path(__file__).resolve().parents[1] / "config/baskets.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["baskets"]["kr_pocket"]


def prices(rows=201):
    return pd.DataFrame(
        {"069500": 30_000.0, "357870": 100_000.0, "KS200": 100.0},
        index=pd.bdate_range("2025-01-02", periods=rows),
    )


def test_quantity_uses_previous_close_and_unaffordable_gap_is_rejected(basket, monkeypatch):
    plans = []
    original = basket_rebalancer.BasketRebalancer.plan_rebalance

    def record(self, current_prices):
        orders = original(self, current_prices)
        plans.append([(o.symbol, o.quantity, o.price) for o in orders])
        return orders

    monkeypatch.setattr(basket_rebalancer.BasketRebalancer, "plan_rebalance", record)
    stable = prices()
    jumped = prices()
    jumped.iloc[-1, jumped.columns.get_loc("069500")] = 60_000.0

    no_gap = replay(basket_rebalancer, stable, basket)
    gap = replay(basket_rebalancer, jumped, basket)

    assert plans[0] == plans[1] == [("069500", 4, 30_000.0)]
    assert no_gap.filled_orders.iloc[0] == 1
    assert gap.filled_orders.iloc[0] == 0
    assert gap.rejected_orders.iloc[0] == 1
    assert gap.cash.iloc[0] == basket["initial_capital"]


def test_deposits_are_not_profit_and_no_runtime_objects_are_created(basket, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("연구 비교가 실제 계좌 또는 주문 실행부에 접근했습니다")

    monkeypatch.setattr(basket_rebalancer.BasketRebalancer, "__init__", forbidden)
    monkeypatch.setattr(basket_rebalancer.BasketRebalancer, "execute", forbidden)
    monkeypatch.setattr(basket_rebalancer, "PortfolioManager", forbidden)
    monkeypatch.setattr(basket_rebalancer, "DataCollector", forbidden)
    frame = replay(basket_rebalancer, prices(280), basket, cost_multiple=0)

    assert frame.flow.sum() > 0
    assert frame.total.iloc[-1] == pytest.approx(basket["initial_capital"] + frame.flow.sum())
    assert frame.twr.tolist() == pytest.approx([1.0] * len(frame))
    assert frame.drawdown.min() == pytest.approx(0)


def test_missing_price_inside_comparison_is_rejected(basket):
    panel = prices(210)
    panel.iloc[205, panel.columns.get_loc("069500")] = float("nan")
    with pytest.raises(ValueError, match="가격과 날짜"):
        replay(basket_rebalancer, panel, basket)


def test_index_warmup_can_precede_etf_listing(basket):
    panel = prices(210)
    panel.loc[panel.index[:203], ["069500", "357870"]] = float("nan")
    frame = replay(basket_rebalancer, panel, basket)
    assert frame.index[0] == panel.index[204]
    assert frame.planned_orders.iloc[0] > 0
