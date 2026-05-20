import json
import hashlib
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


TEST_EXECUTION_SESSION_ID = "target_weight_candidate_2026-04-10_test_session"


def _daily_ops_with_summary_hash(payload: dict) -> dict:
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    payload["summary_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


class FakeCollector:
    def __init__(self, frames):
        self.frames = frames

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["close"])
        return df.copy()


def _ohlcv(dates, close):
    close = np.array(close, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [1_000_000] * len(close),
        }
    )


def _frames_for_rotation():
    dates = pd.bdate_range("2025-01-01", "2025-03-10")
    steps = np.arange(len(dates), dtype=float)
    a = 100 + steps * 0.5
    b = 100 + np.minimum(steps, 24) * 0.9 - np.maximum(steps - 24, 0) * 0.8
    c = 100 + np.maximum(steps - 24, 0) * 1.4
    benchmark = np.full(len(dates), 100.0)
    return {
        "AAA": _ohlcv(dates, a),
        "BBB": _ohlcv(dates, b),
        "CCC": _ohlcv(dates, c),
        "KS11": _ohlcv(dates, benchmark),
    }


def _params():
    return {
        "target_top_n": 2,
        "target_exposure": 0.80,
        "target_tolerance_pct": 0.0,
        "short_lookback": 2,
        "long_lookback": 3,
        "short_weight": 0.5,
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
    }


def _plan_execution_now():
    return datetime(2026, 4, 10, 9, 0)


def _adapter_plan():
    from core.target_weight_rotation import TargetWeightOrder, TargetWeightPlan

    orders = [
        TargetWeightOrder(
            symbol="AAA",
            action="BUY",
            price=100.0,
            quantity=11_000,
            notional=1_100_000.0,
            current_quantity=0,
            target_quantity=11_000,
            current_weight_pct=0.0,
            target_weight_pct=11.0,
            reason="target_weight_rebalance_buy",
        ),
        TargetWeightOrder(
            symbol="BBB",
            action="BUY",
            price=200.0,
            quantity=4_500,
            notional=900_000.0,
            current_quantity=0,
            target_quantity=4_500,
            current_weight_pct=0.0,
            target_weight_pct=9.0,
            reason="target_weight_rebalance_buy",
        ),
        TargetWeightOrder(
            symbol="CCC",
            action="BUY",
            price=300.0,
            quantity=4_000,
            notional=1_200_000.0,
            current_quantity=0,
            target_quantity=4_000,
            current_weight_pct=0.0,
            target_weight_pct=12.0,
            reason="target_weight_rebalance_buy",
        ),
    ]
    return TargetWeightPlan(
        candidate_id="target_weight_candidate",
        as_of_date="2026-04-10",
        trade_day="2026-04-10",
        score_day="2026-04-09",
        params_hash="hash",
        symbols=["AAA", "BBB", "CCC"],
        targets=["AAA", "BBB", "CCC"],
        prices={"AAA": 100.0, "BBB": 200.0, "CCC": 300.0},
        target_exposure=0.32,
        base_target_exposure=0.8,
        risk_off=True,
        nav=10_000_000.0,
        cash_before=10_000_000.0,
        market_value_before=0.0,
        cash_after_estimate=6_800_000.0,
        gross_exposure_after=3_200_000.0,
        target_position_count=3,
        orders=orders,
        diagnostics={
            "missing_symbols": [],
            "benchmark_symbol": "KS11",
            "price_last_dates": {
                "AAA": "2026-04-10",
                "BBB": "2026-04-10",
                "CCC": "2026-04-10",
            },
            "benchmark_last_date": "2026-04-10",
            "missing_position_symbols": [],
            "position_avg_prices_before": {},
            "liquidity": {
                "lookback_days": 20,
                "symbols": {
                    order.symbol: {
                        "complete": True,
                        "reason": "liquidity window available",
                        "observations": 20,
                        "avg_daily_value": 100_000_000.0,
                        "last_daily_value": 100_000_000.0,
                    }
                    for order in orders
                },
            },
        },
        target_quantities_after={order.symbol: order.target_quantity for order in orders},
        position_quantities_before={},
    )


def _pilot_caps_for_plan(plan, *, snapshot=None):
    import tools.target_weight_rotation_pilot as twp

    snapshot = snapshot if snapshot is not None else twp.build_pilot_authorization_snapshot(plan)
    return {
        "max_orders_per_day": 10,
        "max_concurrent_positions": 10,
        "max_notional_per_trade": 2_000_000,
        "max_gross_exposure": 10_000_000,
        "target_weight_plan_snapshot": snapshot,
    }


def _pilot_check_for_plan(plan, *, snapshot=None):
    snapshot = snapshot if snapshot is not None else _pilot_caps_for_plan(plan)["target_weight_plan_snapshot"]
    return SimpleNamespace(
        allowed=True,
        reason="ok",
        remaining_orders=10,
        remaining_exposure=10_000_000,
        auth={
            "strategy": plan.candidate_id,
            "enabled": True,
            "target_weight_plan_snapshot": snapshot,
        },
        caps_snapshot=_pilot_caps_for_plan(plan, snapshot=snapshot),
    )


def _execute_plan_submit_guards_ok(plan):
    return {
        "pilot_validation": SimpleNamespace(allowed=True, reason="ok"),
        "preflight_refresh": {
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
        },
        "execution_trade_day_check": {
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight execution trade day matches current KST date",
            "plan_trade_day": plan.trade_day,
            "execution_day": plan.trade_day,
        },
        "execution_market_session_check": {
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight execution market session is open",
        },
        "pilot_authorization_snapshot_check": {
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight pilot authorization snapshot matches current plan",
            "mismatches": [],
        },
    }


@pytest.fixture(autouse=True)
def _target_weight_preflight_refresh_ok(monkeypatch):
    import tools.target_weight_rotation_pilot as twp

    def _ok(strategy, date):
        return {
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
            "strategy": strategy,
            "date": date,
            "overall": "pass",
            "entry_allowed": True,
            "runtime_state": "normal",
            "notifier_health": "configured",
            "pilot_authorized": True,
            "blocking_requirements": [],
            "block_reasons": [],
        }

    monkeypatch.setattr(twp, "refresh_paper_preflight_status", _ok)


def _complete_execution(plan, execution_session_id=TEST_EXECUTION_SESSION_ID):
    details = []
    for index, order in enumerate(plan.orders):
        order_id = f"ORD-TW-{index + 1:03d}"
        result = {
            "success": True,
            "symbol": order.symbol,
            "action": order.action,
            "price": order.price,
            "quantity": order.quantity,
            "mode": "paper",
            "execution_session_id": execution_session_id,
            "order_id": order_id,
        }
        if order.action == "BUY":
            result["paper_fixed_quantity"] = True
        details.append({
            "order": {
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity,
            },
            "status": "success",
            "result": result,
        })
    return {
        "executed": len(plan.orders),
        "skipped": 0,
        "failed": 0,
        "halted": False,
        "halt_reason": "",
        "details": details,
        "execution_session_id": execution_session_id,
    }


def _complete_fills(plan, execution_session_id=TEST_EXECUTION_SESSION_ID):
    return [
        SimpleNamespace(
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            strategy=plan.candidate_id,
            mode="paper",
            account_key=plan.candidate_id,
            executed_at=f"{plan.trade_day} 09:00:00",
            execution_session_id=execution_session_id,
            order_id=f"ORD-TW-{index + 1:03d}",
        )
        for index, order in enumerate(plan.orders)
    ]


class _StaticTradeQuery:
    def __init__(self, trades):
        self._trades = list(trades)

    def filter(self, *args):
        return self

    def all(self):
        return list(self._trades)


class _StaticTradeSession:
    def __init__(self, trades):
        self._trades = list(trades)

    def query(self, model):
        return _StaticTradeQuery(self._trades)

    def close(self):
        pass


def _paper_trade_history_for_plan(plan, *, start_date="2026-01-05", day_count=60, daily_sell_count=0):
    from database.models import TradeHistory

    trades = []
    start = datetime.fromisoformat(start_date)
    for day_index in range(day_count):
        for index, order in enumerate(plan.orders):
            action = "SELL" if index >= len(plan.orders) - daily_sell_count else order.action
            trades.append(TradeHistory(
                account_key=plan.candidate_id,
                symbol=order.symbol,
                action=action,
                price=order.price,
                quantity=order.quantity,
                total_amount=order.price * order.quantity,
                commission=0.0,
                tax=0.0,
                slippage=0.0,
                expected_price=order.price,
                actual_slippage_pct=0.0,
                execution_session_id=TEST_EXECUTION_SESSION_ID,
                order_id=f"ORD-TW-{day_index + 1:03d}-{index + 1:03d}",
                strategy=plan.candidate_id,
                reason="target weight e2e fill quality fixture",
                mode="paper",
                executed_at=start + pd.Timedelta(days=day_index, hours=9, minutes=index),
                price_gap=0.0,
            ))
    return trades


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        ({"executed": 0, "failed": 0, "details": [{"status": "skipped_liquidity_preflight"}]}, False),
        ({"executed": 0, "failed": 0, "details": [{"status": "skipped_pre_trade_risk"}]}, False),
        ({"executed": 1, "failed": 0, "details": [{"status": "success"}]}, True),
        ({"executed": 0, "failed": 1, "details": [{"status": "failed"}]}, True),
        ({"executed": 0, "failed": 0, "details": [{"status": "exception"}]}, True),
    ],
)
def test_execution_reached_order_submission_tracks_actual_order_attempts(execution, expected):
    from tools.target_weight_rotation_pilot import _execution_reached_order_submission

    assert _execution_reached_order_submission(execution) is expected


def test_validate_execution_trade_day_allows_same_kst_day():
    from tools.target_weight_rotation_pilot import validate_execution_trade_day

    check = validate_execution_trade_day(_adapter_plan(), now=_plan_execution_now())

    assert check["allowed"] is True
    assert check["execution_day"] == "2026-04-10"
    assert check["plan_trade_day"] == "2026-04-10"


def test_validate_execution_trade_day_blocks_stale_plan():
    from tools.target_weight_rotation_pilot import validate_execution_trade_day

    check = validate_execution_trade_day(
        _adapter_plan(),
        now=datetime(2026, 4, 11, 9, 0),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert check["execution_day"] == "2026-04-11"
    assert "target_weight_execution_trade_day_mismatch" in check["reason"]


def test_validate_execution_market_session_allows_regular_session():
    from tools.target_weight_rotation_pilot import validate_execution_market_session

    check = validate_execution_market_session(
        _adapter_plan(),
        config=SimpleNamespace(trading={"mode": "paper"}),
        now=datetime(2026, 4, 10, 10, 0),
    )

    assert check["allowed"] is True
    assert check["complete"] is True
    assert check["execution_day"] == "2026-04-10"
    assert check["execution_time"] == "10:00:00"


def test_validate_execution_market_session_blocks_after_close():
    from tools.target_weight_rotation_pilot import validate_execution_market_session

    check = validate_execution_market_session(
        _adapter_plan(),
        config=SimpleNamespace(trading={"mode": "paper"}),
        now=datetime(2026, 4, 10, 16, 0),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert check["execution_day"] == "2026-04-10"
    assert "target_weight_execution_market_session_closed" in check["reason"]


def test_validate_pilot_authorization_snapshot_accepts_matching_plan():
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    check = twp.validate_pilot_authorization_snapshot(plan, _pilot_check_for_plan(plan))

    assert check["allowed"] is True
    assert check["complete"] is True
    assert check["mismatches"] == []


def test_validate_pilot_authorization_snapshot_requires_snapshot_for_prefixless_plan():
    import tools.target_weight_rotation_pilot as twp

    plan = replace(_adapter_plan(), candidate_id="risk_overlay_candidate")
    pilot_check = SimpleNamespace(
        allowed=True,
        reason="ok",
        auth={"strategy": plan.candidate_id, "enabled": True},
        caps_snapshot={"max_orders_per_day": 10},
    )

    check = twp.validate_pilot_authorization_snapshot(plan, pilot_check)

    assert check["allowed"] is False
    assert check["complete"] is False
    assert "target_weight_pilot_authorization_snapshot_missing" in check["reason"]


def test_validate_pilot_authorization_snapshot_accepts_prefixless_matching_plan():
    import tools.target_weight_rotation_pilot as twp

    plan = replace(_adapter_plan(), candidate_id="risk_overlay_candidate")
    check = twp.validate_pilot_authorization_snapshot(plan, _pilot_check_for_plan(plan))

    assert check["allowed"] is True
    assert check["complete"] is True
    assert check["mismatches"] == []


def test_validate_pilot_authorization_snapshot_allows_small_money_drift():
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    snapshot = twp.build_pilot_authorization_snapshot(plan)
    snapshot["gross_exposure_after"] += 1_000.0
    snapshot["max_order_notional"] += 1_000.0

    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=snapshot),
    )

    assert check["allowed"] is True
    assert check["complete"] is True
    assert check["mismatches"] == []


def test_validate_pilot_authorization_snapshot_blocks_large_money_drift():
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    snapshot = twp.build_pilot_authorization_snapshot(plan)
    tolerance = twp._authorization_snapshot_money_tolerance(snapshot["gross_exposure_after"])
    snapshot["gross_exposure_after"] += tolerance + 1.0

    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=snapshot),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert "target_weight_pilot_authorization_snapshot_mismatch" in check["reason"]
    assert check["mismatches"][0]["field"] == "gross_exposure_after"
    assert check["mismatches"][0]["tolerance"] == pytest.approx(tolerance)


def test_validate_pilot_authorization_snapshot_blocks_params_hash_mismatch():
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_snapshot = twp.build_pilot_authorization_snapshot(
        replace(plan, params_hash="old-hash")
    )
    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert "target_weight_pilot_authorization_snapshot_mismatch" in check["reason"]
    assert check["mismatches"][0]["field"] == "params_hash"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 0),
        ("snapshot_type", "legacy_target_weight_authorization"),
    ],
)
def test_validate_pilot_authorization_snapshot_blocks_snapshot_contract_mismatch(
    field,
    value,
):
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_snapshot = twp.build_pilot_authorization_snapshot(plan)
    stale_snapshot[field] = value

    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert "target_weight_pilot_authorization_snapshot_mismatch" in check["reason"]
    assert check["mismatches"][0]["field"] == field


def test_validate_pilot_authorization_snapshot_blocks_risk_off_mismatch():
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_snapshot = twp.build_pilot_authorization_snapshot(
        replace(plan, risk_off=not plan.risk_off)
    )
    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )

    assert check["allowed"] is False
    assert check["complete"] is False
    assert "target_weight_pilot_authorization_snapshot_mismatch" in check["reason"]
    assert check["mismatches"][0]["field"] == "risk_off"


def test_load_portfolio_drawdown_guard_state_uses_prior_evidence_peak_and_cooldown(monkeypatch):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(
        pe,
        "get_canonical_records",
        lambda candidate_id: [
            {
                "date": "2026-04-08",
                "strategy": candidate_id,
                "total_value": 10_000_000.0,
                "pilot_caps_snapshot": {
                    "target_weight_plan": {
                        "portfolio_drawdown_guard": {"cooldown_after_plan": 0},
                    },
                },
            },
            {
                "date": "2026-04-09",
                "strategy": candidate_id,
                "portfolio_value": 8_900_000.0,
                "pilot_caps_snapshot": {
                    "target_weight_plan": {
                        "portfolio_drawdown_guard": {"cooldown_after_plan": 1},
                    },
                },
            },
            {
                "date": "2026-04-10",
                "strategy": candidate_id,
                "total_value": 9_500_000.0,
            },
        ],
    )

    state = twp.load_portfolio_drawdown_guard_state(
        "target_weight_candidate",
        as_of_date="2026-04-10",
    )

    assert state["source"] == "paper_evidence"
    assert state["record_count"] == 2
    assert state["latest_record_date"] == "2026-04-09"
    assert state["peak_value"] == 10_000_000.0
    assert state["last_equity_value"] == 8_900_000.0
    assert state["last_evidence_value"] == 8_900_000.0
    assert state["cooldown_remaining"] == 1


def test_build_plan_passes_portfolio_drawdown_guard_state(monkeypatch):
    import tools.target_weight_rotation_pilot as twp

    state = {
        "source": "paper_evidence",
        "record_count": 2,
        "peak_value": 10_000_000.0,
        "last_equity_value": 8_900_000.0,
        "cooldown_remaining": 1,
    }
    captured = {}

    monkeypatch.setattr(
        twp,
        "load_canonical_target_weight_spec",
        lambda candidate_id: SimpleNamespace(
            params={
                "target_top_n": 2,
                "short_lookback": 20,
                "long_lookback": 60,
                "portfolio_drawdown_guard_trigger_pct": 10.0,
                "portfolio_drawdown_guard_exposure": 0.40,
                "portfolio_drawdown_guard_cooldown_rebalances": 1,
            },
        ),
    )
    monkeypatch.setattr(twp, "_load_symbols", lambda config, raw_symbols: ["AAA", "BBB"])
    monkeypatch.setattr(twp, "_load_positions", lambda candidate_id: {})
    monkeypatch.setattr(twp, "_portfolio_cash", lambda config, candidate_id, cash: 1_000_000.0)
    monkeypatch.setattr(
        twp,
        "load_portfolio_drawdown_guard_state",
        lambda candidate_id, *, as_of_date=None: state,
    )

    def build_target_weight_plan(**kwargs):
        captured.update(kwargs)
        return _adapter_plan()

    monkeypatch.setattr(twp, "build_target_weight_plan", build_target_weight_plan)

    plan = twp.build_plan(
        candidate_id="target_weight_candidate",
        as_of_date="2026-04-10",
        config=SimpleNamespace(),
    )

    assert plan.candidate_id == "target_weight_candidate"
    assert captured["portfolio_drawdown_guard_state"] is state
    assert captured["as_of_date"] == "2026-04-10"


def test_authorization_snapshot_tracks_portfolio_drawdown_guard():
    import tools.target_weight_rotation_pilot as twp

    guard = {
        "enabled": True,
        "active": True,
        "triggered": True,
        "drawdown_pct": -11.0,
        "cooldown_after_plan": 1,
    }
    plan = _adapter_plan()
    plan = replace(plan, diagnostics={**plan.diagnostics, "portfolio_drawdown_guard": guard})

    snapshot = twp.build_pilot_authorization_snapshot(plan)
    stale_snapshot = deepcopy(snapshot)
    stale_snapshot["portfolio_drawdown_guard"] = {**guard, "cooldown_after_plan": 0}
    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )

    assert snapshot["portfolio_drawdown_guard"] == guard
    assert check["allowed"] is False
    assert check["mismatches"][0]["field"] == "portfolio_drawdown_guard"


def test_authorization_snapshot_ignores_portfolio_drawdown_guard_metadata_drift():
    import tools.target_weight_rotation_pilot as twp

    guard = {
        "enabled": True,
        "active": False,
        "triggered": False,
        "drawdown_pct": 0.0,
        "cooldown_after_plan": 0,
        "state_record_count": 3,
        "latest_record_date": "2026-05-18",
    }
    plan = _adapter_plan()
    plan = replace(plan, diagnostics={**plan.diagnostics, "portfolio_drawdown_guard": guard})

    stale_snapshot = twp.build_pilot_authorization_snapshot(plan)
    stale_snapshot["portfolio_drawdown_guard"] = {
        **guard,
        "state_record_count": 2,
        "latest_record_date": "2026-05-15",
    }
    check = twp.validate_pilot_authorization_snapshot(
        plan,
        _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )

    assert check["allowed"] is True
    assert check["mismatches"] == []


def test_build_pilot_evidence_caps_snapshot_persists_portfolio_drawdown_guard():
    import tools.target_weight_rotation_pilot as twp

    guard = {
        "enabled": True,
        "active": True,
        "triggered": False,
        "drawdown_pct": -3.0,
        "cooldown_after_plan": 0,
    }
    plan = _adapter_plan()
    plan = replace(plan, diagnostics={**plan.diagnostics, "portfolio_drawdown_guard": guard})

    caps = twp.build_pilot_evidence_caps_snapshot(
        plan,
        SimpleNamespace(caps_snapshot={}),
        {
            "executed": 0,
            "failed": 0,
            "skipped": len(plan.orders),
            "halted": True,
            "details": [],
        },
    )

    assert caps["target_weight_plan"]["portfolio_drawdown_guard"] == guard


def _db_persistence_proof_for_plan(plan):
    return {
        "checked": True,
        "complete": True,
        "reason": "target-weight paper execution is persisted in DB",
        "trade_history": {
            "source": "database.trade_history",
            "row_count": len(plan.orders),
            "expected_row_count": len(plan.orders),
            "execution_session_id": TEST_EXECUTION_SESSION_ID,
            "trade_ids": list(range(1, len(plan.orders) + 1)),
        },
        "positions": {
            "source": "database.positions",
            "expected_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "actual_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "missing_or_mismatched_symbols": [],
        },
    }


def _existing_pilot_evidence_record(plan):
    return {
        "date": plan.trade_day,
        "strategy": plan.candidate_id,
        "evidence_mode": "pilot_paper",
        "session_mode": "pilot_paper",
        "execution_backed": True,
        "pilot_authorized": True,
        "pilot_caps_snapshot": {
            "target_weight_plan": {
                "candidate_id": plan.candidate_id,
                "trade_day": plan.trade_day,
                "score_day": plan.score_day,
                "params_hash": plan.params_hash,
                "targets": list(plan.targets),
                "target_exposure": plan.target_exposure,
                "base_target_exposure": plan.base_target_exposure,
                "risk_off": plan.risk_off,
                "gross_exposure_after": plan.gross_exposure_after,
                "max_order_notional": plan.max_order_notional,
                "position_quantities_before": {
                    order.symbol: order.current_quantity
                    for order in plan.orders
                    if order.current_quantity > 0
                },
                "target_quantities_after": {
                    order.symbol: order.target_quantity
                    for order in plan.orders
                    if order.target_quantity > 0
                },
            },
            "target_weight_execution": {
                "complete": True,
                "params_hash": plan.params_hash,
                "planned_orders": len(plan.orders),
                "idempotency_allowed": True,
                "execution_trade_day_allowed": True,
                "execution_market_session_allowed": True,
                "execution_market_session_check": {
                    "checked": True,
                    "allowed": True,
                    "complete": True,
                },
                "pilot_authorization_snapshot_allowed": True,
                "pilot_authorization_snapshot_check": {
                    "checked": True,
                    "allowed": True,
                    "complete": True,
                },
                "preflight_refresh_complete": True,
                "pre_execution_complete": True,
                "liquidity_complete": True,
                "pre_trade_risk_complete": True,
                "order_count_complete": True,
                "order_result_complete": True,
                "order_complete": True,
                "order_result_reconciliation": {"complete": True},
                "fill_complete": True,
                "execution_session_id": TEST_EXECUTION_SESSION_ID,
                "fill_reconciliation": {
                    "complete": True,
                    "source": "database.trade_history",
                    "execution_session_id": TEST_EXECUTION_SESSION_ID,
                    "fills": [
                        {
                            "symbol": order.symbol,
                            "action": order.action,
                            "quantity": int(order.quantity),
                            "execution_session_id": TEST_EXECUTION_SESSION_ID,
                            "order_id": f"ORD-TW-{index + 1:03d}",
                        }
                        for index, order in enumerate(plan.orders)
                    ],
                },
                "position_reconciliation": {
                    "complete": True,
                    "source": "database.positions",
                },
                "db_persistence_complete": True,
                "db_persistence_proof": _db_persistence_proof_for_plan(plan),
            },
        },
        "total_value": 10_000_000.0,
        "cash": 3_000_000.0,
        "invested": 7_000_000.0,
        "position_count": len(plan.targets),
        "daily_return": 0.1,
        "same_universe_excess": 0.05,
        "exposure_matched_excess": 0.04,
        "cash_adjusted_excess": 0.03,
        "benchmark_status": "final",
    }


def _repairable_invalid_pilot_evidence_record(plan):
    record = deepcopy(_existing_pilot_evidence_record(plan))
    execution = record["pilot_caps_snapshot"]["target_weight_execution"]
    cash = float(plan.cash_before)
    order_costs = []
    total_commission = 0.0
    for order in plan.orders:
        commission = 100.0
        required_cash = float(order.notional) + commission
        item = {
            "symbol": order.symbol,
            "action": order.action,
            "quantity": int(order.quantity),
            "plan_price": float(order.price),
            "execution_price": float(order.price),
            "commission": commission,
            "tax": 0.0,
            "capital_gains_tax": 0.0,
            "slippage": 0.0,
            "cash_before": round(cash, 2),
            "required_cash": round(required_cash, 2),
            "cash_delta": round(-required_cash, 2),
        }
        cash -= required_cash
        item["cash_after"] = round(cash, 2)
        total_commission += commission
        order_costs.append(item)

    invested = sum(float(order.notional) for order in plan.orders)
    total_value = cash + invested
    fills = [
        {
            "symbol": order.symbol,
            "action": order.action,
            "quantity": int(order.quantity),
            "strategy": plan.candidate_id,
            "mode": "paper",
            "account_key": plan.candidate_id,
            "executed_at": f"{plan.trade_day} 09:00:00",
            "execution_session_id": TEST_EXECUTION_SESSION_ID,
            "order_id": f"ORD-TW-{index + 1:03d}",
        }
        for index, order in enumerate(plan.orders)
    ]
    execution.update({
        "executed_orders": len(plan.orders),
        "pre_trade_risk_check": {
            "checked": True,
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
            "projected_cash_after_costs": round(cash, 2),
            "projected_gross_exposure_after_costs": round(invested, 2),
            "projected_total_value_after_costs": round(total_value, 2),
            "target_position_count": len(plan.targets),
            "order_costs": order_costs,
            "cost_summary": {
                "commission": round(total_commission, 2),
                "tax": 0.0,
                "capital_gains_tax": 0.0,
                "slippage": 0.0,
                "total_explicit_costs": round(total_commission, 2),
            },
        },
        "fill_reconciliation": {
            "checked": True,
            "complete": True,
            "source": "database.trade_history",
            "reason": "paper trade fills match target-weight plan",
            "execution_session_id": TEST_EXECUTION_SESSION_ID,
            "expected_quantities": {
                f"{order.symbol}:{order.action}": int(order.quantity)
                for order in plan.orders
            },
            "actual_quantities": {
                f"{order.symbol}:{order.action}": int(order.quantity)
                for order in plan.orders
            },
            "mismatches": [],
            "unexpected_fills": [],
            "unlinked_fills": [],
            "fill_count": len(plan.orders),
            "fills": fills,
        },
        "position_reconciliation": {
            "checked": True,
            "complete": True,
            "source": "database.positions",
            "reason": "paper positions match target-weight target quantities",
            "expected_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "actual_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "mismatches": [],
            "unexpected_positions": [],
        },
    })
    record.update({
        "total_value": 0.0,
        "cash": 0.0,
        "invested": 0.0,
        "position_count": 0,
        "daily_return": None,
        "cumulative_return": None,
        "mdd": None,
        "total_trades": 0,
        "buy_count": 0,
        "sell_count": 0,
        "order_submit_count": 0,
        "fill_count": 0,
        "raw_fill_rate": None,
        "effective_fill_rate": None,
        "turnover": None,
        "same_universe_excess": None,
        "exposure_matched_excess": None,
        "cash_adjusted_excess": None,
        "benchmark_status": "failed",
        "benchmark_meta": {"warning": "daily_return is null"},
        "record_version": 1,
        "day_number": 1,
    })
    return record


