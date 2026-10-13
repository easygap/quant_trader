"""Execution-cost observations, with no authority over orders or portfolios.

Price shortfall uses BUY=+1/SELL=-1 on cumulative fills, never sums repeated
broker snapshots. Slippage already embedded in fill prices is not charged twice.
Read reports through SQLite mode=ro; missing schemas remain explicitly missing.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import closing
from datetime import datetime, timezone, timedelta
from functools import wraps
import inspect
import json
import math
from pathlib import Path
import sqlite3
import uuid

from loguru import logger

KST = timezone(timedelta(hours=9))
_ACTIVE = ContextVar("execution_cost_observation", default=None)


def _now():
    return datetime.now(KST).isoformat()


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _stamp(value, *, system_local=False):
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # OrderState timestamps come from datetime.now() in the host time zone.
        # Report query dates without an offset retain their documented KST basis.
        if parsed.tzinfo is None and not system_local:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST).isoformat()
    except (TypeError, ValueError):
        return None


def _currency(symbol):
    symbol = str(symbol).strip()
    if len(symbol) == 6 and symbol.isdigit():
        return "KRW"
    if symbol.isascii() and symbol.isalpha():
        return "USD"
    return "UNKNOWN"


def _broker_environment(executor):
    if executor.mode != "live":
        return "paper_simulation"
    mock = getattr(getattr(executor, "kis_api", None), "use_mock", None)
    return "vts" if mock is True else ("production" if mock is False else "unknown")


def _elapsed(start, end):
    start, end = _stamp(start), _stamp(end)
    if start and end:
        value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        return value if value >= 0 else None
    return None


def _append(payload, event_type, context=None):
    try:
        from database.repositories import append_execution_cost_event
        append_execution_cost_event({**payload, "observed_at": _now()}, event_type)
        return True
    except Exception as exc:
        if context is not None:
            context["write_failed"] = True
        # Never log SQL values, broker responses, credentials, or account numbers.
        logger.warning("체결비용 관측 저장 실패: {} — 주문 동작은 유지, 분석 자료 누락", type(exc).__name__)
        return False


def _safe_observer(function):
    """Measurement failure must never change order, portfolio or retry behavior."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            context = _ACTIVE.get()
            if context is not None:
                context["write_failed"] = True
            logger.warning("체결비용 관측 실패: {} — 주문 동작 유지, 분석 자료 누락", type(exc).__name__)
            return None
    return wrapped


