"""Actual executor/ledger observations, cumulative broker reconciliation and read-only reports."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from core.execution_analytics import execution_cost_report


@pytest.fixture
def executor(monkeypatch):
    from config.config_loader import Config
    from core.order_executor import OrderExecutor
    from database.models import get_engine, init_database
    assert os.environ.get("QUANT_DB_PATH")
    assert Path(os.environ["QUANT_DB_PATH"]).resolve() != Path("data/quant_trader.db").resolve()
    monkeypatch.setattr(Config, "_instance", None)
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("Execution tests must be offline"))
    monkeypatch.setattr("core.notifier.Notifier", lambda *a, **k: SimpleNamespace())
    init_database()
    ex = OrderExecutor(account_key="execution_test_" + uuid.uuid4().hex)
    ex.config.trading["skip_earnings_days"] = 0
    ex.config.risk_params["gap_risk"]["enabled"] = False
    ex.config.risk_params["transaction_costs"] = {
        "commission_rate": .001, "tax_rate": .002, "slippage": .001,
        "slippage_ticks": 0, "dynamic_slippage": {"enabled": False},
    }
    monkeypatch.setattr(ex, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(ex, "_pre_order_check", lambda **kw: {"allowed": True})
    monkeypatch.setattr(ex, "_report_buy_rejection", lambda *a, **kw: None)
    monkeypatch.setattr(ex.risk_manager, "check_correlation_risk", lambda *a, **kw: {"scale": 1., "high_corr_symbols": [], "reason": ""})
    ex.report_path = get_engine().url.database
    yield ex
    from core.order_guard import OrderGuard
    OrderGuard.clear("005930")


def _buy(executor, **kwargs):
    return executor.execute_buy_quantity(
        symbol="005930", price=60000, quantity=5, capital=10000000,
        available_cash=10000000, strategy="execution_observation_test", avg_daily_volume=1000000,
        **kwargs,
    )


def _report(executor):
    return execution_cost_report(account_key=executor.account_key, db_path=executor.report_path)


def test_actual_paper_round_trip_observes_decision_and_signed_fill_costs(executor):
    decision_at = (datetime.now(timezone(timedelta(hours=9))) - timedelta(seconds=20)).isoformat()
    bought = _buy(executor, decision_context={"price": 59900, "at": decision_at, "source": "basket_plan"})
    assert bought["success"], bought
    sold = executor.execute_sell("005930", 61000, quantity=5, emergency=True)
    assert sold["success"], sold
    report = _report(executor)
    rows = {row["action"]: row for row in report["orders"]}
    buy, sell = rows["BUY"], rows["SELL"]
    assert buy["reference_kind"] == "basket_plan" and buy["reference_price"] == 59900
    assert buy["reference_at"] == decision_at
    assert buy["price_shortfall_amount"] == (60060 - 59900) * 5
    assert sell["reference_kind"] == "executor_input"
    assert sell["price_shortfall_amount"] == (61000 - 60939) * 5
    for row, result in ((buy, bought), (sell, sold)):
        assert row["order_id"] == result["order_id"]
        assert row["fill_price_basis"] == row["fees_basis"] == "modeled"
        assert row["submitted_at"] and row["fill_observed_at"]
        assert row["reference_to_submit_seconds"] >= 0
        assert row["price_plus_fees_amount"] == row["price_shortfall_amount"] + result["costs"]["commission"] + result["costs"]["tax"]
        assert row["fully_observed_total_cost_amount"] is None
        assert row["opportunity_cost_amount"] is None and not row["opportunity_cost_included"]
    assert report["coverage"]["attempts"] == report["coverage"]["filled_orders"] == 2
    assert sum(group["filled_orders"] for group in report["summary_by_basis"]) == 2


def test_preorder_rejection_is_recorded_without_inventing_a_fill(executor):
    result = executor.execute_buy_quantity("005930", 60000, 0, 10000000, 10000000)
    assert not result["success"]
    row = _report(executor)["orders"][0]
    assert row["status"] == "REJECTED_BEFORE_ORDER" and row["order_id"] is None
    assert row["filled_qty"] == 0 and row["price_shortfall_amount"] is None
    assert row["reason"] == "quantity must be positive"


def test_observation_storage_failure_does_not_change_actual_fill(executor, monkeypatch):
    from database.repositories import get_position
    monkeypatch.setattr("database.repositories.append_execution_cost_event", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk unavailable")))
    result = _buy(executor)
    assert result["success"] and result["execution_observability"] == "incomplete"
    assert get_position("005930", account_key=executor.account_key).quantity == 5
    assert _report(executor)["coverage"]["legacy_unmeasured_orders"] == 1


def _live(executor, monkeypatch, execution):
    executor.mode = "live"
    executor.kis_api = SimpleNamespace(buy_order=lambda *a, **kw: {"odno": "123456"})
    monkeypatch.setattr(executor, "_live_buy_gate_check", lambda *a, **kw: {"allowed": True})
    monkeypatch.setattr(executor, "_live_unfilled_order_block", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_resolve_live_execution", lambda *a, **kw: execution)
    monkeypatch.setattr(executor, "_halt_after_unconfirmed_live_execution", lambda **kw: None, raising=False)
    return executor


def test_real_partial_fill_recovery_updates_cumulative_quantity_once(executor, monkeypatch):
    from database.models import ExecutionCostEvent, get_session
    from database.repositories import reconcile_order_record
    ex = _live(executor, monkeypatch, {"confirmed": False, "filled_qty": 2, "fill_price": 60090,
                                      "remaining_qty": 3, "reason": "partial", "actual_slippage_pct": .15})
    pending = _buy(ex)
    assert not pending["success"] and pending["order_pending"], pending
    partial = _report(ex)["orders"][0]
    assert partial["status"] == "PARTIAL_FILLED" and partial["filled_qty"] == 2
    assert partial["price_shortfall_amount"] == 180
    assert partial["fees_basis"] == "unavailable"
    # Actual recovery path fetches a new cumulative broker average, without a resend.
    ex.kis_api.get_order_execution_after_order = lambda *a, **kw: {"filled_qty": 5, "fill_price": 60100, "remaining_qty": 0}
    recovered = ex._reconcile_persistent_open_order_records([])
    assert len(recovered) == 1
    reconcile_order_record(pending["order_id"], filled_qty=5, filled_price=60100, execution_price_observed=True,
                           reason="broker_execution_confirmed_after_recovery_check")
    report = _report(ex)
    row = report["orders"][0]
    assert row["filled_qty"] == 5 and row["price_shortfall_amount"] == 500
    assert row["remaining_qty"] == 0 and row["fill_price_basis"] == "broker_observed"
    assert row["price_plus_fees_amount"] is None  # no broker fees / full-size fee estimate
    assert report["coverage"]["attempts"] == 1
    assert sum(g["price_shortfall_amount"] or 0 for g in report["summary_by_basis"]) == 500
    with get_session() as session:
        assert session.query(ExecutionCostEvent).filter_by(order_id=pending["order_id"], event_type="reconciliation").count() == 1


def test_reconciliation_cannot_reuse_partial_price_for_more_shares(executor, monkeypatch):
    from database.repositories import reconcile_order_record
    ex = _live(executor, monkeypatch, {"confirmed": False, "filled_qty": 2, "fill_price": 60090,
                                      "remaining_qty": 3, "reason": "partial"})
    pending = _buy(ex)
    assert pending["order_pending"], pending
    ex.kis_api.get_order_execution_after_order = lambda *a, **kw: {"filled_qty": 5, "remaining_qty": 0}
    assert ex._reconcile_persistent_open_order_records([])
    row = _report(ex)["orders"][0]
    assert row["filled_qty"] == 5 and row["filled_price"] is None
    assert row["measurement_status"] == "fill_price_unavailable"
    assert row["price_shortfall_amount"] is None
    # A later close/cancel update still carrying the old ledger price cannot
    # upgrade the missing cumulative average into observed evidence.
    reconcile_order_record(pending["order_id"], filled_qty=5, filled_price=60090)
    assert _report(ex)["orders"][0]["filled_price"] is None


def test_confirmed_live_fill_does_not_label_model_fees_as_actual(executor, monkeypatch):
    ex = _live(executor, monkeypatch, {"confirmed": True, "filled_qty": 5, "fill_price": 59900,
                                      "remaining_qty": 0, "actual_slippage_pct": -.1667})
    result = _buy(ex)
    assert result["success"], result
    row = _report(ex)["orders"][0]
    assert row["price_shortfall_amount"] == -500  # price improvement remains negative
    assert row["fees_basis"] == "estimated" and row["fill_price_basis"] == "broker_observed"
    assert row["fully_observed_total_cost_amount"] is None
    assert row["price_plus_fees_amount"] == -500 + result["costs"]["commission"]


@pytest.mark.parametrize("unknown_response", [False, True])
def test_broker_retry_or_unknown_response_is_one_unfilled_attempt(executor, monkeypatch, unknown_response):
    from api.kis_api import KISOrderResponseUnknown
    ex = _live(executor, monkeypatch, {})
    calls = []
    def broker(*args):
        calls.append(args)
        if unknown_response:
            raise KISOrderResponseUnknown("test response lost")
        return None
    ex.kis_api.buy_order = broker
    ex.kis_api.use_mock = True
    monkeypatch.setattr("core.order_executor.time_mod.sleep", lambda seconds: None)
    result = _buy(ex)
    assert not result["success"]
    assert len(calls) == (1 if unknown_response else 3)
    report = _report(ex)
    assert report["coverage"]["attempts"] == 1
    row = report["orders"][0]
    assert row["status"] == ("SUBMITTED" if unknown_response else "REJECTED")
    assert row["filled_qty"] == 0 and row["price_shortfall_amount"] is None
    assert row["broker_environment"] == "vts"


def test_since_offsets_limits_and_account_scope_are_consistent(executor):
    assert _buy(executor)["success"]
    assert executor.execute_sell("005930", 61000, quantity=5, emergency=True)["success"]
    report = execution_cost_report(db_path=executor.report_path, account_key=executor.account_key,
                                   since=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), limit=1)
    assert report["coverage"]["total_matching_attempts"] == 2
    assert report["coverage"]["attempts"] == 1 and report["coverage"]["truncated"]
    assert report["orders"][0]["action"] == "SELL"
    assert execution_cost_report(db_path=executor.report_path, account_key="unrelated")["orders"] == []
    assert execution_cost_report(db_path=executor.report_path, since="not-a-date")["reason"] == "invalid_since_timestamp"


def test_report_missing_database_and_schema_never_create_or_migrate(tmp_path):
    missing = tmp_path / "absent.db"
    assert execution_cost_report(db_path=missing)["reason"] == "database_missing"
    assert not missing.exists()
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as connection:
        connection.execute("CREATE TABLE unrelated (x INTEGER)")
    before = hashlib.sha256(old.read_bytes()).hexdigest()
    assert execution_cost_report(db_path=old)["reason"] == "execution_observation_schema_missing"
    assert hashlib.sha256(old.read_bytes()).hexdigest() == before
    assert {p.name for p in tmp_path.iterdir()} == {"old.db"}


def test_legacy_trade_is_missing_coverage_never_backfilled(executor):
    from database.repositories import save_trade
    save_trade(symbol="005930", action="BUY", price=59000, quantity=1,
               account_key=executor.account_key, mode="paper", order_id="legacy-before-analytics", expected_price=58000)
    save_trade(symbol="005930", action="SELL", price=59000, quantity=1,
               account_key=executor.account_key, mode="paper")
    report = _report(executor)
    assert report["orders"] == []
    assert report["coverage"]["legacy_unmeasured_orders"] == 1
    assert report["coverage"]["legacy_unlinked_trades"] == 1


def test_cli_reads_persisted_report_without_changing_ledger(executor, tmp_path):
    assert _buy(executor)["success"]
    # Backup a consistent read-only snapshot, no copies of production data here.
    target = tmp_path / "cli.db"
    with sqlite3.connect(executor.report_path) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    completed = subprocess.run([sys.executable, "tools/execution_cost_report.py", "--db", str(target),
                                "--account-key", executor.account_key], capture_output=True, text=True,
                               encoding="utf-8", check=True)
    report = json.loads(completed.stdout)
    assert report["coverage"]["filled_orders"] == 1
    assert report["orders"][0]["currency"] == "KRW"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
