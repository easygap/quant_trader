"""Forward close observations without orders or operating NAV rewrites."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from core.basket_close import collect_basket_closes, finalize_basket_closes, DIRECT_CLOSE_METHOD


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import database.models as models
    from config.config_loader import Config
    monkeypatch.setenv("QUANT_DB_PATH", str(tmp_path / "close.db"))
    monkeypatch.setenv("QUANT_RUNTIME_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(Config, "_instance", None)
    for name in ("_engine", "_ScopedSession", "_SessionFactory"):
        monkeypatch.setattr(models, name, None)
    models.init_database()
    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer._load_baskets_config", lambda: {
        "test": {"enabled": True, "initial_capital": 1000}, "off": {"enabled": False, "initial_capital": 1000},
    })
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("no network in close tests"))
    monkeypatch.setattr("core.order_executor.OrderExecutor.execute_buy", lambda *a, **k: pytest.fail("no orders"))
    monkeypatch.setattr("core.order_executor.OrderExecutor.execute_buy_quantity", lambda *a, **k: pytest.fail("no orders"))
    monkeypatch.setattr("core.order_executor.OrderExecutor.execute_sell", lambda *a, **k: pytest.fail("no orders"))
    with models.get_session() as session:
        session.add(models.TradeHistory(account_key="basket_rebalance:test", mode="paper", symbol="005930", action="BUY",
                                        price=100, quantity=5, total_amount=500, commission=1, tax=0,
                                        executed_at=datetime(2026, 10, 13, 10)))
        session.add(models.Position(account_key="basket_rebalance:test", mode="paper", symbol="005930", quantity=5,
                                    avg_price=100, total_invested=500))
        session.commit()
    yield models
    if models._engine:
        models._engine.dispose()


def prices(value=110, wrong_day=None):
    return SimpleNamespace(fetch_korean_stock=lambda symbol, start, end: pd.DataFrame({"date": [wrong_day or start], "close": [value]}),
                           get_last_source_info=lambda: {"source": "fixture"})


@pytest.mark.parametrize("hour", [8, 10, 15])
def test_intraday_only_reads_missing_coverage_without_orm_or_prices(ledger, monkeypatch, hour):
    from config.config_loader import Config
    database = Path(Config.get().database["sqlite_path"])
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr("database.models.get_session", lambda: pytest.fail("no ORM access before settlement"))
    result = collect_basket_closes(Config.get(), now=datetime(2026, 10, 13, hour, 30),
                                    collector_factory=lambda: pytest.fail("no provider before settlement"))
    assert result["status"] == "previous_close_missing" and result["rows_saved"] == 0
    assert result["last_completed_session"] == "2026-10-12"
    assert result["coverage"]["missing"] == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_weekend_reports_friday_gap_and_detects_existing_close_read_only(ledger, monkeypatch):
    from config.config_loader import Config
    from database.repositories import append_portfolio_valuation
    now = datetime(2026, 10, 17, 8, 30)
    result = collect_basket_closes(now=now, collector_factory=lambda: pytest.fail("no holiday prices"))
    assert result["status"] == "previous_close_missing"
    assert result["expected_session"] == "2026-10-16"
    append_portfolio_valuation(account_key="basket_rebalance:test", mode="paper", day="2026-10-16",
        measured_at=datetime(2026, 10, 16, 15, 30), total_value=1049, cash=499, cumulative_return=0,
        daily_return=0, mdd=0, context={"valuation_kind": "close"})
    database = Path(Config.get().database["sqlite_path"])
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr("database.models.get_session", lambda: pytest.fail("coverage must not initialize ORM"))
    result = collect_basket_closes(now=now, collector_factory=lambda: pytest.fail("no holiday prices"))
    assert result["status"] == "awaiting_close" and result["coverage"]["present"] == 1
    assert result["coverage"]["verification"] == "presence_only"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_missing_schema_is_reported_without_migration(tmp_path, monkeypatch):
    from config.config_loader import Config
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER)")
    monkeypatch.setenv("QUANT_DB_PATH", str(database))
    monkeypatch.setattr(Config, "_instance", None)
    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer._load_baskets_config", lambda: {"test": {"enabled": True}})
    before = database.read_bytes()
    result = collect_basket_closes(now=datetime(2026, 10, 13, 8, 30))
    assert result["status"] == "previous_close_missing"
    assert result["coverage"]["reason"] == "close_schema_missing"
    assert database.read_bytes() == before


def test_first_direct_close_requires_no_intraday_seed_and_preserves_operating_ledger(ledger):
    from database.repositories import get_portfolio_valuations
    result = collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=prices)
    assert result["status"] == "completed" and result["rows_saved"] == 1
    close = get_portfolio_valuations("basket_rebalance:test", kind="close")[0]
    assert close["cash"] == 499 and close["total_value"] == 1049
    assert close["cumulative_return"] == 0 and close["context"]["first_close_in_series"]
    assert close["context"]["return_method"] == DIRECT_CLOSE_METHOD
    assert close["context"]["baseline_valuation_id"] is None
    with ledger.get_session() as session:
        assert session.query(ledger.PortfolioSnapshot).count() == 0
        assert session.query(ledger.TradeHistory).count() == 1
        assert session.query(ledger.CashFlow).count() == 0
        assert session.query(ledger.Position).one().quantity == 5
    again = collect_basket_closes(now=datetime(2026, 10, 13, 17), collector_factory=prices)
    assert again["rows_saved"] == 0 and again["baskets"][0]["status"] == "already_recorded"


def test_both_writers_share_real_os_lock_and_leave_no_duplicate(ledger):
    from core.basket_close import _close_lock
    from database.repositories import get_portfolio_valuations
    with _close_lock() as acquired:
        assert acquired
        result = collect_basket_closes(now=datetime(2026, 10, 13, 16, 30),
                                      collector_factory=lambda: pytest.fail("locked collector must not price"))
        assert result["status"] == "busy" and result["rows_saved"] == 0
        assert finalize_basket_closes(SimpleNamespace(), datetime(2026, 10, 13, 16, 30)) == []
    assert get_portfolio_valuations("basket_rebalance:test", kind="close") == []
    assert collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=prices)["rows_saved"] == 1


@pytest.mark.parametrize("bad", ["null_cost", "fractional_quantity", "future_flow"])
def test_incomplete_cost_or_invalid_ledger_fails_before_price_fetch(ledger, bad):
    from database.repositories import get_portfolio_valuations
    with ledger.get_session() as session:
        if bad == "null_cost":
            session.query(ledger.TradeHistory).one().commission = None
        elif bad == "fractional_quantity":
            session.query(ledger.Position).one().quantity = 5.5
        else:
            session.add(ledger.CashFlow(account_key="basket_rebalance:test", mode="paper", amount=100,
                                        occurred_at=datetime(2026, 10, 14, 10)))
        session.commit()
    provider = prices()
    provider.fetch_korean_stock = lambda *a: pytest.fail("invalid ledger must not price")
    result = collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=lambda: provider)
    assert result["status"] == "unavailable" and result["rows_saved"] == 0
    assert get_portfolio_valuations("basket_rebalance:test", kind="close") == []


def test_after_close_trade_and_deposit_are_excluded_from_close_cutoff(ledger):
    from database.repositories import get_portfolio_valuations
    with ledger.get_session() as session:
        session.add(ledger.TradeHistory(account_key="basket_rebalance:test", mode="paper", symbol="005930", action="SELL",
                                        quantity=2, price=120, total_amount=240, commission=1, tax=1,
                                        executed_at=datetime(2026, 10, 13, 16)))
        session.query(ledger.Position).one().quantity = 3
        session.add(ledger.CashFlow(account_key="basket_rebalance:test", mode="paper", amount=100,
                                    occurred_at=datetime(2026, 10, 13, 16)))
        session.commit()
    result = collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=prices)
    assert result["rows_saved"] == 1
    close = get_portfolio_valuations("basket_rebalance:test", kind="close")[0]
    assert close["cash"] == 499 and close["context"]["positions"][0]["quantity"] == 5
    assert close["total_value"] == 1049
    # The next true close includes both the later trade and contribution.
    result = collect_basket_closes(now=datetime(2026, 10, 14, 16, 30), collector_factory=prices)
    assert result["rows_saved"] == 1
    second = get_portfolio_valuations("basket_rebalance:test", kind="close")[-1]
    assert second["cash"] == 837 and second["total_value"] == 1167
    assert second["context"]["flow_since"] == 100
    assert second["cumulative_return"] == pytest.approx((1167 / 1149 - 1) * 100)


def test_intraday_recovery_continues_direct_series_without_old_profit_jump(ledger):
    from config.config_loader import Config
    from database.repositories import append_portfolio_valuation, get_portfolio_valuations
    collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=prices)
    append_portfolio_valuation(account_key="basket_rebalance:test", mode="paper", day="2026-10-14",
        measured_at=datetime(2026, 10, 14, 10), total_value=999, cash=499, cumulative_return=80,
        daily_return=0, mdd=0, context={"valuation_kind": "intraday", "positions": [{"symbol": "005930", "quantity": 5, "price": 100}]})
    rb = SimpleNamespace(config=Config.get(), account_key="basket_rebalance:test", data_collector=prices(120),
                         portfolio_mgr=SimpleNamespace(initial_capital=1000), _ledger_mode=lambda: "paper")
    assert finalize_basket_closes(rb, datetime(2026, 10, 15, 8, 30)) == ["2026-10-14"]
    close = get_portfolio_valuations(rb.account_key, kind="close")[-1]
    assert close["total_value"] == 1099
    assert close["cumulative_return"] == pytest.approx((1099 / 1049 - 1) * 100)
    assert close["context"]["return_method"] == DIRECT_CLOSE_METHOD
    assert close["context"]["initial_capital"] == 1000
    assert len(close["context"]["trade_ids"]) == 1


def test_actual_writer_rows_pass_paper_validation_with_deposit_and_recovery(ledger):
    from config.config_loader import Config
    import core.paper_validation as validation
    from database.repositories import append_portfolio_valuation
    key = "basket_rebalance:test"
    assert collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=prices)["rows_saved"] == 1
    with ledger.get_session() as session:
        session.add(ledger.CashFlow(account_key=key, mode="paper", amount=100, occurred_at=datetime(2026, 10, 14, 9)))
        session.commit()
    assert collect_basket_closes(now=datetime(2026, 10, 14, 16, 30), collector_factory=lambda: prices(120))["rows_saved"] == 1
    append_portfolio_valuation(account_key=key, mode="paper", day="2026-10-15", measured_at=datetime(2026, 10, 15, 10),
        total_value=1099, cash=599, cumulative_return=80, daily_return=0, mdd=0,
        context={"valuation_kind": "intraday", "positions": [{"symbol": "005930", "quantity": 5, "price": 100}]})
    rb = SimpleNamespace(config=Config.get(), account_key=key, data_collector=prices(130),
                         portfolio_mgr=SimpleNamespace(initial_capital=1000), _ledger_mode=lambda: "paper")
    assert finalize_basket_closes(rb, datetime(2026, 10, 16, 8, 30)) == ["2026-10-15"]
    # The fixture clock is ahead of wall time. Set only this isolated fixture's
    # recording timestamps to follow each simulated observation.
    with ledger.get_session() as session:
        for row in session.query(ledger.PortfolioValuation):
            observed = [datetime.fromisoformat(p["observed_at"]) for p in json.loads(row.detail).get("positions", []) if "observed_at" in p]
            row.recorded_at = max([row.measured_at] + observed) + timedelta(minutes=1)
        session.commit()
    database, issues = validation._read_database(Path(Config.get().database["sqlite_path"]).resolve())
    assert not issues
    by_id = {row["id"]: row for row in database["portfolio_valuations"]}
    closes = sorted((row for row in by_id.values() if row["kind"] == "close"), key=lambda row: row["date"])
    previous = None
    for row in closes:
        source = validation._source_close(row, by_id, datetime(2026, 10, 16, 17, tzinfo=validation.KST), initial_capital=1000)
        end = validation._dt(source["measured_at"])
        _, issues = validation._ledger_interval(database, key, previous or end, end, source)
        assert not issues
        previous = end
    assert len(closes) == 3


@pytest.mark.parametrize("bad", ["stale", "missing", "positions"])
def test_unverified_prices_or_ledger_never_make_a_close(ledger, bad):
    from database.repositories import get_portfolio_valuations
    if bad == "positions":
        with ledger.get_session() as session:
            session.query(ledger.Position).one().quantity = 6
            session.commit()
    provider = prices(wrong_day="2026-10-12") if bad == "stale" else prices()
    if bad == "missing":
        provider.fetch_korean_stock = lambda *a: pd.DataFrame()
    result = collect_basket_closes(now=datetime(2026, 10, 13, 16, 30), collector_factory=lambda: provider)
    assert result["status"] == "unavailable" and result["rows_saved"] == 0
    assert get_portfolio_valuations("basket_rebalance:test", kind="close") == []
