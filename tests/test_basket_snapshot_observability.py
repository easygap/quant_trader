from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.basket_observability import allocation_diagnostics, monthly_contribution_status, valuation_context


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import database.models as models
    from config.config_loader import Config
    monkeypatch.setenv("QUANT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(Config, "_instance", None)
    for field in ("_engine", "_ScopedSession", "_SessionFactory"):
        monkeypatch.setattr(models, field, None)
    models.init_database()
    yield models
    if models._engine:
        models._engine.dispose()


def seed(account="basket_rebalance:test", at=datetime(2026, 10, 12, 10, 10), **changes):
    from database.repositories import save_portfolio_snapshot
    values = dict(account_key=account, total_value=1000, cash=100, invested=900, cumulative_return=0,
                  daily_return=0, mdd=0, snapshot_date=at, measured_at=at)
    values.update(changes)
    assert save_portfolio_snapshot(**values)


def test_current_mdd_blocks_pass_but_historical_rejection_does_not(ledger, monkeypatch):
    import core.basket_evaluation as evaluation
    from core.basket_rebalancer import BasketRebalancer
    from core.cycle_observability import record_cycle_event
    from core.trading_hours import _KST
    monkeypatch.setattr("core.trading_hours._now_kst", lambda: datetime(2026, 10, 13, 10, tzinfo=_KST))
    monkeypatch.setattr(BasketRebalancer, "_load_baskets_config", lambda: {"test": {"initial_capital": 1000}})
    monkeypatch.setattr(evaluation, "evaluate_basket_paper_operation", lambda **kw: {"verdict": "PASS_CANDIDATE", "metrics": {}, "issues": []})
    seed(mdd=16.26)
    record_cycle_event("ORDER_REJECTED", "MDD", strategy="basket_rebalance:test")
    record_cycle_event("ORDER_REJECTED", "other", strategy="basket_rebalance:other")
    result, _ = evaluation.collect_basket_paper_evaluation(basket_name="test", include_benchmark=False)
    assert result["verdict"] == "WAIT"
    assert result["operation_verdict"] == "PASS_CANDIDATE"
    assert result["entry_status"]["status"] == "BLOCKED"
    assert result["metrics"]["recent_order_rejections"] == 1
    seed(mdd=7, at=datetime(2026, 10, 13, 10, 10))
    result, _ = evaluation.collect_basket_paper_evaluation(basket_name="test", include_benchmark=False)
    assert result["verdict"] == "PASS_CANDIDATE"
    assert result["entry_status"]["allowed"] is None


@pytest.mark.parametrize("hour,minute", [(8, 49), (15, 31), (17, 43)])
@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_basket_executor_rejects_off_session_before_creating_executor(monkeypatch, hour, minute, action):
    from core.basket_rebalancer import BasketRebalancer, RebalanceOrder
    from config.config_loader import Config
    from core.trading_hours import _KST
    monkeypatch.setattr("core.trading_hours._now_kst", lambda: datetime(2026, 10, 13, hour, minute, tzinfo=_KST))
    executor = MagicMock(side_effect=AssertionError("must not construct executor"))
    monkeypatch.setattr("core.order_executor.OrderExecutor", executor)
    rb = object.__new__(BasketRebalancer)
    rb.config = Config.get()
    result = rb.execute([RebalanceOrder("069500", action, 1, 100000)])
    assert result["blocked"] and result["executed"] == 0
    executor.assert_not_called()


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_direct_basket_order_precheck_also_obeys_session(monkeypatch, action):
    from core.order_executor import OrderExecutor
    from core.trading_hours import TradingHours, _KST
    from config.config_loader import Config
    monkeypatch.setattr("core.trading_hours._now_kst", lambda: datetime(2026, 10, 13, 8, 49, tzinfo=_KST))
    executor = object.__new__(OrderExecutor)
    executor.mode = "paper"
    executor.account_key = "basket_rebalance:test"
    executor.trading_hours = TradingHours(Config.get())
    for name in ("_global_trading_halt_check", "_monthly_buy_cap_check", "_drawdown_pre_order_check"):
        monkeypatch.setattr(executor, name, lambda *a, **kw: {"allowed": True})
    assert not executor._pre_order_check(action=action)["allowed"]


def test_premarket_snapshot_is_not_attributed_to_today(monkeypatch):
    from core.basket_rebalancer import BasketRebalancer
    from config.config_loader import Config
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 8, 49, tzinfo=tz)
    monkeypatch.setattr("core.basket_rebalancer.datetime", Clock)
    rb = object.__new__(BasketRebalancer)
    rb.config, rb.basket_name = Config.get(), "test"
    rb.portfolio_mgr = MagicMock()
    assert rb._nav_attribution_date().date() == date(2026, 10, 12)
    assert rb.save_daily_nav_snapshot() is False
    rb.portfolio_mgr.save_daily_snapshot.assert_not_called()