def _adapter_plan_for_date(day: str):
    score_day = {
        "2026-04-08": "2026-04-07",
        "2026-04-09": "2026-04-08",
        "2026-04-10": "2026-04-09",
    }.get(day, "2026-04-09")
    return replace(_adapter_plan(), as_of_date=day, trade_day=day, score_day=score_day)


def _adapter_plan_for_strategy(strategy: str, day: str = "2026-04-10"):
    return replace(_adapter_plan_for_date(day), candidate_id=strategy)


class SimpleCostRiskManager:
    def __init__(
        self,
        *,
        commission_rate: float = 0.0,
        slippage_per_share: float = 0.0,
        tax_rate: float = 0.0,
        max_position_ratio: float = 0.90,
        max_investment_ratio: float = 1.20,
        min_cash_ratio: float = 0.0,
        max_positions: int = 10,
    ):
        self.commission_rate = commission_rate
        self.slippage_per_share = slippage_per_share
        self.tax_rate = tax_rate
        self.risk_params = {
            "diversification": {
                "max_position_ratio": max_position_ratio,
                "max_investment_ratio": max_investment_ratio,
                "min_cash_ratio": min_cash_ratio,
                "max_positions": max_positions,
            }
        }

    def calculate_transaction_costs(
        self,
        price,
        quantity,
        action="BUY",
        avg_daily_volume=None,
        avg_price=None,
    ):
        commission = round(price * quantity * self.commission_rate, 0)
        tax = round(price * quantity * self.tax_rate, 0) if action.upper() == "SELL" else 0
        slippage = round(self.slippage_per_share * quantity, 0)
        execution_price = price + self.slippage_per_share
        if action.upper() == "SELL":
            execution_price = max(0.0, price - self.slippage_per_share)
        return {
            "commission": commission,
            "tax": tax,
            "capital_gains_tax": 0,
            "slippage": slippage,
            "total_cost": commission + tax + slippage,
            "execution_price": execution_price,
            "participation_rate": None if avg_daily_volume is None else quantity / avg_daily_volume,
            "slippage_multiplier": 1.0,
        }


def test_target_weight_pilot_help_lists_shadow_days():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/target_weight_rotation_pilot.py", "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--shadow-days" in result.stdout
    assert "--readiness-audit" in result.stdout
    assert "--allow-rerun" in result.stdout


@pytest.mark.parametrize(
    "mode",
    ["--readiness-audit", "--daily-ops-summary", "--execute", "--collect-evidence"],
)
def test_target_weight_cli_rejects_cash_override_for_operational_modes(
    monkeypatch,
    capsys,
    mode,
):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            mode,
            "--cash",
            "1000000",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "--cash cannot be combined" in captured.err
    assert mode in captured.err


def test_target_weight_cli_requires_execute_for_collect_evidence(monkeypatch, capsys):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--collect-evidence",
        ],
    )
    monkeypatch.setattr(twp, "run_pilot", lambda **kwargs: pytest.fail("run_pilot should not run"))

    with pytest.raises(SystemExit) as exc:
        twp.main()

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "--collect-evidence requires --execute" in captured.err


def test_target_weight_daily_ops_cli_marks_not_checked_gates(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    not_checked_day = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution trade day check not required",
    }
    not_checked_session = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution market session check not required",
    }
    not_checked_auth = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "pilot authorization snapshot check not required",
    }

    def fake_run_daily_ops_summary(**kwargs):
        return {
            "daily_ops_summary": {
                "candidate_id": "target_weight_rotation_top5_60_120_floor0_hold3_risk60_35",
                "trade_day": "2026-04-10",
                "status": "READY_TO_ENABLE_CAPS",
                "next_step": "추천 cap 승인 후 readiness audit 재실행",
                "decision": {
                    "blocking_reasons": [],
                    "warning_reasons": [],
                    "execution_trade_day_check": not_checked_day,
                    "execution_market_session_check": not_checked_session,
                    "pilot_authorization_snapshot_check": not_checked_auth,
                },
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "remaining_pilot_days": 60,
                    "shadow_days": 3,
                    "invalid_execution_days": 0,
                },
            },
            "artifact_path": tmp_path / "readiness.json",
            "experiment_manifest_path": tmp_path / "manifest.json",
            "daily_ops_summary_path": tmp_path / "summary.json",
            "daily_ops_summary_report_path": tmp_path / "summary.md",
        }

    monkeypatch.setattr(twp, "run_daily_ops_summary", fake_run_daily_ops_summary)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--daily-ops-summary",
            "--output-dir",
            str(tmp_path),
        ],
    )

    twp.main()

    output = capsys.readouterr().out
    assert "execution day: N/A (NOT CHECKED)" in output
    assert "market session: N/A (NOT CHECKED)" in output
    assert "pilot auth snapshot: NOT CHECKED - pilot authorization snapshot check not required" in output
    assert "execution day: N/A (PASS)" not in output
    assert "market session: N/A (PASS)" not in output


def test_target_weight_daily_ops_cli_exits_nonzero_on_invalid_evidence(
    monkeypatch,
    tmp_path,
    capsys,
):
    import tools.target_weight_rotation_pilot as twp

    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }

    def fake_run_daily_ops_summary(**kwargs):
        return {
            "daily_ops_summary": {
                "candidate_id": "target_weight_rotation_test",
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_INVALID",
                "next_step": "final benchmark/portfolio evidence 확정 후 daily ops 재점검",
                "decision": {
                    "blocking_reasons": [
                        "execution_idempotency: target_weight_duplicate_execution_attempt"
                    ],
                    "warning_reasons": [],
                    "execution_trade_day_check": {**pass_check, "execution_day": "2026-04-10"},
                    "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
                    "pilot_authorization_snapshot_check": pass_check,
                },
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "remaining_pilot_days": 60,
                    "shadow_days": 2,
                    "repaired_pilot_days": 0,
                    "invalid_execution_days": 1,
                },
            },
            "artifact_path": tmp_path / "readiness.json",
            "experiment_manifest_path": tmp_path / "manifest.json",
            "daily_ops_summary_path": tmp_path / "summary.json",
            "daily_ops_summary_report_path": tmp_path / "summary.md",
        }

    monkeypatch.setattr(twp, "run_daily_ops_summary", fake_run_daily_ops_summary)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--daily-ops-summary",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "status: PILOT_EVIDENCE_INVALID" in output
    assert "final benchmark/portfolio evidence" in output
    assert "blocker: execution_idempotency" in output


def test_target_weight_daily_ops_cli_writes_failure_artifact(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    def fail_daily_ops(**kwargs):
        raise ValueError("target_weight_sector_map_missing: sector map is required")

    monkeypatch.setattr(twp, "run_daily_ops_summary", fail_daily_ops)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            "target_weight_rotation_test",
            "--daily-ops-summary",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    artifacts = list(tmp_path.glob("target_weight_daily_ops_summary_failure_*.json"))
    reports = list(tmp_path.glob("target_weight_daily_ops_summary_failure_*.md"))
    assert len(artifacts) == 1
    assert len(reports) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    report = reports[0].read_text(encoding="utf-8")

    assert exc.value.code == 1
    assert "status: BLOCKED" in output
    assert "orders_submitted: false" in output
    assert payload["artifact_type"] == "target_weight_no_order_operation_failure"
    assert payload["mode"] == "daily_ops_summary"
    assert payload["status"] == "BLOCKED"
    assert payload["no_order_safety"]["orders_submitted"] is False
    assert "target_weight_sector_map_missing" in payload["reason"]
    assert "Target-weight No-order Operation Failure" in report
    assert "target_weight_sector_map_missing" in report


def test_target_weight_daily_ops_cli_writes_failure_artifact_for_data_error(monkeypatch, tmp_path, capsys):
    from core.data_collector import DataCollectionError
    import tools.target_weight_rotation_pilot as twp

    def fail_daily_ops(**kwargs):
        raise DataCollectionError("KIS fallback 비활성화 상태. FDR/yfinance 실패로 수집 중단: 005930")

    monkeypatch.setattr(twp, "run_daily_ops_summary", fail_daily_ops)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            "target_weight_rotation_test",
            "--daily-ops-summary",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    artifacts = list(tmp_path.glob("target_weight_daily_ops_summary_failure_*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))

    assert exc.value.code == 1
    assert "status: BLOCKED" in output
    assert payload["error"]["type"] == "DataCollectionError"
    assert payload["no_order_safety"]["orders_submitted"] is False
    assert "FDR/yfinance 실패" in payload["reason"]


def test_target_weight_readiness_cli_marks_not_checked_gates(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_260_000,
        "max_gross_exposure": 3_360_000,
    }
    not_checked_day = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution trade day check not required",
    }
    not_checked_session = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution market session check not required",
    }
    not_checked_auth = {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "pilot authorization snapshot check not required",
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "audit": {
                "candidate_id": plan.candidate_id,
                "trade_day": plan.trade_day,
                "ready_for_cap_approval": True,
                "ready_for_capped_pilot": False,
                "next_action": "enable pilot with suggested caps, then rerun readiness audit",
                "blocking_reasons": [],
                "warning_reasons": [],
                "operator_commands": {"enable_suggested_caps": "python tools/paper_pilot_control.py --enable"},
                "plan_summary": {
                    "score_day": plan.score_day,
                    "order_count": len(plan.orders),
                    "target_position_count": plan.target_position_count,
                    "max_order_notional": plan.max_order_notional,
                    "gross_exposure_after": plan.gross_exposure_after,
                },
                "launch_readiness": {
                    "clean_final_days_current": 3,
                    "clean_final_days_required": 3,
                    "infra_ready": True,
                    "pilot_authorization_present": False,
                    "launch_ready": False,
                },
                "cap_recommendation": {"suggested_caps": suggested_caps},
                "plan_validation": {"reason": "no active pilot authorization"},
                "execution_idempotency": {"reason": "no completed session for this trade day"},
                "pre_execution_reconciliation": {"reason": "starting positions match target-weight plan"},
                "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
                "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
                "execution_trade_day_check": not_checked_day,
                "execution_market_session_check": not_checked_session,
                "pilot_authorization_snapshot_check": not_checked_auth,
            },
            "artifact_path": tmp_path / "readiness.json",
            "report_path": tmp_path / "readiness.md",
            "experiment_manifest_path": tmp_path / "manifest.json",
        }

    monkeypatch.setattr(twp, "run_pilot_readiness_audit", fake_run_pilot_readiness_audit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--readiness-audit",
            "--output-dir",
            str(tmp_path),
        ],
    )

    twp.main()

    output = capsys.readouterr().out
    assert "execution day: N/A (NOT CHECKED)" in output
    assert "market session: N/A (NOT CHECKED)" in output
    assert "pilot auth snapshot: NOT CHECKED - pilot authorization snapshot check not required" in output
    assert "execution day: N/A (PASS)" not in output
    assert "market session: N/A (PASS)" not in output


def test_target_weight_readiness_cli_writes_failure_artifact(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    def fail_readiness(**kwargs):
        raise ValueError("target_weight_sector_map_missing: sector map is required")

    monkeypatch.setattr(twp, "run_pilot_readiness_audit", fail_readiness)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            "target_weight_rotation_test",
            "--readiness-audit",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    artifacts = list(tmp_path.glob("target_weight_readiness_audit_failure_*.json"))
    reports = list(tmp_path.glob("target_weight_readiness_audit_failure_*.md"))
    assert len(artifacts) == 1
    assert len(reports) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    report = reports[0].read_text(encoding="utf-8")

    assert exc.value.code == 1
    assert "status: BLOCKED" in output
    assert "orders_submitted: false" in output
    assert payload["artifact_type"] == "target_weight_no_order_operation_failure"
    assert payload["mode"] == "readiness_audit"
    assert payload["status"] == "BLOCKED"
    assert payload["no_order_safety"]["pilot_evidence_recorded"] is False
    assert "target_weight_sector_map_missing" in payload["reason"]
    assert "Target-weight No-order Operation Failure" in report
    assert "target_weight_sector_map_missing" in report


def test_target_weight_readiness_cli_writes_failure_artifact_for_data_error(monkeypatch, tmp_path, capsys):
    from core.data_collector import DataCollectionError
    import tools.target_weight_rotation_pilot as twp

    def fail_readiness(**kwargs):
        raise DataCollectionError("KIS fallback 비활성화 상태. FDR/yfinance 실패로 수집 중단: 005930")

    monkeypatch.setattr(twp, "run_pilot_readiness_audit", fail_readiness)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            "target_weight_rotation_test",
            "--readiness-audit",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    artifacts = list(tmp_path.glob("target_weight_readiness_audit_failure_*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))

    assert exc.value.code == 1
    assert "status: BLOCKED" in output
    assert payload["error"]["type"] == "DataCollectionError"
    assert payload["no_order_safety"]["pilot_evidence_recorded"] is False
    assert "FDR/yfinance 실패" in payload["reason"]


def test_target_weight_pilot_control_enable_guard_blocks_requested_caps(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    calls = {}

    def fake_build_preview_caps(**kwargs):
        calls["caps"] = kwargs
        return {"requested": True}

    def fake_run_pilot_readiness_audit(**kwargs):
        calls["audit"] = kwargs
        return {
            "audit": {
                "ready_for_cap_approval": True,
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": False,
                    "reason": "max order notional exceeds cap",
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.build_preview_caps",
        fake_build_preview_caps,
    )
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=2,
        max_positions=2,
        max_notional=1_000_000,
        max_exposure=3_000_000,
    )

    with pytest.raises(ValueError, match="requested target-weight pilot caps"):
        _target_weight_enable_guard(args)

    assert calls["caps"] == {
        "max_orders": 2,
        "max_positions": 2,
        "max_notional": 1_000_000,
        "max_exposure": 3_000_000,
    }
    assert calls["audit"]["preview_caps"] == {"requested": True}


def test_target_weight_pilot_control_enable_guard_passes_safe_requested_caps(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_300_000,
        "max_gross_exposure": 3_300_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": _adapter_plan_for_strategy(DEFAULT_TARGET_WEIGHT_CANDIDATE_ID),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "proposed pilot caps satisfied",
                },
                "cap_recommendation": {
                    "minimum_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_200_000,
                        "max_gross_exposure": 3_200_000,
                    },
                    "suggested_caps": suggested_caps,
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    result = _target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True


def test_target_weight_pilot_control_enable_guard_allows_tighter_safe_caps(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": _adapter_plan_for_strategy(DEFAULT_TARGET_WEIGHT_CANDIDATE_ID),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "tighter caps still satisfy the plan",
                },
                "cap_recommendation": {
                    "minimum_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_200_000,
                        "max_gross_exposure": 3_200_000,
                    },
                    "suggested_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_310_000,
                        "max_gross_exposure": 3_350_000,
                    },
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    result = _target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True


def test_target_weight_pilot_control_enable_guard_allows_one_step_money_cap_drift(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": _adapter_plan_for_strategy(DEFAULT_TARGET_WEIGHT_CANDIDATE_ID),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "one rounding step drift still satisfies the plan",
                },
                "cap_recommendation": {
                    "minimum_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_200_000,
                        "max_gross_exposure": 3_200_000,
                    },
                    "suggested_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_290_000,
                        "max_gross_exposure": 3_290_000,
                    },
                    "rounding_step": 10_000,
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    result = _target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True


def test_target_weight_pilot_control_enable_guard_blocks_caps_above_suggested(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "looser caps still satisfy the plan",
                },
                "cap_recommendation": {
                    "minimum_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_200_000,
                        "max_gross_exposure": 3_200_000,
                    },
                    "suggested_caps": {
                        "max_orders_per_day": 3,
                        "max_concurrent_positions": 3,
                        "max_notional_per_trade": 1_300_000,
                        "max_gross_exposure": 3_300_000,
                    }
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=4,
        max_positions=4,
        max_notional=2_000_000,
        max_exposure=4_000_000,
    )

    with pytest.raises(ValueError, match="must stay within readiness cap envelope"):
        _target_weight_enable_guard(args)


def test_target_weight_pilot_control_enable_guard_blocks_audit_trade_day_mismatch(
    monkeypatch,
    tmp_path,
):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_300_000,
        "max_gross_exposure": 3_300_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": _adapter_plan_for_strategy(DEFAULT_TARGET_WEIGHT_CANDIDATE_ID),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-09",
                "blocking_reasons": [],
                "cap_preview": {"allowed": True, "reason": "proposed pilot caps satisfied"},
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    with pytest.raises(ValueError, match="readiness audit trade day mismatch"):
        _target_weight_enable_guard(args)


def test_target_weight_pilot_control_enable_guard_requires_plan_snapshot(
    monkeypatch,
    tmp_path,
):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_300_000,
        "max_gross_exposure": 3_300_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {"allowed": True, "reason": "proposed pilot caps satisfied"},
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    with pytest.raises(ValueError, match="readiness plan missing"):
        _target_weight_enable_guard(args)


def test_target_weight_pilot_control_enable_guard_blocks_plan_candidate_mismatch(
    monkeypatch,
    tmp_path,
):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_300_000,
        "max_gross_exposure": 3_300_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": _adapter_plan_for_strategy("target_weight_other_candidate"),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {"allowed": True, "reason": "proposed pilot caps satisfied"},
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    with pytest.raises(ValueError, match="readiness plan candidate mismatch"):
        _target_weight_enable_guard(args)


def test_target_weight_pilot_control_enable_guard_blocks_plan_trade_day_mismatch(
    monkeypatch,
    tmp_path,
):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 3,
        "max_notional_per_trade": 1_300_000,
        "max_gross_exposure": 3_300_000,
    }
    stale_plan = _adapter_plan_for_strategy(DEFAULT_TARGET_WEIGHT_CANDIDATE_ID, "2026-04-09")

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "plan": stale_plan,
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {"allowed": True, "reason": "proposed pilot caps satisfied"},
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.build_pilot_authorization_snapshot",
        lambda *args, **kwargs: pytest.fail("mismatched plan must not create auth snapshot"),
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    with pytest.raises(ValueError, match="readiness plan trade day mismatch"):
        _target_weight_enable_guard(args)


def test_target_weight_pilot_control_enable_guard_covers_non_default_target_weight(monkeypatch, tmp_path):
    from tools.paper_pilot_control import _target_weight_enable_guard

    calls = {}
    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 4,
        "max_notional_per_trade": 1_500_000,
        "max_gross_exposure": 4_000_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        calls["audit"] = kwargs
        return {
            "plan": _adapter_plan_for_strategy("target_weight_rotation_next_candidate"),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "proposed pilot caps satisfied",
                },
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy="target_weight_rotation_next_candidate",
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=4,
        max_notional=1_500_000,
        max_exposure=4_000_000,
    )

    result = _target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True
    assert calls["audit"]["candidate_id"] == "target_weight_rotation_next_candidate"
    assert calls["audit"]["as_of_date"] == "2026-04-10"
    assert calls["audit"]["preview_caps"] == {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 4,
        "max_notional_per_trade": 1_500_000,
        "max_gross_exposure": 4_000_000,
    }


def test_target_weight_pilot_control_enable_guard_uses_canonical_metadata_for_prefixless_candidate(
    monkeypatch,
    tmp_path,
):
    import tools.paper_pilot_control as ppc

    strategy = "risk_overlay_candidate"
    metadata_path = tmp_path / "run_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "strategy_specs": [
                    {
                        "candidate_id": strategy,
                        "base_strategy": "target_weight_rotation",
                        "params_hash": "hash-risk-overlay",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ppc, "PROMOTION_METADATA_PATH", metadata_path)

    calls = {}
    suggested_caps = {
        "max_orders_per_day": 3,
        "max_concurrent_positions": 4,
        "max_notional_per_trade": 1_500_000,
        "max_gross_exposure": 4_000_000,
    }

    def fake_run_pilot_readiness_audit(**kwargs):
        calls["audit"] = kwargs
        return {
            "plan": _adapter_plan_for_strategy(strategy),
            "audit": {
                "ready_for_cap_approval": True,
                "trade_day": "2026-04-10",
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "proposed pilot caps satisfied",
                },
                "cap_recommendation": {"suggested_caps": suggested_caps},
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=strategy,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=4,
        max_notional=1_500_000,
        max_exposure=4_000_000,
    )

    result = ppc._target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True
    assert calls["audit"]["candidate_id"] == strategy
    assert calls["audit"]["as_of_date"] == "2026-04-10"


def test_target_weight_pilot_control_enable_guard_skips_non_target_weight(monkeypatch):
    from tools.paper_pilot_control import _target_weight_enable_guard

    def fail_audit(**kwargs):
        pytest.fail("non target-weight strategy must not run target-weight audit")

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fail_audit,
    )
    args = SimpleNamespace(
        strategy="scoring",
        valid_from="2026-04-10",
        max_orders=2,
        max_positions=2,
        max_notional=1_000_000,
        max_exposure=3_000_000,
    )

    assert _target_weight_enable_guard(args) is None


