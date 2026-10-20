"""주문 직전 시세 점검 (2026-10-20).

- 시세 기준일이 오늘이 아니면 주문하지 않는다. 공급자 자료가 멈추면(9/17~10/12 KS200처럼)
  전날 가격으로 수량을 정하고 체결까지 그 가격으로 기록된다.
- 전일 종가 대비 ±30%를 넘는 가격은 가격제한폭 밖이라 자료 오류로 본다.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.basket_rebalancer import BasketRebalancer, RebalanceOrder

SESSION = datetime(2026, 10, 20, 10, 10)


@pytest.fixture
def rebalancer(monkeypatch):
    from config.config_loader import Config

    Config._instance = None
    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "guard_test"
    rb.basket = {"risk": {"stop_loss_pct": 0}}
    rb.account_key = rb.execution_strategy = "basket_rebalance:guard_test"
    rb.holdings = {"069500": 1.0}
    rb.rebalance_cfg = {}
    rb._risk_params = Config.get().risk_params
    rb.config = Config.get()
    monkeypatch.setitem(rb.config.settings["trading"], "mode", "paper")
    rb.portfolio_mgr = MagicMock()
    rb.portfolio_mgr.get_available_cash.return_value = 1_000_000
    rb.portfolio_mgr.get_portfolio_summary.return_value = {"total_value": 1_000_000}
    rb._policy_exposure_limits = lambda symbol: None
    monkeypatch.setattr("core.trading_hours._now_kst", lambda: SESSION)
    executor = MagicMock()
    executor.execute_buy_quantity.return_value = {"success": True}
    executor.execute_sell.return_value = {"success": True}
    monkeypatch.setattr("core.order_executor.OrderExecutor", MagicMock(return_value=executor))
    events = []
    monkeypatch.setattr(
        "core.cycle_observability.record_event_once_per_day", lambda *a, **k: events.append(a),
    )
    rb._events = events
    rb._executor = executor
    return rb


def _snapshot(rb, **quote):
    rb._market_snapshot = {"069500": {"price": 86_755.0, "avg_volume": 1e6, **quote}}


def test_stale_quote_date_blocks_order(rebalancer):
    _snapshot(rebalancer, quote_date="2026-10-17", prev_close=88_000.0)
    result = rebalancer.execute([RebalanceOrder("069500", "BUY", 1, 86_755.0)])
    assert result["executed"] == 0 and result["skipped"] == 1
    assert result["details"][0]["status"] == "blocked"
    assert "시세 기준일 2026-10-17" in result["details"][0]["reason"]
    rebalancer._executor.execute_buy_quantity.assert_not_called()
    assert rebalancer._events and rebalancer._events[0][0] == "STALE_QUOTE_BLOCKED"


def test_price_outside_daily_limit_is_treated_as_bad_data(rebalancer):
    _snapshot(rebalancer, quote_date="2026-10-20", prev_close=60_000.0)   # +44.6%
    result = rebalancer.execute([RebalanceOrder("069500", "BUY", 1, 86_755.0)])
    assert result["executed"] == 0
    assert "가격제한폭 밖" in result["details"][0]["reason"]
    rebalancer._executor.execute_buy_quantity.assert_not_called()


def test_fresh_quote_reaches_executor(rebalancer):
    _snapshot(rebalancer, quote_date="2026-10-20", prev_close=90_000.0)   # -3.6%
    result = rebalancer.execute([
        RebalanceOrder("069500", "SELL", 1, 86_755.0),
        RebalanceOrder("069500", "BUY", 1, 86_755.0),
    ])
    assert result["executed"] == 2
    rebalancer._executor.execute_buy_quantity.assert_called_once()
    rebalancer._executor.execute_sell.assert_called_once()
    assert rebalancer._events == []


def test_quote_without_date_keeps_previous_behaviour(rebalancer):
    _snapshot(rebalancer)
    result = rebalancer.execute([RebalanceOrder("069500", "BUY", 1, 86_755.0)])
    assert result["executed"] == 1


def test_snapshot_records_previous_close(monkeypatch):
    import pandas as pd

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.holdings = {"069500": 1.0}
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-10-17", "2026-10-20"]),
        "close": [88_000.0, 86_755.0], "volume": [1e6, 2e6],
    })
    rb.data_collector = SimpleNamespace(
        fetch_korean_stock=lambda *a, **k: frame, get_last_source_info=lambda: {"source": "test"},
    )
    snapshot = rb._fetch_market_snapshot()
    assert snapshot["069500"]["prev_close"] == 88_000.0
    assert snapshot["069500"]["quote_date"] == "2026-10-20"