def test_equity_drift_is_visible_even_when_total_deployment_is_close():
    diag = allocation_diagnostics(380092, 45707,
        [{"symbol": "069500", "quantity": 1, "price": 101605}, {"symbol": "357870", "quantity": 4, "price": 58195}],
        {"069500": .475, "357870": .475}, "357870")
    assert diag["status"] == "ATTENTION"
    assert diag["assets"][0]["gap"] == pytest.approx(-.20768318)
    assert len(diag["issues"]) == 2


def test_partial_contribution_does_not_reset_missing_month():
    result = monthly_contribution_status({"enabled": True, "amount": 100000, "cadence": "monthly"},
        [(datetime(2026, 8, 26), 100000), (datetime(2026, 9, 30), 10000)], date(2026, 7, 10), date(2026, 10, 13))
    assert result["missing_amount"] == 90000
    assert "2026-09" in result["issues"][0]
    assert not result["months"][-1]["due"]


def test_quote_date_and_observation_time_prevent_false_close():
    p = SimpleNamespace(symbol="A", quantity=1)
    now = datetime(2026, 10, 13, 16)
    quote = {"A": {"price": 100, "quote_date": "2026-10-12", "observed_at": now.isoformat()}}
    assert valuation_context(quote, [p], now, now, time(15, 30))["valuation_kind"] == "unverified"
    quote["A"].update(quote_date="2026-10-13", observed_at="2026-10-13T10:10:00")
    assert valuation_context(quote, [p], now, now, time(15, 30))["valuation_kind"] == "unverified"
    quote["A"]["observed_at"] = now.isoformat()
    assert valuation_context(quote, [p], now, now, time(15, 30))["valuation_kind"] == "close"


def test_append_only_measurements_survive_daily_upserts(ledger):
    from database.repositories import get_portfolio_valuations
    morning = datetime(2026, 10, 12, 10, 10)
    seed(at=morning, cumulative_return=1.123456789, valuation_context={"valuation_kind": "intraday"})
    seed(at=morning, cumulative_return=1.123456789, valuation_context={"valuation_kind": "intraday"})
    seed(at=morning.replace(hour=13), total_value=1100, cash=200, cumulative_return=1.123456789)
    rows = get_portfolio_valuations("basket_rebalance:test")
    assert len(rows) == 2
    assert rows[0]["cumulative_return"] == 1.123456789
    assert rows[0]["kind"] == "intraday" and rows[1]["kind"] == "unverified"
    with ledger.get_session() as session:
        assert session.query(ledger.PortfolioSnapshot).count() == 1


def test_failed_measurement_insert_rolls_back_daily_upsert(ledger, monkeypatch):
    from database.repositories import save_portfolio_snapshot, get_latest_snapshot_summary
    seed()
    monkeypatch.setattr("database.repositories._append_valuation", MagicMock(side_effect=ValueError("write failed")))
    assert save_portfolio_snapshot(total_value=2000, cash=1100, invested=900, account_key="basket_rebalance:test", snapshot_date=datetime(2026, 10, 12)) is False
    assert get_latest_snapshot_summary("basket_rebalance:test")["total_value"] == 1000