def test_paper_pilot_control_enable_stops_before_auth_when_target_weight_guard_fails(
    monkeypatch,
    capsys,
):
    import core.paper_pilot as pp
    import tools.paper_pilot_control as ppc
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID

    monkeypatch.setattr(pp, "check_pilot_prerequisites", lambda strategy: (True, "ok"))
    monkeypatch.setattr(pp, "enable_pilot", lambda *args, **kwargs: pytest.fail("pilot auth must not be written"))
    monkeypatch.setattr(
        ppc,
        "_target_weight_enable_guard",
        lambda args: (_ for _ in ()).throw(ValueError("target-weight audit blocked")),
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        valid_to="2026-04-30",
        max_orders=2,
        max_positions=2,
        max_notional=1_000_000,
        max_exposure=3_000_000,
        reason="test",
    )

    with pytest.raises(SystemExit) as exc:
        ppc.run_enable(args)

    assert exc.value.code == 1
    assert "target-weight audit blocked" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["paper_pilot_control.py", "--strategy", "scoring", "--enable", "--disable"],
        ["paper_pilot_control.py", "--strategy", "scoring", "--enable", "--status"],
        [
            "paper_pilot_control.py",
            "--strategy",
            "scoring",
            "--disable",
            "--check-prerequisites",
        ],
    ],
)
def test_paper_pilot_control_rejects_conflicting_cli_actions(monkeypatch, capsys, argv):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        ppc.main()

    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_paper_pilot_control_enable_writes_target_weight_plan_snapshot(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.paper_pilot_control as ppc
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID

    plan = replace(_adapter_plan(), candidate_id=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID)
    captured = {}

    monkeypatch.setattr(pp, "check_pilot_prerequisites", lambda strategy: (True, "ok"))

    def fake_enable_pilot(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            strategy=kwargs["strategy"],
            valid_from=kwargs["valid_from"],
            valid_to=kwargs["valid_to"],
            max_orders_per_day=kwargs["max_orders"],
            max_concurrent_positions=kwargs["max_positions"],
            max_notional_per_trade=kwargs["max_notional"],
            max_gross_exposure=kwargs["max_exposure"],
            operator_reason=kwargs["reason"],
        )

    monkeypatch.setattr(pp, "enable_pilot", fake_enable_pilot)
    monkeypatch.setattr(
        ppc,
        "_target_weight_enable_guard",
        lambda args: {
            "plan": plan,
            "audit": {
                "ready_for_cap_approval": True,
                "blocking_reasons": [],
                "cap_preview": {"allowed": True},
            },
            "target_weight_plan_snapshot": {
                "candidate_id": plan.candidate_id,
                "trade_day": plan.trade_day,
                "params_hash": plan.params_hash,
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        },
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        valid_to="2026-04-30",
        max_orders=3,
        max_positions=3,
        max_notional=2_000_000,
        max_exposure=10_000_000,
        reason="test",
    )

    ppc.run_enable(args)

    assert captured["target_weight_plan_snapshot"]["candidate_id"] == plan.candidate_id
    assert captured["target_weight_plan_snapshot"]["trade_day"] == plan.trade_day
    assert captured["target_weight_plan_snapshot"]["params_hash"] == plan.params_hash


def test_paper_pilot_control_status_prints_target_weight_daily_ops(tmp_path, monkeypatch, capsys):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-12")
    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "next_operator_trade_day": "2026-04-13",
                "status": "PILOT_EVIDENCE_RECORDED",
                "next_step": "다음 KRX 영업일 fresh readiness와 cap 재승인 점검",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {
                    "post_evidence_diagnostics": [
                        "execution_idempotency: duplicate",
                        "pilot_authorization_snapshot: stale same-day approval",
                    ],
                },
                "operator_commands": {
                    "enable_suggested_caps": (
                        "python tools/paper_pilot_control.py --strategy target_weight_candidate --enable"
                    ),
                    "execute_capped_paper": "# blocked: pilot_paper evidence already recorded for 2026-04-10",
                    "next_daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-13 "
                        "--daily-ops-summary"
                    ),
                    "next_readiness_audit": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-13 "
                        "--readiness-audit"
                    ),
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "current_priority_action": {
                        "strategy": strategy,
                        "desc": "다음 KRX 영업일 fresh readiness 점검",
                        "command": "# blocked: not before 2026-04-13; target_weight_future_as_of_date_blocked",
                        "scheduled_command": (
                            "python tools/target_weight_rotation_pilot.py "
                            "--candidate-id target_weight_candidate --as-of-date 2026-04-13 "
                            "--daily-ops-summary"
                        ),
                        "not_before_date": "2026-04-13",
                        "premature_run_guard": "target_weight_future_as_of_date_blocked",
                        "verified_pilot_days": 1,
                        "target_days": 60,
                        "remaining_pilot_days": 59,
                        "shadow_days": 3,
                        "repaired_pilot_days": 0,
                        "invalid_execution_days": 0,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops" in output
    assert "Status: PILOT_EVIDENCE_RECORDED" in output
    assert "Next operator trade day: 2026-04-13" in output
    assert "Not before date: 2026-04-13" in output
    assert "Premature run guard: target_weight_future_as_of_date_blocked" in output
    assert "Verified pilot days: 1/60" in output
    assert "Current blockers priority: 다음 KRX 영업일 fresh readiness 점검" in output
    assert (
        "Priority evidence: verified=1/60 remaining=59 shadow=3 repaired=0 invalid=0"
        in output
    )
    assert (
        "Priority command: # blocked: not before 2026-04-13; "
        "target_weight_future_as_of_date_blocked"
    ) in output
    assert "Scheduled priority command:" in output
    assert "Post-evidence diagnostics: 2" in output
    assert "Cap approval: BLOCKED by daily ops" in output
    assert "Enable cap command: # blocked: pilot_paper evidence already recorded" in output
    assert "--strategy target_weight_candidate --enable" not in output
    assert "Adapter execution: BLOCKED by daily ops" in output
    assert "pilot_paper evidence already recorded" in output
    assert "Next daily ops command:" in output
    assert "--as-of-date 2026-04-13 --daily-ops-summary" in output
    assert "Next readiness command:" in output
    assert "--as-of-date 2026-04-13 --readiness-audit" in output
    assert (
        "Operator next action: WAIT until 2026-04-13: "
        "target_weight_future_as_of_date_blocked"
    ) in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_uses_repaired_summary_run_guard_without_blockers(
    tmp_path,
    monkeypatch,
    capsys,
):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-12")
    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "next_operator_trade_day": "2026-04-13",
                "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
                "next_step": "다음 KRX 영업일 fresh readiness 점검",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "repaired_pilot_days": 1,
                    "invalid_execution_days": 1,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": (
                        "# blocked: repaired pilot_paper evidence already recorded for 2026-04-10"
                    ),
                    "next_daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-13 "
                        "--daily-ops-summary"
                    ),
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Current blockers warning: current_blockers.json missing" in output
    assert "Status: PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE" in output
    assert "Not before date: 2026-04-13" in output
    assert "Premature run guard: target_weight_future_as_of_date_blocked" in output
    assert (
        "Operator next action: WAIT until 2026-04-13: "
        "target_weight_future_as_of_date_blocked"
    ) in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_blocks_stale_ready_to_enable_caps(
    tmp_path,
    monkeypatch,
    capsys,
):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-11")
    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "READY_TO_ENABLE_CAPS",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                },
                "decision": {"blocking_reasons": []},
                "operator_commands": {
                    "enable_suggested_caps": (
                        "python tools/paper_pilot_control.py "
                        "--strategy target_weight_candidate --enable "
                        "--from 2026-04-10 --to 2026-07-03"
                    ),
                    "execute_capped_paper": "# blocked: cap approval required",
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Status: READY_TO_ENABLE_CAPS" in output
    assert "Cap approval: BLOCKED by daily ops" in output
    assert "Enable cap command: # blocked: daily_ops_summary.trade_day is stale" in output
    assert "--enable --from 2026-04-10" not in output


def test_paper_pilot_control_status_hides_elapsed_not_before_guard(tmp_path, monkeypatch, capsys):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-13")
    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "next_operator_trade_day": "2026-04-13",
                "status": "PILOT_EVIDENCE_RECORDED",
                "next_step": "다음 KRX 영업일 fresh readiness와 cap 재승인 점검",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: pilot_paper evidence already recorded for 2026-04-10",
                    "next_daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-13 "
                        "--daily-ops-summary"
                    ),
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "current_priority_action": {
                        "not_before_date": "2026-04-13",
                        "premature_run_guard": "target_weight_future_as_of_date_blocked",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops" in output
    assert "Next operator trade day: 2026-04-13" in output
    assert "Not before date:" not in output
    assert "Premature run guard:" not in output
    assert "Next daily ops command:" in output
    assert "Operator next action: RUN no-order daily ops check:" in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_warns_when_current_blockers_missing(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "next_step": "다음 KRX 영업일 fresh readiness와 cap 재승인 점검",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {},
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Current blockers warning: current_blockers.json missing" in output
    assert (
        "Regenerate current blockers command: "
        "python tools/evaluate_and_promote.py --current-blockers"
    ) in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_warns_on_stale_current_blockers_priority(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "next_step": "다음 KRX 영업일 fresh readiness와 cap 재승인 점검",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {},
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "commands": {
                        "regenerate_current_blockers": (
                            "python tools/evaluate_and_promote.py --current-blockers"
                        ),
                    },
                    "current_priority_action": {
                        "strategy": strategy,
                        "source_path": (
                            "reports/paper_runtime/"
                            f"target_weight_daily_ops_summary_{strategy}_2026-04-09.json"
                        ),
                        "daily_ops_status": "READY_TO_EXECUTE",
                        "daily_ops_trade_day": "2026-04-09",
                        "desc": "READY_TO_EXECUTE 당일 capped paper 실행",
                        "command": "python tools/target_weight_rotation_pilot.py --execute-capped-paper",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Current blockers warning: stale priority action" in output
    assert (
        "daily_ops_status priority=READY_TO_EXECUTE "
        "latest=PILOT_EVIDENCE_RECORDED"
    ) in output
    assert (
        "daily_ops_trade_day priority=2026-04-09 latest=2026-04-10"
        in output
    )
    assert (
        "source_path priority="
        f"target_weight_daily_ops_summary_{strategy}_2026-04-09.json "
        f"latest={summary_path.name}"
    ) in output
    assert (
        "Regenerate current blockers command: "
        "python tools/evaluate_and_promote.py --current-blockers"
    ) in output
    assert "Current blockers priority: READY_TO_EXECUTE 당일 capped paper 실행" in output


def test_paper_pilot_control_status_prints_promotion_freshness_warning(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "next_step": "다음 KRX 영업일 fresh readiness와 cap 재승인 점검",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {},
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "promotion_artifact_freshness": {
                    "status": "AGING",
                    "age_days": 5.98,
                    "max_age_days": 7,
                    "warning": (
                        "canonical artifact is close to the freshness limit; "
                        "plan a canonical refresh before live review"
                    ),
                    "check_command": (
                        "python tools/evaluate_and_promote.py --check-only"
                    ),
                    "refresh_command": (
                        "python tools/evaluate_and_promote.py --canonical"
                    ),
                },
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "commands": {
                        "regenerate_current_blockers": (
                            "python tools/evaluate_and_promote.py --current-blockers"
                        ),
                    },
                    "current_priority_action": {
                        "strategy": strategy,
                        "desc": "다음 KRX 영업일 fresh readiness 점검",
                        "daily_ops_status": "PILOT_EVIDENCE_RECORDED",
                        "daily_ops_trade_day": "2026-04-10",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Promotion artifact freshness: AGING (age=5.98/7 days)" in output
    assert "Promotion freshness warning: canonical artifact is close" in output
    assert (
        "Promotion freshness check command: "
        "python tools/evaluate_and_promote.py --check-only"
    ) in output
    assert (
        "Promotion freshness refresh command: "
        "python tools/evaluate_and_promote.py --canonical"
    ) in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_prints_evidence_maintenance_commands(tmp_path, capsys):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_INVALID",
                "next_step": "final benchmark/portfolio evidence 확정 후 daily ops 재점검",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "shadow_days": 3,
                    "repaired_pilot_days": 1,
                    "invalid_execution_days": 2,
                    "invalid_reasons": {"target_weight_benchmark_status_not_final": 1},
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: pilot_paper evidence invalid for 2026-04-10",
                    "finalize_pilot_evidence": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate "
                        "--finalize-pilot-evidence --finalize-date 2026-04-10"
                    ),
                    "repair_pilot_evidence": (
                        "# fallback: use only if finalize cannot produce promotable proof; "
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate "
                        "--repair-pilot-evidence --repair-date 2026-04-10"
                    ),
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Status: PILOT_EVIDENCE_INVALID" in output
    assert "Effective target-weight execution: BLOCKED" in output
    assert (
        "Effective reason: daily ops requires evidence finalize or repair first"
        in output
    )
    assert "Evidence breakdown: shadow=3 repaired=1 invalid=2" in output
    assert "Finalize evidence command:" in output
    assert "--finalize-pilot-evidence --finalize-date 2026-04-10" in output
    assert "Repair evidence command:" in output
    assert "fallback: use only if finalize cannot produce promotable proof" in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_waits_when_finalize_missing_performance(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate "
        "--finalize-pilot-evidence --finalize-date 2026-04-10"
    )
    snapshot_diagnostics_command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate "
        "--diagnose-portfolio-snapshot --snapshot-date 2026-04-10"
    )
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_INVALID",
                "next_step": "final benchmark/portfolio evidence 확정 후 daily ops 재점검",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "shadow_days": 2,
                    "repaired_pilot_days": 2,
                    "invalid_execution_days": 3,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: daily order cap already reached",
                    "finalize_pilot_evidence": finalize_command,
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    action = {
        "strategy": strategy,
        "source_path": summary_path.as_posix(),
        "daily_ops_status": "PILOT_EVIDENCE_INVALID",
        "daily_ops_trade_day": "2026-04-10",
        "desc": "확정되지 않은 pilot evidence 정리",
        "command": (
            "# blocked: final performance evidence unavailable; "
            "target_weight_pilot_evidence_finalize_missing_performance: "
            "total_value/daily_return unavailable"
        ),
        "scheduled_command": finalize_command,
        "performance_evidence_guard": (
            "target_weight_pilot_evidence_finalize_missing_performance"
        ),
        "finalize_report_source": (
            "reports/paper_runtime/"
            "target_weight_pilot_evidence_finalize_target_weight_candidate_2026-04-10.json"
        ),
        "finalize_report_generated_at": "2026-04-10T15:40:00",
        "finalize_report_status": "blocked",
        "finalize_source_record_fields_usable": ["cash"],
        "finalize_source_record_fields_unusable": ["total_value"],
        "finalize_missing_performance_fields": ["total_value", "daily_return"],
        "finalize_portfolio_metrics_checked": True,
        "finalize_portfolio_metrics_probe_status": "missing_current_snapshot_after_trades",
        "finalize_portfolio_metrics_probe_reason": (
            "trades exist after previous snapshot but current snapshot is missing"
        ),
        "finalize_portfolio_metrics_recovery_hint": (
            "run end-of-day portfolio snapshot capture for the trade day"
        ),
        "finalize_portfolio_snapshot_diagnostics_command": (
            snapshot_diagnostics_command
        ),
        "finalize_portfolio_metrics_current_snapshot_found": False,
        "finalize_portfolio_metrics_previous_snapshot_found": True,
        "finalize_portfolio_metrics_previous_snapshot_at": "2026-04-09T15:35:00",
        "finalize_portfolio_metrics_trades_today": 1,
        "finalize_portfolio_metrics_trades_since_previous": 1,
        "finalize_portfolio_metrics_fields_present": [],
        "verified_pilot_days": 0,
        "target_days": 60,
        "shadow_days": 2,
        "repaired_pilot_days": 2,
        "invalid_execution_days": 3,
    }
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "commands": {
                        "regenerate_current_blockers": (
                            "python tools/evaluate_and_promote.py --current-blockers"
                        ),
                    },
                    "current_priority_action": action,
                },
                "next_actions": [action],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Status: PILOT_EVIDENCE_INVALID" in output
    assert "Effective target-weight execution: BLOCKED" in output
    assert (
        "Effective reason: daily ops requires evidence finalize or repair first"
        in output
    )
    assert "Current blockers priority: 확정되지 않은 pilot evidence 정리" in output
    assert (
        "Priority command: # blocked: final performance evidence unavailable"
        in output
    )
    assert "Scheduled priority command:" in output
    assert finalize_command in output
    assert "Portfolio snapshot diagnostics command:" in output
    assert snapshot_diagnostics_command in output
    assert (
        "Operator next action: RUN no-order portfolio snapshot diagnostics "
        "before finalize:"
    ) in output
    assert "Performance evidence guard: waiting for total_value/daily_return" in output
    assert "Finalize report status: blocked" in output
    assert "Finalize report generated at: 2026-04-10T15:40:00" in output
    assert "Source record usable fields: cash" in output
    assert "Source record unusable fields: total_value" in output
    assert "Missing performance fields: total_value, daily_return" in output
    assert "Portfolio metrics probe: missing_current_snapshot_after_trades" in output
    assert (
        "Portfolio metrics probe reason: trades exist after previous snapshot "
        "but current snapshot is missing"
    ) in output
    assert (
        "Portfolio metrics recovery: run end-of-day portfolio snapshot capture "
        "for the trade day"
    ) in output
    assert "Portfolio metrics snapshot found: current=False previous=True" in output
    assert "Portfolio metrics previous snapshot at: 2026-04-09T15:35:00" in output
    assert "Portfolio metrics trades: today=1 since_previous=1" in output
    assert "Portfolio metrics probe fields: none" in output
    assert "Finalize report source: reports/paper_runtime/" in output
    assert "RUN current blockers priority command" not in output
    assert summary_path.as_posix() in output


def test_paper_pilot_control_status_refreshes_legacy_finalize_diagnostics(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate "
        "--finalize-pilot-evidence --finalize-date 2026-04-10"
    )
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_INVALID",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "shadow_days": 2,
                    "invalid_execution_days": 1,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: evidence invalid",
                    "finalize_pilot_evidence": finalize_command,
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    action = {
        "strategy": strategy,
        "source_path": summary_path.as_posix(),
        "daily_ops_status": "PILOT_EVIDENCE_INVALID",
        "daily_ops_trade_day": "2026-04-10",
        "desc": "구버전 finalize report 진단 보강",
        "command": finalize_command,
        "scheduled_command": finalize_command,
        "performance_evidence_guard": (
            "target_weight_pilot_evidence_finalize_missing_performance"
        ),
        "finalize_report_diagnostics_status": "missing",
        "finalize_diagnostics_refresh_command": finalize_command,
        "finalize_report_source": (
            "reports/paper_runtime/"
            "target_weight_pilot_evidence_finalize_target_weight_candidate_2026-04-10.json"
        ),
        "verified_pilot_days": 0,
        "target_days": 60,
        "shadow_days": 2,
        "invalid_execution_days": 1,
    }
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "commands": {
                        "regenerate_current_blockers": (
                            "python tools/evaluate_and_promote.py --current-blockers"
                        ),
                    },
                    "current_priority_action": action,
                },
                "next_actions": [action],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Finalize diagnostics: missing" in output
    assert "Diagnostics refresh command:" in output
    assert finalize_command in output
    assert (
        "Operator next action: RUN no-order finalize diagnostics refresh"
        in output
    )
    assert "WAIT for final portfolio performance evidence" not in output


def test_paper_pilot_control_status_guides_missing_snapshot_history(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    finalize_command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate "
        "--finalize-pilot-evidence --finalize-date 2026-04-10"
    )
    snapshot_diagnostics_command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate "
        "--diagnose-portfolio-snapshot --snapshot-date 2026-04-10"
    )
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_INVALID",
                "evidence_progress": {
                    "verified_pilot_days": 0,
                    "target_days": 60,
                    "invalid_execution_days": 1,
                },
                "decision": {},
                "operator_commands": {
                    "finalize_pilot_evidence": finalize_command,
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    action = {
        "strategy": strategy,
        "source_path": summary_path.as_posix(),
        "daily_ops_status": "PILOT_EVIDENCE_INVALID",
        "daily_ops_trade_day": "2026-04-10",
        "desc": "portfolio snapshot 이력 복구 필요",
        "command": (
            "# blocked: final performance evidence unavailable; "
            "target_weight_pilot_evidence_finalize_missing_performance: "
            "total_value/daily_return unavailable"
        ),
        "scheduled_command": finalize_command,
        "performance_evidence_guard": (
            "target_weight_pilot_evidence_finalize_missing_performance"
        ),
        "finalize_report_diagnostics_status": "present",
        "finalize_missing_performance_fields": ["total_value", "daily_return"],
        "finalize_portfolio_metrics_checked": True,
        "finalize_portfolio_metrics_probe_status": "missing_snapshot_history",
        "finalize_portfolio_metrics_probe_reason": (
            "no portfolio snapshot exists for account_key"
        ),
        "finalize_portfolio_metrics_recovery_hint": (
            "restore or create portfolio snapshot history for the target-weight "
            "account_key"
        ),
        "finalize_portfolio_snapshot_diagnostics_command": (
            snapshot_diagnostics_command
        ),
        "finalize_portfolio_metrics_current_snapshot_found": False,
        "finalize_portfolio_metrics_previous_snapshot_found": False,
        "finalize_portfolio_metrics_trades_today": 0,
        "finalize_portfolio_metrics_trades_since_previous": 0,
        "finalize_portfolio_metrics_fields_present": [],
    }
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "commands": {},
                    "current_priority_action": action,
                },
                "next_actions": [action],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Portfolio metrics probe: missing_snapshot_history" in output
    assert "Portfolio metrics recovery: restore or create portfolio snapshot history" in output
    assert (
        "Operator next action: RUN no-order portfolio snapshot diagnostics "
        "before finalize"
        in output
    )
    assert "Portfolio snapshot diagnostics command:" in output
    assert snapshot_diagnostics_command in output
    assert finalize_command in output
    assert "WAIT for final portfolio performance evidence" not in output


def test_target_weight_effective_execution_status_labels_ready_and_blockers():
    import tools.paper_pilot_control as ppc

    ready_status, ready_reason = ppc._target_weight_effective_execution_status(
        {"status": "READY_TO_EXECUTE"},
        execute_command=(
            "python tools/target_weight_rotation_pilot.py "
            "--execute --collect-evidence"
        ),
    )
    invalid_status, invalid_reason = ppc._target_weight_effective_execution_status(
        {"status": "PILOT_EVIDENCE_INVALID"},
        execute_command="# blocked: pilot_paper evidence invalid",
    )
    cap_status, cap_reason = ppc._target_weight_effective_execution_status(
        {"status": "READY_TO_ENABLE_CAPS"},
        execute_command="# blocked: cap approval required",
    )

    assert ready_status == "READY"
    assert ready_reason == "daily ops READY_TO_EXECUTE command available"
    assert invalid_status == "BLOCKED"
    assert invalid_reason == "daily ops requires evidence finalize or repair first"
    assert cap_status == "BLOCKED"
    assert cap_reason == "daily ops requires cap approval before execution"


def test_paper_pilot_control_status_labels_target_weight_entry_as_core(monkeypatch, capsys):
    import core.paper_pilot as paper_pilot
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    monkeypatch.setattr(ppc, "_is_target_weight_strategy_for_enable", lambda value: value == strategy)
    monkeypatch.setattr(
        paper_pilot,
        "get_active_pilot",
        lambda value: SimpleNamespace(
            valid_from="2026-04-10",
            valid_to="2026-07-03",
            max_orders_per_day=3,
            max_concurrent_positions=3,
            max_notional_per_trade=2_000_000,
            max_gross_exposure=10_000_000,
        ),
    )
    monkeypatch.setattr(
        paper_pilot,
        "check_pilot_entry",
        lambda value: SimpleNamespace(
            allowed=True,
            reason="pilot authorized",
            remaining_orders=3,
            remaining_exposure=10_000_000,
        ),
    )
    monkeypatch.setattr(
        ppc,
        "_print_target_weight_daily_ops_status",
        lambda value: print("\n  Target-weight Daily Ops: MISSING"),
    )

    ppc.run_status(strategy)

    output = capsys.readouterr().out
    assert "Core Entry Check: ALLOWED" in output
    assert "Target-weight Daily Ops: MISSING" in output


def test_paper_pilot_control_status_prints_notifier_recovery_command(
    monkeypatch,
    capsys,
):
    import core.paper_pilot as paper_pilot
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    monkeypatch.setattr(
        ppc,
        "_is_target_weight_strategy_for_enable",
        lambda value: value == strategy,
    )
    monkeypatch.setattr(paper_pilot, "get_active_pilot", lambda value: None)
    monkeypatch.setattr(
        paper_pilot,
        "check_pilot_entry",
        lambda value: SimpleNamespace(
            allowed=False,
            reason="notifier stale - rerun preflight with --send-test-notification",
            remaining_orders=None,
            remaining_exposure=None,
        ),
    )
    monkeypatch.setattr(
        ppc,
        "_print_target_weight_daily_ops_status",
        lambda value: None,
    )

    ppc.run_status(strategy)

    output = capsys.readouterr().out
    assert "Core Entry Check: BLOCKED" in output
    assert (
        "Core recovery command: "
        "python tools/paper_preflight.py --strategy target_weight_candidate "
        "--with-pilot-check --send-test-notification"
    ) in output


def test_paper_pilot_control_status_uses_current_blockers_default_strategy(
    monkeypatch,
    tmp_path,
    capsys,
):
    import database.models as db_models
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(ppc, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(db_models, "init_database", lambda: None)
    monkeypatch.setattr(ppc, "run_status", lambda value: captured.setdefault("strategy", value))
    monkeypatch.setattr(sys, "argv", ["paper_pilot_control.py", "--status"])

    ppc.main()

    output = capsys.readouterr().out
    assert captured["strategy"] == strategy
    assert "using current blockers primary strategy" in output
    assert strategy in output


def test_paper_pilot_control_write_actions_require_explicit_strategy(monkeypatch, capsys):
    import tools.paper_pilot_control as ppc

    monkeypatch.setattr(sys, "argv", ["paper_pilot_control.py", "--enable"])

    with pytest.raises(SystemExit):
        ppc.main()

    error = capsys.readouterr().err
    assert "--strategy is required" in error
    assert "unless --status" in error


def test_paper_pilot_control_status_prints_daily_ops_command_when_missing(tmp_path, capsys):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops: MISSING" in output
    assert f"--candidate-id {strategy} --daily-ops-summary" in output


def test_paper_pilot_control_status_prints_daily_ops_failure_when_no_summary(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100000.json"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:00:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: market data stale",
                "error": {
                    "type": "ValueError",
                    "message": "market data stale",
                },
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops: FAILED" in output
    assert f"Failure source: {failure_path.as_posix()}" in output
    assert (
        "Failure reason: target_weight_daily_ops_summary_blocked: market data stale"
        in output
    )
    assert "Failure error: ValueError: market data stale" in output
    assert "--as-of-date 2026-04-10 --daily-ops-summary" in output


def test_paper_pilot_control_status_waits_when_only_failure_needs_market_data(
    tmp_path,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
        "--daily-ops-summary"
    )
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100000.json"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:00:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": (
                    "target_weight_daily_ops_summary_blocked: "
                    "target_weight_requested_trade_day_unavailable"
                ),
                "operator_commands": {"daily_ops_summary": command},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops: FAILED" in output
    assert "Wait: requested trade-day market data unavailable" in output
    assert f"Scheduled recovery command: {command}" in output
    assert f"Run: {command}" not in output


def test_paper_pilot_control_status_warns_when_daily_ops_failure_is_newer(
    tmp_path,
    monkeypatch,
    capsys,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100500.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: already recorded",
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: missing readiness audit",
                "error": {
                    "type": "ValueError",
                    "message": "missing readiness audit",
                },
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(summary_path, (1_000, 1_000))
    os.utime(failure_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops:" in output
    assert "Failure warning: latest daily ops failure is newer than loaded summary" in output
    assert f"Failure source: {failure_path.as_posix()}" in output
    assert (
        "Failure reason: target_weight_daily_ops_summary_blocked: missing readiness audit"
        in output
    )
    assert "Failure recovery command:" in output
    assert "Status: PILOT_EVIDENCE_RECORDED" in output


def test_paper_pilot_control_status_accepts_current_failure_priority(
    tmp_path,
    monkeypatch,
    capsys,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100500.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: already recorded",
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = (
        "python tools/target_weight_rotation_pilot.py "
        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
        "--daily-ops-summary"
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": (
                    "target_weight_daily_ops_summary_blocked: "
                    "target_weight_requested_trade_day_unavailable"
                ),
                "operator_commands": {"daily_ops_summary": command},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    action = {
        "strategy": strategy,
        "source": "latest_daily_ops_failure",
        "source_path": failure_path.as_posix(),
        "daily_ops_status": "FAILED",
        "failure_reason": (
            "target_weight_daily_ops_summary_blocked: "
            "target_weight_requested_trade_day_unavailable"
        ),
        "desc": "daily ops summary 실패 원인 해소 후 summary 재생성",
        "command": (
            "# blocked: requested trade-day market data unavailable; "
            "target_weight_requested_trade_day_unavailable"
        ),
        "scheduled_command": command,
        "order_safety": "no_order",
    }
    (tmp_path / "current_blockers.json").write_text(
        json.dumps(
            {
                "operator_runbook": {
                    "primary_strategy": strategy,
                    "current_priority_action": action,
                    "commands": {
                        "regenerate_current_blockers": (
                            "python tools/evaluate_and_promote.py --current-blockers"
                        ),
                    },
                },
                "next_actions": [action],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(summary_path, (1_000, 1_000))
    os.utime(failure_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Failure warning: latest daily ops failure is newer than loaded summary" in output
    assert "Current blockers priority: daily ops summary 실패 원인 해소 후 summary 재생성" in output
    assert (
        "Operator next action: WAIT for requested trade-day market data, then rerun "
        "daily ops recovery command:"
        in output
    )
    assert "stale priority action" not in output


def test_paper_pilot_control_status_uses_generated_at_for_daily_ops_failure_order(
    tmp_path,
    monkeypatch,
    capsys,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100500.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "generated_at": "2026-04-10T10:00:00",
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: already recorded",
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: generated later",
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(summary_path, (2_000, 2_000))
    os.utime(failure_path, (1_000, 1_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert (
        "Failure warning: latest daily ops failure is newer than loaded summary"
        in output
    )
    assert "generated later" in output
    assert "Status: PILOT_EVIDENCE_RECORDED" in output


def test_paper_pilot_control_status_ignores_older_generated_failure_with_newer_mtime(
    tmp_path,
    monkeypatch,
    capsys,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    failure_path = (
        summary_dir
        / f"target_weight_daily_ops_summary_failure_{strategy}_20260410100500.json"
    )
    summary_path.write_text(
        json.dumps(
            _daily_ops_with_summary_hash({
                "artifact_type": "target_weight_daily_ops_summary",
                "candidate_id": strategy,
                "generated_at": "2026-04-10T10:10:00",
                "trade_day": "2026-04-10",
                "status": "PILOT_EVIDENCE_RECORDED",
                "evidence_progress": {
                    "verified_pilot_days": 1,
                    "target_days": 60,
                },
                "decision": {},
                "operator_commands": {
                    "execute_capped_paper": "# blocked: already recorded",
                },
            }),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    failure_path.write_text(
        json.dumps(
            {
                "artifact_type": "target_weight_no_order_operation_failure",
                "schema_version": 1,
                "generated_at": "2026-04-10T10:05:00",
                "mode": "daily_ops_summary",
                "candidate_id": strategy,
                "as_of_date": "2026-04-10",
                "status": "BLOCKED",
                "reason": "target_weight_daily_ops_summary_blocked: older failure",
                "operator_commands": {
                    "daily_ops_summary": (
                        "python tools/target_weight_rotation_pilot.py "
                        "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                        "--daily-ops-summary"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.utime(summary_path, (1_000, 1_000))
    os.utime(failure_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Failure warning:" not in output
    assert "Failure source:" not in output
    assert "Status: PILOT_EVIDENCE_RECORDED" in output


def test_paper_pilot_control_ignores_future_daily_ops_summary(tmp_path, monkeypatch):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    current_summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "PILOT_EVIDENCE_RECORDED",
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    future_summary = {
        **current_summary,
        "trade_day": "2026-04-13",
        "status": "BLOCKED",
        "operator_commands": {
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute"
        },
    }
    malformed_summary = {
        **current_summary,
        "trade_day": "not-a-date",
        "status": "READY_TO_EXECUTE",
    }
    missing_trade_day_summary = {
        key: value for key, value in current_summary.items() if key != "trade_day"
    }
    missing_trade_day_summary["status"] = "READY_TO_EXECUTE"
    current_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    future_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-13.json"
    malformed_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_malformed.json"
    missing_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_missing_trade_day.json"
    current_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(current_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    future_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(future_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    malformed_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(malformed_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    missing_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(missing_trade_day_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(current_path, (1_000, 1_000))
    os.utime(future_path, (2_000, 2_000))
    os.utime(malformed_path, (3_000, 3_000))
    os.utime(missing_path, (4_000, 4_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    summary = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert summary is not None
    assert summary["trade_day"] == "2026-04-10"
    assert summary["status"] == "PILOT_EVIDENCE_RECORDED"


def test_paper_pilot_control_prefers_latest_trade_day_over_mtime(tmp_path, monkeypatch):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    older_summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "PILOT_EVIDENCE_RECORDED",
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    newer_summary = {
        **older_summary,
        "trade_day": "2026-04-13",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
    }
    older_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    newer_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-13.json"
    older_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(older_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    newer_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(newer_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(newer_path, (1_000, 1_000))
    os.utime(older_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-13")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    assert loaded["trade_day"] == "2026-04-13"
    assert loaded["status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"


def test_paper_pilot_control_prefers_latest_generated_summary_for_same_trade_day(
    tmp_path,
    monkeypatch,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    earlier_summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-04-10T10:00:00",
        "trade_day": "2026-04-10",
        "status": "PILOT_EVIDENCE_RECORDED",
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    later_summary = {
        **earlier_summary,
        "generated_at": "2026-04-10T10:10:00",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
    }
    earlier_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_earlier.json"
    later_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_later.json"
    earlier_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(earlier_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    later_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(later_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(later_path, (1_000, 1_000))
    os.utime(earlier_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    assert loaded["status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"
    assert loaded["generated_at"] == "2026-04-10T10:10:00"


def test_paper_pilot_control_skips_daily_ops_summary_hash_mismatch(tmp_path, monkeypatch):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    valid_summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "PILOT_EVIDENCE_RECORDED",
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    tampered_summary = {
        **valid_summary,
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                "--execute --collect-evidence"
            ),
        },
    }
    valid_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    tampered_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_tampered.json"
    valid_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(valid_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    tampered_payload = _daily_ops_with_summary_hash(tampered_summary)
    tampered_payload["next_step"] = "tampered after hash"
    tampered_path.write_text(json.dumps(tampered_payload, ensure_ascii=False), encoding="utf-8")
    os.utime(valid_path, (1_000, 1_000))
    os.utime(tampered_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    assert loaded["status"] == "PILOT_EVIDENCE_RECORDED"


def test_paper_pilot_control_status_warns_on_daily_ops_summary_hash_mismatch(
    tmp_path,
    monkeypatch,
    capsys,
):
    import os
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    valid_summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "PILOT_EVIDENCE_RECORDED",
        "evidence_progress": {"verified_pilot_days": 1, "target_days": 60},
        "decision": {},
        "operator_commands": {"execute_capped_paper": "# blocked: already recorded"},
    }
    tampered_summary = {
        **valid_summary,
        "trade_day": "2026-04-11",
        "status": "READY_TO_EXECUTE",
    }
    valid_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    tampered_path = summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-11.json"
    valid_path.write_text(
        json.dumps(_daily_ops_with_summary_hash(valid_summary), ensure_ascii=False),
        encoding="utf-8",
    )
    tampered_payload = _daily_ops_with_summary_hash(tampered_summary)
    tampered_payload["next_step"] = "tampered after hash"
    tampered_path.write_text(json.dumps(tampered_payload, ensure_ascii=False), encoding="utf-8")
    os.utime(valid_path, (1_000, 1_000))
    os.utime(tampered_path, (2_000, 2_000))
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-11")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops:" in output
    assert "Integrity warning:" in output
    assert "summary_hash mismatch or missing" in output
    assert "Status: PILOT_EVIDENCE_RECORDED" in output


def test_paper_pilot_control_status_marks_invalid_when_only_bad_daily_ops_exists(
    tmp_path,
    monkeypatch,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id target_weight_candidate --as-of-date 2026-04-10 "
                "--execute --collect-evidence"
            ),
        },
    }
    payload = _daily_ops_with_summary_hash(summary)
    payload["status"] = "READY_TO_ENABLE_CAPS"
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Target-weight Daily Ops: INVALID" in output
    assert "Integrity warning:" in output
    assert "summary_hash mismatch or missing" in output
    assert f"--candidate-id {strategy} --daily-ops-summary" in output


def test_paper_pilot_control_blocks_loaded_ready_execute_scope_mismatch(tmp_path, monkeypatch):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id other_candidate --as-of-date 2026-04-09 "
                "--execute --collect-evidence"
            ),
        },
    }
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(summary), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    command = loaded["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_execute_command_unavailable")
    assert "candidate_id mismatch" in command
    assert "as_of_date mismatch" in command


def test_paper_pilot_control_blocks_stale_generated_ready_execute(tmp_path, monkeypatch):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-04-10T10:00:00",
        "trade_day": "2026-04-10",
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy} --as-of-date 2026-04-10 "
                "--execute --collect-evidence"
            ),
        },
    }
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(summary), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ppc,
        "_current_kst_datetime",
        lambda: datetime(2026, 4, 10, 10, 31, tzinfo=ppc.KST),
    )

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    command = loaded["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_summary.generated_at is stale")
    assert "rerun daily ops summary before action" in command


def test_paper_pilot_control_status_warns_stale_ready_execute_generated_at(
    tmp_path,
    monkeypatch,
    capsys,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "generated_at": "2026-04-10T10:00:00",
        "trade_day": "2026-04-10",
        "status": "READY_TO_EXECUTE",
        "evidence_progress": {"verified_pilot_days": 12, "target_days": 60},
        "decision": {"blocking_reasons": []},
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy} --as-of-date 2026-04-10 "
                "--execute --collect-evidence"
            ),
        },
    }
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(summary), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ppc,
        "_current_kst_datetime",
        lambda: datetime(2026, 4, 10, 10, 31, tzinfo=ppc.KST),
    )

    ppc._print_target_weight_daily_ops_status(strategy, reports_dir=tmp_path)

    output = capsys.readouterr().out
    assert "Generated at: 2026-04-10T10:00:00" in output
    assert "Freshness warning: daily_ops_summary.generated_at is stale" in output
    assert "Effective target-weight execution: BLOCKED" in output
    assert "Adapter execution: BLOCKED by daily ops" in output


def test_paper_pilot_control_blocks_loaded_ready_execute_candidate_prefix_collision(
    tmp_path,
    monkeypatch,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "status": "READY_TO_EXECUTE",
        "operator_commands": {
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy}_shadow --as-of-date 2026-04-10 "
                "--execute --collect-evidence"
            ),
        },
    }
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(summary), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    command = loaded["operator_commands"]["execute_capped_paper"]
    assert command.startswith("# blocked: daily_ops_execute_command_unavailable")
    assert "candidate_id mismatch" in command


def test_paper_pilot_control_repairs_next_check_command_scope_mismatch(
    tmp_path,
    monkeypatch,
):
    import tools.paper_pilot_control as ppc

    strategy = "target_weight_candidate"
    summary_dir = tmp_path / "paper_runtime"
    summary_dir.mkdir(parents=True)
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "candidate_id": strategy,
        "trade_day": "2026-04-10",
        "next_operator_trade_day": "2026-04-13",
        "status": "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
        "operator_commands": {
            "next_daily_ops_summary": (
                "python tools/target_weight_rotation_pilot.py "
                "--candidate-id wrong_candidate --as-of-date 2026-04-12 "
                "--readiness-audit"
            ),
            "next_readiness_audit": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {strategy}_shadow --as-of-date 2026-04-13 "
                "--daily-ops-summary"
            ),
        },
    }
    (
        summary_dir / f"target_weight_daily_ops_summary_{strategy}_2026-04-10.json"
    ).write_text(
        json.dumps(_daily_ops_with_summary_hash(summary), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(ppc, "_current_kst_date", lambda: "2026-04-10")

    loaded = ppc._load_latest_target_weight_daily_ops(strategy, reports_dir=tmp_path)

    assert loaded is not None
    commands = loaded["operator_commands"]
    assert commands["next_daily_ops_summary"] == (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --as-of-date 2026-04-13 --daily-ops-summary"
    )
    assert commands["next_readiness_audit"] == (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --as-of-date 2026-04-13 --readiness-audit"
    )


def test_shadow_batch_cli_exits_nonzero_when_target_unmet(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    calls = {}

    def fake_run_shadow_bootstrap(**kwargs):
        calls.update(kwargs)
        return {
            "summary": {
                "recorded": 1,
                "already_recorded": 0,
                "duplicate_trade_day": 0,
                "failed": 1,
                "covered_unique_trade_days": 1,
                "target_unique_trade_days": 2,
                "target_met": False,
            },
            "launch_artifacts": {"attempted": False},
            "artifact_path": tmp_path / "shadow_batch.json",
            "start_date": "2026-04-10",
            "end_date": "2026-04-13",
            "requested_dates": ["2026-04-10", "2026-04-13"],
        }

    monkeypatch.setattr(twp, "run_shadow_bootstrap", fake_run_shadow_bootstrap)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--shadow-days",
            "2",
            "--shadow-end-date",
            "2026-04-13",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert calls["target_unique_trade_days"] == 2
    assert "met=NO" in output
    assert "status: BLOCKED - shadow bootstrap incomplete" in output


def test_pilot_cli_exits_nonzero_when_execution_fidelity_blocked(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()

    def fake_run_pilot(**kwargs):
        return {
            "plan": plan,
            "validation": SimpleNamespace(allowed=True, reason="pilot caps satisfied"),
            "cap_preview": SimpleNamespace(allowed=True, reason="proposed pilot caps"),
            "cap_recommendation": {
                "suggested_caps": {
                    "max_orders_per_day": 3,
                    "max_concurrent_positions": 3,
                    "max_notional_per_trade": 1_260_000,
                    "max_gross_exposure": 3_360_000,
                }
            },
            "execution": {
                "executed": len(plan.orders),
                "failed": 0,
                "skipped": 0,
                "halted": False,
                "halt_reason": "",
                "details": [],
            },
            "execution_evidence": {
                "complete": False,
                "reason": "target_weight_position_mismatch: CCC actual=0 target=4000",
            },
            "evidence_collection": {"attempted": False, "recorded": False},
            "shadow_evidence_summary": {"attempted": False, "recorded": False},
            "launch_artifacts": {"attempted": False},
            "artifact_path": tmp_path / "session.json",
        }

    monkeypatch.setattr(twp, "run_pilot", fake_run_pilot)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--execute",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "execution fidelity: BLOCKED" in output
    assert "target_weight_position_mismatch" in output


def test_target_weight_plan_uses_prior_day_scores_for_targets():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    c = frames["CCC"].copy()
    c.loc[c["date"] == pd.Timestamp("2025-02-03"), "close"] = 500.0
    frames["CCC"] = c

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.score_day == "2025-01-31"
    assert set(plan.targets) == {"AAA", "BBB"}
    assert "CCC" not in plan.targets
    assert {order.symbol for order in plan.orders if order.action == "BUY"} == {"AAA", "BBB"}


def test_target_weight_plan_records_full_expected_quantities_after_rebalance():
    from core.target_weight_rotation import build_target_weight_plan

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={**_params(), "target_tolerance_pct": 100.0},
        cash=100_000.0,
        positions={"AAA": {"quantity": 100, "avg_price": 100.0}},
        as_of_date="2025-02-03",
        collector=FakeCollector(_frames_for_rotation()),
    )

    assert plan.orders == []
    assert plan.target_quantities_after is not None
    assert plan.expected_position_quantities == plan.target_quantities_after
    assert set(plan.targets).issubset(set(plan.target_quantities_after))
    assert plan.target_quantities_after["AAA"] == 100
    assert plan.position_quantities_before == {"AAA": 100}
    assert plan.starting_position_quantities == {"AAA": 100}
    assert plan.to_dict()["target_quantities_after"] == plan.target_quantities_after
    assert plan.to_dict()["position_quantities_before"] == plan.position_quantities_before


def test_target_weight_plan_risk_overlay_uses_prior_day_benchmark():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    benchmark = frames["KS11"].copy()
    same_day_mask = benchmark["date"] == pd.Timestamp("2025-02-03")
    benchmark.loc[same_day_mask, "close"] = 50.0
    benchmark.loc[same_day_mask, "open"] = 50.0
    benchmark.loc[same_day_mask, "high"] = 50.0
    benchmark.loc[same_day_mask, "low"] = 50.0
    frames["KS11"] = benchmark

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={
            **_params(),
            "market_exposure_mode": "benchmark_risk",
            "market_ma_period": 5,
            "bear_target_exposure": 0.35,
            "benchmark_drawdown_lookback": 5,
            "benchmark_drawdown_trigger_pct": 4.0,
        },
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.target_exposure == 0.8
    assert plan.risk_off is False


def test_pilot_plan_validation_blocks_order_count_and_notional_caps():
    from core.target_weight_rotation import build_target_weight_plan, validate_plan_against_pilot

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(_frames_for_rotation()),
    )
    pilot_check = SimpleNamespace(
        allowed=True,
        reason="pilot authorized",
        remaining_orders=1,
        caps_snapshot={
            "max_orders_per_day": 1,
            "max_concurrent_positions": 1,
            "max_notional_per_trade": 10_000,
            "max_gross_exposure": 30_000,
        },
    )

    validation = validate_plan_against_pilot(plan, pilot_check)

    assert validation.allowed is False
    assert "remaining_orders" in validation.reason
    assert "max order notional" in validation.reason
    assert "target positions" in validation.reason


def test_execute_plan_dry_run_preserves_sell_before_buy_ordering():
    from core.target_weight_rotation import build_target_weight_plan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={**_params(), "target_top_n": 1},
        cash=50_000.0,
        positions={"BBB": {"quantity": 20, "avg_price": 105.0}},
        as_of_date="2025-03-03",
        collector=FakeCollector(_frames_for_rotation()),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=True,
    )

    actions = [detail["order"]["action"] for detail in execution["details"]]
    assert actions == sorted(actions, reverse=True)
    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)


def test_execute_plan_blocks_direct_submit_without_wrapper_guards(monkeypatch):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("missing wrapper guards must block before order submission"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_pilot_validation_required" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_pilot_validation"


def test_execute_plan_requires_preflight_refresh_before_order_submission(monkeypatch):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    guards = _execute_plan_submit_guards_ok(plan)
    del guards["preflight_refresh"]
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("missing preflight refresh must block before order submission"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **guards,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_preflight_refresh_required" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_preflight_refresh"


@pytest.mark.parametrize(
    ("guard_name", "reason_text", "status"),
    [
        (
            "execution_trade_day_check",
            "target_weight_execution_trade_day_check_required",
            "skipped_execution_trade_day_mismatch",
        ),
        (
            "execution_market_session_check",
            "target_weight_execution_market_session_check_required",
            "skipped_execution_market_session_closed",
        ),
        (
            "pilot_authorization_snapshot_check",
            "target_weight_pilot_authorization_snapshot_check_required",
            "skipped_pilot_authorization_snapshot_mismatch",
        ),
    ],
)
def test_execute_plan_requires_submission_guard_inputs(
    monkeypatch,
    guard_name,
    reason_text,
    status,
):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    guards = _execute_plan_submit_guards_ok(plan)
    del guards[guard_name]
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail(f"missing {guard_name} must block before order submission"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **guards,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert reason_text in execution["halt_reason"]
    assert execution["details"][0]["status"] == status


def test_execute_plan_uses_portfolio_current_capital_for_buy_orders():
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    capital_values = []

    class FakeExecutor:
        def __init__(self, config, account_key=""):
            pass

        def execute_sell(self, **kwargs):
            return {"success": True}

        def execute_buy_quantity(self, **kwargs):
            capital_values.append(kwargs["capital"])
            return {
                "success": True,
                "symbol": kwargs["symbol"],
                "action": "BUY",
                "quantity": kwargs["quantity"],
                "price": kwargs["price"],
                "mode": "paper",
            }

    class FakePortfolio:
        def __init__(self, config, account_key=""):
            pass

        def get_available_cash(self):
            return 10_000_000.0

        def get_current_capital(self):
            return 11_000_000.0

    with patch("core.order_executor.OrderExecutor", FakeExecutor), \
         patch("core.portfolio_manager.PortfolioManager", FakePortfolio), \
         patch("tools.target_weight_rotation_pilot._load_positions", lambda account_key: {}):
        execution = execute_plan(
            plan,
            config=SimpleNamespace(trading={"mode": "paper"}),
            dry_run=False,
            **_execute_plan_submit_guards_ok(plan),
            execution_idempotency={"allowed": True},
            pre_execution_reconciliation={"complete": True},
            liquidity_check={"checked": True, "complete": True, "reason": "ok"},
            pre_trade_risk_check={"checked": True, "complete": True, "reason": "ok"},
        )

    assert execution["executed"] == len(plan.orders)
    assert execution["failed"] == 0
    assert execution["halted"] is False
    assert capital_values == [11_000_000.0] * len(plan.orders)


def test_execute_plan_stops_after_failed_sell_before_buy(tmp_path):
    from core.target_weight_rotation import TargetWeightOrder, TargetWeightPlan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = TargetWeightPlan(
        candidate_id="candidate",
        as_of_date="2025-03-03",
        trade_day="2025-03-03",
        score_day="2025-02-28",
        params_hash="hash",
        symbols=["AAA", "BBB"],
        targets=["BBB"],
        prices={"AAA": 100.0, "BBB": 200.0},
        target_exposure=0.8,
        base_target_exposure=0.8,
        risk_off=False,
        nav=10_000.0,
        cash_before=1_000.0,
        market_value_before=9_000.0,
        cash_after_estimate=1_000.0,
        gross_exposure_after=8_000.0,
        target_position_count=1,
        orders=[
            TargetWeightOrder(
                symbol="AAA",
                action="SELL",
                price=100.0,
                quantity=10,
                notional=1_000.0,
                current_quantity=10,
                target_quantity=0,
                current_weight_pct=10.0,
                target_weight_pct=0.0,
                reason="sell first",
            ),
            TargetWeightOrder(
                symbol="BBB",
                action="BUY",
                price=200.0,
                quantity=5,
                notional=1_000.0,
                current_quantity=0,
                target_quantity=5,
                current_weight_pct=0.0,
                target_weight_pct=10.0,
                reason="buy second",
            ),
        ],
        diagnostics={},
    )

    class FakeExecutor:
        buy_calls = 0

        def __init__(self, config, account_key=""):
            pass

        def execute_sell(self, **kwargs):
            return {"success": False, "reason": "no position"}

        def execute_buy_quantity(self, **kwargs):
            FakeExecutor.buy_calls += 1
            return {"success": True}

    class FakePortfolio:
        def __init__(self, config, account_key=""):
            pass

        def get_available_cash(self):
            return 10_000.0

        def get_current_capital(self):
            return 10_000.0

    with patch("core.order_executor.OrderExecutor", FakeExecutor), \
         patch("core.portfolio_manager.PortfolioManager", FakePortfolio), \
         patch("core.paper_pilot.RUNTIME_DIR", tmp_path / "paper_runtime"), \
         patch(
             "tools.target_weight_rotation_pilot._load_positions",
             lambda account_key: {"AAA": SimpleNamespace(quantity=10)},
         ):
        execution = execute_plan(
            plan,
            config=SimpleNamespace(trading={"mode": "paper"}),
            dry_run=False,
            **_execute_plan_submit_guards_ok(plan),
            liquidity_check={"checked": True, "complete": True, "reason": "ok"},
            pre_trade_risk_check={"checked": True, "complete": True, "reason": "ok"},
        )

    assert execution["failed"] == 1
    assert execution["halted"] is True
    assert execution["details"][1]["status"] == "skipped_after_failure"
    assert FakeExecutor.buy_calls == 0


def test_execute_plan_blocks_stale_starting_positions_before_order_submission(monkeypatch, tmp_path):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    monkeypatch.setattr("core.paper_pilot.RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot._load_positions",
        lambda account_key: {"ZZZ": SimpleNamespace(quantity=3)},
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **_execute_plan_submit_guards_ok(plan),
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_pre_execution_position_drift" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_pre_execution_position_drift"


def test_execute_plan_rechecks_positions_after_supplied_pre_execution_snapshot(monkeypatch, tmp_path):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    monkeypatch.setattr("core.paper_pilot.RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot._load_positions",
        lambda account_key: {"ZZZ": SimpleNamespace(quantity=3)},
    )
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("position drift must block before order submission"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **_execute_plan_submit_guards_ok(plan),
        execution_idempotency={"allowed": True},
        pre_execution_reconciliation={
            "checked": True,
            "complete": True,
            "reason": "stale pre-execution snapshot looked clean",
        },
        liquidity_check={"checked": True, "complete": True, "reason": "ok"},
        pre_trade_risk_check={"checked": True, "complete": True, "reason": "ok"},
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_pre_execution_position_drift" in execution["halt_reason"]
    assert execution["pre_execution_reconciliation"]["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]


def test_execute_plan_blocks_duplicate_session_before_order_submission(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot._load_positions",
        lambda account_key: pytest.fail("duplicate execution must block before position reads"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **_execute_plan_submit_guards_ok(plan),
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_duplicate_execution_attempt" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_duplicate_execution_attempt"


def test_reconcile_order_results_flags_quantity_mismatch():
    from tools.target_weight_rotation_pilot import reconcile_order_results

    plan = _adapter_plan()
    execution = _complete_execution(plan)
    execution["details"][0]["result"]["quantity"] = plan.orders[0].quantity - 1

    reconciliation = reconcile_order_results(plan, execution)

    assert reconciliation["complete"] is False
    assert "target_weight_order_result_mismatch" in reconciliation["reason"]
    assert reconciliation["mismatches"][0]["type"] == "quantity"
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[0].symbol


def test_reconcile_plan_fills_flags_missing_trade_history_fill():
    from tools.target_weight_rotation_pilot import reconcile_plan_fills

    plan = _adapter_plan()
    fills = _complete_fills(plan)[:-1]

    reconciliation = reconcile_plan_fills(plan, fills)

    assert reconciliation["source"] == "database.trade_history"
    assert reconciliation["query"]["account_key"] == plan.candidate_id
    assert reconciliation["query"]["mode"] == "paper"
    assert reconciliation["complete"] is False
    assert "target_weight_fill_reconciliation_mismatch" in reconciliation["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol
    assert reconciliation["mismatches"][0]["actual_quantity"] == 0


def test_reconcile_plan_fills_flags_partial_quantity():
    from tools.target_weight_rotation_pilot import reconcile_plan_fills

    plan = _adapter_plan()
    fills = _complete_fills(plan)
    fills[0].quantity = plan.orders[0].quantity - 1

    reconciliation = reconcile_plan_fills(plan, fills)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[0].symbol
    assert reconciliation["mismatches"][0]["expected_quantity"] == plan.orders[0].quantity
    assert reconciliation["mismatches"][0]["actual_quantity"] == plan.orders[0].quantity - 1


def test_load_paper_trade_fills_filters_by_execution_session_id(monkeypatch):
    import database.repositories as repositories
    from tools.target_weight_rotation_pilot import load_paper_trade_fills

    plan = _adapter_plan()
    target_fill = _complete_fills(plan, execution_session_id="session-a")[0]
    other_fill = _complete_fills(plan, execution_session_id="session-b")[0]
    captured = {}

    def fake_get_trade_history(**kwargs):
        captured.update(kwargs)
        return [target_fill, other_fill]

    monkeypatch.setattr(repositories, "get_trade_history", fake_get_trade_history)

    fills = load_paper_trade_fills(plan, execution_session_id="session-a")

    assert captured["execution_session_id"] == "session-a"
    assert fills == [target_fill]


def test_reconcile_plan_fills_rejects_same_day_unlinked_trade_history_fill():
    from tools.target_weight_rotation_pilot import reconcile_plan_fills

    plan = _adapter_plan()
    unlinked_fills = _complete_fills(plan, execution_session_id="")

    reconciliation = reconcile_plan_fills(
        plan,
        unlinked_fills,
        execution_session_id=TEST_EXECUTION_SESSION_ID,
    )

    assert reconciliation["complete"] is False
    assert "target_weight_fill_reconciliation_mismatch" in reconciliation["reason"]
    assert "unlinked" in reconciliation["reason"]
    assert reconciliation["actual_quantities"] == {}
    assert reconciliation["unlinked_fills"][0]["symbol"] == plan.orders[0].symbol
    assert reconciliation["mismatches"][0]["actual_quantity"] == 0


def test_verify_existing_pilot_evidence_accepts_complete_record(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [_existing_pilot_evidence_record(plan)])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is True
    assert verification["reason"] == "existing pilot_paper evidence verified"


def test_verify_existing_pilot_evidence_rejects_failed_benchmark(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = 0
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_evidence_quality"
    }
    assert verification["mismatches"][0]["actual"] == "target_weight_benchmark_status_not_final"


def test_verify_existing_pilot_evidence_rejects_missing_db_persistence_proof(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    execution = record["pilot_caps_snapshot"]["target_weight_execution"]
    execution.pop("db_persistence_complete")
    execution.pop("db_persistence_proof")
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    fields = {item["field"] for item in verification["mismatches"]}
    assert "target_weight_execution.db_persistence_complete" in fields
    assert "target_weight_execution.db_persistence_proof.complete" in fields


def test_repair_target_weight_pilot_evidence_appends_verified_record(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    evidence_dir = tmp_path / "paper_evidence"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", evidence_dir)

    def final_benchmark(*, date, daily_return, cash_ratio, watchlist_symbols):
        assert daily_return is not None
        assert watchlist_symbols == list(plan.targets)
        return {
            "same_universe_excess": round(daily_return - 0.10, 4),
            "exposure_matched_excess": round(daily_return - 0.05, 4),
            "cash_adjusted_excess": round(daily_return - 0.02, 4),
            "benchmark_status": "final",
            "benchmark_meta": {
                "type": "universe_equal_weight",
                "date": date.strftime("%Y-%m-%d"),
                "completeness": 1.0,
                "cash_ratio": cash_ratio,
            },
        }

    monkeypatch.setattr(pe, "_compute_benchmark_excess", final_benchmark)
    record = _repairable_invalid_pilot_evidence_record(plan)
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    result = twp.repair_target_weight_pilot_evidence(
        candidate_id=plan.candidate_id,
        repair_date=plan.trade_day,
        output_dir=tmp_path / "paper_runtime",
    )

    assert result["status"] == "repaired"
    assert result["appended_record_version"] == 2
    assert result["proof_status_after"]["reason"] == "verified_target_weight_pilot_evidence"
    assert result["promotion_status_after"]["eligible"] is False
    assert (
        result["promotion_status_after"]["reason"]
        == "target_weight_repaired_performance_not_promotable"
    )

    records = pe._read_all_evidence(pe._evidence_path(plan.candidate_id))
    assert len(records) == 2
    repaired = records[-1]
    valid, reason = pe._target_weight_record_proof_status(plan.candidate_id, repaired)
    repair_valid, repair_reason = pe._target_weight_record_proof_status(
        plan.candidate_id,
        repaired,
        allow_repaired_performance=True,
    )

    expected_total_value = (
        record["pilot_caps_snapshot"]["target_weight_execution"]["pre_trade_risk_check"][
            "projected_total_value_after_costs"
        ]
    )
    assert valid is False
    assert reason == "target_weight_repaired_performance_not_promotable"
    assert repair_valid is True
    assert repair_reason == "verified_target_weight_pilot_evidence"
    assert repaired["promotion_eligible"] is False
    assert repaired["promotion_exclusion_reason"] == "target_weight_repaired_performance_not_promotable"
    assert repaired["total_value"] == expected_total_value
    assert repaired["daily_return"] == pytest.approx((expected_total_value / plan.cash_before - 1.0) * 100)
    assert repaired["benchmark_status"] == "final"
    assert repaired["benchmark_meta"]["performance_repair"] is True
    assert repaired["benchmark_meta"]["repair_source"] == "target_weight_execution.pre_trade_risk_check"
    assert repaired["total_trades"] == len(plan.orders)

    progress = twp.summarize_target_weight_evidence_progress(plan.candidate_id)
    assert progress["verified_pilot_days"] == 0
    assert progress["repaired_pilot_days"] == 1
    assert progress["latest_repaired_pilot_date"] == plan.trade_day
    assert progress["invalid_reasons"] == {
        "target_weight_repaired_performance_not_promotable": 1,
    }


def test_finalize_target_weight_pilot_evidence_appends_promotable_record(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")

    def final_benchmark(*, date, daily_return, cash_ratio, watchlist_symbols):
        assert daily_return == pytest.approx(0.1)
        assert watchlist_symbols == list(plan.targets)
        return {
            "same_universe_excess": 0.11,
            "exposure_matched_excess": 0.09,
            "cash_adjusted_excess": 0.07,
            "benchmark_status": "final",
            "benchmark_meta": {
                "type": "universe_equal_weight",
                "date": date.strftime("%Y-%m-%d"),
                "cash_ratio": cash_ratio,
            },
        }

    monkeypatch.setattr(pe, "_compute_benchmark_excess", final_benchmark)
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    result = twp.finalize_target_weight_pilot_evidence(
        candidate_id=plan.candidate_id,
        finalize_date=plan.trade_day,
        output_dir=tmp_path / "paper_runtime",
    )

    assert result["status"] == "finalized"
    assert result["appended_record_version"] == 2
    assert result["proof_status_after"]["reason"] == "verified_target_weight_pilot_evidence"

    records = pe._read_all_evidence(pe._evidence_path(plan.candidate_id))
    assert len(records) == 2
    finalized = records[-1]
    valid, reason = pe._target_weight_record_proof_status(plan.candidate_id, finalized)
    assert valid is True
    assert reason == "verified_target_weight_pilot_evidence"
    assert finalized["benchmark_status"] == "final"
    assert finalized["same_universe_excess"] == 0.11
    assert finalized.get("promotion_eligible") is not False
    assert (finalized.get("benchmark_meta") or {}).get("performance_repair") is not True

    progress = twp.summarize_target_weight_evidence_progress(plan.candidate_id)
    assert progress["verified_pilot_days"] == 1
    assert progress["repaired_pilot_days"] == 0
    assert progress["latest_verified_pilot_date"] == plan.trade_day


def test_finalize_target_weight_pilot_evidence_reports_missing_performance_diagnostics(
    monkeypatch,
    tmp_path,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "current_snapshot",
            "_portfolio_probe_reason": "test snapshot",
            "_portfolio_probe_current_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_at": date.isoformat(),
            "_portfolio_probe_trades_today": 0,
            "_portfolio_probe_trades_since_previous": 0,
            "cash": 1_000_000.0,
        },
    )
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = None
    record["daily_return"] = None
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    with pytest.raises(ValueError, match="target_weight_pilot_evidence_finalize_missing_performance"):
        twp.finalize_target_weight_pilot_evidence(
            candidate_id=plan.candidate_id,
            finalize_date=plan.trade_day,
            output_dir=tmp_path / "paper_runtime",
        )

    report_path = next((tmp_path / "paper_runtime").glob("target_weight_pilot_evidence_finalize_*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    performance_status = report["performance_evidence_status"]
    assert report["status"] == "blocked"
    assert report["finalized_fields"] == {"cash": 1_000_000.0}
    assert performance_status["source_record_fields_present"]
    assert "total_value" not in performance_status["source_record_fields_present"]
    assert "daily_return" not in performance_status["source_record_fields_present"]
    assert "cash" in performance_status["source_record_fields_usable"]
    assert performance_status["source_record_fields_unusable"] == []
    assert performance_status["portfolio_metrics_checked"] is True
    assert performance_status["portfolio_metrics_probe_status"] == "current_snapshot"
    assert performance_status["portfolio_metrics_probe_reason"] == "test snapshot"
    assert performance_status["portfolio_metrics_current_snapshot_found"] is True
    assert performance_status["portfolio_metrics_fields_present"] == ["cash"]
    assert performance_status["missing_fields_after_probe"] == [
        "total_value",
        "daily_return",
    ]

    report_md = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Performance Evidence Status" in report_md
    assert "Source record fields usable:" in report_md
    assert "Portfolio metrics probe: `current_snapshot`" in report_md
    assert "Missing fields after probe: `total_value, daily_return`" in report_md


def test_finalize_target_weight_pilot_evidence_marks_zero_total_value_unusable(
    monkeypatch,
    tmp_path,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "missing_snapshot_history",
            "_portfolio_probe_reason": "no portfolio snapshot exists for account_key",
            "_portfolio_probe_current_snapshot_found": False,
            "_portfolio_probe_previous_snapshot_found": False,
            "_portfolio_probe_trades_today": 0,
            "_portfolio_probe_trades_since_previous": 0,
        },
    )
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = 0
    record["daily_return"] = None
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    with pytest.raises(ValueError, match="target_weight_pilot_evidence_finalize_missing_performance"):
        twp.finalize_target_weight_pilot_evidence(
            candidate_id=plan.candidate_id,
            finalize_date=plan.trade_day,
            output_dir=tmp_path / "paper_runtime",
        )

    report_path = next((tmp_path / "paper_runtime").glob("target_weight_pilot_evidence_finalize_*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    performance_status = report["performance_evidence_status"]
    assert "total_value" in performance_status["source_record_fields_present"]
    assert "total_value" not in performance_status["source_record_fields_usable"]
    assert "total_value" in performance_status["source_record_fields_unusable"]
    assert performance_status["portfolio_metrics_probe_status"] == "missing_snapshot_history"
    assert performance_status["missing_fields_after_probe"] == [
        "total_value",
        "daily_return",
    ]


def test_finalize_target_weight_pilot_evidence_cli_prints_missing_performance_diagnostics(
    monkeypatch,
    tmp_path,
    capsys,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    output_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "current_snapshot",
            "_portfolio_probe_reason": "test snapshot",
            "_portfolio_probe_current_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_at": date.isoformat(),
            "_portfolio_probe_trades_today": 0,
            "_portfolio_probe_trades_since_previous": 0,
            "cash": 1_000_000.0,
        },
    )
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = None
    record["daily_return"] = None
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            plan.candidate_id,
            "--finalize-pilot-evidence",
            "--finalize-date",
            plan.trade_day,
            "--output-dir",
            str(output_dir),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Target-weight pilot evidence finalize" in output
    assert "status: BLOCKED - target_weight_pilot_evidence_finalize_missing_performance" in output
    assert "source_record_fields:" in output
    assert "source_record_usable_fields:" in output
    assert "source_record_unusable_fields: none" in output
    assert "portfolio_metrics_checked: True" in output
    assert "portfolio_metrics_probe: current_snapshot" in output
    assert "portfolio_metrics_probe_reason: test snapshot" in output
    assert "portfolio_metrics_current_snapshot_found: True" in output
    assert "portfolio_metrics_fields: cash" in output
    assert "missing_performance_fields: total_value, daily_return" in output
    assert "artifact:" in output
    assert "report:" in output
    assert "next: WAIT for final portfolio performance evidence" in output


def test_diagnose_target_weight_portfolio_snapshot_reports_missing_history(
    monkeypatch,
    tmp_path,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "missing_snapshot_history",
            "_portfolio_probe_reason": "no portfolio snapshot exists for account_key",
            "_portfolio_probe_current_snapshot_found": False,
            "_portfolio_probe_previous_snapshot_found": False,
            "_portfolio_probe_trades_today": 0,
            "_portfolio_probe_trades_since_previous": 0,
        },
    )
    monkeypatch.setattr(
        twp,
        "_target_weight_snapshot_database_state",
        lambda **kwargs: {
            "checked": True,
            "account_key": plan.candidate_id,
            "snapshot_date": plan.trade_day,
            "snapshot_count": 0,
            "current_snapshot_found": False,
            "latest_snapshot_at": None,
            "trade_count_total": 0,
            "trade_count_on_date": 0,
            "position_count": 0,
        },
    )
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = 0
    record["daily_return"] = None
    execution = record["pilot_caps_snapshot"]["target_weight_execution"]
    execution.pop("db_persistence_complete")
    execution.pop("db_persistence_proof")
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    report = twp.diagnose_target_weight_portfolio_snapshot(
        candidate_id=plan.candidate_id,
        snapshot_date=plan.trade_day,
        output_dir=tmp_path / "paper_runtime",
    )

    assert report["status"] == "blocked_missing_snapshot_history"
    assert report["portfolio_metrics_probe"]["status"] == "missing_snapshot_history"
    assert report["missing_required_fields"] == ["total_value", "daily_return"]
    assert report["source_record_status"]["fields_unusable"] == ["total_value"]
    readiness = report["snapshot_recovery_readiness"]
    artifact_execution = report["artifact_execution_state"]
    assert artifact_execution["fill_count"] == len(plan.orders)
    assert artifact_execution["db_persistence_complete"] is False
    assert readiness["status"] == "blocked"
    assert readiness["safe_to_write_snapshot"] is False
    assert "portfolio_snapshot_history_missing" in readiness["blockers"]
    assert "db_execution_state_missing_for_account_key" in readiness["blockers"]
    assert "artifact_fills_without_current_db_trades" in readiness["blockers"]
    assert "source_record_db_persistence_incomplete" in readiness["blockers"]
    assert report["no_order_safety"]["portfolio_snapshot_written"] is False
    assert "--diagnose-portfolio-snapshot --snapshot-date 2026-04-10" in (
        report["operator_commands"]["diagnose_portfolio_snapshot"]
    )

    report_path = Path(report["artifact_path"])
    assert report_path.exists()
    report_md = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "Target-weight Portfolio Snapshot Diagnostics" in report_md
    assert "Probe status: `missing_snapshot_history`" in report_md
    assert "DB persistence complete: `False`" in report_md


def test_diagnose_target_weight_portfolio_snapshot_accepts_db_persistence_proof(
    monkeypatch,
    tmp_path,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "missing_current_snapshot_after_trades",
            "_portfolio_probe_reason": "current snapshot is missing after trades",
            "_portfolio_probe_current_snapshot_found": False,
            "_portfolio_probe_previous_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_at": "2026-04-09T15:35:00",
            "_portfolio_probe_trades_today": len(plan.orders),
            "_portfolio_probe_trades_since_previous": len(plan.orders),
        },
    )
    monkeypatch.setattr(
        twp,
        "_target_weight_snapshot_database_state",
        lambda **kwargs: {
            "checked": True,
            "account_key": plan.candidate_id,
            "snapshot_date": plan.trade_day,
            "snapshot_count": 1,
            "current_snapshot_found": False,
            "latest_snapshot_at": "2026-04-09T15:35:00",
            "trade_count_total": len(plan.orders),
            "trade_count_on_date": len(plan.orders),
            "position_count": len(plan.orders),
        },
    )
    record = _existing_pilot_evidence_record(plan)
    execution = record["pilot_caps_snapshot"]["target_weight_execution"]
    execution["db_persistence_complete"] = True
    execution["db_persistence_proof"] = {
        "checked": True,
        "complete": True,
        "reason": "target-weight paper execution is persisted in DB",
        "trade_history": {
            "source": "database.trade_history",
            "row_count": len(plan.orders),
            "expected_row_count": len(plan.orders),
            "execution_session_id": TEST_EXECUTION_SESSION_ID,
        },
        "positions": {
            "source": "database.positions",
            "expected_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "actual_quantities": {
                order.symbol: int(order.target_quantity)
                for order in plan.orders
            },
            "missing_or_mismatched_symbols": [],
        },
    }
    execution["fill_reconciliation"]["source"] = "database.trade_history"
    execution["position_reconciliation"]["source"] = "database.positions"
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = None
    record["daily_return"] = None
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    report = twp.diagnose_target_weight_portfolio_snapshot(
        candidate_id=plan.candidate_id,
        snapshot_date=plan.trade_day,
        output_dir=tmp_path / "paper_runtime",
    )

    artifact_execution = report["artifact_execution_state"]
    readiness = report["snapshot_recovery_readiness"]
    assert artifact_execution["db_persistence_checked"] is True
    assert artifact_execution["db_persistence_complete"] is True
    assert artifact_execution["db_trade_history_source"] == "database.trade_history"
    assert artifact_execution["db_positions_source"] == "database.positions"
    assert artifact_execution["db_trade_history_row_count"] == len(plan.orders)
    assert "source_record_db_persistence_incomplete" not in readiness["blockers"]
    assert "source_record_trade_history_not_database_backed" not in readiness["blockers"]
    assert "source_record_positions_not_database_backed" not in readiness["blockers"]
    assert readiness["authoritative_sources"]["artifact_db_persistence_complete"] is True


def test_diagnose_target_weight_portfolio_snapshot_cli_prints_blocker(
    monkeypatch,
    tmp_path,
    capsys,
):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    output_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(
        pe,
        "_probe_portfolio_metrics",
        lambda account_key, date: {
            "_portfolio_probe_status": "missing_current_snapshot_after_trades",
            "_portfolio_probe_reason": (
                "trades exist after previous snapshot but current snapshot is missing"
            ),
            "_portfolio_probe_current_snapshot_found": False,
            "_portfolio_probe_previous_snapshot_found": True,
            "_portfolio_probe_previous_snapshot_at": "2026-04-09T15:35:00",
            "_portfolio_probe_trades_today": 1,
            "_portfolio_probe_trades_since_previous": 1,
        },
    )
    monkeypatch.setattr(
        twp,
        "_target_weight_snapshot_database_state",
        lambda **kwargs: {
            "checked": True,
            "account_key": plan.candidate_id,
            "snapshot_date": plan.trade_day,
            "snapshot_count": 1,
            "current_snapshot_found": False,
            "latest_snapshot_at": "2026-04-09T15:35:00",
            "trade_count_total": 1,
            "trade_count_on_date": 1,
            "position_count": 4,
        },
    )
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = None
    record["daily_return"] = None
    execution = record["pilot_caps_snapshot"]["target_weight_execution"]
    execution.pop("db_persistence_complete")
    execution.pop("db_persistence_proof")
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--candidate-id",
            plan.candidate_id,
            "--diagnose-portfolio-snapshot",
            "--snapshot-date",
            plan.trade_day,
            "--output-dir",
            str(output_dir),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Target-weight portfolio snapshot diagnostics" in output
    assert "status: blocked_missing_current_snapshot_after_trades" in output
    assert "portfolio_metrics_probe: missing_current_snapshot_after_trades" in output
    assert "portfolio_metrics_previous_snapshot_at: 2026-04-09T15:35:00" in output
    assert "database_state: checked=True snapshots=1 current_snapshot=False" in output
    assert "artifact_execution_state: found=True complete=True fills=3" in output
    assert "db_persistence=False" in output
    assert "artifact_db_persistence: checked=False complete=False" in output
    assert "snapshot_recovery_readiness: blocked" in output
    assert "snapshot_safe_to_write: False" in output
    assert "current_portfolio_snapshot_missing_after_trades" in output
    assert "source_record_db_persistence_incomplete" in output
    assert "recovery_hint: run end-of-day portfolio snapshot capture" in output
    assert "artifact:" in output
    assert "report:" in output


def test_repair_target_weight_pilot_evidence_rejects_incomplete_execution(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    record = _repairable_invalid_pilot_evidence_record(plan)
    record["pilot_caps_snapshot"]["target_weight_execution"]["fill_reconciliation"]["complete"] = False
    pe._append_jsonl(pe._evidence_path(plan.candidate_id), record)

    with pytest.raises(ValueError, match="target_weight_fill_reconciliation_incomplete"):
        twp.repair_target_weight_pilot_evidence(
            candidate_id=plan.candidate_id,
            repair_date=plan.trade_day,
            output_dir=tmp_path / "paper_runtime",
        )

    records = pe._read_all_evidence(pe._evidence_path(plan.candidate_id))
    assert len(records) == 1


def test_verify_existing_pilot_evidence_rejects_plan_snapshot_mismatch(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    plan_snapshot = record["pilot_caps_snapshot"]["target_weight_plan"]
    plan_snapshot["target_quantities_after"] = {
        **plan_snapshot["target_quantities_after"],
        "AAA": 1,
    }
    plan_snapshot["gross_exposure_after"] = 1_000_000.0
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} >= {
        "target_weight_plan.target_quantities_after",
        "target_weight_plan.gross_exposure_after",
    }


def test_verify_existing_pilot_evidence_reports_corrupt_plan_quantities(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    record["pilot_caps_snapshot"]["target_weight_plan"]["target_quantities_after"]["AAA"] = "bad-quantity"
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_plan.target_quantities_after"
    }


def test_verify_existing_pilot_evidence_rejects_non_pilot_record(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    record["evidence_mode"] = "real_paper"
    record["session_mode"] = "normal_paper"
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} >= {
        "record.evidence_mode",
        "record.session_mode",
    }


def test_verify_existing_pilot_evidence_rejects_missing_pre_trade_risk(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["pre_trade_risk_complete"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.pre_trade_risk_complete"
    }


def test_verify_existing_pilot_evidence_rejects_missing_execution_trade_day_check(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["execution_trade_day_allowed"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.execution_trade_day_allowed"
    }


def test_verify_existing_pilot_evidence_rejects_missing_market_session_check(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["execution_market_session_allowed"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.execution_market_session_allowed"
    }


def test_verify_existing_pilot_evidence_rejects_missing_authorization_snapshot_check(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["pilot_authorization_snapshot_allowed"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.pilot_authorization_snapshot_allowed"
    }


def test_verify_existing_pilot_evidence_rejects_missing_preflight_refresh(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["preflight_refresh_complete"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.preflight_refresh_complete"
    }


def test_preview_plan_against_caps_flags_default_pilot_caps():
    from tools.target_weight_rotation_pilot import build_preview_caps, preview_plan_against_caps

    default_preview = preview_plan_against_caps(_adapter_plan())
    relaxed_preview = preview_plan_against_caps(
        _adapter_plan(),
        build_preview_caps(
            max_orders=3,
            max_positions=3,
            max_notional=1_300_000,
            max_exposure=3_300_000,
        ),
    )

    assert default_preview.allowed is False
    assert "remaining_orders" in default_preview.reason
    assert "max order notional" in default_preview.reason
    assert "target positions" in default_preview.reason
    assert "gross exposure" in default_preview.reason
    assert relaxed_preview.allowed is True


def test_pilot_plan_validation_blocks_remaining_exposure_shortfall():
    from core.target_weight_rotation import validate_plan_against_pilot

    plan = _adapter_plan()
    validation = validate_plan_against_pilot(
        plan,
        SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=3_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )

    assert validation.allowed is False
    assert "remaining_exposure" in validation.reason
    assert "required exposure increase" in validation.violations[0]


def test_pilot_plan_validation_uses_net_exposure_increase_for_existing_positions():
    from core.target_weight_rotation import validate_plan_against_pilot

    plan = replace(
        _adapter_plan(),
        market_value_before=3_000_000.0,
        gross_exposure_after=3_200_000.0,
    )
    validation = validate_plan_against_pilot(
        plan,
        SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=250_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )

    assert validation.allowed is True


def test_pilot_valid_to_counts_inclusive_krx_business_days(monkeypatch):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "_load_kr_market_holidays", lambda: {"2026-04-14"})

    assert twp._pilot_valid_to("2026-04-10", target_pilot_days=3) == "2026-04-15"


def test_pilot_valid_to_rejects_invalid_target_days():
    import tools.target_weight_rotation_pilot as twp

    with pytest.raises(ValueError, match="target_pilot_days"):
        twp._pilot_valid_to("2026-04-10", target_pilot_days=0)


def test_recommend_pilot_caps_matches_target_weight_plan(monkeypatch):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(
        twp,
        "_load_kr_market_holidays",
        lambda: {"2026-05-05", "2026-05-24", "2026-06-06"},
    )

    rec = twp.recommend_pilot_caps(_adapter_plan())
    minimum = rec["minimum_caps"]
    suggested = rec["suggested_caps"]

    assert minimum["max_orders_per_day"] == 3
    assert minimum["max_concurrent_positions"] == 3
    assert minimum["max_notional_per_trade"] == 1_200_000
    assert minimum["max_gross_exposure"] == 3_200_000
    assert suggested["max_orders_per_day"] == 3
    assert suggested["max_concurrent_positions"] == 3
    assert suggested["max_notional_per_trade"] == 1_260_000
    assert suggested["max_gross_exposure"] == 3_360_000
    assert rec["suggested_preview"]["allowed"] is True
    assert rec["valid_from"] == "2026-04-10"
    assert rec["valid_to"] == "2026-07-03"
    assert rec["target_pilot_days"] == 60
    assert "YYYY-MM-DD" not in rec["enable_command"]
    assert "\n" not in rec["enable_command"]
    assert "\\" not in rec["enable_command"]
    assert "--from 2026-04-10 --to 2026-07-03" in rec["enable_command"]
    assert "--max-orders 3 --max-positions 3" in rec["enable_command"]
    assert "--max-notional 1260000 --max-exposure 3360000" in rec["enable_command"]


def test_build_target_weight_experiment_manifest_freezes_pilot_flow():
    from tools.target_weight_rotation_pilot import (
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    audit = {
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": False,
        "next_action": "enable pilot with suggested caps, then rerun readiness audit",
        "blocking_reasons": ["pilot_authorization: no active capped pilot authorization"],
        "warning_reasons": ["preview_caps: current preview blocked"],
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=recommend_pilot_caps(plan),
        readiness_audit=audit,
        max_order_adv_pct=3.5,
    )

    assert manifest["artifact_type"] == "target_weight_paper_experiment_manifest"
    assert manifest["candidate_id"] == plan.candidate_id
    assert manifest["mode"] == "capped_paper_pilot"
    assert manifest["live_enabled"] is False
    assert manifest["target_pilot_days"] == 60
    assert len(manifest["manifest_hash"]) == 64
    assert manifest["candidate_snapshot"]["params_hash"] == plan.params_hash
    assert manifest["evidence_policy"]["required_provenance"]["evidence_mode"] == "pilot_paper"
    assert manifest["evidence_policy"]["target_weight_execution_required"]["fill_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["execution_trade_day_allowed"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["execution_market_session_allowed"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["pilot_authorization_snapshot_allowed"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["preflight_refresh_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["order_count_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["order_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["order_result_reconciliation_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["fill_reconciliation_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["db_persistence_complete"] is True
    assert manifest["evidence_policy"]["target_weight_execution_required"]["db_persistence_proof_complete"] is True
    assert (
        manifest["evidence_policy"]["target_weight_execution_required"]["db_trade_history_source"]
        == "database.trade_history"
    )
    assert "shadow_bootstrap" in manifest["evidence_policy"]["blocked_evidence"]
    assert "execution evidence without DB persistence proof" in manifest["evidence_policy"]["blocked_evidence"]
    assert manifest["risk_controls"]["liquidity_max_order_adv_pct"] == 3.5
    assert manifest["current_decision"]["ready_for_cap_approval"] is True
    assert "enable pilot" in manifest["current_decision"]["next_action"]
    assert manifest["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert "pilot_authorization" in manifest["operator_commands"]["execute_capped_paper"]
    assert manifest["no_order_safety"]["orders_submitted"] is False


def test_summarize_target_weight_evidence_progress_counts_verified_days(monkeypatch):
    from tools.target_weight_rotation_pilot import summarize_target_weight_evidence_progress

    strategy = "target_weight_candidate"
    params_hash = "hash"
    records = [
        {
            "date": "2026-04-08",
            "strategy": strategy,
            "execution_backed": False,
            "evidence_mode": "shadow_bootstrap",
            "session_mode": "shadow_bootstrap",
        },
        {
            "date": "2026-04-09",
            "strategy": strategy,
            "execution_backed": True,
            "evidence_mode": "pilot_paper",
            "session_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": {
                "target_weight_plan": {
                    "candidate_id": strategy,
                    "trade_day": "2026-04-09",
                    "params_hash": params_hash,
                },
                "target_weight_execution": {
                    "complete": True,
                    "params_hash": params_hash,
                    "execution_session_id": TEST_EXECUTION_SESSION_ID,
                    "execution_trade_day_allowed": True,
                    "execution_market_session_allowed": True,
                    "pilot_authorization_snapshot_allowed": True,
                    "preflight_refresh_complete": True,
                    "pre_execution_complete": True,
                    "liquidity_complete": True,
                    "pre_trade_risk_complete": True,
                    "order_count_complete": True,
                    "order_result_complete": True,
                    "order_complete": True,
                    "order_result_reconciliation": {"complete": True},
                    "fill_complete": True,
                    "fill_reconciliation": {
                        "complete": True,
                        "source": "database.trade_history",
                        "execution_session_id": TEST_EXECUTION_SESSION_ID,
                        "fills": [
                            {
                                "symbol": "005930",
                                "action": "BUY",
                                "quantity": 1,
                                "execution_session_id": TEST_EXECUTION_SESSION_ID,
                                "order_id": "ORD-TW-001",
                            }
                        ],
                    },
                    "position_reconciliation": {
                        "complete": True,
                        "source": "database.positions",
                    },
                    "db_persistence_complete": True,
                    "db_persistence_proof": {
                        "checked": True,
                        "complete": True,
                        "reason": "target-weight paper execution is persisted in DB",
                        "trade_history": {
                            "source": "database.trade_history",
                            "row_count": 1,
                            "expected_row_count": 1,
                            "execution_session_id": TEST_EXECUTION_SESSION_ID,
                            "trade_ids": [1],
                        },
                        "positions": {
                            "source": "database.positions",
                            "expected_quantities": {"005930": 1},
                            "actual_quantities": {"005930": 1},
                            "missing_or_mismatched_symbols": [],
                        },
                    },
                },
            },
            "total_value": 10_000_000.0,
            "daily_return": 0.1,
            "same_universe_excess": 0.05,
            "exposure_matched_excess": 0.04,
            "cash_adjusted_excess": 0.03,
            "benchmark_status": "final",
        },
        {
            "date": "2026-04-10",
            "strategy": strategy,
            "execution_backed": True,
            "evidence_mode": "pilot_paper",
            "session_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": {
                "target_weight_plan": {
                    "candidate_id": strategy,
                    "trade_day": "2026-04-10",
                    "params_hash": params_hash,
                },
            },
        },
    ]
    monkeypatch.setattr(
        "core.paper_evidence.get_canonical_records",
        lambda candidate_id: records if candidate_id == strategy else [],
    )

    progress = summarize_target_weight_evidence_progress(strategy)

    assert progress["verified_pilot_days"] == 1
    assert progress["remaining_pilot_days"] == 59
    assert progress["shadow_days"] == 1
    assert progress["invalid_execution_days"] == 1
    assert progress["invalid_reasons"]["missing_target_weight_execution"] == 1
    assert progress["latest_verified_pilot_date"] == "2026-04-09"


def test_summarize_target_weight_evidence_progress_rejects_failed_benchmark(monkeypatch):
    from tools.target_weight_rotation_pilot import summarize_target_weight_evidence_progress

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    record["benchmark_status"] = "failed"
    record["same_universe_excess"] = None
    record["exposure_matched_excess"] = None
    record["cash_adjusted_excess"] = None
    record["total_value"] = 0
    monkeypatch.setattr(
        "core.paper_evidence.get_canonical_records",
        lambda candidate_id: [record] if candidate_id == plan.candidate_id else [],
    )

    progress = summarize_target_weight_evidence_progress(plan.candidate_id)

    assert progress["verified_pilot_days"] == 0
    assert progress["remaining_pilot_days"] == 60
    assert progress["invalid_execution_days"] == 1
    assert progress["invalid_reasons"] == {"target_weight_benchmark_status_not_final": 1}
    assert progress["latest_record_date"] == plan.trade_day
    assert progress["latest_verified_pilot_date"] is None


def test_build_target_weight_daily_ops_summary_writes_operator_view(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
        write_target_weight_daily_ops_summary,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": False,
        "next_action": "enable pilot with suggested caps, then rerun readiness audit",
        "blocking_reasons": ["pilot_authorization: no active capped pilot authorization"],
        "warning_reasons": ["preview_caps: current preview blocked"],
        "operator_commands": {
            "collect_shadow_days": "python tools/target_weight_rotation_pilot.py --shadow-days 3",
            "rerun_readiness_audit": "python tools/target_weight_rotation_pilot.py --readiness-audit",
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute --collect-evidence",
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": {
            "checked": True,
            "complete": True,
            "reason": "target_weight_data_quality_passed",
            "trade_day": plan.trade_day,
            "score_day": plan.score_day,
            "symbols_checked": 3,
            "required_symbols": ["AAA", "BBB", "CCC"],
            "price_last_dates": {
                "AAA": plan.trade_day,
                "BBB": plan.trade_day,
                "CCC": plan.trade_day,
            },
            "missing_price_last_date_symbols": [],
            "stale_price_symbols": {},
            "missing_symbols": [],
            "missing_position_symbols": [],
            "benchmark_symbol": "KS11",
            "benchmark_last_date": plan.trade_day,
            "benchmark_stale": False,
            "violations": [],
            "warnings": [],
        },
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )
    json_path, md_path = write_target_weight_daily_ops_summary(summary, output_dir=tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    report = md_path.read_text(encoding="utf-8")

    assert summary["status"] == "READY_TO_ENABLE_CAPS"
    assert len(summary["summary_hash"]) == 64
    assert summary["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert "pilot_authorization" in summary["operator_commands"]["execute_capped_paper"]
    assert payload["evidence_progress"]["remaining_pilot_days"] == 48
    assert payload["no_order_safety"]["summary_only"] is True
    assert "Verified pilot days: 12/60" in report
    assert "READY_TO_ENABLE_CAPS" in report
    assert "# blocked: pilot_authorization" in report


def test_build_target_weight_daily_ops_summary_blocks_cap_ready_without_enable_command(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": False,
        "next_action": "enable pilot with suggested caps, then rerun readiness audit",
        "blocking_reasons": ["pilot_authorization: no active capped pilot authorization"],
        "warning_reasons": [],
        "operator_commands": {
            "enable_suggested_caps": "# blocked: stale cap approval command",
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert "cap 승인 명령이 차단 또는 누락됨" in summary["next_step"]
    assert summary["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: pilot_authorization"
    )
    assert any(
        "daily_ops_enable_command_unavailable" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_blocks_cap_command_for_wrong_candidate(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": False,
        "next_action": "enable pilot with suggested caps, then rerun readiness audit",
        "blocking_reasons": ["pilot_authorization: no active capped pilot authorization"],
        "warning_reasons": [],
        "operator_commands": {
            "enable_suggested_caps": (
                f"python tools/paper_pilot_control.py --strategy {plan.candidate_id}_shadow --enable"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert any(
        "daily_ops_enable_command_unavailable" in reason
        and "candidate_id mismatch" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_blocks_cap_command_without_enable_flag(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": False,
        "next_action": "enable pilot with suggested caps, then rerun readiness audit",
        "blocking_reasons": ["pilot_authorization: no active capped pilot authorization"],
        "warning_reasons": [],
        "operator_commands": {
            "enable_suggested_caps": (
                f"python tools/paper_pilot_control.py --strategy {plan.candidate_id}"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: daily_ops_enable_command_unavailable"
    )
    assert any(
        "daily_ops_enable_command_unavailable" in reason
        and "missing --enable" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_allows_execute_only_when_ready(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
        write_target_weight_daily_ops_summary,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    execute_command = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {plan.candidate_id} --as-of-date {plan.trade_day} "
        "--execute --collect-evidence"
    )
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    data_quality_check = {
        **pass_check,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "symbols_checked": 3,
        "required_symbols": ["AAA", "BBB", "CCC"],
        "price_last_dates": {
            "AAA": plan.trade_day,
            "BBB": plan.trade_day,
            "CCC": plan.trade_day,
        },
        "missing_price_last_date_symbols": [],
        "stale_price_symbols": {},
        "missing_symbols": [],
        "missing_position_symbols": [],
        "benchmark_symbol": "KS11",
        "benchmark_last_date": plan.trade_day,
        "benchmark_stale": False,
        "violations": [],
        "warnings": [],
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": True,
        "next_action": "execute capped paper pilot with --execute --collect-evidence",
        "blocking_reasons": [],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": True},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "collect_shadow_days": "python tools/target_weight_rotation_pilot.py --shadow-days 3",
            "rerun_readiness_audit": "python tools/target_weight_rotation_pilot.py --readiness-audit",
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": execute_command,
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": data_quality_check,
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )
    _json_path, md_path = write_target_weight_daily_ops_summary(summary, output_dir=tmp_path)
    report = md_path.read_text(encoding="utf-8")

    assert summary["status"] == "READY_TO_EXECUTE"
    assert summary["operator_commands"]["execute_capped_paper"] == execute_command
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: daily_ops_summary.status == READY_TO_EXECUTE"
    )
    assert manifest["operator_commands"]["execute_capped_paper"] == execute_command
    assert "Execute Capped Paper" in report
    assert "READY_TO_EXECUTE" in report


def test_build_target_weight_daily_ops_summary_blocks_execute_command_for_wrong_trade_day(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    data_quality_check = {
        **pass_check,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "symbols_checked": 3,
        "required_symbols": ["AAA", "BBB", "CCC"],
        "price_last_dates": {
            "AAA": plan.trade_day,
            "BBB": plan.trade_day,
            "CCC": plan.trade_day,
        },
        "missing_price_last_date_symbols": [],
        "stale_price_symbols": {},
        "missing_symbols": [],
        "missing_position_symbols": [],
        "benchmark_symbol": "KS11",
        "benchmark_last_date": plan.trade_day,
        "benchmark_stale": False,
        "violations": [],
        "warnings": [],
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": True,
        "next_action": "execute capped paper pilot with --execute --collect-evidence",
        "blocking_reasons": [],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": True},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {plan.candidate_id} --as-of-date 2026-04-09 "
                "--execute --collect-evidence"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": data_quality_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert summary["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: daily_ops_execute_command_unavailable"
    )
    assert any(
        "daily_ops_execute_command_unavailable" in reason
        and "as_of_date mismatch" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_blocks_execute_command_without_required_flags(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    data_quality_check = {
        **pass_check,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "symbols_checked": 3,
        "required_symbols": ["AAA", "BBB", "CCC"],
        "price_last_dates": {
            "AAA": plan.trade_day,
            "BBB": plan.trade_day,
            "CCC": plan.trade_day,
        },
        "missing_price_last_date_symbols": [],
        "stale_price_symbols": {},
        "missing_symbols": [],
        "missing_position_symbols": [],
        "benchmark_symbol": "KS11",
        "benchmark_last_date": plan.trade_day,
        "benchmark_stale": False,
        "violations": [],
        "warnings": [],
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": True,
        "next_action": "execute capped paper pilot with --execute --collect-evidence",
        "blocking_reasons": [],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": True},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {plan.candidate_id} --as-of-date {plan.trade_day} "
                "--readiness-audit"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": data_quality_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert summary["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: daily_ops_execute_command_unavailable"
    )
    assert any(
        "daily_ops_execute_command_unavailable" in reason
        and "missing --execute" in reason
        and "missing --collect-evidence" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_blocks_ready_audit_without_execute_command(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": True,
        "ready_for_capped_pilot": True,
        "next_action": "execute capped paper pilot with --execute --collect-evidence",
        "blocking_reasons": [],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": True},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "execute_capped_paper": "# blocked: stale readiness command",
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        "pre_trade_risk_check": {
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert summary["status"] == "BLOCKED"
    assert "실행 명령이 차단 또는 누락됨" in summary["next_step"]
    assert summary["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: daily_ops_execute_command_unavailable"
    )
    assert any(
        "daily_ops_execute_command_unavailable" in reason
        for reason in summary["decision"]["blocking_reasons"]
    )


def test_build_target_weight_daily_ops_summary_marks_today_recorded(tmp_path, monkeypatch):
    import tools.target_weight_rotation_pilot as twp

    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
        write_target_weight_daily_ops_summary,
    )

    monkeypatch.setattr(twp, "_load_kr_market_holidays", lambda: {"2026-04-13"})
    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    snapshot_mismatch = {
        "checked": True,
        "allowed": False,
        "complete": False,
        "reason": "target_weight_pilot_authorization_snapshot_mismatch: stale same-day approval",
        "mismatches": [{"field": "portfolio_drawdown_guard"}],
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": False,
        "ready_for_capped_pilot": False,
        "next_action": "resolve duplicate execution blocker",
        "blocking_reasons": [
            "execution_idempotency: target_weight_duplicate_execution_attempt",
            "pilot_authorization_snapshot: target_weight_pilot_authorization_snapshot_mismatch",
        ],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": False},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": snapshot_mismatch,
        "operator_commands": {
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute --collect-evidence",
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 1,
        "remaining_pilot_days": 59,
        "progress_ratio": 0.0167,
        "shadow_days": 2,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 3,
        "latest_record_date": plan.trade_day,
        "latest_verified_pilot_date": plan.trade_day,
        "latest_shadow_date": "2026-04-09",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )
    _json_path, md_path = write_target_weight_daily_ops_summary(summary, output_dir=tmp_path)
    report = md_path.read_text(encoding="utf-8")

    assert summary["status"] == "PILOT_EVIDENCE_RECORDED"
    assert summary["decision"]["blocking_reasons"] == []
    assert summary["decision"]["post_evidence_diagnostics"] == [
        "execution_idempotency: target_weight_duplicate_execution_attempt",
        "pilot_authorization_snapshot: target_weight_pilot_authorization_snapshot_mismatch",
    ]
    assert summary["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert "already recorded" in summary["operator_commands"]["execute_capped_paper"]
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: pilot_paper evidence already recorded"
    )
    assert "2026-04-14" in summary["operator_commands"]["enable_suggested_caps"]
    assert summary["operator_commands"]["finalize_pilot_evidence"].startswith(
        "# blocked: pilot_paper evidence already finalized"
    )
    assert summary["next_operator_trade_day"] == "2026-04-14"
    assert summary["not_before_date"] == "2026-04-14"
    assert summary["premature_run_guard"] == "target_weight_future_as_of_date_blocked"
    assert (
        summary["operator_commands"]["next_daily_ops_summary"]
        == (
            "python tools/target_weight_rotation_pilot.py "
            "--candidate-id target_weight_candidate --as-of-date 2026-04-14 "
            "--daily-ops-summary"
        )
    )
    assert (
        summary["operator_commands"]["next_readiness_audit"]
        == (
            "python tools/target_weight_rotation_pilot.py "
            "--candidate-id target_weight_candidate --as-of-date 2026-04-14 "
            "--readiness-audit"
        )
    )
    assert "fresh readiness" in summary["next_step"]
    assert "PILOT_EVIDENCE_RECORDED" in report
    assert "Not before date: `2026-04-14`" in report
    assert "Premature run guard: `target_weight_future_as_of_date_blocked`" in report
    assert "Next Daily Ops Summary" in report
    assert "--as-of-date 2026-04-14 --daily-ops-summary" in report
    assert "Post-evidence Diagnostics" in report


def test_build_target_weight_daily_ops_summary_marks_today_invalid_evidence():
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        render_target_weight_daily_ops_markdown,
    )

    plan = _adapter_plan()
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": False,
        "ready_for_capped_pilot": False,
        "next_action": "resolve duplicate execution blocker",
        "blocking_reasons": [
            "execution_idempotency: target_weight_duplicate_execution_attempt"
        ],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": False},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "enable_suggested_caps": (
                f"python tools/paper_pilot_control.py --strategy {plan.candidate_id} --enable"
            ),
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute --collect-evidence",
            "finalize_pilot_evidence": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {plan.candidate_id} --finalize-pilot-evidence --finalize-date {plan.trade_day}"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 0,
        "remaining_pilot_days": 60,
        "progress_ratio": 0.0,
        "shadow_days": 2,
        "invalid_execution_days": 1,
        "invalid_reasons": {"target_weight_benchmark_status_not_final": 1},
        "non_promotable_days": 0,
        "total_canonical_records": 3,
        "latest_record_date": plan.trade_day,
        "latest_verified_pilot_date": None,
        "latest_shadow_date": "2026-04-09",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest={"manifest_hash": "m" * 64},
        evidence_progress=progress,
    )
    report = render_target_weight_daily_ops_markdown(summary)

    assert summary["status"] == "PILOT_EVIDENCE_INVALID"
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: pilot_paper evidence invalid"
    )
    assert "evidence invalid" in summary["operator_commands"]["execute_capped_paper"]
    assert summary["operator_commands"]["finalize_pilot_evidence"].endswith(
        f"--finalize-pilot-evidence --finalize-date {plan.trade_day}"
    )
    assert summary["operator_commands"]["repair_pilot_evidence"].endswith(
        f"--repair-pilot-evidence --repair-date {plan.trade_day}"
    )
    assert "final benchmark/portfolio evidence" in summary["next_step"]
    assert "PILOT_EVIDENCE_INVALID" in report
    assert "Finalize Pilot Evidence" in report


def test_build_target_weight_daily_ops_summary_stops_repair_loop_after_repaired_evidence():
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        render_target_weight_daily_ops_markdown,
    )

    plan = _adapter_plan()
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": False,
        "ready_for_capped_pilot": False,
        "next_action": "resolve duplicate execution blocker",
        "blocking_reasons": [
            "pilot_validation: max_orders_per_day=4 reached (today=4)",
            "execution_idempotency: target_weight_duplicate_execution_attempt",
        ],
        "warning_reasons": [],
        "launch_readiness": {"launch_ready": False},
        "plan_validation": {"allowed": True},
        "execution_trade_day_check": {**pass_check, "execution_day": plan.trade_day},
        "execution_market_session_check": {**pass_check, "execution_time": "10:00:00"},
        "pilot_authorization_snapshot_check": pass_check,
        "operator_commands": {
            "daily_ops_summary": "python tools/target_weight_rotation_pilot.py --daily-ops-summary",
            "enable_suggested_caps": (
                f"python tools/paper_pilot_control.py --strategy {plan.candidate_id} --enable"
            ),
            "execute_capped_paper": "python tools/target_weight_rotation_pilot.py --execute --collect-evidence",
        },
        "plan_summary": {
            "order_count": 0,
            "target_position_count": 3,
            "max_order_notional": 0.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 0,
        "remaining_pilot_days": 60,
        "progress_ratio": 0.0,
        "shadow_days": 2,
        "invalid_execution_days": 1,
        "invalid_reasons": {"target_weight_repaired_performance_not_promotable": 1},
        "repaired_pilot_days": 1,
        "non_promotable_days": 0,
        "total_canonical_records": 4,
        "latest_record_date": plan.trade_day,
        "latest_verified_pilot_date": None,
        "latest_repaired_pilot_date": plan.trade_day,
        "latest_shadow_date": "2026-04-09",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest={"manifest_hash": "m" * 64},
        evidence_progress=progress,
    )
    report = render_target_weight_daily_ops_markdown(summary)

    assert summary["status"] == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"
    assert summary["next_operator_trade_day"] is not None
    assert summary["not_before_date"] == summary["next_operator_trade_day"]
    assert summary["premature_run_guard"] == "target_weight_future_as_of_date_blocked"
    assert "복구 보존" in summary["next_step"]
    assert summary["operator_commands"]["execute_capped_paper"].startswith(
        "# blocked: repaired pilot_paper evidence already recorded"
    )
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: repaired pilot_paper evidence already recorded"
    )
    assert summary["next_operator_trade_day"] in summary["operator_commands"]["enable_suggested_caps"]
    assert summary["operator_commands"]["finalize_pilot_evidence"].startswith(
        "# blocked: repaired pilot_paper evidence already appended"
    )
    assert summary["operator_commands"]["repair_pilot_evidence"].startswith(
        "# blocked: repaired pilot_paper evidence already appended"
    )
    assert summary["decision"]["blocking_reasons"] == []
    assert len(summary["decision"]["post_evidence_diagnostics"]) == 2
    assert f"Not before date: `{summary['next_operator_trade_day']}`" in report
    assert "Premature run guard: `target_weight_future_as_of_date_blocked`" in report


def test_build_target_weight_daily_ops_summary_prefers_finalize_for_missing_performance():
    from tools.target_weight_rotation_pilot import build_target_weight_daily_ops_summary

    plan = _adapter_plan()
    pass_check = {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "ok",
    }
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": False,
        "ready_for_capped_pilot": False,
        "blocking_reasons": ["execution_idempotency: duplicate execution"],
        "operator_commands": {
            "finalize_pilot_evidence": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {plan.candidate_id} "
                f"--finalize-pilot-evidence --finalize-date {plan.trade_day}"
            ),
            "repair_pilot_evidence": (
                "python tools/target_weight_rotation_pilot.py "
                f"--candidate-id {plan.candidate_id} "
                f"--repair-pilot-evidence --repair-date {plan.trade_day}"
            ),
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": pass_check,
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 0,
        "remaining_pilot_days": 60,
        "progress_ratio": 0.0,
        "shadow_days": 2,
        "invalid_execution_days": 1,
        "invalid_reasons": {"target_weight_daily_return_missing": 1},
        "non_promotable_days": 0,
        "total_canonical_records": 3,
        "latest_record_date": plan.trade_day,
        "latest_verified_pilot_date": None,
        "latest_shadow_date": "2026-04-09",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest={"manifest_hash": "m" * 64},
        evidence_progress=progress,
    )

    assert summary["status"] == "PILOT_EVIDENCE_INVALID"
    assert "final benchmark/portfolio evidence" in summary["next_step"]
    assert summary["operator_commands"]["finalize_pilot_evidence"].endswith(
        f"--finalize-pilot-evidence --finalize-date {plan.trade_day}"
    )
    assert summary["operator_commands"]["repair_pilot_evidence"].startswith(
        "# fallback: use only if finalize cannot produce promotable proof"
    )


def test_build_target_weight_daily_ops_summary_blocks_stale_execution_day(tmp_path):
    from tools.target_weight_rotation_pilot import (
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        recommend_pilot_caps,
        validate_execution_trade_day,
        write_target_weight_daily_ops_summary,
    )

    plan = _adapter_plan()
    cap_recommendation = recommend_pilot_caps(plan)
    execution_trade_day_check = validate_execution_trade_day(
        plan,
        now=datetime(2026, 4, 11, 9, 0),
    )
    audit = {
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": False,
        "ready_for_capped_pilot": False,
        "next_action": "rerun readiness audit with current market data before enabling or executing pilot",
        "blocking_reasons": [f"execution_trade_day: {execution_trade_day_check['reason']}"],
        "warning_reasons": [],
        "execution_trade_day_check": execution_trade_day_check,
        "operator_commands": {
            "collect_shadow_days": "python tools/target_weight_rotation_pilot.py --shadow-days 3",
            "rerun_readiness_audit": "python tools/target_weight_rotation_pilot.py --readiness-audit",
            "enable_suggested_caps": cap_recommendation["enable_command"],
            "execute_capped_paper": f"# blocked: {execution_trade_day_check['reason']}",
        },
        "plan_summary": {
            "order_count": 3,
            "target_position_count": 3,
            "max_order_notional": 1_200_000.0,
            "gross_exposure_after": 3_200_000.0,
        },
        "data_quality_check": {
            "checked": True,
            "complete": True,
            "reason": "target_weight_data_quality_passed",
            "trade_day": plan.trade_day,
            "score_day": plan.score_day,
            "symbols_checked": 3,
            "required_symbols": ["AAA", "BBB", "CCC"],
            "price_last_dates": {
                "AAA": plan.trade_day,
                "BBB": plan.trade_day,
                "CCC": plan.trade_day,
            },
            "missing_price_last_date_symbols": [],
            "stale_price_symbols": {},
            "missing_symbols": [],
            "missing_position_symbols": [],
            "benchmark_symbol": "KS11",
            "benchmark_last_date": plan.trade_day,
            "benchmark_stale": False,
            "violations": [],
            "warnings": [],
        },
        "liquidity_check": {"complete": True, "reason": "target_weight_liquidity_preflight_passed"},
        "pre_trade_risk_check": {"complete": True, "reason": "target_weight_pre_trade_risk_passed"},
    }
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }

    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )
    json_path, md_path = write_target_weight_daily_ops_summary(summary, output_dir=tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    report = md_path.read_text(encoding="utf-8")

    assert summary["status"] == "BLOCKED"
    assert summary["decision"]["execution_trade_day_check"]["allowed"] is False
    assert summary["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert summary["operator_commands"]["enable_suggested_caps"].startswith(
        "# blocked: daily_ops_summary.status == BLOCKED"
    )
    assert payload["risk_snapshot"]["execution_trade_day_allowed"] is False
    assert "READY_TO_EXECUTE" not in report
    assert "Status: `READY_TO_ENABLE_CAPS`" not in report
    assert "Execution day check: BLOCKED" in report
    assert "target_weight_execution_trade_day_mismatch" in report


def test_assess_plan_data_quality_blocks_stale_symbol_price():
    from tools.target_weight_rotation_pilot import assess_plan_data_quality

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["price_last_dates"] = {
        "AAA": "2026-04-10",
        "BBB": "2026-04-09",
        "CCC": "2026-04-10",
    }
    plan = replace(plan, diagnostics=diagnostics)

    quality = assess_plan_data_quality(plan)

    assert quality["checked"] is True
    assert quality["complete"] is False
    assert quality["stale_price_symbols"] == {"BBB": "2026-04-09"}
    assert "target_weight_data_quality_failed" in quality["reason"]


def test_assess_plan_data_quality_blocks_benchmark_before_trade_day():
    from tools.target_weight_rotation_pilot import assess_plan_data_quality

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["benchmark_last_date"] = plan.score_day
    plan = replace(plan, diagnostics=diagnostics)

    quality = assess_plan_data_quality(plan)

    assert quality["checked"] is True
    assert quality["complete"] is False
    assert quality["benchmark_stale"] is True
    assert any(
        "benchmark_latest_before_trade_day" in violation
        for violation in quality["violations"]
    )
    assert "target_weight_data_quality_failed" in quality["reason"]


def test_build_pilot_readiness_audit_blocks_data_quality_issue():
    from tools.target_weight_rotation_pilot import (
        build_pilot_readiness_audit,
        preview_plan_against_caps,
        recommend_pilot_caps,
        render_pilot_readiness_audit_markdown,
        validate_plan_against_pilot,
    )

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["price_last_dates"] = {
        "AAA": "2026-04-10",
        "BBB": "2026-04-09",
        "CCC": "2026-04-10",
    }
    plan = replace(plan, diagnostics=diagnostics)
    pilot_check = _pilot_check_for_plan(plan)
    validation = validate_plan_against_pilot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan)
    cap_recommendation = recommend_pilot_caps(plan)

    audit = build_pilot_readiness_audit(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        cap_recommendation=cap_recommendation,
        preflight_refresh={
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
        },
        launch_readiness={
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
        execution_idempotency={
            "checked": True,
            "allowed": True,
            "reason": "no completed session for this trade day",
        },
        execution_trade_day_check={
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight execution trade day matches current KST date",
        },
        execution_market_session_check={
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight execution market session is open",
        },
        pilot_authorization_snapshot_check={
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight pilot authorization snapshot matches current plan",
            "mismatches": [],
        },
        pre_execution_reconciliation={
            "checked": True,
            "complete": True,
            "reason": "starting positions match target-weight plan",
        },
        liquidity_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        pre_trade_risk_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
        trading_mode="paper",
    )
    report = render_pilot_readiness_audit_markdown(audit)

    assert audit["data_quality_check"]["complete"] is False
    assert audit["ready_for_cap_approval"] is False
    assert any("data_quality:" in reason for reason in audit["blocking_reasons"])
    assert "Data Quality" in report
    assert "stale symbol price data" in report


def test_readiness_and_daily_ops_mark_not_checked_authorization_snapshot():
    from tools.target_weight_rotation_pilot import (
        build_pilot_readiness_audit,
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        preview_plan_against_caps,
        recommend_pilot_caps,
        render_pilot_readiness_audit_markdown,
        render_target_weight_daily_ops_markdown,
        validate_pilot_authorization_snapshot,
        validate_plan_against_pilot,
    )

    plan = _adapter_plan()
    pilot_check = SimpleNamespace(
        allowed=False,
        reason="pilot entry blocked",
        remaining_orders=0,
        remaining_exposure=0,
        auth=None,
        caps_snapshot={},
    )
    validation = validate_plan_against_pilot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan)
    cap_recommendation = recommend_pilot_caps(plan)
    auth_snapshot_check = validate_pilot_authorization_snapshot(plan, pilot_check)

    audit = build_pilot_readiness_audit(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        cap_recommendation=cap_recommendation,
        preflight_refresh={
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
        },
        launch_readiness={
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": False,
            "infra_ready": True,
            "launch_ready": False,
            "blocking_requirements": ["pilot authorization missing"],
            "runtime_state": "normal",
        },
        execution_idempotency={
            "checked": True,
            "allowed": True,
            "reason": "no completed session for this trade day",
        },
        execution_trade_day_check={
            "checked": True,
            "allowed": True,
            "complete": True,
            "reason": "target-weight execution trade day matches current KST date",
        },
        execution_market_session_check={
            "checked": False,
            "allowed": True,
            "complete": True,
            "reason": "execution market session check not required",
        },
        pilot_authorization_snapshot_check=auth_snapshot_check,
        pre_execution_reconciliation={
            "checked": True,
            "complete": True,
            "reason": "starting positions match target-weight plan",
        },
        liquidity_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        pre_trade_risk_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
        trading_mode="paper",
    )
    readiness_report = render_pilot_readiness_audit_markdown(audit)
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 0,
        "remaining_pilot_days": 60,
        "progress_ratio": 0.0,
        "shadow_days": 0,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 0,
        "latest_record_date": None,
        "latest_verified_pilot_date": None,
        "latest_shadow_date": None,
        "ready_for_promotion_day_count": False,
    }
    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )
    daily_report = render_target_weight_daily_ops_markdown(summary)

    assert auth_snapshot_check["checked"] is False
    assert "Pilot auth snapshot: NOT CHECKED" in readiness_report
    assert "Pilot auth snapshot: PASS" not in readiness_report
    assert "Market session check: NOT CHECKED" in readiness_report
    assert "Pilot auth snapshot: NOT CHECKED" in daily_report
    assert "Pilot auth snapshot: PASS" not in daily_report
    assert summary["risk_snapshot"]["pilot_authorization_snapshot_checked"] is False
    assert summary["risk_snapshot"]["pilot_authorization_snapshot_status"] == "NOT CHECKED"
    assert summary["risk_snapshot"]["execution_market_session_status"] == "NOT CHECKED"


def test_unchecked_execution_gates_do_not_mark_capped_pilot_ready():
    from tools.target_weight_rotation_pilot import (
        build_pilot_readiness_audit,
        build_target_weight_daily_ops_summary,
        build_target_weight_experiment_manifest,
        preview_plan_against_caps,
        recommend_pilot_caps,
        render_pilot_readiness_audit_markdown,
        validate_plan_against_pilot,
    )

    plan = _adapter_plan()
    pilot_check = _pilot_check_for_plan(plan)
    validation = validate_plan_against_pilot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan)
    cap_recommendation = recommend_pilot_caps(plan)

    audit = build_pilot_readiness_audit(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        cap_recommendation=cap_recommendation,
        preflight_refresh={
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
        },
        launch_readiness={
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
        execution_idempotency={
            "checked": True,
            "allowed": True,
            "reason": "no completed session for this trade day",
        },
        execution_trade_day_check={
            "checked": False,
            "allowed": True,
            "complete": True,
            "reason": "execution trade day check not required",
        },
        execution_market_session_check={
            "checked": False,
            "allowed": True,
            "complete": True,
            "reason": "execution market session check not required",
        },
        pilot_authorization_snapshot_check={
            "checked": False,
            "allowed": True,
            "complete": True,
            "reason": "pilot authorization snapshot check not required",
            "mismatches": [],
        },
        pre_execution_reconciliation={
            "checked": True,
            "complete": True,
            "reason": "starting positions match target-weight plan",
        },
        liquidity_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_liquidity_preflight_passed",
        },
        pre_trade_risk_check={
            "checked": True,
            "complete": True,
            "reason": "target_weight_pre_trade_risk_passed",
        },
        trading_mode="paper",
    )
    readiness_report = render_pilot_readiness_audit_markdown(audit)
    manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
    )
    progress = {
        "candidate_id": plan.candidate_id,
        "target_days": 60,
        "verified_pilot_days": 12,
        "remaining_pilot_days": 48,
        "progress_ratio": 0.2,
        "shadow_days": 3,
        "invalid_execution_days": 0,
        "invalid_reasons": {},
        "non_promotable_days": 0,
        "total_canonical_records": 15,
        "latest_record_date": "2026-04-10",
        "latest_verified_pilot_date": "2026-04-10",
        "latest_shadow_date": "2026-04-03",
        "ready_for_promotion_day_count": False,
    }
    summary = build_target_weight_daily_ops_summary(
        audit=audit,
        experiment_manifest=manifest,
        evidence_progress=progress,
    )

    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert audit["next_action"] == "enable pilot with suggested caps, then rerun readiness audit"
    assert "Status: **CAP_APPROVAL_READY**" in readiness_report
    assert "Status: **READY**" not in readiness_report
    assert summary["status"] == "READY_TO_ENABLE_CAPS"
    assert summary["risk_snapshot"]["execution_trade_day_status"] == "NOT CHECKED"
    assert summary["risk_snapshot"]["execution_market_session_status"] == "NOT CHECKED"
    assert summary["risk_snapshot"]["pilot_authorization_snapshot_status"] == "NOT CHECKED"


def test_assess_plan_liquidity_blocks_large_adv_order():
    from tools.target_weight_rotation_pilot import assess_plan_liquidity

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            "AAA": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            },
            "BBB": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 100_000_000.0,
                "last_daily_value": 100_000_000.0,
            },
            "CCC": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 100_000_000.0,
                "last_daily_value": 100_000_000.0,
            },
        },
    }
    plan = replace(plan, diagnostics=diagnostics)

    liquidity = assess_plan_liquidity(plan, max_order_adv_pct=5.0)

    assert liquidity["complete"] is False
    assert "target_weight_liquidity_preflight_failed" in liquidity["reason"]
    assert liquidity["orders"][0]["symbol"] == "AAA"
    assert liquidity["orders"][0]["order_adv_pct"] == 11.0
    assert "AAA" in liquidity["violations"][0]


def test_assess_plan_liquidity_blocks_missing_diagnostics():
    from tools.target_weight_rotation_pilot import assess_plan_liquidity

    plan = replace(_adapter_plan(), diagnostics={"missing_symbols": []})

    liquidity = assess_plan_liquidity(plan)

    assert liquidity["checked"] is True
    assert liquidity["complete"] is False
    assert "target_weight_liquidity_preflight_failed" in liquidity["reason"]
    assert "missing liquidity diagnostics" in liquidity["violations"]


def test_assess_plan_pre_trade_risk_passes_when_cash_covers_costed_orders():
    from tools.target_weight_rotation_pilot import assess_plan_pre_trade_risk

    plan = _adapter_plan()
    risk = assess_plan_pre_trade_risk(
        plan,
        risk_manager=SimpleCostRiskManager(
            commission_rate=0.001,
            slippage_per_share=1.0,
        ),
    )

    assert risk["complete"] is True
    assert risk["reason"] == "target_weight_pre_trade_risk_passed"
    assert risk["projected_cash_after_costs"] < plan.cash_after_estimate
    assert risk["cost_summary"]["commission"] > 0
    assert risk["cost_summary"]["slippage"] > 0
    assert risk["order_costs"][0]["avg_daily_volume"] == 1_000_000.0


def test_assess_plan_pre_trade_risk_blocks_cash_shortfall_after_costs():
    from tools.target_weight_rotation_pilot import assess_plan_pre_trade_risk

    plan = replace(_adapter_plan(), cash_before=3_200_000.0)
    risk = assess_plan_pre_trade_risk(
        plan,
        risk_manager=SimpleCostRiskManager(slippage_per_share=1.0),
    )

    assert risk["complete"] is False
    assert "target_weight_pre_trade_risk_failed" in risk["reason"]
    assert "required cash" in risk["violations"][0]
    assert risk["projected_cash_after_costs"] < 0


def test_assess_plan_pre_trade_risk_uses_execution_price_for_position_limits():
    from tools.target_weight_rotation_pilot import assess_plan_pre_trade_risk

    plan = _adapter_plan()
    risk = assess_plan_pre_trade_risk(
        plan,
        risk_manager=SimpleCostRiskManager(
            slippage_per_share=100.0,
            max_position_ratio=0.20,
            max_investment_ratio=1.0,
            min_cash_ratio=0.0,
        ),
    )

    aaa_ratio = next(row for row in risk["position_ratios"] if row["symbol"] == "AAA")

    assert risk["complete"] is False
    assert risk["projected_position_prices"]["AAA"] == 200.0
    assert risk["projected_gross_exposure_after_costs"] == 5_150_000.0
    assert aaa_ratio["valuation_price"] == 200.0
    assert aaa_ratio["value"] == 2_200_000.0
    assert any("AAA: projected position ratio" in item for item in risk["violations"])


def test_execute_plan_blocks_liquidity_before_order_submission(monkeypatch):
    from tools.target_weight_rotation_pilot import assess_plan_liquidity, execute_plan

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("liquidity failure must not submit orders"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **_execute_plan_submit_guards_ok(plan),
        execution_idempotency={"allowed": True},
        pre_execution_reconciliation={"complete": True},
        liquidity_check=assess_plan_liquidity(plan, max_order_adv_pct=5.0),
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert execution["details"][0]["status"] == "skipped_liquidity_preflight"
    assert "target_weight_liquidity_preflight_failed" in execution["halt_reason"]


def test_execute_plan_blocks_pre_trade_risk_before_order_submission(monkeypatch):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    risk = {
        "checked": True,
        "complete": False,
        "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
        "violations": ["cash shortfall"],
        "warnings": [],
        "cost_summary": {},
        "order_costs": [],
    }
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("pre-trade risk failure must not submit orders"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        **_execute_plan_submit_guards_ok(plan),
        execution_idempotency={"allowed": True},
        pre_execution_reconciliation={"complete": True},
        liquidity_check={"checked": True, "complete": True, "reason": "ok"},
        pre_trade_risk_check=risk,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert execution["details"][0]["status"] == "skipped_pre_trade_risk"
    assert "target_weight_pre_trade_risk_failed" in execution["halt_reason"]


def test_reconcile_plan_positions_uses_full_expected_book():
    from tools.target_weight_rotation_pilot import reconcile_plan_positions

    plan = replace(
        _adapter_plan(),
        target_quantities_after={
            "AAA": 11_000,
            "BBB": 4_500,
            "CCC": 4_000,
            "DDD": 12,
        },
    )
    positions = {
        order.symbol: SimpleNamespace(quantity=order.target_quantity)
        for order in plan.orders
    }

    reconciliation = reconcile_plan_positions(plan, positions)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == [
        {"symbol": "DDD", "target_quantity": 12, "actual_quantity": 0}
    ]
    assert "DDD actual=0 target=12" in reconciliation["reason"]


def test_reconcile_plan_positions_blocks_unexpected_positive_positions():
    from tools.target_weight_rotation_pilot import reconcile_plan_positions

    plan = _adapter_plan()
    positions = {
        order.symbol: SimpleNamespace(quantity=order.target_quantity)
        for order in plan.orders
    }
    positions["ZZZ"] = SimpleNamespace(quantity=3)

    reconciliation = reconcile_plan_positions(plan, positions)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == []
    assert reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert "unexpected: ZZZ actual=3" in reconciliation["reason"]


def test_reconcile_plan_starting_positions_blocks_stale_plan_inputs():
    from tools.target_weight_rotation_pilot import reconcile_plan_starting_positions

    plan = replace(
        _adapter_plan(),
        position_quantities_before={"AAA": 7},
    )

    reconciliation = reconcile_plan_starting_positions(
        plan,
        {
            "AAA": SimpleNamespace(quantity=9),
            "ZZZ": SimpleNamespace(quantity=3),
        },
    )

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == [
        {"symbol": "AAA", "expected_quantity": 7, "actual_quantity": 9}
    ]
    assert reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert "target_weight_pre_execution_position_drift" in reconciliation["reason"]


def test_resolve_shadow_batch_range_supports_auto_days():
    from tools.target_weight_rotation_pilot import resolve_shadow_batch_range

    start, end, dates = resolve_shadow_batch_range(
        shadow_days=3,
        shadow_end_date="2026-04-13",
    )

    assert start == "2026-04-09"
    assert end == "2026-04-13"
    assert dates == ["2026-04-09", "2026-04-10", "2026-04-13"]

    explicit_start, explicit_end, explicit_dates = resolve_shadow_batch_range(
        shadow_start_date="2026-04-08",
        shadow_end_date="2026-04-10",
    )
    assert explicit_start == "2026-04-08"
    assert explicit_end == "2026-04-10"
    assert explicit_dates == ["2026-04-08", "2026-04-09", "2026-04-10"]

    with pytest.raises(ValueError, match="cannot be combined"):
        resolve_shadow_batch_range(
            shadow_start_date="2026-04-08",
            shadow_end_date="2026-04-10",
            shadow_days=3,
        )
    with pytest.raises(ValueError, match="must be provided together"):
        resolve_shadow_batch_range(shadow_end_date="2026-04-10")
    with pytest.raises(ValueError, match="must be positive"):
        resolve_shadow_batch_range(shadow_days=0, shadow_end_date="2026-04-10")


def test_record_shadow_evidence_for_plan_is_non_promotable(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import record_shadow_evidence_for_plan

    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    plan = _adapter_plan()

    ev = record_shadow_evidence_for_plan(
        plan,
        validation=SimpleNamespace(allowed=False, reason="no active pilot authorization"),
    )
    records = pe.get_canonical_records(plan.candidate_id)

    assert ev is not None
    assert ev.execution_backed is False
    assert ev.evidence_mode == "shadow_bootstrap"
    assert ev.benchmark_status == "final"
    assert ev.same_universe_excess is None
    assert len(records) == 1
    assert records[0]["benchmark_meta"]["source"] == "target_weight_shadow_plan"
    assert records[0]["diagnostics"][0]["dry_run_only"] is True
    assert records[0]["diagnostics"][0]["pilot_validation_reason"] == "no active pilot authorization"


def test_run_pilot_shadow_generates_readiness_and_runbook(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())

    result = twp.run_pilot(
        record_shadow_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    readiness = result["launch_artifacts"]["launch_readiness"]

    assert result["shadow_evidence"] is not None
    assert result["launch_artifacts"]["attempted"] is True
    assert Path(readiness["json_path"]).exists()
    assert Path(readiness["md_path"]).exists()
    runbook_path = Path(result["launch_artifacts"]["runbook_path"])
    assert runbook_path.exists()
    assert readiness["launch_ready"] is False
    assert readiness["shadow_days"] >= 1
    assert payload["cap_recommendation"]["suggested_caps"]["max_orders_per_day"] == 3
    assert payload["cap_recommendation"]["suggested_caps"]["max_concurrent_positions"] == 3
    assert payload["cap_recommendation"]["suggested_preview"]["allowed"] is True
    assert payload["launch_artifacts"]["attempted"] is True
    assert payload["launch_artifacts"]["launch_readiness"]["clean_final_days_current"] == 1
    assert "clean_final_days" in payload["launch_artifacts"]["launch_readiness"]["blocking_requirements"][0]
    runbook_text = runbook_path.read_text(encoding="utf-8")
    assert "## Target-weight Cap Recommendation" in runbook_text
    assert "Liquidity preflight:" in runbook_text
    assert "Pre-trade risk validation:" in runbook_text
    assert "--max-orders 3 --max-positions 3" in runbook_text
    assert "--max-notional 1260000 --max-exposure 3360000" in runbook_text


def test_run_pilot_readiness_audit_writes_no_order_artifact(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=False,
            reason="no active pilot authorization",
            remaining_orders=None,
            remaining_exposure=None,
            caps_snapshot=None,
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": False,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": False,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    report_text = result["report_path"].read_text(encoding="utf-8")
    manifest = json.loads(result["experiment_manifest_path"].read_text(encoding="utf-8"))

    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert audit["next_action"] == "enable pilot with suggested caps, then rerun readiness audit"
    assert "--readiness-audit" in audit["operator_commands"]["rerun_readiness_audit"]
    assert "--max-orders 3 --max-positions 3" in audit["operator_commands"]["enable_suggested_caps"]
    assert audit["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert "pilot authorization is not active" in audit["operator_commands"]["execute_capped_paper"]
    assert any("pilot_authorization" in reason for reason in audit["blocking_reasons"])
    assert any("pilot_validation" in reason for reason in audit["blocking_reasons"])
    assert audit["execution_idempotency"]["allowed"] is True
    assert audit["pre_execution_reconciliation"]["complete"] is True
    assert audit["liquidity_check"]["complete"] is True
    assert audit["pre_trade_risk_check"]["complete"] is True
    assert audit["cap_recommendation"]["suggested_caps"]["max_orders_per_day"] == 3
    assert payload["artifact_type"] == "target_weight_rotation_pilot_readiness_audit"
    assert payload["no_order_safety"]["orders_submitted"] is False
    assert payload["no_order_safety"]["shadow_evidence_recorded"] is False
    assert payload["no_order_safety"]["pilot_evidence_recorded"] is False
    assert result["report_path"].exists()
    assert "# Target-weight Pilot Readiness Audit" in report_text
    assert "CAP_APPROVAL_READY" in report_text
    assert "## Liquidity Preflight" in report_text
    assert "## Pre-trade Risk" in report_text
    assert "## Operator Commands" in report_text
    assert "--max-notional 1260000 --max-exposure 3360000" in report_text
    assert result["experiment_manifest_path"].exists()
    assert manifest["artifact_type"] == "target_weight_paper_experiment_manifest"
    assert manifest["candidate_id"] == plan.candidate_id
    assert manifest["current_decision"]["ready_for_cap_approval"] is True
    assert manifest["current_decision"]["ready_for_capped_pilot"] is False
    assert manifest["risk_controls"]["pilot_caps"]["max_orders_per_day"] == 3
    assert manifest["evidence_policy"]["pilot_paper_days_required"] == 60
    assert manifest["no_order_safety"]["manifest_only"] is True


def test_run_pilot_readiness_audit_blocks_cash_override(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_cash_override_blocked"):
        twp.run_pilot_readiness_audit(cash=1_000_000.0, output_dir=tmp_path)


def test_run_pilot_readiness_audit_blocks_future_as_of_before_plan(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_future_as_of_date_blocked"):
        twp.run_pilot_readiness_audit(
            as_of_date="2026-04-11",
            output_dir=tmp_path,
            execution_now=datetime(2026, 4, 10, 9, 0),
        )


def test_run_pilot_readiness_audit_blocks_requested_as_of_when_trade_day_stale(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    stale_plan = replace(_adapter_plan(), as_of_date="2026-04-11", trade_day="2026-04-10")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: stale_plan)
    monkeypatch.setattr(
        twp,
        "refresh_paper_preflight_status",
        lambda *args, **kwargs: pytest.fail("preflight should not run for stale requested trade day"),
    )

    with pytest.raises(ValueError, match="target_weight_requested_trade_day_unavailable"):
        twp.run_pilot_readiness_audit(
            as_of_date="2026-04-11",
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
            execution_now=datetime(2026, 4, 11, 9, 0),
        )

    assert list(tmp_path.glob("target_weight_pilot_readiness_audit_*.json")) == []


def test_run_daily_ops_summary_blocks_cash_override(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(
        twp,
        "run_pilot_readiness_audit",
        lambda **kwargs: pytest.fail("readiness audit should not run"),
    )

    with pytest.raises(ValueError, match="target_weight_cash_override_blocked"):
        twp.run_daily_ops_summary(cash=1_000_000.0, output_dir=tmp_path)


def test_run_daily_ops_summary_blocks_future_as_of_before_audit(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_future_as_of_date_blocked"):
        twp.run_daily_ops_summary(
            as_of_date="2026-04-11",
            output_dir=tmp_path,
            execution_now=datetime(2026, 4, 10, 9, 0),
        )


def test_run_daily_ops_summary_blocks_requested_as_of_when_trade_day_stale(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    stale_plan = replace(_adapter_plan(), as_of_date="2026-04-11", trade_day="2026-04-10")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: stale_plan)

    with pytest.raises(ValueError, match="target_weight_requested_trade_day_unavailable"):
        twp.run_daily_ops_summary(
            as_of_date="2026-04-11",
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
            execution_now=datetime(2026, 4, 11, 9, 0),
        )

    assert list(tmp_path.glob("target_weight_daily_ops_summary_*.json")) == []


@pytest.mark.parametrize("kwargs", [{}, {"execute": True}, {"record_shadow_evidence": True}])
def test_run_pilot_blocks_future_as_of_before_plan(monkeypatch, tmp_path, kwargs):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_future_as_of_date_blocked"):
        twp.run_pilot(
            as_of_date="2026-04-11",
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
            execution_now=datetime(2026, 4, 10, 9, 0),
            **kwargs,
        )


def test_run_shadow_bootstrap_blocks_future_end_date_before_plan(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_future_as_of_date_blocked"):
        twp.run_shadow_bootstrap(
            start_date="2026-04-10",
            end_date="2026-04-11",
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
            execution_now=datetime(2026, 4, 10, 9, 0),
        )


@pytest.mark.parametrize("kwargs", [{"execute": True}, {"collect_evidence": True}])
def test_run_pilot_blocks_cash_override_for_operational_paths(monkeypatch, tmp_path, kwargs):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_cash_override_blocked"):
        twp.run_pilot(
            cash=1_000_000.0,
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
            **kwargs,
        )


def test_run_pilot_blocks_collect_evidence_without_execute(monkeypatch, tmp_path):
    import tools.target_weight_rotation_pilot as twp

    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: pytest.fail("build_plan should not run"))

    with pytest.raises(ValueError, match="target_weight_collect_evidence_requires_execute"):
        twp.run_pilot(
            collect_evidence=True,
            output_dir=tmp_path,
            config=SimpleNamespace(trading={"mode": "paper"}),
        )


def test_run_pilot_readiness_audit_refreshes_preflight_before_gate_checks(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    refresh_calls = []
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})

    def fake_refresh(strategy, date):
        refresh_calls.append((strategy, date))
        return {
            "checked": True,
            "complete": True,
            "reason": "paper preflight refreshed",
            "strategy": strategy,
            "date": date,
            "overall": "warn",
            "entry_allowed": True,
            "runtime_state": "blocked_insufficient_evidence",
            "notifier_health": "configured",
            "pilot_authorized": True,
        }

    monkeypatch.setattr(twp, "refresh_paper_preflight_status", fake_refresh)
    monkeypatch.setattr(pp, "check_pilot_entry", lambda *args, **kwargs: _pilot_check_for_plan(plan))
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": True,
            "strategy_eligible": True,
            "runtime_state": "blocked_insufficient_evidence",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert refresh_calls == [(plan.candidate_id, plan.trade_day)]
    assert result["audit"]["preflight_refresh"]["notifier_health"] == "configured"
    assert result["audit"]["preflight_refresh"]["pilot_authorized"] is True


def test_run_pilot_readiness_audit_blocks_failed_preflight_refresh(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "refresh_paper_preflight_status",
        lambda *args, **kwargs: {
            "checked": True,
            "complete": False,
            "reason": "paper preflight failed: DB error",
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "overall": "fail",
            "entry_allowed": False,
            "runtime_state": "normal",
            "notifier_health": "configured",
            "pilot_authorized": True,
        },
    )
    monkeypatch.setattr(pp, "check_pilot_entry", lambda *args, **kwargs: _pilot_check_for_plan(plan))
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": True,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")
    manifest = json.loads(result["experiment_manifest_path"].read_text(encoding="utf-8"))

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["preflight_refresh"]["complete"] is False
    assert any("preflight_refresh" in reason for reason in audit["blocking_reasons"])
    assert "paper preflight failed: DB error" in report_text
    assert manifest["current_decision"]["ready_for_cap_approval"] is False


def test_run_pilot_blocks_order_submission_when_preflight_refresh_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    saved_sessions = []
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        twp,
        "refresh_paper_preflight_status",
        lambda *args, **kwargs: {
            "checked": True,
            "complete": False,
            "reason": "paper preflight failed: DB error",
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "overall": "fail",
            "entry_allowed": False,
        },
    )
    monkeypatch.setattr(pp, "check_pilot_entry", lambda *args, **kwargs: _pilot_check_for_plan(plan))
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("failed preflight must block before order submission"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("failed preflight must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["preflight_refresh"]["complete"] is False
    assert result["execution"]["halted"] is True
    assert result["execution"]["halt_reason"] == "paper preflight failed: DB error"
    assert result["execution_evidence"]["complete"] is False
    assert result["execution_evidence"]["preflight_refresh_complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert result["evidence_collection"]["reason"] == "paper preflight failed: DB error"
    assert saved_sessions == []
    assert payload["preflight_refresh"]["complete"] is False
    assert payload["execution"]["details"][0]["status"] == "skipped_preflight_refresh"


def test_run_pilot_readiness_audit_blocks_stale_execution_day(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": True,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=datetime(2026, 4, 11, 9, 0),
    )
    audit = result["audit"]
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    report_text = result["report_path"].read_text(encoding="utf-8")
    manifest = json.loads(result["experiment_manifest_path"].read_text(encoding="utf-8"))

    assert audit["execution_trade_day_check"]["allowed"] is False
    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert any("target_weight_execution_trade_day_mismatch" in reason for reason in audit["blocking_reasons"])
    assert audit["next_action"] == "rerun readiness audit with current market data before enabling or executing pilot"
    assert audit["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert payload["execution_trade_day_check"]["execution_day"] == "2026-04-11"
    assert "BLOCKED" in report_text
    assert "Execution day check: BLOCKED" in report_text
    assert "target_weight_execution_trade_day_mismatch" in report_text
    assert manifest["current_decision"]["ready_for_cap_approval"] is False
    assert manifest["operator_commands"]["execute_capped_paper"].startswith("# blocked:")


def test_run_pilot_readiness_audit_blocks_after_close_execution(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(pp, "check_pilot_entry", lambda *args, **kwargs: _pilot_check_for_plan(plan))
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": True,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=datetime(2026, 4, 10, 16, 0),
    )
    audit = result["audit"]
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    report_text = result["report_path"].read_text(encoding="utf-8")
    manifest = json.loads(result["experiment_manifest_path"].read_text(encoding="utf-8"))

    assert audit["execution_trade_day_check"]["allowed"] is True
    assert audit["execution_market_session_check"]["allowed"] is False
    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert any("target_weight_execution_market_session_closed" in reason for reason in audit["blocking_reasons"])
    assert audit["next_action"] == "wait for KRX regular session, then rerun readiness audit before executing pilot"
    assert audit["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert payload["execution_market_session_check"]["execution_time"] == "16:00:00"
    assert "Market session check: BLOCKED" in report_text
    assert "target_weight_execution_market_session_closed" in report_text
    assert manifest["current_decision"]["ready_for_cap_approval"] is True
    assert manifest["current_decision"]["ready_for_capped_pilot"] is False
    assert manifest["operator_commands"]["execute_capped_paper"].startswith("# blocked:")


def test_run_pilot_readiness_audit_blocks_authorization_snapshot_mismatch(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_snapshot = twp.build_pilot_authorization_snapshot(
        replace(plan, trade_day="2026-04-09", as_of_date="2026-04-09")
    )
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": True,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["pilot_authorization_snapshot_check"]["allowed"] is False
    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert any("pilot_authorization_snapshot" in reason for reason in audit["blocking_reasons"])
    assert audit["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert payload["pilot_authorization_snapshot_check"]["allowed"] is False
    assert "Pilot auth snapshot: BLOCKED" in report_text


def test_run_pilot_readiness_audit_blocks_liquidity_preflight(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["liquidity_check"]["complete"] is False
    assert any("liquidity_preflight" in reason for reason in audit["blocking_reasons"])
    assert "BLOCKED" in report_text
    assert "target_weight_liquidity_preflight_failed" in report_text


def test_run_pilot_readiness_audit_blocks_missing_liquidity_diagnostics(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = replace(_adapter_plan(), diagnostics={"missing_symbols": []})
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["liquidity_check"]["complete"] is False
    assert any("missing liquidity diagnostics" in reason for reason in audit["blocking_reasons"])
    assert "missing liquidity diagnostics" in report_text


def test_run_pilot_readiness_audit_blocks_pre_trade_risk(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "assess_plan_pre_trade_risk",
        lambda *args, **kwargs: {
            "checked": True,
            "complete": False,
            "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
            "violations": ["cash shortfall"],
            "warnings": [],
            "cost_summary": {"total_explicit_costs": 1000.0},
            "projected_cash_after_costs": -1000.0,
            "projected_cash_ratio_after_costs": -0.01,
            "projected_investment_ratio_after_costs": 1.01,
        },
    )
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["pre_trade_risk_check"]["complete"] is False
    assert any("pre_trade_risk" in reason for reason in audit["blocking_reasons"])
    assert "## Pre-trade Risk" in report_text
    assert "target_weight_pre_trade_risk_failed" in report_text


def test_run_pilot_without_shadow_does_not_generate_readiness(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())

    result = twp.run_pilot(
        record_shadow_evidence=False,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["launch_artifacts"] == {"attempted": False}
    assert not (runtime_dir / "target_weight_candidate_pilot_launch_readiness.json").exists()
    assert not (runtime_dir / "target_weight_candidate_pilot_runbook.md").exists()


def test_run_pilot_blocks_evidence_when_execution_incomplete(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: {
            "executed": 1,
            "skipped": 1,
            "failed": 1,
            "halted": True,
            "halt_reason": "BUY CCC failed: rejected",
            "details": [],
        },
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: [])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("incomplete execution must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_execution_incomplete" in result["evidence_collection"]["reason"]
    assert saved_sessions[0]["execution_complete"] is False
    assert saved_sessions[0]["evidence_collectible"] is False
    assert payload["evidence_collection"]["status"] == "blocked"


def test_run_pilot_blocks_evidence_when_position_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(pp, "save_pilot_session_artifact", lambda **kwargs: None)
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders[:-1]
        },
    ])
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("position mismatch must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    reconciliation = result["execution_evidence"]["position_reconciliation"]
    assert result["execution_evidence"]["pre_execution_complete"] is True
    assert result["execution_evidence"]["order_complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_position_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol


def test_run_pilot_blocks_evidence_when_order_result_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    execution = _complete_execution(plan)
    execution["details"][0]["result"]["quantity"] = plan.orders[0].quantity - 1
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: execution)
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("mismatched order result must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    reconciliation = result["execution_evidence"]["order_result_reconciliation"]
    assert result["execution_evidence"]["pre_execution_complete"] is True
    assert result["execution_evidence"]["order_count_complete"] is True
    assert result["execution_evidence"]["order_result_complete"] is False
    assert result["execution_evidence"]["order_complete"] is False
    assert result["execution_evidence"]["position_reconciliation"]["complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_order_result_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["type"] == "quantity"
    assert saved_sessions[0]["target_weight_execution"]["order_result_complete"] is False


def test_run_pilot_blocks_evidence_when_fill_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    incomplete_fills = _complete_fills(plan)[:-1]
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: incomplete_fills)
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("mismatched fills must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    reconciliation = result["execution_evidence"]["fill_reconciliation"]
    assert result["execution_evidence"]["order_complete"] is True
    assert result["execution_evidence"]["fill_complete"] is False
    assert result["execution_evidence"]["position_reconciliation"]["complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_fill_reconciliation_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol
    assert saved_sessions[0]["target_weight_execution"]["fill_complete"] is False


def test_run_pilot_blocks_evidence_when_trade_fills_are_unlinked(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    execution = _complete_execution(plan, execution_session_id=TEST_EXECUTION_SESSION_ID)
    unlinked_fills = _complete_fills(plan, execution_session_id="")
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: execution)
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: unlinked_fills)
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("unlinked fills must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    reconciliation = result["execution_evidence"]["fill_reconciliation"]
    assert result["execution_evidence"]["fill_complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_fill_reconciliation_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["execution_session_id"] == TEST_EXECUTION_SESSION_ID
    assert reconciliation["unlinked_fills"][0]["execution_session_id"] == ""
    assert saved_sessions[0]["target_weight_execution"]["fill_complete"] is False


def test_run_pilot_records_artifact_when_pilot_cap_validation_blocks(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=1,
            remaining_exposure=500_000,
            caps_snapshot={
                "max_orders_per_day": 1,
                "max_concurrent_positions": 1,
                "max_notional_per_trade": 500_000,
                "max_gross_exposure": 500_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("cap validation block must not write runtime pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("cap validation block must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("cap validation block must not read positions"),
    )
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("cap validation block must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("cap validation block must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert result["validation"].allowed is False
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["details"][0]["status"] == "skipped_pilot_validation"
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pilot_validation_failed" in result["evidence_collection"]["reason"]
    assert artifact["plan_validation"]["allowed"] is False
    assert artifact["execution"]["details"][0]["status"] == "skipped_pilot_validation"
    assert artifact["evidence_collection"]["status"] == "blocked"


def test_run_pilot_blocks_order_submission_when_starting_positions_drift(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("stale plan must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: {"ZZZ": SimpleNamespace(quantity=3)},
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("stale plan must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    pre_reconciliation = result["execution_evidence"]["pre_execution_reconciliation"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution_evidence"]["pre_execution_complete"] is False
    assert result["execution_evidence"]["order_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pre_execution_position_drift" in result["evidence_collection"]["reason"]
    assert pre_reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert saved_sessions == []


def test_run_pilot_rechecks_starting_positions_inside_execute_plan(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: _pilot_check_for_plan(plan),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    position_snapshots = iter([
        {},
        {"ZZZ": SimpleNamespace(quantity=3)},
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("final position drift must block before order submission"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("blocked execution must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    pre_reconciliation = result["execution_evidence"]["pre_execution_reconciliation"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution_evidence"]["pre_execution_complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pre_execution_position_drift" in result["evidence_collection"]["reason"]
    assert pre_reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert saved_sessions == []


def test_run_pilot_blocks_order_submission_when_liquidity_preflight_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("liquidity failure must not submit orders"),
    )
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("liquidity failure must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("liquidity failure must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["liquidity_check"]["complete"] is False
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution_evidence"]["liquidity_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_liquidity_preflight_failed" in result["evidence_collection"]["reason"]
    assert saved_sessions == []


def test_run_pilot_blocks_order_submission_when_pre_trade_risk_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    risk = {
        "checked": True,
        "complete": False,
        "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
        "violations": ["cash shortfall"],
        "warnings": [],
        "cost_summary": {"total_explicit_costs": 1000.0},
        "order_costs": [],
    }
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "assess_plan_pre_trade_risk", lambda *args, **kwargs: risk)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("pre-trade risk failure must not submit orders"),
    )
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("pre-trade risk failure must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("pre-trade risk failure must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["pre_trade_risk_check"]["complete"] is False
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["details"][0]["status"] == "skipped_pre_trade_risk"
    assert result["execution_evidence"]["pre_trade_risk_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pre_trade_risk_failed" in result["evidence_collection"]["reason"]
    assert saved_sessions == []


def test_run_pilot_blocks_stale_trade_day_before_order_submission(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("stale trade day must not write runtime pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "check_execution_idempotency",
        lambda *args, **kwargs: pytest.fail("stale trade day must block before idempotency"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("stale trade day must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("stale trade day must not read positions"),
    )
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("stale trade day must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("stale trade day must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        as_of_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=datetime(2026, 4, 11, 9, 0),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_trade_day_check"]["allowed"] is False
    assert result["execution_idempotency"] is None
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution"]["details"][0]["status"] == "skipped_execution_trade_day_mismatch"
    assert result["execution_evidence"]["execution_trade_day_allowed"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_execution_trade_day_mismatch" in result["evidence_collection"]["reason"]
    assert payload["execution_trade_day_check"]["allowed"] is False
    assert payload["execution"]["execution_trade_day_check"]["allowed"] is False


def test_run_pilot_blocks_same_trade_day_after_close_before_order_submission(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("closed market session must not write runtime pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "check_execution_idempotency",
        lambda *args, **kwargs: pytest.fail("closed market session must block before idempotency"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("closed market session must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("closed market session must not read positions"),
    )
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("closed market session must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("closed market session must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        as_of_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=datetime(2026, 4, 10, 16, 0),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_trade_day_check"]["allowed"] is True
    assert result["execution_market_session_check"]["allowed"] is False
    assert result["execution_idempotency"] is None
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution"]["details"][0]["status"] == "skipped_execution_market_session_closed"
    assert result["execution_evidence"]["execution_market_session_allowed"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_execution_market_session_closed" in result["evidence_collection"]["reason"]
    assert payload["execution_market_session_check"]["allowed"] is False
    assert payload["execution"]["execution_market_session_check"]["allowed"] is False


def test_run_pilot_blocks_authorization_snapshot_mismatch_before_order_submission(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_snapshot = twp.build_pilot_authorization_snapshot(
        replace(plan, params_hash="old-hash")
    )
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: _pilot_check_for_plan(plan, snapshot=stale_snapshot),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("stale authorization snapshot must not write runtime pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "check_execution_idempotency",
        lambda *args, **kwargs: pytest.fail("stale authorization snapshot must block before idempotency"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("stale authorization snapshot must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("stale authorization snapshot must not read positions"),
    )
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan, **kwargs: pytest.fail("stale authorization snapshot must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("stale authorization snapshot must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["pilot_authorization_snapshot_check"]["allowed"] is False
    assert result["execution_idempotency"] is None
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution"]["details"][0]["status"] == "skipped_pilot_authorization_snapshot_mismatch"
    assert result["execution_evidence"]["pilot_authorization_snapshot_allowed"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pilot_authorization_snapshot_mismatch" in result["evidence_collection"]["reason"]
    assert payload["pilot_authorization_snapshot_check"]["allowed"] is False
    assert payload["execution"]["pilot_authorization_snapshot_check"]["allowed"] is False


def test_run_pilot_blocks_duplicate_execute_session(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("duplicate attempt must not overwrite the prior pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("duplicate attempt must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("duplicate attempt must block before position reads"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("duplicate attempt must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_idempotency"]["allowed"] is False
    assert result["execution_idempotency"]["previous_session_found"] is True
    assert result["execution"]["halted"] is True
    assert result["execution_evidence"]["idempotency_allowed"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_duplicate_execution_attempt" in result["evidence_collection"]["reason"]
    assert payload["execution_idempotency"]["allowed"] is False


def test_run_pilot_blocks_in_progress_execution_lock(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    lock_path = pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "execution_session_id": "already-running",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("locked execution must not write a session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("locked execution must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("locked execution must block before position reads"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("locked execution must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_idempotency"]["allowed"] is False
    assert result["execution_idempotency"]["execution_lock_found"] is True
    assert "target_weight_execution_lock_present" in result["execution_idempotency"]["reason"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["halted"] is True
    assert result["execution_lock"] is None
    assert lock_path.exists()
    assert payload["execution_idempotency"]["execution_lock_found"] is True


def test_check_execution_idempotency_blocks_completed_rerun_even_when_allowed(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    from tools.target_weight_rotation_pilot import check_execution_idempotency

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )

    idempotency = check_execution_idempotency(plan, allow_rerun=True)

    assert idempotency["allowed"] is False
    assert idempotency["allow_rerun"] is True
    assert "target_weight_completed_execution_rerun_blocked" in idempotency["reason"]


def test_check_execution_idempotency_blocks_rerun_after_order_submission(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    from tools.target_weight_rotation_pilot import check_execution_idempotency

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": False,
                "orders_planned": len(plan.orders),
                "orders_executed": 0,
                "order_submission_reached": True,
                "target_weight_execution": {
                    "failed_orders": 1,
                    "halted": True,
                },
            },
        }),
        encoding="utf-8",
    )

    idempotency = check_execution_idempotency(plan, allow_rerun=True)

    assert idempotency["allowed"] is False
    assert idempotency["allow_rerun"] is True
    assert idempotency["previous_order_submission_reached"] is True
    assert "target_weight_unsafe_execution_rerun_blocked" in idempotency["reason"]


def test_run_pilot_blocks_completed_rerun_even_when_allowed(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("completed rerun must not overwrite prior pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("completed rerun must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("completed rerun must block before position reads"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("completed rerun must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        allow_rerun=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["execution_idempotency"]["allowed"] is False
    assert "target_weight_completed_execution_rerun_blocked" in result["execution_idempotency"]["reason"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["halted"] is True
    assert result["evidence_collection"]["status"] == "blocked"


def test_run_pilot_allows_duplicate_session_with_explicit_rerun(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    collected = []
    saved_sessions = []
    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": False,
                "orders_planned": len(plan.orders),
                "orders_executed": 0,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: collected.append(kwargs) or SimpleNamespace(date=kwargs["date"].strftime("%Y-%m-%d")),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        allow_rerun=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["execution_idempotency"]["allowed"] is True
    assert result["execution_idempotency"]["previous_session_found"] is True
    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "recorded"
    assert result["execution_lock"]["acquired"] is True
    assert result["execution_lock_release"]["released"] is True
    assert not pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).with_suffix(".lock").exists()
    assert len(collected) == 1
    assert saved_sessions[0]["target_weight_execution"]["idempotency_allowed"] is True


def test_run_pilot_records_evidence_after_complete_execution(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    collected = []
    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))

    def collect_daily_evidence(**kwargs):
        collected.append(kwargs)
        return SimpleNamespace(date=kwargs["date"].strftime("%Y-%m-%d"))

    monkeypatch.setattr(pe, "collect_daily_evidence", collect_daily_evidence)

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "recorded"
    assert len(collected) == 1
    caps = collected[0]["pilot_caps_snapshot"]
    assert caps["target_weight_execution"]["complete"] is True
    assert caps["target_weight_execution"]["pre_execution_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["liquidity_complete"] is True
    assert caps["target_weight_execution"]["liquidity_check"]["complete"] is True
    assert caps["target_weight_execution"]["pre_trade_risk_complete"] is True
    assert caps["target_weight_execution"]["pre_trade_risk_check"]["complete"] is True
    assert caps["target_weight_execution"]["order_result_complete"] is True
    assert caps["target_weight_execution"]["order_result_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["fill_complete"] is True
    assert caps["target_weight_execution"]["db_persistence_complete"] is True
    assert caps["target_weight_execution"]["fill_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["fill_reconciliation"]["source"] == "database.trade_history"
    assert caps["target_weight_execution"]["position_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["position_reconciliation"]["source"] == "database.positions"
    assert caps["target_weight_execution"]["db_persistence_proof"]["complete"] is True
    assert (
        caps["target_weight_execution"]["db_persistence_proof"]["trade_history"]["row_count"]
        == len(plan.orders)
    )
    assert caps["target_weight_execution"]["planned_orders"] == len(plan.orders)
    assert caps["target_weight_plan"]["params_hash"] == plan.params_hash
    assert caps["target_weight_plan"]["position_quantities_before"] == {}
    assert saved_sessions[0]["execution_complete"] is True


def test_target_weight_execution_evidence_flows_to_promotion_and_live_gate(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.paper_pilot_control as ppc
    import tools.target_weight_rotation_pilot as twp
    from core.live_gate import (
        LIVE_GATE_ARTIFACT_TYPE,
        LIVE_GATE_SCHEMA_VERSION,
        validate_live_readiness,
    )
    from core.promotion_engine import load_metrics_from_artifact, promote
    from tools.evaluate_and_promote import (
        build_current_blockers_report,
        build_data_snapshot_manifest,
        build_promotion_blocker_summary,
    )

    def write_json(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = _adapter_plan()
    evidence_dir = tmp_path / "paper_evidence"
    promotion_dir = tmp_path / "promotion"
    runtime_dir = tmp_path / "paper_runtime"
    collected = []
    saved_sessions = []

    monkeypatch.setattr(pe, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pp, "check_pilot_prerequisites", lambda strategy: (True, "ok"))
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=False,
            reason="no active pilot authorization",
            remaining_orders=None,
            remaining_exposure=None,
            caps_snapshot=None,
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": False,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": False,
            "blocking_requirements": [],
        },
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})

    readiness = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "readiness",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    audit = readiness["audit"]
    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert audit["operator_commands"]["execute_capped_paper"].startswith("# blocked:")
    assert "pilot authorization is not active" in audit["operator_commands"]["execute_capped_paper"]

    enabled_snapshot = twp.build_pilot_authorization_snapshot(
        plan,
        readiness_audit=audit,
    )
    monkeypatch.setattr(
        ppc,
        "_target_weight_enable_guard",
        lambda args: {
            **readiness,
            "target_weight_plan_snapshot": enabled_snapshot,
        },
    )
    ppc.run_enable(SimpleNamespace(
        strategy=plan.candidate_id,
        valid_from=plan.trade_day,
        valid_to="2026-04-30",
        max_orders=10,
        max_positions=10,
        max_notional=2_000_000,
        max_exposure=10_000_000,
        reason="target-weight e2e fixture",
    ))
    active_auth = pp.get_active_pilot(plan.candidate_id, plan.trade_day)
    assert active_auth is not None
    assert active_auth.target_weight_plan_snapshot["candidate_id"] == plan.candidate_id
    assert active_auth.target_weight_plan_snapshot["trade_day"] == plan.trade_day
    assert active_auth.target_weight_plan_snapshot["params_hash"] == plan.params_hash

    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: _pilot_check_for_plan(
            plan,
            snapshot=active_auth.target_weight_plan_snapshot,
        ),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))

    def capture_daily_evidence(**kwargs):
        collected.append(kwargs)
        return SimpleNamespace(date=kwargs["date"].strftime("%Y-%m-%d"))

    monkeypatch.setattr(pe, "collect_daily_evidence", capture_daily_evidence)

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "recorded"
    assert saved_sessions[0]["execution_complete"] is True
    assert len(collected) == 1

    proof_caps = collected[0]["pilot_caps_snapshot"]
    jsonl_path = evidence_dir / f"daily_evidence_{plan.candidate_id}.jsonl"
    for index, day in enumerate(pd.bdate_range("2026-01-05", periods=60)):
        record_date = day.strftime("%Y-%m-%d")
        caps = deepcopy(proof_caps)
        caps["target_weight_plan"]["trade_day"] = record_date
        pe._append_jsonl(jsonl_path, {
            "date": record_date,
            "day_number": index + 1,
            "strategy": plan.candidate_id,
            "execution_backed": True,
            "evidence_mode": "pilot_paper",
            "session_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": caps,
            "daily_return": 0.2 if index % 2 == 0 else 0.1,
            "cumulative_return": round((index + 1) * 0.15, 2),
            "mdd": -3.0,
            "total_trades": len(plan.orders),
            "sell_count": 1,
            "winning_trades": 1,
            "losing_trades": 0,
            "same_universe_excess": 0.05,
            "exposure_matched_excess": 0.04,
            "cash_adjusted_excess": 0.03,
            "benchmark_status": "final",
            "status": "normal",
            "anomalies": [],
        })

    trades = _paper_trade_history_for_plan(plan, daily_sell_count=1)
    monkeypatch.setattr("database.models.get_session", lambda: _StaticTradeSession(trades))

    pkg_path, _ = pe.generate_promotion_package(plan.candidate_id)
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))

    assert pkg["recommendation"] == "ELIGIBLE"
    assert pkg["target_weight_verified_pilot_days"] == 60
    assert pkg["target_weight_invalid_days"] == 0
    assert pkg["target_weight_evidence"]["params_hash"] == plan.params_hash
    assert pkg["target_weight_evidence"]["all_promotable_days_verified"] is True

    snapshot_manifest = build_data_snapshot_manifest(
        provider="test-provider",
        universe_rule="target-weight e2e fixture",
        eval_start="2026-01-05",
        eval_end="2026-03-27",
        universe_lookback_start="2025-10-01",
        universe_lookback_end="2025-12-31",
        universe=plan.symbols,
        liquidity_coverage={
            symbol: {"rows": 62, "start": "2025-10-01", "end": "2025-12-31"}
            for symbol in plan.symbols
        },
        benchmark_coverage={
            symbol: {"rows": 60, "start": "2026-01-05", "end": "2026-03-27"}
            for symbol in plan.symbols
        },
        fetch_errors={},
    )
    write_json(
        promotion_dir / "run_metadata.json",
        {
            "schema_version": LIVE_GATE_SCHEMA_VERSION,
            "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
            "commit_hash": "abc123",
            "config_yaml_hash": "yaml-ok",
            "config_resolved_hash": "resolved-ok",
            "generated_at": datetime(2026, 4, 10, 12, 0, 0).isoformat(),
            "data_snapshot_hash": snapshot_manifest["data_snapshot_hash"],
            "data_snapshot_manifest": snapshot_manifest,
            "evaluation_errors": {},
            "walk_forward_errors": {},
            "strategy_specs": [{
                "candidate_id": plan.candidate_id,
                "params_hash": plan.params_hash,
            }],
        },
    )
    write_json(
        promotion_dir / "metrics_summary.json",
        {
            plan.candidate_id: {
                "total_return": 24.0,
                "profit_factor": 1.55,
                "mdd": -8.0,
                "wf_positive_rate": 0.8,
                "wf_sharpe_positive_rate": 0.8,
                "wf_windows": 5,
                "wf_total_trades": 120,
                "sharpe": 0.75,
                "benchmark_excess_return": 4.0,
                "benchmark_excess_sharpe": 0.25,
                "ev_per_trade": 5000,
                "cost_adjusted_cagr": 9.0,
                "turnover_per_year": 350.0,
            }
        },
    )
    write_json(
        promotion_dir / "walk_forward_summary.json",
        {plan.candidate_id: {"windows": 5, "positive": 4, "sharpe_pos": 4, "total_trades": 120}},
    )
    write_json(
        promotion_dir / "benchmark_comparison.json",
        {
            "strategy_excess_return_pct": {plan.candidate_id: 4.0},
            "strategy_excess_sharpe": {plan.candidate_id: 0.25},
        },
    )

    metrics = load_metrics_from_artifact(str(promotion_dir), evidence_dir=str(evidence_dir))
    promotion = promote(metrics[plan.candidate_id])
    assert promotion.status == "live_candidate"
    promotions = {
        plan.candidate_id: {
            "status": promotion.status,
            "allowed_modes": promotion.allowed_modes,
            "reason": promotion.reason,
        }
    }
    write_json(promotion_dir / "promotion_result.json", promotions)
    run_metadata = json.loads((promotion_dir / "run_metadata.json").read_text(encoding="utf-8"))
    metrics_summary = json.loads((promotion_dir / "metrics_summary.json").read_text(encoding="utf-8"))
    blocker_summary = build_promotion_blocker_summary(promotions, metrics_summary, run_metadata)
    write_json(
        promotion_dir / "promotion_blocker_summary.json",
        blocker_summary,
    )
    write_json(
        promotion_dir.parent / "current_blockers.json",
        build_current_blockers_report(
            blocker_summary,
            generated_at=run_metadata["generated_at"],
        ),
    )

    issues = validate_live_readiness(
        SimpleNamespace(yaml_hash="yaml-ok", resolved_hash="resolved-ok"),
        plan.candidate_id,
        promotion_dir=promotion_dir,
        evidence_dir=evidence_dir,
        current_git_hash="abc123",
        now=datetime(2026, 4, 10, 12, 0, 0),
    )

    assert issues == []


def test_existing_pilot_evidence_uses_latest_same_day_record(monkeypatch):
    import core.paper_evidence as pe
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    stale_record = _existing_pilot_evidence_record(plan)
    stale_record["pilot_caps_snapshot"]["target_weight_execution"]["complete"] = False
    latest_record = _existing_pilot_evidence_record(plan)
    monkeypatch.setattr(
        pe,
        "get_canonical_records",
        lambda strategy: [stale_record, latest_record],
    )

    result = twp.verify_existing_pilot_evidence_record(plan)

    assert result["valid"] is True
    assert result["reason"] == "existing pilot_paper evidence verified"


def test_run_pilot_accepts_verified_already_recorded_pilot_evidence(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(pe, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [_existing_pilot_evidence_record(plan)])

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "already_recorded"
    assert result["evidence_collection"]["existing_evidence"]["valid"] is True
    assert payload["evidence_collection"]["existing_evidence"]["valid"] is True
    assert saved_sessions[0]["execution_complete"] is True


def test_run_pilot_blocks_unverified_already_recorded_evidence(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    stale_record = _existing_pilot_evidence_record(plan)
    stale_record["evidence_mode"] = "real_paper"
    stale_record["session_mode"] = "normal_paper"
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan, **kwargs: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(pe, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [stale_record])

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
        execution_now=_plan_execution_now(),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_existing_evidence_invalid" in result["evidence_collection"]["reason"]
    assert result["evidence_collection"]["existing_evidence"]["valid"] is False
    assert payload["evidence_collection"]["status"] == "blocked"
    assert saved_sessions[0]["execution_complete"] is True


def test_run_shadow_bootstrap_records_range_and_skips_duplicates(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(
        twp,
        "build_plan",
        lambda **kwargs: _adapter_plan_for_date(kwargs["as_of_date"]),
    )

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-08",
        end_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    records = pe.get_canonical_records("target_weight_candidate")
    readiness = result["launch_artifacts"]["launch_readiness"]

    assert result["summary"]["recorded"] == 3
    assert result["summary"]["already_recorded"] == 0
    assert payload["summary"]["recorded"] == 3
    assert [record["date"] for record in records] == ["2026-04-08", "2026-04-09", "2026-04-10"]
    assert all(record["execution_backed"] is False for record in records)
    assert readiness["clean_final_days_current"] == 3
    assert Path(result["launch_artifacts"]["runbook_path"]).exists()

    second = twp.run_shadow_bootstrap(
        start_date="2026-04-08",
        end_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert second["summary"]["recorded"] == 0
    assert second["summary"]["already_recorded"] == 3
    assert len(pe.get_canonical_records("target_weight_candidate")) == 3


def test_run_shadow_bootstrap_skips_duplicate_trade_day_in_batch(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(
        twp,
        "build_plan",
        lambda **kwargs: _adapter_plan_for_date("2026-04-10"),
    )

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-10",
        end_date="2026-04-13",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["summary"]["recorded"] == 1
    assert result["summary"]["duplicate_trade_day"] == 1
    assert len(pe.get_canonical_records("target_weight_candidate")) == 1


def test_run_shadow_bootstrap_auto_days_backfills_unique_trade_days(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)

    def build_plan(**kwargs):
        as_of = kwargs["as_of_date"]
        if as_of == "2026-04-13":
            return replace(_adapter_plan_for_date("2026-04-10"), as_of_date=as_of)
        return _adapter_plan_for_date(as_of)

    monkeypatch.setattr(twp, "build_plan", build_plan)

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-10",
        end_date="2026-04-13",
        target_unique_trade_days=2,
        max_scan_weekdays=3,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    records = pe.get_canonical_records("target_weight_candidate")

    assert result["requested_dates"] == ["2026-04-09", "2026-04-13"]
    assert result["summary"]["recorded"] == 2
    assert result["summary"]["duplicate_trade_day"] == 0
    assert result["summary"]["covered_unique_trade_days"] == 2
    assert result["summary"]["target_unique_trade_days"] == 2
    assert result["summary"]["target_met"] is True
    assert [record["date"] for record in records] == ["2026-04-09", "2026-04-10"]
    assert payload["summary"]["covered_unique_trade_days"] == 2
    assert payload["summary"]["target_met"] is True