def observe_execution(action):
    """Observe public executor entry, including pre-order risk rejections."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            decision = kwargs.pop("decision_context", None)
            decision = decision if isinstance(decision, dict) else {}
            bound = signature.bind(self, *args, **kwargs)
            values = bound.arguments
            entered = _now()
            reference = _number(decision.get("price"), positive=True)
            reference_at = _stamp(decision.get("at"), system_local=True)
            genuine_decision = reference is not None and reference_at is not None and reference_at <= entered
            payload = {
                "schema_version": 1, "attempt_id": uuid.uuid4().hex,
                "account_key": getattr(self, "account_key", "") or "", "mode": self.mode,
                "symbol": str(values.get("symbol", "")), "action": action,
                "strategy": str(values.get("strategy", "")), "order_id": None,
                "requested_qty": _number(values.get("quantity"), positive=True),
                "reference_price": reference if genuine_decision else _number(values.get("price"), positive=True),
                "reference_at": reference_at if genuine_decision else entered,
                "reference_kind": "basket_plan" if genuine_decision and decision.get("source") == "basket_plan" else ("decision" if genuine_decision else "executor_input"),
                "executor_entered_at": entered, "signal_at": _stamp(values.get("signal_at"), system_local=True),
                "status": "INPUT_RECEIVED", "filled_qty": 0, "filled_price": None,
                "commission_amount": None, "tax_amount": None, "fees_basis": "unavailable",
                "fill_price_basis": "modeled" if self.mode != "live" else "broker_observed",
                "currency": _currency(values.get("symbol", "")),
                "broker_environment": _broker_environment(self),
                "execution_session_id": str(values.get("execution_session_id", "")),
            }
            context = {"payload": payload, "order": None, "write_failed": False}
            token = _ACTIVE.set(context)
            _append(payload, "input", context)
            try:
                result = function(self, *args, **kwargs)
                _finish_observation(context, result)
                return result
            except Exception:
                if context["order"] is not None:
                    observe_order_state(context["order"], outcome="execution_exception")
                else:
                    _append({**payload, "status": "EXECUTION_EXCEPTION", "outcome": "execution_exception"}, "exception", context)
                raise
            finally:
                _ACTIVE.reset(token)
        return wrapped
    return decorate


@_safe_observer
def _finish_observation(context, result):
    payload = context["payload"]
    if context["order"] is not None:
        observe_order_state(context["order"], result=result)
    else:
        payload.update(status="REJECTED_BEFORE_ORDER", outcome="rejected",
                       reason=str(result.get("reason", "not_executed"))[:400])
        _append(payload, "result", context)
    if isinstance(result, dict):
        result["execution_observation_id"] = payload["attempt_id"]
        result["execution_observability"] = "incomplete" if context["write_failed"] else "recorded"


@_safe_observer
def observe_order_created(order):
    context = _ACTIVE.get()
    if context is not None:
        context["order"] = order
        order.execution_observation_id = context["payload"]["attempt_id"]
        observe_order_state(order)


def _existing_payload(order_id):
    context = _ACTIVE.get()
    if context is not None and context.get("order") is not None and context["order"].order_id == order_id:
        return dict(context["payload"]), context
    try:
        from database.repositories import latest_execution_cost_payload
        return latest_execution_cost_payload(order_id), None
    except Exception as exc:
        logger.warning("체결비용 관측 조회 실패: {}", type(exc).__name__)
        return None, None


@_safe_observer
def observe_order_state(order, *, result=None, outcome=None):
    payload, context = _existing_payload(order.order_id)
    if payload is None:
        return  # Legacy order: do not invent a decision benchmark.
    payload.update(order_id=order.order_id, status=getattr(order.status, "value", order.status),
                   requested_qty=order.requested_qty, filled_qty=order.filled_qty,
                   filled_price=_number(order.filled_price, positive=True),
                   submitted_at=_stamp(order.submitted_at, system_local=True),
                   fill_observed_at=_stamp(order.filled_at, system_local=True),
                   remaining_qty=max(0, order.requested_qty - order.filled_qty))
    if result is not None:
        payload["outcome"] = "completed" if result.get("success") else ("pending" if result.get("order_pending") else "rejected")
        payload["reason"] = str(result.get("reason", ""))[:400]
        costs = result.get("costs")
        if isinstance(costs, dict) and result.get("success"):
            tax = _number(costs.get("tax"))
            gains_tax = _number(costs.get("capital_gains_tax", 0))
            payload.update(commission_amount=_number(costs.get("commission")),
                           tax_amount=tax + gains_tax if tax is not None and gains_tax is not None else None,
                           fees_basis="estimated" if order.mode == "live" else "modeled",
                           cost_quantity=order.filled_qty)
    if outcome:
        payload["outcome"] = outcome
    if context is not None:
        context["payload"].update(payload)
    _append(payload, "result" if result is not None or outcome else "order_state", context)


@_safe_observer
def observe_reconciled_order(record: dict, *, execution_price_observed=None):
    payload, context = _existing_payload(record["order_id"])
    if payload is None:
        return
    old_qty = payload.get("filled_qty", 0)
    quantity = record.get("filled_qty", 0)
    price = _number(record.get("filled_price"), positive=True)
    if execution_price_observed is not True:
        # Only preserve our previously observed average for the *same* quantity.
        # The order ledger may itself carry forward an old partial-fill price.
        price = _number(payload.get("filled_price"), positive=True) if quantity == old_qty else None
    payload.update(status=record["status"], filled_qty=quantity, filled_price=price,
                   remaining_qty=max(0, (payload.get("requested_qty") or 0) - quantity),
                   broker_remaining_qty=record.get("remaining_qty"),
                   reconciled_observed_at=_now(), outcome="reconciled")
    if payload.get("cost_quantity") != quantity:
        payload.update(commission_amount=None, tax_amount=None, fees_basis="unavailable")
    if context is not None:
        context["payload"].update(payload)
    _append(payload, "reconciliation", context)


def _cost_row(payload):
    row = dict(payload)
    reference, fill, quantity = (_number(row.get("reference_price"), positive=True),
                                 _number(row.get("filled_price"), positive=True),
                                 _number(row.get("filled_qty"), positive=True))
    denominator = reference * quantity if reference and quantity else None
    price_cost = (1 if row.get("action") == "BUY" else -1) * (fill - reference) * quantity if denominator and fill else None
    commission, tax = _number(row.get("commission_amount")), _number(row.get("tax_amount"))
    fees = commission + tax if commission is not None and tax is not None else None
    total = price_cost + fees if price_cost is not None and fees is not None else None
    row.update(price_shortfall_amount=price_cost, price_shortfall_bps=price_cost / denominator * 10000 if price_cost is not None else None,
               explicit_cost_amount=fees, price_plus_fees_amount=total,
               price_plus_fees_bps=total / denominator * 10000 if total is not None else None,
               filled_reference_notional=denominator, opportunity_cost_amount=None, opportunity_cost_included=False,
               opportunity_cost_reason="no_independent_end_mark_for_unfilled_quantity",
               fully_observed_total_cost_amount=total if row.get("fees_basis") == "broker_observed" else None,
               reference_to_submit_seconds=_elapsed(row.get("reference_at"), row.get("submitted_at")),
               submit_to_fill_observed_seconds=_elapsed(row.get("submitted_at"), row.get("fill_observed_at")),
               timestamp_basis="local_observation_not_exchange_execution_timestamp")
    if quantity and not fill:
        row["measurement_status"] = "fill_price_unavailable"
    elif not reference:
        row["measurement_status"] = "reference_unavailable"
    else:
        row["measurement_status"] = "measured" if price_cost is not None else "no_fill"
    return row


def execution_cost_report(*, account_key=None, mode=None, since=None, limit=200, db_path=None):
    """Read-only API; newest attempts with cumulative order state, no DB creation."""
    if db_path is None:
        from config.config_loader import Config
        config = Config.get().database
        if config.get("type") == "postgresql":
            return {"status": "unavailable", "reason": "sqlite_report_only", "orders": [], "summary_by_basis": [], "coverage": {}}
        db_path = Path(__file__).resolve().parents[1] / config.get("sqlite_path", "data/quant_trader.db")
    path = Path(db_path).resolve()
    report = {"schema_version": 1, "status": "unavailable", "as_of": _now(), "orders": [],
              "summary_by_basis": [], "coverage": {}, "limitations": [
                  "BUY는 체결가 상승, SELL은 체결가 하락이 양의 비용입니다.",
                  "기준은 basket_plan/decision 또는 executor_input이며 과거 주문 가격을 소급 추정하지 않습니다.",
                  "모의 체결과 비용 추정은 실제 브로커 측정과 분리합니다. 체결가에 포함된 슬리피지를 다시 더하지 않습니다.",
                  "미체결 잔량의 기회비용과 독립적인 주문 도착가격은 수집되지 않아 포함하지 않습니다.",
                  "latest 상태의 누적 체결량 기준이며 같은 주문의 부분체결/재조회 이벤트를 합산하지 않습니다.",
              ]}
    if not path.is_file():
        return {**report, "reason": "database_missing"}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "execution_cost_events" not in tables:
                return {**report, "reason": "execution_observation_schema_missing"}
            clauses, values = [], []
            if account_key is not None:
                clauses.append("account_key=?")
                values.append(account_key)
            if mode is not None:
                clauses.append("mode=?")
                values.append(mode)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            since_at = _stamp(since) if since is not None else None
            if since is not None and since_at is None:
                return {**report, "reason": "invalid_since_timestamp"}
            latest_where = " WHERE id IN (SELECT MAX(id) FROM execution_cost_events" + where + " GROUP BY attempt_id)"
            latest_values = list(values)
            if since_at:
                latest_where += " AND json_extract(payload_json, '$.executor_entered_at') >= ?"
                latest_values.append(since_at)
            total_matching = connection.execute("SELECT COUNT(*) FROM execution_cost_events" + latest_where, latest_values).fetchone()[0]
            rows = connection.execute(
                "SELECT payload_json FROM execution_cost_events" + latest_where + " ORDER BY id DESC LIMIT ?",
                [*latest_values, max(1, min(int(limit), 1000))],
            ).fetchall()
            legacy = set()
            unlinked_legacy_trades = 0
            for table in ("order_records", "trade_history"):
                if table not in tables:
                    continue
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "order_id" not in columns:
                    continue
                legacy_where = where + (" AND " if where else " WHERE ") + (
                    "order_id IS NOT NULL AND order_id != '' AND NOT EXISTS "
                    "(SELECT 1 FROM execution_cost_events e WHERE e.order_id=t.order_id "
                    "AND e.account_key=t.account_key AND e.mode=t.mode)"
                )
                query = f"SELECT DISTINCT order_id FROM {table} t" + legacy_where
                for record in connection.execute(query, values):
                    legacy.add(record[0])
                if table == "trade_history":
                    missing_where = where + (" AND " if where else " WHERE ") + "(order_id IS NULL OR order_id='')"
                    unlinked_legacy_trades = connection.execute("SELECT COUNT(*) FROM trade_history" + missing_where, values).fetchone()[0]
            orders = [_cost_row(json.loads(record[0])) for record in rows]
            groups = {}
            for row in orders:
                key = (row["mode"], row["reference_kind"], row["fill_price_basis"], row["fees_basis"], row.get("currency", "UNKNOWN"), row.get("broker_environment", "unknown"))
                group = groups.setdefault(key, {"mode": key[0], "reference_kind": key[1], "fill_price_basis": key[2], "fees_basis": key[3],
                    "currency": key[4],
                    "broker_environment": key[5],
                    "attempts": 0, "filled_orders": 0, "priced_fills": 0, "filled_reference_notional": 0., "price_shortfall_amount": 0.,
                    "price_plus_fees_amount": 0., "fees_covered_fills": 0, "fees_covered_notional": 0.})
                group["attempts"] += 1
                group["filled_orders"] += int((row.get("filled_qty") or 0) > 0)
                if row["price_shortfall_amount"] is not None:
                    group["priced_fills"] += 1
                    group["filled_reference_notional"] += row["filled_reference_notional"]
                    group["price_shortfall_amount"] += row["price_shortfall_amount"]
                if row["price_plus_fees_amount"] is not None:
                    group["fees_covered_fills"] += 1
                    group["fees_covered_notional"] += row["filled_reference_notional"]
                    group["price_plus_fees_amount"] += row["price_plus_fees_amount"]
            for group in groups.values():
                group["price_shortfall_bps"] = group["price_shortfall_amount"] / group["filled_reference_notional"] * 10000 if group["filled_reference_notional"] else None
                group["price_plus_fees_bps"] = group["price_plus_fees_amount"] / group["fees_covered_notional"] * 10000 if group["fees_covered_notional"] else None
                if not group["priced_fills"]:
                    group["price_shortfall_amount"] = None
                if not group["fees_covered_fills"]:
                    group["price_plus_fees_amount"] = None
            report.update(status="ready" if orders else "unavailable", reason=None if orders else "no_new_execution_observations",
                          orders=orders, summary_by_basis=list(groups.values()), coverage={
                              "attempts": len(orders), "total_matching_attempts": total_matching, "truncated": total_matching > len(orders),
                              "linked_orders": sum(bool(row.get("order_id")) for row in orders),
                              "filled_orders": sum((row.get("filled_qty") or 0) > 0 for row in orders),
                              "unpriced_fills": sum(row["measurement_status"] == "fill_price_unavailable" for row in orders),
                              "without_reference": sum(row["reference_price"] is None for row in orders), "legacy_unmeasured_orders": len(legacy),
                              "legacy_unlinked_trades": unlinked_legacy_trades,
                              "legacy_scope": "all_available_history_for_account_and_mode", "since": since})
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        report.update(status="unavailable", reason=f"execution_observation_read_failed:{type(exc).__name__}")
    return report