def test_deposit_day_return_links_measured_subperiods_without_rounding(ledger, monkeypatch):
    from core.portfolio_manager import PortfolioManager
    from database.repositories import record_cash_flow, get_portfolio_valuations
    from main import account_performance_points
    key = "basket_rebalance:test"
    with ledger.get_session() as s:
        s.add(ledger.Position(account_key=key, mode="paper", symbol="A", quantity=1, avg_price=300000, total_invested=300000))
        s.add(ledger.TradeHistory(account_key=key, mode="paper", symbol="A", action="BUY", quantity=1,
                                 price=300000, total_amount=300000, executed_at=datetime(2026, 8, 24)))
        s.commit()
    seed(at=datetime(2026, 8, 25, 10), total_value=280000, cash=0, cumulative_return=(280000 / 300000 - 1) * 100)
    pm = PortfolioManager(account_key=key, initial_capital=300000)
    assert pm.save_daily_snapshot({"A": 285001}, datetime(2026, 8, 26), valuation_context={"measured_at": "2026-08-26T10:10:00"})
    record_cash_flow(100000, account_key=key, occurred_at=datetime(2026, 8, 26, 12))
    assert pm.save_daily_snapshot({"A": 286003}, datetime(2026, 8, 26), valuation_context={"measured_at": "2026-08-26T13:10:00"})
    expected_index = (285001 / 300000) * (386003 / 385001)
    with ledger.get_session() as s:
        rows = s.query(ledger.PortfolioSnapshot).order_by(ledger.PortfolioSnapshot.date).all()
        assert rows[-1].cumulative_return == pytest.approx((expected_index - 1) * 100, abs=1e-10)
        assert rows[-1].daily_return == pytest.approx((expected_index / (280000 / 300000) - 1) * 100)
        points = account_performance_points(key, rows)
        assert points[-1][1] == pytest.approx(expected_index)
    assert len(get_portfolio_valuations(key)) == 3


def test_close_replays_later_trades_flows_and_never_overwrites_operating_nav(ledger):
    from core.basket_close import finalize_basket_closes
    from database.repositories import get_portfolio_valuations, record_cash_flow, get_latest_snapshot_summary
    from config.config_loader import Config
    key = "basket_rebalance:test"
    context = {"valuation_kind": "intraday", "positions": [{"symbol": "A", "quantity": 2, "price": 100}]}
    seed(total_value=1000, cash=800, valuation_context=context)
    with ledger.get_session() as s:
        s.add(ledger.TradeHistory(account_key=key, mode="paper", symbol="A", action="BUY", quantity=1,
             total_amount=110, price=110, commission=1, tax=0, slippage=5, executed_at=datetime(2026, 10, 12, 12)))
        s.add(ledger.TradeHistory(account_key="other", mode="paper", symbol="A", action="BUY", quantity=5,
             total_amount=550, price=110, executed_at=datetime(2026, 10, 12, 12)))
        s.commit()
    record_cash_flow(100, account_key=key, occurred_at=datetime(2026, 10, 12, 11))
    collector = SimpleNamespace(fetch_korean_stock=lambda *args: pd.DataFrame({"date": [datetime(2026, 10, 12)], "close": [120]}),
                                get_last_source_info=lambda: {"source": "fixture"})
    rb = SimpleNamespace(config=Config.get(), account_key=key, data_collector=collector, _ledger_mode=lambda: "paper")
    assert finalize_basket_closes(rb, datetime(2026, 10, 12, 15, 45)) == []
    assert finalize_basket_closes(rb, datetime(2026, 10, 13, 8)) == ["2026-10-12"]
    assert finalize_basket_closes(rb, datetime(2026, 10, 13, 8)) == []
    close = get_portfolio_valuations(key, kind="close")[0]
    assert close["cash"] == 789
    assert close["total_value"] == 1149
    assert close["cumulative_return"] == pytest.approx((1149 / 1100 - 1) * 100)
    assert close["context"]["positions"][0]["quantity"] == 3
    assert get_latest_snapshot_summary(key)["total_value"] == 1000


def test_stale_price_cannot_finalize_close(ledger):
    from core.basket_close import finalize_basket_closes
    from database.repositories import get_portfolio_valuations
    from config.config_loader import Config
    seed(valuation_context={"valuation_kind": "intraday", "positions": [{"symbol": "A", "quantity": 9, "price": 100}]})
    rb = SimpleNamespace(config=Config.get(), account_key="basket_rebalance:test", _ledger_mode=lambda: "paper",
        data_collector=SimpleNamespace(fetch_korean_stock=lambda *a: pd.DataFrame({"date": [datetime(2026, 10, 9)], "close": [100]})))
    assert finalize_basket_closes(rb, datetime(2026, 10, 13, 10)) == []
    assert get_portfolio_valuations(rb.account_key, kind="close") == []
