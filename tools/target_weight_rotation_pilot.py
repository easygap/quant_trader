#!/usr/bin/env python3
"""Paper/pilot adapter for the canonical target-weight rotation candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from loguru import logger

from core.data_collector import DataCollectionError
from core.target_weight_commands import command_scope_issues as target_weight_command_scope_issues
from core.target_weight_rotation import (
    DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    TargetWeightPlan,
    build_target_weight_plan,
    load_canonical_target_weight_spec,
    normalize_symbol,
    validate_plan_against_pilot,
)

DEFAULT_OUTPUT_DIR = Path("reports/paper_runtime")
DEFAULT_PILOT_PREVIEW_CAPS = {
    "max_orders_per_day": 2,
    "max_concurrent_positions": 2,
    "max_notional_per_trade": 1_000_000,
    "max_gross_exposure": 3_000_000,
}
DEFAULT_CAP_BUFFER_PCT = 0.05
DEFAULT_CAP_ROUNDING_STEP = 10_000
DEFAULT_SHADOW_SCAN_MULTIPLIER = 5
DEFAULT_LIQUIDITY_LOOKBACK_DAYS = 20
DEFAULT_MAX_ORDER_ADV_PCT = 5.0
TARGET_WEIGHT_PILOT_TARGET_DAYS = 60
AUTHORIZATION_SNAPSHOT_SCHEMA_VERSION = 1
AUTHORIZATION_SNAPSHOT_TYPE = "target_weight_plan_authorization"
KST = timezone(timedelta(hours=9))
NO_ORDER_OPERATION_ERRORS = (ValueError, DataCollectionError)
REPAIRABLE_TARGET_WEIGHT_EVIDENCE_REASONS = {
    "target_weight_benchmark_status_not_final",
    "target_weight_excess_metrics_missing",
    "target_weight_daily_return_missing",
    "target_weight_portfolio_value_missing",
}


def _stable_manifest_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _split_symbols(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    symbols = [part.strip() for part in raw.replace("\n", ",").split(",")]
    return [symbol for symbol in symbols if symbol]


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("shadow end date must be on or after start date")

    dates: list[str] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    if not dates:
        raise ValueError("shadow date range contains no weekdays")
    return dates


def _load_kr_market_holidays() -> set[str]:
    try:
        from core.trading_hours import _load_holidays

        return {str(day).strip() for day in _load_holidays() if str(day).strip()}
    except Exception as exc:
        logger.debug("KRX holiday lookup skipped for pilot window calculation: {}", exc)
        return set()


def _is_kr_market_business_day(day: date, holidays: set[str]) -> bool:
    return day.weekday() < 5 and day.isoformat() not in holidays


def _pilot_valid_to(
    valid_from: str,
    target_pilot_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
) -> str:
    """Return an inclusive KRX business-day pilot window end date."""
    if target_pilot_days <= 0:
        raise ValueError("target_pilot_days must be positive")

    current = datetime.strptime(valid_from, "%Y-%m-%d").date()
    holidays = _load_kr_market_holidays()
    counted_days = 0
    while True:
        if _is_kr_market_business_day(current, holidays):
            counted_days += 1
            if counted_days >= target_pilot_days:
                return current.isoformat()
        current += timedelta(days=1)


def _next_kr_market_business_day(day: str) -> str:
    current = datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)
    holidays = _load_kr_market_holidays()
    while not _is_kr_market_business_day(current, holidays):
        current += timedelta(days=1)
    return current.isoformat()


def _coerce_kst_datetime(now: datetime | None = None) -> datetime:
    current = now or datetime.now(KST)
    if current.tzinfo is not None:
        current = current.astimezone(KST).replace(tzinfo=None)
    return current


def _execution_day(now: datetime | None = None) -> str:
    current = _coerce_kst_datetime(now)
    return current.date().strftime("%Y-%m-%d")


def _require_not_future_as_of_date(
    as_of_date: str | None,
    *,
    context: str,
    now: datetime | None = None,
) -> None:
    if not as_of_date:
        return
    requested = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    current = _coerce_kst_datetime(now).date()
    if requested > current:
        raise ValueError(
            "target_weight_future_as_of_date_blocked: "
            f"{context} as_of_date={requested.isoformat()} "
            f"current_kst_date={current.isoformat()}; rerun on or after the requested date"
        )


def _require_requested_as_of_trade_day(
    plan: TargetWeightPlan,
    as_of_date: str | None,
    *,
    context: str,
) -> None:
    if not as_of_date:
        return
    if str(plan.trade_day) == str(as_of_date):
        return
    raise ValueError(
        "target_weight_requested_trade_day_unavailable: "
        f"{context} as_of_date={as_of_date} resolved_trade_day={plan.trade_day}; "
        "refresh current market data or rerun for a date whose resolved trade day matches the request"
    )


def make_execution_session_id(plan: TargetWeightPlan, now: datetime | None = None) -> str:
    current = _coerce_kst_datetime(now)
    stamp = current.strftime("%Y%m%d%H%M%S")
    candidate = "".join(
        char if char.isalnum() or char in {"_", "-"} else "_"
        for char in str(plan.candidate_id)
    ).strip("_") or "target_weight"
    return f"{candidate}_{plan.trade_day}_{stamp}_{uuid.uuid4().hex[:8]}"


def execution_trade_day_check_not_required() -> dict[str, Any]:
    return {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution trade-day check not required",
    }


def validate_execution_trade_day(
    plan: TargetWeightPlan,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    execution_day = _execution_day(now)
    allowed = plan.trade_day == execution_day
    reason = "target-weight execution trade day matches current KST date"
    if not allowed:
        reason = (
            "target_weight_execution_trade_day_mismatch: "
            f"plan_trade_day={plan.trade_day} execution_day={execution_day} "
            f"plan_as_of_date={plan.as_of_date}; rerun with current market data before --execute"
        )
    return {
        "checked": True,
        "allowed": allowed,
        "complete": allowed,
        "reason": reason,
        "plan_trade_day": plan.trade_day,
        "plan_as_of_date": plan.as_of_date,
        "execution_day": execution_day,
        "timezone": "Asia/Seoul",
    }


def execution_market_session_check_not_required() -> dict[str, Any]:
    return {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": "execution market session check not required",
    }


def validate_execution_market_session(
    plan: TargetWeightPlan,
    *,
    config: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from config.config_loader import Config
    from core.trading_hours import TradingHours

    current = _coerce_kst_datetime(now)
    base = {
        "checked": True,
        "plan_trade_day": plan.trade_day,
        "plan_as_of_date": plan.as_of_date,
        "execution_day": current.date().strftime("%Y-%m-%d"),
        "execution_time": current.strftime("%H:%M:%S"),
        "timezone": "Asia/Seoul",
    }
    try:
        trading_hours = TradingHours(config or Config.get())
        order_check = trading_hours.can_place_order(current)
    except Exception as exc:
        return {
            **base,
            "allowed": False,
            "complete": False,
            "reason": f"target_weight_execution_market_session_check_failed: {exc}",
        }

    allowed = bool(order_check.get("allowed", False))
    reason = "target-weight execution market session is open"
    if not allowed:
        reason = (
            "target_weight_execution_market_session_closed: "
            f"{order_check.get('reason', 'market closed')}; "
            "execute only during KRX regular session"
        )
    return {
        **base,
        "allowed": allowed,
        "complete": allowed,
        "reason": reason,
        "market_open": trading_hours.market_open.strftime("%H:%M"),
        "market_close": trading_hours.market_close.strftime("%H:%M"),
    }


def _authorization_snapshot_not_required(reason: str) -> dict[str, Any]:
    return {
        "checked": False,
        "allowed": True,
        "complete": True,
        "reason": reason,
        "mismatches": [],
    }


def build_pilot_authorization_snapshot(
    plan: TargetWeightPlan,
    *,
    readiness_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio_drawdown_guard = plan.diagnostics.get("portfolio_drawdown_guard")
    snapshot = {
        "schema_version": AUTHORIZATION_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_type": AUTHORIZATION_SNAPSHOT_TYPE,
        "candidate_id": plan.candidate_id,
        "as_of_date": plan.as_of_date,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "params_hash": plan.params_hash,
        "targets": list(plan.targets),
        "target_position_count": int(plan.target_position_count),
        "target_exposure": plan.target_exposure,
        "base_target_exposure": plan.base_target_exposure,
        "risk_off": bool(plan.risk_off),
        "gross_exposure_after": plan.gross_exposure_after,
        "max_order_notional": plan.max_order_notional,
        "order_count": len(plan.orders),
        "position_quantities_before": _starting_position_quantities(plan),
        "target_quantities_after": _expected_position_quantities(plan),
    }
    if isinstance(portfolio_drawdown_guard, dict):
        snapshot["portfolio_drawdown_guard"] = dict(portfolio_drawdown_guard)
    if readiness_audit:
        snapshot["readiness_audit"] = {
            "generated_at": readiness_audit.get("generated_at"),
            "ready_for_cap_approval": bool(readiness_audit.get("ready_for_cap_approval")),
            "ready_for_capped_pilot": bool(readiness_audit.get("ready_for_capped_pilot")),
            "blocking_reasons": list(readiness_audit.get("blocking_reasons") or []),
            "warning_reasons": list(readiness_audit.get("warning_reasons") or []),
            "execution_trade_day_check": readiness_audit.get("execution_trade_day_check"),
            "execution_market_session_check": readiness_audit.get("execution_market_session_check"),
        }
    return snapshot


def _auth_payload_from_pilot_check(pilot_check: Any) -> dict[str, Any] | None:
    auth = getattr(pilot_check, "auth", None)
    if auth is None:
        return None
    if isinstance(auth, dict):
        return auth
    try:
        return asdict(auth)
    except TypeError:
        payload = vars(auth) if hasattr(auth, "__dict__") else None
        return dict(payload) if isinstance(payload, dict) else None


def _snapshot_from_pilot_check(pilot_check: Any) -> tuple[dict[str, Any] | None, bool]:
    auth_payload = _auth_payload_from_pilot_check(pilot_check)
    caps_snapshot = getattr(pilot_check, "caps_snapshot", None) or {}
    snapshot = None
    if isinstance(auth_payload, dict):
        snapshot = auth_payload.get("target_weight_plan_snapshot")
    if snapshot is None and isinstance(caps_snapshot, dict):
        snapshot = caps_snapshot.get("target_weight_plan_snapshot")
    return snapshot, auth_payload is not None


def _normalized_quantities(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, Any] = {}
    for symbol, quantity in raw.items():
        key = normalize_symbol(symbol)
        if isinstance(quantity, bool):
            normalized[key] = quantity
            continue
        try:
            normalized[key] = int(quantity)
        except (TypeError, ValueError):
            normalized[key] = quantity
    return normalized


AUTHORIZATION_SNAPSHOT_MONEY_TOLERANCE_KRW = 10_000.0
AUTHORIZATION_SNAPSHOT_MONEY_TOLERANCE_PCT = 0.005
PORTFOLIO_DRAWDOWN_GUARD_AUTHORIZATION_FIELDS = (
    "enabled",
    "active",
    "triggered",
    "trigger_pct",
    "exposure_pct",
    "cooldown_rebalances",
    "cooldown_before",
    "cooldown_after_trigger",
    "cooldown_after_plan",
    "drawdown_pct",
    "last_equity_value",
    "peak_value",
    "target_exposure_before",
    "target_exposure_after",
)


def _authorization_portfolio_drawdown_guard(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    return {
        field: raw.get(field)
        for field in PORTFOLIO_DRAWDOWN_GUARD_AUTHORIZATION_FIELDS
        if field in raw
    }


def _numbers_match(actual: Any, expected: Any, *, absolute_tolerance: float | None = None) -> bool:
    try:
        actual_num = float(actual)
        expected_num = float(expected)
    except (TypeError, ValueError):
        return actual == expected
    tolerance = max(1e-6, abs(expected_num) * 1e-9)
    if absolute_tolerance is not None:
        tolerance = max(tolerance, float(absolute_tolerance))
    return abs(actual_num - expected_num) <= tolerance


def _authorization_snapshot_money_tolerance(expected: Any) -> float:
    try:
        expected_num = abs(float(expected))
    except (TypeError, ValueError):
        expected_num = 0.0
    return max(
        AUTHORIZATION_SNAPSHOT_MONEY_TOLERANCE_KRW,
        expected_num * AUTHORIZATION_SNAPSHOT_MONEY_TOLERANCE_PCT,
    )


def validate_pilot_authorization_snapshot(
    plan: TargetWeightPlan,
    pilot_check: Any,
) -> dict[str, Any]:
    if not getattr(pilot_check, "allowed", False):
        return _authorization_snapshot_not_required(
            "pilot authorization snapshot not checked because pilot entry is blocked"
        )

    snapshot, auth_present = _snapshot_from_pilot_check(pilot_check)
    if snapshot is None:
        if not auth_present:
            return _authorization_snapshot_not_required(
                "pilot authorization snapshot not available on synthetic pilot check"
            )
        return {
            "checked": True,
            "allowed": False,
            "complete": False,
            "reason": (
                "target_weight_pilot_authorization_snapshot_missing: "
                "re-enable pilot caps after readiness audit so approval is tied to the current plan"
            ),
            "mismatches": [
                {
                    "field": "target_weight_plan_snapshot",
                    "expected": "present",
                    "actual": None,
                }
            ],
        }
    if not isinstance(snapshot, dict):
        return {
            "checked": True,
            "allowed": False,
            "complete": False,
            "reason": "target_weight_pilot_authorization_snapshot_invalid: snapshot is not an object",
            "mismatches": [
                {
                    "field": "target_weight_plan_snapshot",
                    "expected": "object",
                    "actual": type(snapshot).__name__,
                }
            ],
        }

    expected = build_pilot_authorization_snapshot(plan)
    checks: list[tuple[str, Any, Any]] = [
        ("schema_version", snapshot.get("schema_version"), expected["schema_version"]),
        ("snapshot_type", snapshot.get("snapshot_type"), expected["snapshot_type"]),
        ("candidate_id", snapshot.get("candidate_id"), expected["candidate_id"]),
        ("as_of_date", snapshot.get("as_of_date"), expected["as_of_date"]),
        ("trade_day", snapshot.get("trade_day"), expected["trade_day"]),
        ("score_day", snapshot.get("score_day"), expected["score_day"]),
        ("params_hash", snapshot.get("params_hash"), expected["params_hash"]),
        ("risk_off", snapshot.get("risk_off"), expected["risk_off"]),
        (
            "targets",
            [normalize_symbol(symbol) for symbol in snapshot.get("targets", [])],
            [normalize_symbol(symbol) for symbol in plan.targets],
        ),
        ("target_position_count", snapshot.get("target_position_count"), expected["target_position_count"]),
        ("order_count", snapshot.get("order_count"), expected["order_count"]),
        (
            "position_quantities_before",
            _normalized_quantities(snapshot.get("position_quantities_before")),
            expected["position_quantities_before"],
        ),
        (
            "target_quantities_after",
            _normalized_quantities(snapshot.get("target_quantities_after")),
            expected["target_quantities_after"],
        ),
    ]
    if "portfolio_drawdown_guard" in expected or "portfolio_drawdown_guard" in snapshot:
        checks.append((
            "portfolio_drawdown_guard",
            _authorization_portfolio_drawdown_guard(snapshot.get("portfolio_drawdown_guard")),
            _authorization_portfolio_drawdown_guard(expected.get("portfolio_drawdown_guard")),
        ))
    mismatches = [
        {"field": field, "expected": expected_value, "actual": actual_value}
        for field, actual_value, expected_value in checks
        if actual_value != expected_value
    ]
    numeric_checks = [
        ("target_exposure", snapshot.get("target_exposure"), expected["target_exposure"], None),
        ("base_target_exposure", snapshot.get("base_target_exposure"), expected["base_target_exposure"], None),
        (
            "gross_exposure_after",
            snapshot.get("gross_exposure_after"),
            expected["gross_exposure_after"],
            _authorization_snapshot_money_tolerance(expected["gross_exposure_after"]),
        ),
        (
            "max_order_notional",
            snapshot.get("max_order_notional"),
            expected["max_order_notional"],
            _authorization_snapshot_money_tolerance(expected["max_order_notional"]),
        ),
    ]
    for field, actual_value, expected_value, absolute_tolerance in numeric_checks:
        if not _numbers_match(actual_value, expected_value, absolute_tolerance=absolute_tolerance):
            mismatches.append({
                "field": field,
                "expected": expected_value,
                "actual": actual_value,
                "tolerance": absolute_tolerance,
            })

    if mismatches:
        preview = ", ".join(
            f"{item['field']} actual={item['actual']} expected={item['expected']}"
            for item in mismatches[:5]
        )
        if len(mismatches) > 5:
            preview = f"{preview}, +{len(mismatches) - 5} more"
        return {
            "checked": True,
            "allowed": False,
            "complete": False,
            "reason": f"target_weight_pilot_authorization_snapshot_mismatch: {preview}",
            "mismatches": mismatches,
            "authorized_snapshot": snapshot,
            "current_snapshot": expected,
        }

    return {
        "checked": True,
        "allowed": True,
        "complete": True,
        "reason": "target-weight pilot authorization snapshot matches current plan",
        "mismatches": [],
        "authorized_snapshot": snapshot,
        "current_snapshot": expected,
    }


def _recent_weekday_dates(end_date: str, count: int) -> list[str]:
    if count <= 0:
        raise ValueError("shadow days must be positive")
    day = datetime.strptime(end_date, "%Y-%m-%d").date()
    dates: list[str] = []
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y-%m-%d"))
        day -= timedelta(days=1)
    return list(reversed(dates))


def resolve_shadow_batch_range(
    *,
    shadow_start_date: str | None = None,
    shadow_end_date: str | None = None,
    shadow_days: int | None = None,
    today: str | None = None,
) -> tuple[str, str, list[str]]:
    if shadow_days is not None:
        if shadow_start_date is not None:
            raise ValueError("--shadow-days cannot be combined with --shadow-start-date")
        shadow_day_count = int(shadow_days)
        if shadow_day_count <= 0:
            raise ValueError("--shadow-days must be positive")
        end_date = shadow_end_date or today or datetime.now().strftime("%Y-%m-%d")
        dates = _recent_weekday_dates(end_date, shadow_day_count)
        return dates[0], dates[-1], dates

    if shadow_start_date is None or shadow_end_date is None:
        raise ValueError("--shadow-start-date and --shadow-end-date must be provided together")
    dates = _date_range(shadow_start_date, shadow_end_date)
    return shadow_start_date, shadow_end_date, dates


def _load_symbols(config: Any, raw_symbols: str | None) -> list[str]:
    explicit = _split_symbols(raw_symbols)
    if explicit:
        return explicit
    from core.watchlist_manager import WatchlistManager

    return WatchlistManager(config).resolve()


def _load_positions(account_key: str) -> dict[str, Any]:
    from database.repositories import get_all_positions

    return {pos.symbol: pos for pos in get_all_positions(account_key=account_key)}


def _portfolio_cash(config: Any, account_key: str, cash_override: float | None) -> float:
    if cash_override is not None:
        return float(cash_override)

    from core.portfolio_manager import PortfolioManager

    summary = PortfolioManager(config, account_key=account_key).get_portfolio_summary()
    return float(summary.get("cash", 0.0))


def _require_actual_paper_cash(cash_override: float | None, *, context: str) -> None:
    if cash_override is None:
        return
    raise ValueError(
        "target_weight_cash_override_blocked: "
        f"--cash cannot be used for {context}; use actual paper account cash"
    )


def _pilot_check_to_dict(pilot_check: Any) -> dict[str, Any]:
    try:
        return asdict(pilot_check)
    except TypeError:
        return {
            "allowed": getattr(pilot_check, "allowed", False),
            "reason": getattr(pilot_check, "reason", ""),
            "auth": getattr(pilot_check, "auth", None),
            "remaining_orders": getattr(pilot_check, "remaining_orders", None),
            "remaining_exposure": getattr(pilot_check, "remaining_exposure", None),
            "caps_snapshot": getattr(pilot_check, "caps_snapshot", None),
        }


def build_preview_caps(
    *,
    max_orders: int | None = None,
    max_positions: int | None = None,
    max_notional: int | None = None,
    max_exposure: int | None = None,
) -> dict[str, int]:
    caps = dict(DEFAULT_PILOT_PREVIEW_CAPS)
    if max_orders is not None:
        caps["max_orders_per_day"] = int(max_orders)
    if max_positions is not None:
        caps["max_concurrent_positions"] = int(max_positions)
    if max_notional is not None:
        caps["max_notional_per_trade"] = int(max_notional)
    if max_exposure is not None:
        caps["max_gross_exposure"] = int(max_exposure)
    return caps


def preview_plan_against_caps(plan: TargetWeightPlan, caps: dict[str, int] | None = None) -> Any:
    from core.paper_pilot import PilotCheckResult

    caps = dict(caps or DEFAULT_PILOT_PREVIEW_CAPS)
    synthetic_check = PilotCheckResult(
        allowed=True,
        reason="proposed pilot caps",
        remaining_orders=int(caps["max_orders_per_day"]),
        remaining_exposure=int(caps["max_gross_exposure"]),
        caps_snapshot=caps,
    )
    return validate_plan_against_pilot(plan, synthetic_check)


def _round_up_to_step(value: float, *, step: int = DEFAULT_CAP_ROUNDING_STEP) -> int:
    if value <= 0:
        return 0
    whole_value = int(math.ceil(value))
    return ((whole_value + step - 1) // step) * step


def _minimum_money_cap(value: float, *, step: int = DEFAULT_CAP_ROUNDING_STEP) -> int:
    return max(1, _round_up_to_step(value, step=step))


def _format_enable_command(
    plan: TargetWeightPlan,
    caps: dict[str, int],
    *,
    valid_to: str | None = None,
    target_pilot_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
) -> str:
    pilot_valid_to = valid_to or _pilot_valid_to(plan.trade_day, target_pilot_days)
    parts = [
        "python tools/paper_pilot_control.py",
        f"--strategy {plan.candidate_id}",
        "--enable",
        f"--from {plan.trade_day}",
        f"--to {pilot_valid_to}",
        f"--max-orders {caps['max_orders_per_day']}",
        f"--max-positions {caps['max_concurrent_positions']}",
        f"--max-notional {caps['max_notional_per_trade']}",
        f"--max-exposure {caps['max_gross_exposure']}",
        '--reason "target-weight shadow dry-run matched suggested pilot caps"',
    ]
    return " ".join(parts)


def recommend_pilot_caps(
    plan: TargetWeightPlan,
    *,
    buffer_pct: float = DEFAULT_CAP_BUFFER_PCT,
    rounding_step: int = DEFAULT_CAP_ROUNDING_STEP,
    target_pilot_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
) -> dict[str, Any]:
    """Build plan-specific pilot caps that are tight enough for first execution."""
    minimum_caps = {
        "max_orders_per_day": max(1, len(plan.orders)),
        "max_concurrent_positions": max(1, int(plan.target_position_count)),
        "max_notional_per_trade": max(1, _round_up_to_step(plan.max_order_notional, step=rounding_step)),
        "max_gross_exposure": max(1, _round_up_to_step(plan.gross_exposure_after, step=rounding_step)),
    }
    suggested_caps = {
        "max_orders_per_day": minimum_caps["max_orders_per_day"],
        "max_concurrent_positions": minimum_caps["max_concurrent_positions"],
        "max_notional_per_trade": _minimum_money_cap(plan.max_order_notional * (1 + buffer_pct), step=rounding_step),
        "max_gross_exposure": _minimum_money_cap(plan.gross_exposure_after * (1 + buffer_pct), step=rounding_step),
    }
    suggested_preview = preview_plan_against_caps(plan, suggested_caps)
    pilot_valid_to = _pilot_valid_to(plan.trade_day, target_pilot_days)
    return {
        "minimum_caps": minimum_caps,
        "suggested_caps": suggested_caps,
        "buffer_pct": buffer_pct,
        "rounding_step": rounding_step,
        "valid_from": plan.trade_day,
        "valid_to": pilot_valid_to,
        "target_pilot_days": int(target_pilot_days),
        "planned_orders": len(plan.orders),
        "target_position_count": int(plan.target_position_count),
        "max_order_notional": plan.max_order_notional,
        "gross_exposure_after": plan.gross_exposure_after,
        "suggested_preview": asdict(suggested_preview),
        "enable_command": _format_enable_command(
            plan,
            suggested_caps,
            valid_to=pilot_valid_to,
            target_pilot_days=target_pilot_days,
        ),
        "operator_note": (
            "Use suggested caps for the first capped paper pilot only after launch readiness "
            "requirements and preflight checks pass. The caps are based on the dry-run plan and "
            "do not imply live eligibility."
        ),
    }


def _portfolio_drawdown_guard_enabled(params: dict[str, Any]) -> bool:
    return max(
        0.0,
        float(params.get("portfolio_drawdown_guard_trigger_pct", 0.0) or 0.0),
    ) > 0


def _record_total_value(record: dict[str, Any]) -> float | None:
    for key in ("total_value", "portfolio_value"):
        value = record.get(key)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
    return None


def _record_portfolio_drawdown_guard(record: dict[str, Any]) -> dict[str, Any]:
    caps = record.get("pilot_caps_snapshot") or {}
    plan = caps.get("target_weight_plan") or {}
    guard = plan.get("portfolio_drawdown_guard") or {}
    return guard if isinstance(guard, dict) else {}


def load_portfolio_drawdown_guard_state(
    candidate_id: str,
    *,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Build explicit state for the stateful target-weight portfolio drawdown guard."""
    from core.paper_evidence import get_canonical_records

    cutoff = None
    if as_of_date:
        cutoff = datetime.strptime(str(as_of_date), "%Y-%m-%d").date()

    values: list[float] = []
    latest_record_date: str | None = None
    cooldown_remaining = 0
    records = sorted(get_canonical_records(candidate_id), key=lambda rec: str(rec.get("date") or ""))
    for record in records:
        record_date_raw = record.get("date")
        if not record_date_raw:
            continue
        try:
            record_date = datetime.strptime(str(record_date_raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff is not None and record_date >= cutoff:
            continue

        total_value = _record_total_value(record)
        if total_value is not None:
            values.append(total_value)
            latest_record_date = record_date.strftime("%Y-%m-%d")

        guard = _record_portfolio_drawdown_guard(record)
        if guard:
            try:
                cooldown_remaining = max(
                    0,
                    int(guard.get("cooldown_after_plan", cooldown_remaining) or 0),
                )
            except (TypeError, ValueError):
                cooldown_remaining = 0

    if not values:
        return {
            "source": "cold_start",
            "record_count": 0,
            "latest_record_date": None,
            "peak_value": None,
            "last_evidence_value": None,
            "cooldown_remaining": 0,
        }

    return {
        "source": "paper_evidence",
        "record_count": len(values),
        "latest_record_date": latest_record_date,
        "peak_value": max(values),
        "last_equity_value": values[-1],
        "last_evidence_value": values[-1],
        "cooldown_remaining": cooldown_remaining,
    }


def build_plan(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    as_of_date: str | None = None,
    cash: float | None = None,
    config: Any | None = None,
    collector: Any | None = None,
) -> TargetWeightPlan:
    from config.config_loader import Config

    config = config or Config.get()
    spec = load_canonical_target_weight_spec(candidate_id)
    symbols = _load_symbols(config, raw_symbols)
    positions = _load_positions(candidate_id)
    plan_cash = _portfolio_cash(config, candidate_id, cash)
    portfolio_drawdown_guard_state = (
        load_portfolio_drawdown_guard_state(candidate_id, as_of_date=as_of_date)
        if _portfolio_drawdown_guard_enabled(spec.params)
        else None
    )
    return build_target_weight_plan(
        candidate_id=candidate_id,
        symbols=symbols,
        params=spec.params,
        cash=plan_cash,
        positions=positions,
        as_of_date=as_of_date,
        collector=collector,
        portfolio_drawdown_guard_state=portfolio_drawdown_guard_state,
    )


def execute_plan(
    plan: TargetWeightPlan,
    *,
    config: Any | None = None,
    dry_run: bool = True,
    stop_on_failure: bool = True,
    pilot_validation: Any | None = None,
    preflight_refresh: dict[str, Any] | None = None,
    execution_trade_day_check: dict[str, Any] | None = None,
    execution_market_session_check: dict[str, Any] | None = None,
    pilot_authorization_snapshot_check: dict[str, Any] | None = None,
    pre_execution_reconciliation: dict[str, Any] | None = None,
    execution_idempotency: dict[str, Any] | None = None,
    liquidity_check: dict[str, Any] | None = None,
    pre_trade_risk_check: dict[str, Any] | None = None,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    allow_rerun: bool = False,
    execution_session_id: str | None = None,
) -> dict[str, Any]:
    from config.config_loader import Config

    config = config or Config.get()
    if config.trading.get("mode") == "live":
        raise ValueError("target-weight pilot adapter refuses live mode")

    results = {
        "executed": 0,
        "skipped": 0,
        "failed": 0,
        "halted": False,
        "halt_reason": "",
        "details": [],
        "execution_session_id": execution_session_id or "",
    }
    if dry_run:
        for order in plan.orders:
            results["skipped"] += 1
            results["details"].append({
                "order": asdict(order),
                "status": "dry_run",
            })
        return results

    if pilot_validation is None:
        return blocked_execution_for_pilot_validation(
            plan,
            SimpleNamespace(
                allowed=False,
                reason=(
                    "target_weight_pilot_validation_required: "
                    "execute_plan requires run_pilot validation before order submission"
                ),
            ),
        )
    if not getattr(pilot_validation, "allowed", False):
        return blocked_execution_for_pilot_validation(plan, pilot_validation)

    if preflight_refresh is None:
        return blocked_execution_for_preflight_refresh(
            plan,
            {
                "checked": False,
                "complete": False,
                "reason": (
                    "target_weight_preflight_refresh_required: "
                    "execute_plan requires refreshed paper preflight before order submission"
                ),
            },
        )
    if not preflight_refresh.get("complete", False):
        return blocked_execution_for_preflight_refresh(plan, preflight_refresh)

    if execution_trade_day_check is None:
        return blocked_execution_for_trade_day_mismatch(
            plan,
            {
                "checked": False,
                "allowed": False,
                "complete": False,
                "reason": (
                    "target_weight_execution_trade_day_check_required: "
                    "execute_plan requires current trade-day validation before order submission"
                ),
            },
        )
    if not execution_trade_day_check.get("allowed", False):
        return blocked_execution_for_trade_day_mismatch(plan, execution_trade_day_check)

    if execution_market_session_check is None:
        return blocked_execution_for_market_session(
            plan,
            {
                "checked": False,
                "allowed": False,
                "complete": False,
                "reason": (
                    "target_weight_execution_market_session_check_required: "
                    "execute_plan requires market-session validation before order submission"
                ),
            },
        )
    if not execution_market_session_check.get("allowed", False):
        return blocked_execution_for_market_session(plan, execution_market_session_check)

    if pilot_authorization_snapshot_check is None:
        return blocked_execution_for_authorization_snapshot_mismatch(
            plan,
            {
                "checked": False,
                "allowed": False,
                "complete": False,
                "reason": (
                    "target_weight_pilot_authorization_snapshot_check_required: "
                    "execute_plan requires pilot authorization snapshot validation before order submission"
                ),
                "mismatches": [],
            },
        )
    if not pilot_authorization_snapshot_check.get("allowed", False):
        return blocked_execution_for_authorization_snapshot_mismatch(
            plan,
            pilot_authorization_snapshot_check,
        )

    if execution_idempotency is None:
        execution_idempotency = check_execution_idempotency(
            plan,
            allow_rerun=allow_rerun,
        )
    if not execution_idempotency["allowed"]:
        return blocked_execution_for_duplicate_execution(plan, execution_idempotency)

    if pre_execution_reconciliation is None:
        pre_execution_reconciliation = load_starting_position_reconciliation(plan)
    if not pre_execution_reconciliation["complete"]:
        return blocked_execution_for_pre_execution_drift(plan, pre_execution_reconciliation)

    if liquidity_check is None:
        try:
            liquidity_check = assess_plan_liquidity(
                plan,
                max_order_adv_pct=max_order_adv_pct,
            )
        except Exception as exc:
            logger.exception("target-weight liquidity preflight failed for {}", plan.candidate_id)
            liquidity_check = failed_liquidity_preflight(
                plan,
                exc,
                max_order_adv_pct=max_order_adv_pct,
            )
    if liquidity_check is not None and not liquidity_check.get("complete", False):
        return blocked_execution_for_liquidity(plan, liquidity_check)

    if pre_trade_risk_check is None:
        try:
            pre_trade_risk_check = assess_plan_pre_trade_risk(plan, config=config)
        except Exception as exc:
            logger.exception("target-weight pre-trade risk validation failed for {}", plan.candidate_id)
            pre_trade_risk_check = failed_pre_trade_risk_validation(plan, exc)
    if pre_trade_risk_check is not None and not pre_trade_risk_check.get("complete", False):
        return blocked_execution_for_pre_trade_risk(plan, pre_trade_risk_check)

    pre_execution_reconciliation = load_starting_position_reconciliation(plan)
    if not pre_execution_reconciliation["complete"]:
        return blocked_execution_for_pre_execution_drift(plan, pre_execution_reconciliation)
    results["pre_execution_reconciliation"] = pre_execution_reconciliation

    from core.order_executor import OrderExecutor
    from core.portfolio_manager import PortfolioManager

    execution_session_id = execution_session_id or make_execution_session_id(plan)
    results["execution_session_id"] = execution_session_id
    executor = OrderExecutor(config, account_key=plan.candidate_id)
    portfolio = PortfolioManager(config, account_key=plan.candidate_id)

    halt_reason = ""
    for order in plan.orders:
        if stop_on_failure and halt_reason:
            results["skipped"] += 1
            results["details"].append({
                "order": asdict(order),
                "status": "skipped_after_failure",
                "reason": halt_reason,
            })
            continue

        try:
            if order.action == "SELL":
                avg_daily_volume = _avg_daily_volume_for_order(plan, order)
                res = executor.execute_sell(
                    symbol=order.symbol,
                    price=order.price,
                    quantity=order.quantity,
                    reason=order.reason,
                    strategy=plan.candidate_id,
                    avg_daily_volume=avg_daily_volume,
                    execution_session_id=execution_session_id,
                )
            else:
                avg_daily_volume = _avg_daily_volume_for_order(plan, order)
                available_cash = portfolio.get_available_cash()
                total_value = portfolio.get_current_capital()
                res = executor.execute_buy_quantity(
                    symbol=order.symbol,
                    price=order.price,
                    quantity=order.quantity,
                    capital=total_value,
                    available_cash=available_cash,
                    reason=order.reason,
                    strategy=plan.candidate_id,
                    avg_daily_volume=avg_daily_volume,
                    execution_session_id=execution_session_id,
                )
            status = "success" if res.get("success") else "failed"
            if res.get("success"):
                results["executed"] += 1
            else:
                results["failed"] += 1
                if stop_on_failure:
                    halt_reason = f"{order.action} {order.symbol} failed: {res.get('reason', 'unknown')}"
                    results["halted"] = True
                    results["halt_reason"] = halt_reason
            results["details"].append({
                "order": asdict(order),
                "status": status,
                "result": res,
            })
        except Exception as exc:
            logger.exception("target-weight order failed: {}", order.symbol)
            results["failed"] += 1
            if stop_on_failure:
                halt_reason = f"{order.action} {order.symbol} exception: {exc}"
                results["halted"] = True
                results["halt_reason"] = halt_reason
            results["details"].append({
                "order": asdict(order),
                "status": "exception",
                "error": str(exc),
            })

    return results


def _execution_reached_order_submission(execution: dict[str, Any]) -> bool:
    if int(execution.get("executed") or 0) > 0:
        return True
    if int(execution.get("failed") or 0) > 0:
        return True
    return any(
        detail.get("status") in {"success", "failed", "exception"}
        for detail in execution.get("details", [])
        if isinstance(detail, dict)
    )


def _position_quantity(position: Any) -> int:
    if position is None:
        return 0
    if isinstance(position, dict):
        return int(position.get("quantity", 0) or 0)
    return int(getattr(position, "quantity", 0) or 0)


def _expected_position_quantities(plan: TargetWeightPlan) -> dict[str, int]:
    if hasattr(plan, "expected_position_quantities"):
        raw_expected = dict(plan.expected_position_quantities)
    else:
        raw_expected = {order.symbol: int(order.target_quantity) for order in plan.orders}
    expected: dict[str, int] = {}
    for raw_symbol, quantity in raw_expected.items():
        expected[normalize_symbol(raw_symbol)] = int(quantity)
    return dict(sorted(expected.items()))


def _starting_position_quantities(plan: TargetWeightPlan) -> dict[str, int]:
    if hasattr(plan, "starting_position_quantities"):
        raw_expected = dict(plan.starting_position_quantities)
    else:
        raw_expected = {
            order.symbol: int(order.current_quantity)
            for order in plan.orders
            if int(order.current_quantity) > 0
        }
    expected: dict[str, int] = {}
    for raw_symbol, quantity in raw_expected.items():
        expected[normalize_symbol(raw_symbol)] = int(quantity)
    return dict(sorted(expected.items()))


def _actual_position_quantities(positions: dict[str, Any] | None) -> dict[str, int]:
    actual: dict[str, int] = {}
    for raw_symbol, position in (positions or {}).items():
        symbol = normalize_symbol(raw_symbol)
        actual[symbol] = actual.get(symbol, 0) + _position_quantity(position)
    return actual


def check_execution_idempotency(
    plan: TargetWeightPlan,
    *,
    allow_rerun: bool = False,
) -> dict[str, Any]:
    from core.paper_pilot import load_pilot_session_artifact, pilot_session_artifact_path

    artifact_path = pilot_session_artifact_path(plan.candidate_id, plan.trade_day)
    lock_path = artifact_path.with_suffix(".lock")
    result = {
        "checked": True,
        "allowed": True,
        "reason": "no prior target-weight pilot execution session",
        "allow_rerun": bool(allow_rerun),
        "artifact_path": str(artifact_path),
        "lock_path": str(lock_path),
        "previous_session_found": False,
        "execution_lock_found": False,
    }
    if lock_path.exists():
        lock_payload = None
        try:
            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            lock_payload = {"unreadable": True}
        return {
            **result,
            "allowed": False,
            "reason": (
                "target_weight_execution_lock_present: "
                f"existing in-progress pilot lock for {plan.candidate_id} {plan.trade_day}"
            ),
            "execution_lock_found": True,
            "execution_lock": lock_payload,
        }
    try:
        artifact = load_pilot_session_artifact(plan.candidate_id, plan.trade_day)
    except Exception as exc:
        return {
            **result,
            "allowed": False,
            "reason": f"target_weight_execution_idempotency_check_failed: {exc}",
            "previous_session_found": True,
        }

    if artifact is None:
        return result

    pilot_session = artifact.get("pilot_session", {}) if isinstance(artifact, dict) else {}
    result.update({
        "previous_session_found": True,
        "previous_generated_at": artifact.get("generated_at") if isinstance(artifact, dict) else None,
        "previous_execution_complete": pilot_session.get("execution_complete"),
        "previous_orders_planned": pilot_session.get("orders_planned"),
        "previous_orders_executed": pilot_session.get("orders_executed"),
        "previous_order_submission_reached": pilot_session.get("order_submission_reached"),
        "previous_failed_orders": (pilot_session.get("target_weight_execution") or {}).get("failed_orders"),
        "previous_halted": (pilot_session.get("target_weight_execution") or {}).get("halted"),
    })
    if allow_rerun:
        previous_complete = bool(pilot_session.get("execution_complete", False))
        previous_orders_executed = int(pilot_session.get("orders_executed") or 0)
        previous_order_submission_reached = bool(
            pilot_session.get("order_submission_reached", False)
        )
        target_execution = pilot_session.get("target_weight_execution") or {}
        previous_failed_orders = int(target_execution.get("failed_orders") or 0)
        previous_halted = bool(target_execution.get("halted", False))
        if previous_complete and previous_orders_executed > 0:
            return {
                **result,
                "allowed": False,
                "reason": (
                    "target_weight_completed_execution_rerun_blocked: "
                    f"existing completed pilot session for {plan.candidate_id} {plan.trade_day}"
                ),
            }
        if (
            previous_orders_executed > 0
            or previous_order_submission_reached
            or previous_failed_orders > 0
            or previous_halted
        ):
            return {
                **result,
                "allowed": False,
                "reason": (
                    "target_weight_unsafe_execution_rerun_blocked: "
                    f"existing pilot session reached or may have reached orders for "
                    f"{plan.candidate_id} {plan.trade_day}"
                ),
            }
        return {
            **result,
            "allowed": True,
            "reason": "operator allowed incomplete target-weight pilot rerun",
        }
    return {
        **result,
        "allowed": False,
        "reason": (
            "target_weight_duplicate_execution_attempt: "
            f"existing pilot session artifact for {plan.candidate_id} {plan.trade_day}"
        ),
    }


def acquire_execution_lock(
    plan: TargetWeightPlan,
    *,
    execution_session_id: str,
) -> dict[str, Any]:
    from core.paper_pilot import pilot_session_artifact_path

    artifact_path = pilot_session_artifact_path(plan.candidate_id, plan.trade_day)
    lock_path = artifact_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy": plan.candidate_id,
        "date": plan.trade_day,
        "execution_session_id": execution_session_id,
        "params_hash": plan.params_hash,
        "created_at": datetime.now().isoformat(),
        "artifact_path": str(artifact_path),
    }
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        lock_payload = None
        try:
            lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            lock_payload = {"unreadable": True}
        return {
            "checked": True,
            "allowed": False,
            "acquired": False,
            "reason": (
                "target_weight_execution_lock_present: "
                f"existing in-progress pilot lock for {plan.candidate_id} {plan.trade_day}"
            ),
            "path": str(lock_path),
            "existing_lock": lock_payload,
        }
    except Exception as exc:
        return {
            "checked": True,
            "allowed": False,
            "acquired": False,
            "reason": f"target_weight_execution_lock_failed: {exc}",
            "path": str(lock_path),
        }

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return {
        "checked": True,
        "allowed": True,
        "acquired": True,
        "reason": "target_weight_execution_lock_acquired",
        "path": str(lock_path),
        "payload": payload,
    }


def release_execution_lock(lock: dict[str, Any] | None) -> dict[str, Any]:
    if not lock or not lock.get("acquired"):
        return {
            "checked": False,
            "released": False,
            "reason": "execution lock was not acquired",
        }
    path = Path(str(lock.get("path") or ""))
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        return {
            "checked": True,
            "released": False,
            "reason": f"target_weight_execution_lock_release_failed: {exc}",
            "path": str(path),
        }
    return {
        "checked": True,
        "released": True,
        "reason": "target_weight_execution_lock_released",
        "path": str(path),
    }


def reconcile_plan_positions(plan: TargetWeightPlan, positions: dict[str, Any] | None) -> dict[str, Any]:
    expected = _expected_position_quantities(plan)
    actual_all = _actual_position_quantities(positions)
    actual = {symbol: actual_all.get(symbol, 0) for symbol in expected}
    mismatches = [
        {
            "symbol": symbol,
            "target_quantity": target_quantity,
            "actual_quantity": actual.get(symbol, 0),
        }
        for symbol, target_quantity in expected.items()
        if actual.get(symbol, 0) != target_quantity
    ]
    unexpected_positions = [
        {
            "symbol": symbol,
            "actual_quantity": quantity,
        }
        for symbol, quantity in sorted(actual_all.items())
        if symbol not in expected and quantity > 0
    ]
    actual_quantities = dict(sorted(actual.items()))
    actual_quantities.update({
        symbol: quantity
        for symbol, quantity in sorted(actual_all.items())
        if symbol not in expected and quantity > 0
    })
    complete = len(mismatches) == 0 and len(unexpected_positions) == 0
    reason = "post-execution positions match target-weight plan"
    if not complete:
        reason_parts = []
        if mismatches:
            mismatch_text = ", ".join(
                f"{item['symbol']} actual={item['actual_quantity']} target={item['target_quantity']}"
                for item in mismatches
            )
            reason_parts.append(f"mismatches: {mismatch_text}")
        if unexpected_positions:
            unexpected_text = ", ".join(
                f"{item['symbol']} actual={item['actual_quantity']}"
                for item in unexpected_positions
            )
            reason_parts.append(f"unexpected: {unexpected_text}")
        reason = f"target_weight_position_mismatch: {'; '.join(reason_parts)}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "expected_quantities": expected,
        "actual_quantities": actual_quantities,
        "mismatches": mismatches,
        "unexpected_positions": unexpected_positions,
    }


def reconcile_plan_starting_positions(
    plan: TargetWeightPlan,
    positions: dict[str, Any] | None,
) -> dict[str, Any]:
    expected = _starting_position_quantities(plan)
    actual_all = _actual_position_quantities(positions)
    actual = {symbol: actual_all.get(symbol, 0) for symbol in expected}
    mismatches = [
        {
            "symbol": symbol,
            "expected_quantity": expected_quantity,
            "actual_quantity": actual.get(symbol, 0),
        }
        for symbol, expected_quantity in expected.items()
        if actual.get(symbol, 0) != expected_quantity
    ]
    unexpected_positions = [
        {
            "symbol": symbol,
            "actual_quantity": quantity,
        }
        for symbol, quantity in sorted(actual_all.items())
        if symbol not in expected and quantity > 0
    ]
    actual_quantities = dict(sorted(actual.items()))
    actual_quantities.update({
        symbol: quantity
        for symbol, quantity in sorted(actual_all.items())
        if symbol not in expected and quantity > 0
    })
    complete = len(mismatches) == 0 and len(unexpected_positions) == 0
    reason = "pre-execution positions match target-weight plan inputs"
    if not complete:
        reason_parts = []
        if mismatches:
            mismatch_text = ", ".join(
                (
                    f"{item['symbol']} actual={item['actual_quantity']} "
                    f"expected={item['expected_quantity']}"
                )
                for item in mismatches
            )
            reason_parts.append(f"mismatches: {mismatch_text}")
        if unexpected_positions:
            unexpected_text = ", ".join(
                f"{item['symbol']} actual={item['actual_quantity']}"
                for item in unexpected_positions
            )
            reason_parts.append(f"unexpected: {unexpected_text}")
        reason = f"target_weight_pre_execution_position_drift: {'; '.join(reason_parts)}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "expected_quantities": expected,
        "actual_quantities": actual_quantities,
        "mismatches": mismatches,
        "unexpected_positions": unexpected_positions,
    }


def failed_position_reconciliation(plan: TargetWeightPlan, error: Exception) -> dict[str, Any]:
    expected = _expected_position_quantities(plan)
    return {
        "checked": True,
        "complete": False,
        "reason": f"target_weight_position_reconciliation_failed: {error}",
        "expected_quantities": expected,
        "actual_quantities": {},
        "mismatches": [
            {
                "symbol": symbol,
                "target_quantity": target_quantity,
                "actual_quantity": None,
            }
            for symbol, target_quantity in expected.items()
        ],
        "unexpected_positions": [],
    }


def failed_starting_position_reconciliation(plan: TargetWeightPlan, error: Exception) -> dict[str, Any]:
    expected = _starting_position_quantities(plan)
    return {
        "checked": True,
        "complete": False,
        "reason": f"target_weight_pre_execution_reconciliation_failed: {error}",
        "expected_quantities": expected,
        "actual_quantities": {},
        "mismatches": [
            {
                "symbol": symbol,
                "expected_quantity": expected_quantity,
                "actual_quantity": None,
            }
            for symbol, expected_quantity in expected.items()
        ],
        "unexpected_positions": [],
    }


def load_starting_position_reconciliation(plan: TargetWeightPlan) -> dict[str, Any]:
    try:
        return reconcile_plan_starting_positions(
            plan,
            _load_positions(plan.candidate_id),
        )
    except Exception as exc:
        logger.exception(
            "target-weight pre-execution position reconciliation failed for {}",
            plan.candidate_id,
        )
        return failed_starting_position_reconciliation(plan, exc)


def blocked_execution_for_pre_execution_drift(
    plan: TargetWeightPlan,
    pre_execution_reconciliation: dict[str, Any],
) -> dict[str, Any]:
    reason = pre_execution_reconciliation.get(
        "reason",
        "target_weight_pre_execution_position_drift",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "pre_execution_reconciliation": pre_execution_reconciliation,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_pre_execution_position_drift",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_duplicate_execution(
    plan: TargetWeightPlan,
    execution_idempotency: dict[str, Any],
) -> dict[str, Any]:
    reason = execution_idempotency.get(
        "reason",
        "target_weight_duplicate_execution_attempt",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "execution_idempotency": execution_idempotency,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_duplicate_execution_attempt",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_preflight_refresh(
    plan: TargetWeightPlan,
    preflight_refresh: dict[str, Any],
) -> dict[str, Any]:
    reason = preflight_refresh.get(
        "reason",
        "target_weight_preflight_refresh_failed",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "preflight_refresh": preflight_refresh,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_preflight_refresh",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_pilot_validation(
    plan: TargetWeightPlan,
    validation: Any,
) -> dict[str, Any]:
    reason = (
        "target_weight_pilot_validation_failed: "
        f"{getattr(validation, 'reason', 'pilot plan blocked')}"
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_pilot_validation",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_trade_day_mismatch(
    plan: TargetWeightPlan,
    execution_trade_day_check: dict[str, Any],
) -> dict[str, Any]:
    reason = execution_trade_day_check.get(
        "reason",
        "target_weight_execution_trade_day_mismatch",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "execution_trade_day_check": execution_trade_day_check,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_execution_trade_day_mismatch",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_market_session(
    plan: TargetWeightPlan,
    execution_market_session_check: dict[str, Any],
) -> dict[str, Any]:
    reason = execution_market_session_check.get(
        "reason",
        "target_weight_execution_market_session_closed",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "execution_market_session_check": execution_market_session_check,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_execution_market_session_closed",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_authorization_snapshot_mismatch(
    plan: TargetWeightPlan,
    pilot_authorization_snapshot_check: dict[str, Any],
) -> dict[str, Any]:
    reason = pilot_authorization_snapshot_check.get(
        "reason",
        "target_weight_pilot_authorization_snapshot_mismatch",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "pilot_authorization_snapshot_check": pilot_authorization_snapshot_check,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_pilot_authorization_snapshot_mismatch",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_liquidity(
    plan: TargetWeightPlan,
    liquidity_check: dict[str, Any],
) -> dict[str, Any]:
    reason = liquidity_check.get(
        "reason",
        "target_weight_liquidity_preflight_failed",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "liquidity_check": liquidity_check,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_liquidity_preflight",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def blocked_execution_for_pre_trade_risk(
    plan: TargetWeightPlan,
    pre_trade_risk_check: dict[str, Any],
) -> dict[str, Any]:
    reason = pre_trade_risk_check.get(
        "reason",
        "target_weight_pre_trade_risk_failed",
    )
    return {
        "executed": 0,
        "skipped": len(plan.orders),
        "failed": 0,
        "halted": True,
        "halt_reason": reason,
        "pre_trade_risk_check": pre_trade_risk_check,
        "details": [
            {
                "order": asdict(order),
                "status": "skipped_pre_trade_risk",
                "reason": reason,
            }
            for order in plan.orders
        ],
    }


def reconcile_order_results(plan: TargetWeightPlan, execution: dict[str, Any]) -> dict[str, Any]:
    details = list(execution.get("details") or [])
    mismatches: list[dict[str, Any]] = []

    if len(details) != len(plan.orders):
        mismatches.append({
            "type": "detail_count",
            "expected": len(plan.orders),
            "actual": len(details),
            "message": f"detail_count actual={len(details)} expected={len(plan.orders)}",
        })

    for index, order in enumerate(plan.orders):
        if index >= len(details):
            continue
        detail = details[index] or {}
        result = detail.get("result")
        status = detail.get("status")

        if status != "success":
            mismatches.append({
                "type": "detail_status",
                "index": index,
                "symbol": order.symbol,
                "expected": "success",
                "actual": status,
                "message": f"{order.symbol} detail status={status}",
            })

        if not isinstance(result, dict):
            mismatches.append({
                "type": "missing_result",
                "index": index,
                "symbol": order.symbol,
                "message": f"{order.symbol} missing result payload",
            })
            continue

        if result.get("success") is not True:
            mismatches.append({
                "type": "result_success",
                "index": index,
                "symbol": order.symbol,
                "expected": True,
                "actual": result.get("success"),
                "message": f"{order.symbol} result success={result.get('success')}",
            })

        result_symbol = normalize_symbol(str(result.get("symbol") or ""))
        expected_symbol = normalize_symbol(order.symbol)
        if result_symbol != expected_symbol:
            mismatches.append({
                "type": "symbol",
                "index": index,
                "expected": expected_symbol,
                "actual": result_symbol,
                "message": f"{order.symbol} result symbol={result_symbol or '<missing>'}",
            })

        result_action = str(result.get("action") or "").upper()
        expected_action = str(order.action).upper()
        if result_action != expected_action:
            mismatches.append({
                "type": "action",
                "index": index,
                "symbol": order.symbol,
                "expected": expected_action,
                "actual": result_action,
                "message": f"{order.symbol} result action={result_action or '<missing>'}",
            })

        try:
            result_quantity = int(result.get("quantity"))
        except (TypeError, ValueError):
            result_quantity = None
        expected_quantity = int(order.quantity)
        if result_quantity != expected_quantity:
            mismatches.append({
                "type": "quantity",
                "index": index,
                "symbol": order.symbol,
                "expected": expected_quantity,
                "actual": result_quantity,
                "message": f"{order.symbol} result quantity={result_quantity} expected={expected_quantity}",
            })

        result_mode = result.get("mode")
        if result_mode != "paper":
            mismatches.append({
                "type": "mode",
                "index": index,
                "symbol": order.symbol,
                "expected": "paper",
                "actual": result_mode,
                "message": f"{order.symbol} result mode={result_mode or '<missing>'}",
            })

        if expected_action == "BUY" and result.get("paper_fixed_quantity") is not True:
            mismatches.append({
                "type": "buy_path",
                "index": index,
                "symbol": order.symbol,
                "expected": True,
                "actual": result.get("paper_fixed_quantity"),
                "message": f"{order.symbol} buy did not use fixed-quantity paper path",
            })

    complete = len(mismatches) == 0
    reason = "order result payloads match target-weight plan"
    if not complete:
        preview = "; ".join(item["message"] for item in mismatches[:5])
        if len(mismatches) > 5:
            preview = f"{preview}; +{len(mismatches) - 5} more"
        reason = f"target_weight_order_result_mismatch: {preview}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "planned_orders": len(plan.orders),
        "detail_count": len(details),
        "mismatches": mismatches,
    }


def load_paper_trade_fills(
    plan: TargetWeightPlan,
    execution_session_id: str | None = None,
) -> list[Any]:
    from database.repositories import get_trade_history

    trade_day = datetime.strptime(plan.trade_day, "%Y-%m-%d")
    trades = get_trade_history(
        mode="paper",
        start_date=trade_day,
        end_date=trade_day + timedelta(days=1),
        account_key=plan.candidate_id,
        execution_session_id=execution_session_id,
    )
    return [
        trade
        for trade in trades
        if (getattr(trade, "strategy", "") or "") == plan.candidate_id
        and (
            not execution_session_id
            or (getattr(trade, "execution_session_id", "") or "") == execution_session_id
        )
    ]


def _fill_key(symbol: str, action: str) -> str:
    return f"{normalize_symbol(symbol)}:{str(action).upper()}"


def reconcile_plan_fills(
    plan: TargetWeightPlan,
    trades: list[Any] | None,
    execution_session_id: str | None = None,
) -> dict[str, Any]:
    expected: dict[str, int] = {}
    for order in plan.orders:
        key = _fill_key(order.symbol, order.action)
        expected[key] = expected.get(key, 0) + int(order.quantity)

    actual: dict[str, int] = {}
    fill_rows: list[dict[str, Any]] = []
    unlinked_fills: list[dict[str, Any]] = []
    for trade in trades or []:
        symbol = normalize_symbol(str(getattr(trade, "symbol", "") or ""))
        action = str(getattr(trade, "action", "") or "").upper()
        trade_session_id = str(getattr(trade, "execution_session_id", "") or "")
        trade_order_id = str(getattr(trade, "order_id", "") or "")
        try:
            quantity = int(getattr(trade, "quantity"))
        except (TypeError, ValueError):
            quantity = 0
        if execution_session_id and trade_session_id != execution_session_id:
            unlinked_fills.append({
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "strategy": getattr(trade, "strategy", None),
                "mode": getattr(trade, "mode", None),
                "account_key": getattr(trade, "account_key", None),
                "executed_at": str(getattr(trade, "executed_at", "")),
                "execution_session_id": trade_session_id,
                "order_id": trade_order_id,
            })
            continue
        key = _fill_key(symbol, action)
        actual[key] = actual.get(key, 0) + quantity
        fill_rows.append({
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "strategy": getattr(trade, "strategy", None),
            "mode": getattr(trade, "mode", None),
            "account_key": getattr(trade, "account_key", None),
            "executed_at": str(getattr(trade, "executed_at", "")),
            "execution_session_id": trade_session_id,
            "order_id": trade_order_id,
        })

    mismatches = [
        {
            "symbol": key.split(":", 1)[0],
            "action": key.split(":", 1)[1],
            "expected_quantity": expected_quantity,
            "actual_quantity": actual.get(key, 0),
        }
        for key, expected_quantity in sorted(expected.items())
        if actual.get(key, 0) != expected_quantity
    ]
    unexpected_fills = [
        {
            "symbol": key.split(":", 1)[0],
            "action": key.split(":", 1)[1],
            "actual_quantity": quantity,
        }
        for key, quantity in sorted(actual.items())
        if key not in expected and quantity > 0
    ]
    complete = len(mismatches) == 0 and len(unexpected_fills) == 0 and len(unlinked_fills) == 0
    reason = "paper trade fills match target-weight plan"
    if not complete:
        reason_parts = []
        if mismatches:
            mismatch_text = ", ".join(
                (
                    f"{item['symbol']} {item['action']} "
                    f"actual={item['actual_quantity']} expected={item['expected_quantity']}"
                )
                for item in mismatches
            )
            reason_parts.append(f"mismatches: {mismatch_text}")
        if unexpected_fills:
            unexpected_text = ", ".join(
                f"{item['symbol']} {item['action']} actual={item['actual_quantity']}"
                for item in unexpected_fills
            )
            reason_parts.append(f"unexpected: {unexpected_text}")
        if unlinked_fills:
            unlinked_text = ", ".join(
                f"{item['symbol']} {item['action']} quantity={item['quantity']}"
                for item in unlinked_fills
            )
            reason_parts.append(f"unlinked: {unlinked_text}")
        reason = f"target_weight_fill_reconciliation_mismatch: {'; '.join(reason_parts)}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "execution_session_id": execution_session_id or "",
        "expected_quantities": dict(sorted(expected.items())),
        "actual_quantities": dict(sorted(actual.items())),
        "mismatches": mismatches,
        "unexpected_fills": unexpected_fills,
        "unlinked_fills": unlinked_fills,
        "fill_count": len(fill_rows),
        "fills": fill_rows,
    }


def failed_fill_reconciliation(
    plan: TargetWeightPlan,
    error: Exception,
    execution_session_id: str | None = None,
) -> dict[str, Any]:
    expected: dict[str, int] = {}
    for order in plan.orders:
        key = _fill_key(order.symbol, order.action)
        expected[key] = expected.get(key, 0) + int(order.quantity)
    return {
        "checked": True,
        "complete": False,
        "reason": f"target_weight_fill_reconciliation_failed: {error}",
        "execution_session_id": execution_session_id or "",
        "expected_quantities": dict(sorted(expected.items())),
        "actual_quantities": {},
        "mismatches": [
            {
                "symbol": key.split(":", 1)[0],
                "action": key.split(":", 1)[1],
                "expected_quantity": quantity,
                "actual_quantity": None,
            }
            for key, quantity in sorted(expected.items())
        ],
        "unexpected_fills": [],
        "unlinked_fills": [],
        "fill_count": 0,
        "fills": [],
    }


def summarize_execution_for_evidence(
    plan: TargetWeightPlan,
    execution: dict[str, Any],
    execution_trade_day_check: dict[str, Any] | None = None,
    execution_market_session_check: dict[str, Any] | None = None,
    pilot_authorization_snapshot_check: dict[str, Any] | None = None,
    execution_idempotency: dict[str, Any] | None = None,
    preflight_refresh: dict[str, Any] | None = None,
    pre_execution_reconciliation: dict[str, Any] | None = None,
    liquidity_check: dict[str, Any] | None = None,
    pre_trade_risk_check: dict[str, Any] | None = None,
    fill_reconciliation: dict[str, Any] | None = None,
    position_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planned = len(plan.orders)
    executed = int(execution.get("executed", 0) or 0)
    failed = int(execution.get("failed", 0) or 0)
    skipped = int(execution.get("skipped", 0) or 0)
    halted = bool(execution.get("halted", False))
    order_count_complete = failed == 0 and skipped == 0 and not halted and executed == planned
    order_result_reconciliation = reconcile_order_results(plan, execution)
    order_result_complete = bool(order_result_reconciliation.get("complete", False))
    order_complete = order_count_complete and order_result_complete
    if fill_reconciliation is None:
        fill_reconciliation = {
            "checked": False,
            "complete": False,
            "reason": "fill reconciliation not required until target-weight execution is complete",
            "execution_session_id": execution.get("execution_session_id", ""),
            "expected_quantities": {},
            "actual_quantities": {},
            "mismatches": [],
            "unexpected_fills": [],
            "unlinked_fills": [],
            "fill_count": 0,
            "fills": [],
        }
    idempotency = execution_idempotency or execution.get("execution_idempotency") or {
        "checked": False,
        "allowed": True,
        "reason": "execution idempotency check not required",
    }
    trade_day_check = (
        execution_trade_day_check
        or execution.get("execution_trade_day_check")
        or execution_trade_day_check_not_required()
    )
    market_session_check = (
        execution_market_session_check
        or execution.get("execution_market_session_check")
        or execution_market_session_check_not_required()
    )
    authorization_snapshot_check = (
        pilot_authorization_snapshot_check
        or execution.get("pilot_authorization_snapshot_check")
        or _authorization_snapshot_not_required(
            "pilot authorization snapshot check not required"
        )
    )
    preflight = preflight_refresh or execution.get("preflight_refresh") or {
        "checked": False,
        "complete": True,
        "reason": "preflight refresh not required",
    }
    pre_reconciliation = pre_execution_reconciliation or execution.get("pre_execution_reconciliation") or {
        "checked": False,
        "complete": True,
        "reason": "pre-execution position reconciliation not required",
        "expected_quantities": {},
        "actual_quantities": {},
        "mismatches": [],
        "unexpected_positions": [],
    }
    liquidity = liquidity_check or execution.get("liquidity_check") or {
        "checked": False,
        "complete": True,
        "reason": "liquidity preflight not required",
        "orders": [],
        "violations": [],
    }
    pre_trade_risk = pre_trade_risk_check or execution.get("pre_trade_risk_check") or {
        "checked": False,
        "complete": True,
        "reason": "pre-trade risk validation not required",
        "violations": [],
        "order_costs": [],
        "cost_summary": {},
    }
    reconciliation = position_reconciliation or {
        "checked": False,
        "complete": True,
        "reason": "position reconciliation not required",
        "expected_quantities": {},
        "actual_quantities": {},
        "mismatches": [],
        "unexpected_positions": [],
    }
    idempotency_allowed = bool(idempotency.get("allowed", False))
    execution_trade_day_allowed = bool(trade_day_check.get("allowed", False))
    execution_market_session_allowed = bool(market_session_check.get("allowed", False))
    pilot_authorization_snapshot_allowed = bool(authorization_snapshot_check.get("allowed", False))
    preflight_refresh_complete = bool(
        preflight.get("complete", not preflight.get("checked", False))
    )
    pre_execution_complete = bool(pre_reconciliation.get("complete", False))
    liquidity_complete = bool(liquidity.get("complete", False))
    pre_trade_risk_complete = bool(pre_trade_risk.get("complete", False))
    fill_complete = bool(fill_reconciliation.get("complete", False))
    position_complete = bool(reconciliation.get("complete", False))
    complete = (
        idempotency_allowed
        and execution_trade_day_allowed
        and execution_market_session_allowed
        and pilot_authorization_snapshot_allowed
        and preflight_refresh_complete
        and pre_execution_complete
        and liquidity_complete
        and pre_trade_risk_complete
        and order_complete
        and fill_complete
        and position_complete
    )
    reason = "all planned target-weight orders executed"
    if not idempotency_allowed:
        reason = idempotency.get("reason", "target_weight_duplicate_execution_attempt")
    elif not execution_trade_day_allowed:
        reason = trade_day_check.get("reason", "target_weight_execution_trade_day_mismatch")
    elif not execution_market_session_allowed:
        reason = market_session_check.get("reason", "target_weight_execution_market_session_closed")
    elif not pilot_authorization_snapshot_allowed:
        reason = authorization_snapshot_check.get(
            "reason",
            "target_weight_pilot_authorization_snapshot_mismatch",
        )
    elif not preflight_refresh_complete:
        reason = preflight.get("reason", "target_weight_preflight_refresh_failed")
    elif not pre_execution_complete:
        reason = pre_reconciliation.get("reason", "target_weight_pre_execution_position_drift")
    elif not liquidity_complete:
        reason = liquidity.get("reason", "target_weight_liquidity_preflight_failed")
    elif not pre_trade_risk_complete:
        reason = pre_trade_risk.get("reason", "target_weight_pre_trade_risk_failed")
    elif not order_count_complete:
        reason = (
            "target_weight_execution_incomplete: "
            f"executed={executed}/{planned} failed={failed} skipped={skipped} halted={halted}"
        )
        halt_reason = execution.get("halt_reason")
        if halt_reason:
            reason = f"{reason}; halt_reason={halt_reason}"
    elif not order_result_complete:
        reason = order_result_reconciliation.get("reason", "target_weight_order_result_mismatch")
    elif not fill_complete:
        reason = fill_reconciliation.get("reason", "target_weight_fill_reconciliation_mismatch")
    elif not position_complete:
        reason = reconciliation.get("reason", "target_weight_position_mismatch")

    return {
        "complete": complete,
        "reason": reason,
        "idempotency_allowed": idempotency_allowed,
        "execution_trade_day_allowed": execution_trade_day_allowed,
        "execution_market_session_allowed": execution_market_session_allowed,
        "pilot_authorization_snapshot_allowed": pilot_authorization_snapshot_allowed,
        "preflight_refresh_complete": preflight_refresh_complete,
        "pre_execution_complete": pre_execution_complete,
        "liquidity_complete": liquidity_complete,
        "pre_trade_risk_complete": pre_trade_risk_complete,
        "order_complete": order_complete,
        "order_count_complete": order_count_complete,
        "order_result_complete": order_result_complete,
        "fill_complete": fill_complete,
        "planned_orders": planned,
        "executed_orders": executed,
        "failed_orders": failed,
        "skipped_orders": skipped,
        "halted": halted,
        "halt_reason": execution.get("halt_reason", ""),
        "execution_session_id": (
            execution.get("execution_session_id")
            or fill_reconciliation.get("execution_session_id")
            or ""
        ),
        "execution_trade_day_check": trade_day_check,
        "execution_market_session_check": market_session_check,
        "pilot_authorization_snapshot_check": authorization_snapshot_check,
        "execution_idempotency": idempotency,
        "preflight_refresh": preflight,
        "pre_execution_reconciliation": pre_reconciliation,
        "liquidity_check": liquidity,
        "pre_trade_risk_check": pre_trade_risk,
        "order_result_reconciliation": order_result_reconciliation,
        "fill_reconciliation": fill_reconciliation,
        "position_reconciliation": reconciliation,
        "params_hash": plan.params_hash,
        "target_symbols": list(plan.targets),
        "target_exposure": plan.target_exposure,
        "gross_exposure_after": plan.gross_exposure_after,
        "max_order_notional": plan.max_order_notional,
    }


def build_pilot_evidence_caps_snapshot(
    plan: TargetWeightPlan,
    validation: Any,
    execution: dict[str, Any],
    execution_trade_day_check: dict[str, Any] | None = None,
    execution_market_session_check: dict[str, Any] | None = None,
    pilot_authorization_snapshot_check: dict[str, Any] | None = None,
    execution_idempotency: dict[str, Any] | None = None,
    preflight_refresh: dict[str, Any] | None = None,
    pre_execution_reconciliation: dict[str, Any] | None = None,
    liquidity_check: dict[str, Any] | None = None,
    pre_trade_risk_check: dict[str, Any] | None = None,
    fill_reconciliation: dict[str, Any] | None = None,
    position_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caps = dict(getattr(validation, "caps_snapshot", None) or {})
    caps["target_weight_plan"] = _target_weight_plan_evidence_snapshot(plan)
    caps["target_weight_execution"] = summarize_execution_for_evidence(
        plan,
        execution,
        execution_trade_day_check=execution_trade_day_check,
        execution_market_session_check=execution_market_session_check,
        pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
        execution_idempotency=execution_idempotency,
        preflight_refresh=preflight_refresh,
        pre_execution_reconciliation=pre_execution_reconciliation,
        liquidity_check=liquidity_check,
        pre_trade_risk_check=pre_trade_risk_check,
        fill_reconciliation=fill_reconciliation,
        position_reconciliation=position_reconciliation,
    )
    return caps


def _target_weight_plan_evidence_snapshot(plan: TargetWeightPlan) -> dict[str, Any]:
    snapshot = {
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
        "position_quantities_before": _starting_position_quantities(plan),
        "target_quantities_after": _expected_position_quantities(plan),
    }
    portfolio_drawdown_guard = plan.diagnostics.get("portfolio_drawdown_guard")
    if isinstance(portfolio_drawdown_guard, dict):
        snapshot["portfolio_drawdown_guard"] = dict(portfolio_drawdown_guard)
    return snapshot


def _latest_existing_evidence_record(plan: TargetWeightPlan) -> dict[str, Any] | None:
    from core.paper_evidence import get_canonical_records

    latest_record = None
    for record in get_canonical_records(plan.candidate_id):
        if record.get("date") == plan.trade_day:
            latest_record = record
    return latest_record


def _coerce_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _coerce_int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _positive_float_from(*values: Any) -> float | None:
    for value in values:
        number = _coerce_float_or_none(value)
        if number is not None and number > 0:
            return number
    return None


def _repairable_target_weight_execution_status(
    candidate_id: str,
    record: dict[str, Any],
    *,
    total_value: float | None,
) -> tuple[bool, str]:
    """성과 필드만 임시 통과시켜 실행 proof 자체가 완전한지 분리 검증한다."""
    from core.paper_evidence import _target_weight_record_proof_status

    probe = deepcopy(record)
    probe["benchmark_status"] = "final"
    probe["same_universe_excess"] = 0.0
    probe["exposure_matched_excess"] = 0.0
    probe["cash_adjusted_excess"] = 0.0
    probe["daily_return"] = probe.get("daily_return") if probe.get("daily_return") is not None else 0.0
    probe["total_value"] = _positive_float_from(total_value, probe.get("total_value"), 1.0)
    valid, reason = _target_weight_record_proof_status(candidate_id, probe)
    if valid:
        return True, "target_weight_execution_proof_verified"
    return False, reason


def _target_weight_repair_watchlist(record: dict[str, Any]) -> list[str]:
    caps = record.get("pilot_caps_snapshot") or {}
    plan = caps.get("target_weight_plan") or {}
    execution = caps.get("target_weight_execution") or {}
    symbols = (
        plan.get("targets")
        or execution.get("target_symbols")
        or plan.get("symbols")
        or []
    )
    return [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]


def _target_weight_repair_performance_fields(record: dict[str, Any]) -> dict[str, Any]:
    caps = record.get("pilot_caps_snapshot") or {}
    plan = caps.get("target_weight_plan") or {}
    execution = caps.get("target_weight_execution") or {}
    pre_trade = execution.get("pre_trade_risk_check") or {}
    fill_reconciliation = execution.get("fill_reconciliation") or {}
    order_costs = pre_trade.get("order_costs") if isinstance(pre_trade.get("order_costs"), list) else []
    cost_summary = pre_trade.get("cost_summary") or {}

    cash = _positive_float_from(pre_trade.get("projected_cash_after_costs"))
    invested = _positive_float_from(pre_trade.get("projected_gross_exposure_after_costs"))
    total_value = _positive_float_from(pre_trade.get("projected_total_value_after_costs"))

    if cash is None and order_costs:
        cash = _coerce_float_or_none((order_costs[-1] or {}).get("cash_after"))
    if invested is None:
        position_rows = pre_trade.get("position_ratios") or []
        if isinstance(position_rows, list):
            invested_sum = sum(
                _coerce_float_or_none((row or {}).get("value")) or 0.0
                for row in position_rows
                if isinstance(row, dict)
            )
            invested = invested_sum if invested_sum > 0 else None
    if invested is None:
        invested = _positive_float_from(plan.get("gross_exposure_after"), execution.get("gross_exposure_after"))
    if total_value is None and cash is not None and invested is not None:
        total_value = cash + invested
    if cash is None and total_value is not None and invested is not None:
        cash = total_value - invested
    if invested is None and total_value is not None and cash is not None:
        invested = max(total_value - cash, 0.0)

    base_value = None
    for item in order_costs:
        if isinstance(item, dict):
            base_value = _positive_float_from(item.get("cash_before"))
            if base_value is not None:
                break
    guard = plan.get("portfolio_drawdown_guard") or {}
    if base_value is None and isinstance(guard, dict):
        base_value = _positive_float_from(
            guard.get("last_equity_value"),
            guard.get("peak_equity_value"),
            guard.get("peak_value"),
            guard.get("last_value"),
        )
    if base_value is None:
        explicit_costs = _coerce_float_or_none(cost_summary.get("total_explicit_costs")) or 0.0
        base_value = _positive_float_from(record.get("total_value"), total_value + explicit_costs if total_value else None)

    daily_return = _coerce_float_or_none(record.get("daily_return"))
    if daily_return is None and total_value is not None and base_value is not None and base_value > 0:
        daily_return = (total_value / base_value - 1.0) * 100.0

    cumulative_return = _coerce_float_or_none(record.get("cumulative_return"))
    if cumulative_return is None:
        cumulative_return = daily_return

    mdd = _coerce_float_or_none(record.get("mdd"))
    if mdd is None and daily_return is not None:
        mdd = min(0.0, daily_return)
    elif mdd is not None and mdd > 0:
        mdd = -abs(mdd)

    target_quantities = plan.get("target_quantities_after") or {}
    derived_position_count = 0
    if isinstance(target_quantities, dict):
        derived_position_count = sum(
            1
            for quantity in target_quantities.values()
            if _coerce_int_or_zero(quantity) > 0
        )
    position_count = _coerce_int_or_zero(
        pre_trade.get("target_position_count")
        or plan.get("target_position_count")
        or derived_position_count
    )

    fills = fill_reconciliation.get("fills") or []
    fills = fills if isinstance(fills, list) else []
    fill_count = _coerce_int_or_zero(fill_reconciliation.get("fill_count") or len(fills))
    planned_orders = _coerce_int_or_zero(execution.get("planned_orders") or fill_count)
    executed_orders = _coerce_int_or_zero(execution.get("executed_orders") or fill_count)
    if executed_orders <= 0 and fill_reconciliation.get("complete") is True:
        executed_orders = fill_count

    buy_count = sum(1 for fill in fills if str((fill or {}).get("action", "")).upper() == "BUY")
    sell_count = sum(1 for fill in fills if str((fill or {}).get("action", "")).upper() == "SELL")
    if not fills:
        expected_quantities = fill_reconciliation.get("expected_quantities") or {}
        if isinstance(expected_quantities, dict):
            for key in expected_quantities:
                action = str(key).split(":", 1)[-1].upper()
                if action == "BUY":
                    buy_count += 1
                elif action == "SELL":
                    sell_count += 1
    if buy_count + sell_count == 0:
        buy_count = _coerce_int_or_zero(record.get("buy_count"))
        sell_count = _coerce_int_or_zero(record.get("sell_count"))

    traded_notional = 0.0
    for item in order_costs:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).upper()
        if action == "BUY":
            traded_notional += abs(_coerce_float_or_none(item.get("required_cash")) or 0.0)
        elif action == "SELL":
            traded_notional += abs(_coerce_float_or_none(item.get("cash_delta")) or 0.0)
    turnover = traded_notional / total_value if total_value and total_value > 0 and traded_notional > 0 else None

    fill_rate = None
    if planned_orders > 0:
        fill_rate = min(max(executed_orders / planned_orders, 0.0), 1.0)
    if fill_reconciliation.get("complete") is True and planned_orders > 0:
        fill_rate = 1.0

    return {
        "total_value": round(total_value, 2) if total_value is not None else None,
        "cash": round(cash, 2) if cash is not None else None,
        "invested": round(invested, 2) if invested is not None else None,
        "daily_return": round(daily_return, 6) if daily_return is not None else None,
        "cumulative_return": round(cumulative_return, 6) if cumulative_return is not None else None,
        "mdd": round(mdd, 6) if mdd is not None else None,
        "position_count": position_count,
        "total_trades": fill_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "order_submit_count": planned_orders,
        "fill_count": fill_count,
        "raw_fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
        "effective_fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
        "turnover": round(turnover, 6) if turnover is not None else None,
        "base_value": round(base_value, 2) if base_value is not None else None,
        "traded_notional": round(traded_notional, 2),
        "performance_source": "target_weight_execution.pre_trade_risk_check",
    }


def render_target_weight_pilot_evidence_repair_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Target-weight Pilot Evidence Repair",
        "",
        f"- Candidate: `{report['candidate_id']}`",
        f"- Repair date: `{report['repair_date']}`",
        f"- Status: **{report['status']}**",
        f"- Reason: {report['reason']}",
        f"- Source record version: {report.get('source_record_version', 'N/A')}",
        f"- Appended record version: {report.get('appended_record_version', 'N/A')}",
        f"- Proof after repair: {report.get('proof_status_after', {}).get('reason', 'N/A')}",
        f"- Promotion after repair: {report.get('promotion_status_after', {}).get('reason', 'N/A')}",
        "",
        "## Repaired Fields",
    ]
    repaired_fields = report.get("repaired_fields") or {}
    if repaired_fields:
        lines.extend([f"- {key}: `{value}`" for key, value in sorted(repaired_fields.items())])
    else:
        lines.append("- none")
    lines.extend([
        "",
        "## Safety",
        "- No paper orders are submitted by this repair.",
        "- Existing evidence is not overwritten; repaired evidence is appended as a newer record version.",
    ])
    return "\n".join(lines) + "\n"


def write_target_weight_pilot_evidence_repair_report(
    report: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = _safe_path_component(str(report.get("candidate_id") or "target_weight"))
    repair_date = _safe_path_component(str(report.get("repair_date") or "unknown"))
    stem = f"target_weight_pilot_evidence_repair_{candidate}_{repair_date}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_target_weight_pilot_evidence_repair_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_target_weight_pilot_evidence_finalize_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Target-weight Pilot Evidence Finalize",
        "",
        f"- Candidate: `{report['candidate_id']}`",
        f"- Finalize date: `{report['finalize_date']}`",
        f"- Status: **{report['status']}**",
        f"- Reason: {report['reason']}",
        f"- Source record version: {report.get('source_record_version', 'N/A')}",
        f"- Appended record version: {report.get('appended_record_version', 'N/A')}",
        f"- Proof after finalize: {report.get('proof_status_after', {}).get('reason', 'N/A')}",
        "",
        "## Finalized Fields",
    ]
    finalized_fields = report.get("finalized_fields") or {}
    if finalized_fields:
        lines.extend([f"- {key}: `{value}`" for key, value in sorted(finalized_fields.items())])
    else:
        lines.append("- none")
    performance_status = report.get("performance_evidence_status") or {}
    if performance_status:
        lines.extend([
            "",
            "## Performance Evidence Status",
            "- Source record fields present: "
            f"`{', '.join(performance_status.get('source_record_fields_present') or []) or 'none'}`",
            "- Portfolio metrics checked: "
            f"`{performance_status.get('portfolio_metrics_checked', False)}`",
            "- Portfolio metrics fields present: "
            f"`{', '.join(performance_status.get('portfolio_metrics_fields_present') or []) or 'none'}`",
            "- Missing fields after probe: "
            f"`{', '.join(performance_status.get('missing_fields_after_probe') or []) or 'none'}`",
        ])
        if performance_status.get("portfolio_metrics_inferred_from_previous"):
            lines.append("- Portfolio metrics source: `previous snapshot carry-forward`")
    lines.extend([
        "",
        "## No-order Safety",
    ])
    for key, value in (report.get("no_order_safety") or {}).items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def write_target_weight_pilot_evidence_finalize_report(
    report: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = str(report["candidate_id"])
    finalize_date = str(report["finalize_date"])
    stem = f"target_weight_pilot_evidence_finalize_{candidate}_{finalize_date}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_target_weight_pilot_evidence_finalize_markdown(report), encoding="utf-8")
    return json_path, md_path


def finalize_target_weight_pilot_evidence(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    finalize_date: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """기존 execution-backed pilot evidence를 주문 없이 final benchmark 성과 record로 확정한다."""
    from core.paper_evidence import (
        _append_jsonl,
        _collect_portfolio_metrics,
        _compute_benchmark_excess,
        _evidence_path,
        _read_all_evidence,
        _target_weight_record_proof_status,
    )

    parsed_date = datetime.strptime(finalize_date, "%Y-%m-%d")
    jsonl_path = _evidence_path(candidate_id)
    records = _read_all_evidence(jsonl_path)
    latest = None
    for record in reversed(records):
        if record.get("date") == finalize_date:
            latest = record
            break

    base_report = {
        "artifact_type": "target_weight_pilot_evidence_finalize",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": candidate_id,
        "finalize_date": finalize_date,
        "evidence_path": str(jsonl_path),
        "status": "blocked",
        "reason": "",
        "finalized": False,
        "source_record_version": None,
        "appended_record_version": None,
        "finalized_fields": {},
        "performance_evidence_status": {},
        "proof_status_before": {},
        "proof_status_after": {},
        "no_order_safety": {
            "orders_submitted": False,
            "order_executor_called": False,
            "existing_records_overwritten": False,
            "append_only": True,
        },
    }

    def finish(report: dict[str, Any], *, fail: bool = False) -> dict[str, Any]:
        report["finalize_hash"] = _stable_manifest_hash(report)
        json_path, md_path = write_target_weight_pilot_evidence_finalize_report(
            report,
            output_dir=output_dir,
        )
        report["artifact_path"] = str(json_path)
        report["report_path"] = str(md_path)
        if fail:
            raise ValueError(report["reason"])
        return report

    if latest is None:
        report = dict(base_report)
        report["reason"] = (
            "target_weight_pilot_evidence_finalize_missing: "
            f"no evidence for {candidate_id} {finalize_date}"
        )
        return finish(report, fail=True)

    source_version = _coerce_int_or_zero(latest.get("record_version") or 1)
    before_valid, before_reason = _target_weight_record_proof_status(candidate_id, latest)
    report = {
        **base_report,
        "source_record_version": source_version,
        "proof_status_before": {"valid": before_valid, "reason": before_reason},
    }
    if before_valid:
        report.update({
            "status": "already_valid",
            "reason": "target_weight_pilot_evidence_already_valid",
        })
        return finish(report)
    if before_reason == "target_weight_repaired_performance_not_promotable":
        report.update({
            "status": "already_repaired_non_promotable",
            "reason": "target_weight_pilot_evidence_already_repaired_non_promotable",
        })
        return finish(report)
    if before_reason not in REPAIRABLE_TARGET_WEIGHT_EVIDENCE_REASONS:
        report["reason"] = f"target_weight_pilot_evidence_finalize_not_allowed: {before_reason}"
        return finish(report, fail=True)

    updated = deepcopy(latest)
    finalized_fields: dict[str, Any] = {}
    performance_fields = (
        "total_value",
        "cash",
        "invested",
        "daily_return",
        "cumulative_return",
        "mdd",
        "position_count",
    )
    performance_status = {
        "source_record_fields_present": [
            field for field in performance_fields if latest.get(field) is not None
        ],
        "portfolio_metrics_checked": False,
        "portfolio_metrics_fields_present": [],
        "portfolio_metrics_inferred_from_previous": False,
        "missing_fields_after_probe": [],
    }
    total_value = _positive_float_from(updated.get("total_value"))
    cash = _coerce_float_or_none(updated.get("cash"))
    daily_return = _coerce_float_or_none(updated.get("daily_return"))
    portfolio: dict[str, Any] = {}
    if total_value is None or daily_return is None:
        portfolio = _collect_portfolio_metrics(candidate_id, parsed_date)
        performance_status["portfolio_metrics_checked"] = True
        performance_status["portfolio_metrics_fields_present"] = [
            field for field in performance_fields if portfolio.get(field) is not None
        ]
        performance_status["portfolio_metrics_inferred_from_previous"] = bool(
            portfolio.get("_inferred_from_previous")
        )
        for field in performance_fields:
            value = portfolio.get(field)
            if value is not None:
                updated[field] = value
                finalized_fields[field] = value
        total_value = _positive_float_from(updated.get("total_value"))
        cash = _coerce_float_or_none(updated.get("cash"))
        daily_return = _coerce_float_or_none(updated.get("daily_return"))
    performance_status["missing_fields_after_probe"] = [
        field
        for field, parsed_value in (
            ("total_value", _positive_float_from(updated.get("total_value"))),
            ("daily_return", _coerce_float_or_none(updated.get("daily_return"))),
        )
        if parsed_value is None
    ]

    if total_value is None or total_value <= 0 or daily_return is None:
        report["reason"] = "target_weight_pilot_evidence_finalize_missing_performance: total_value/daily_return unavailable"
        report["finalized_fields"] = finalized_fields
        report["performance_evidence_status"] = performance_status
        return finish(report, fail=True)

    cash_ratio = (cash or 0.0) / total_value if total_value > 0 else 1.0
    benchmark = _compute_benchmark_excess(
        date=parsed_date,
        daily_return=daily_return,
        cash_ratio=cash_ratio,
        watchlist_symbols=_target_weight_repair_watchlist(latest),
    )
    benchmark_status = benchmark.get("benchmark_status", "failed")
    if benchmark_status != "final":
        report.update({
            "status": "waiting_for_final_benchmark",
            "reason": f"target_weight_pilot_evidence_finalize_waiting: benchmark_status={benchmark_status}",
            "finalized_fields": finalized_fields,
            "performance_evidence_status": performance_status,
            "benchmark_status": benchmark_status,
        })
        return finish(report)

    for field in (
        "same_universe_excess",
        "exposure_matched_excess",
        "cash_adjusted_excess",
        "benchmark_meta",
        "benchmark_status",
    ):
        value = benchmark.get(field) if field != "benchmark_status" else benchmark_status
        updated[field] = value
        finalized_fields[field] = value
    updated["record_version"] = source_version + 1
    updated["schema_version"] = max(_coerce_int_or_zero(updated.get("schema_version") or 2), 2)

    after_valid, after_reason = _target_weight_record_proof_status(candidate_id, updated)
    if not after_valid:
        report["reason"] = f"target_weight_pilot_evidence_finalize_still_invalid: {after_reason}"
        report["finalized_fields"] = finalized_fields
        report["performance_evidence_status"] = performance_status
        report["proof_status_after"] = {"valid": after_valid, "reason": after_reason}
        return finish(report, fail=True)

    _append_jsonl(jsonl_path, updated)
    report.update({
        "status": "finalized",
        "reason": "target_weight_pilot_evidence_finalized",
        "finalized": True,
        "appended_record_version": updated["record_version"],
        "finalized_fields": finalized_fields,
        "performance_evidence_status": performance_status,
        "proof_status_after": {"valid": after_valid, "reason": after_reason},
        "benchmark_status": benchmark_status,
    })
    return finish(report)


def repair_target_weight_pilot_evidence(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    repair_date: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """중복 주문 없이 target-weight pilot evidence의 성과/benchmark 필드를 append-only로 복구한다."""
    from core.paper_evidence import (
        _append_jsonl,
        _compute_benchmark_excess,
        _evidence_path,
        _read_all_evidence,
        _target_weight_record_proof_status,
    )

    parsed_date = datetime.strptime(repair_date, "%Y-%m-%d")
    jsonl_path = _evidence_path(candidate_id)
    records = _read_all_evidence(jsonl_path)
    latest = None
    for record in reversed(records):
        if record.get("date") == repair_date:
            latest = record
            break

    base_report = {
        "artifact_type": "target_weight_pilot_evidence_repair",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": candidate_id,
        "repair_date": repair_date,
        "evidence_path": str(jsonl_path),
        "status": "blocked",
        "reason": "",
        "repaired": False,
        "source_record_version": None,
        "appended_record_version": None,
        "repaired_fields": {},
        "proof_status_before": {},
        "proof_status_after": {},
        "no_order_safety": {
            "orders_submitted": False,
            "order_executor_called": False,
            "existing_records_overwritten": False,
            "append_only": True,
        },
    }

    def finish(report: dict[str, Any], *, fail: bool = False) -> dict[str, Any]:
        report["repair_hash"] = _stable_manifest_hash(report)
        json_path, md_path = write_target_weight_pilot_evidence_repair_report(
            report,
            output_dir=output_dir,
        )
        report["artifact_path"] = str(json_path)
        report["report_path"] = str(md_path)
        if fail:
            raise ValueError(report["reason"])
        return report

    if latest is None:
        report = dict(base_report)
        report["reason"] = f"target_weight_pilot_evidence_repair_missing: no evidence for {candidate_id} {repair_date}"
        return finish(report, fail=True)

    source_version = _coerce_int_or_zero(latest.get("record_version") or 1)
    before_valid, before_reason = _target_weight_record_proof_status(candidate_id, latest)
    report = {
        **base_report,
        "source_record_version": source_version,
        "proof_status_before": {"valid": before_valid, "reason": before_reason},
    }
    if before_valid:
        report.update({
            "status": "already_valid",
            "reason": "target_weight_pilot_evidence_already_valid",
        })
        return finish(report)
    if before_reason not in REPAIRABLE_TARGET_WEIGHT_EVIDENCE_REASONS:
        report["reason"] = f"target_weight_pilot_evidence_repair_not_allowed: {before_reason}"
        return finish(report, fail=True)

    repaired_fields = _target_weight_repair_performance_fields(latest)
    execution_valid, execution_reason = _repairable_target_weight_execution_status(
        candidate_id,
        latest,
        total_value=repaired_fields.get("total_value"),
    )
    if not execution_valid:
        report["reason"] = f"target_weight_pilot_evidence_repair_execution_invalid: {execution_reason}"
        report["repaired_fields"] = repaired_fields
        return finish(report, fail=True)

    total_value = _positive_float_from(repaired_fields.get("total_value"))
    daily_return = _coerce_float_or_none(repaired_fields.get("daily_return"))
    if total_value is None or daily_return is None:
        report["reason"] = "target_weight_pilot_evidence_repair_missing_performance: total_value/daily_return unavailable"
        report["repaired_fields"] = repaired_fields
        return finish(report, fail=True)

    cash = _coerce_float_or_none(repaired_fields.get("cash")) or 0.0
    cash_ratio = cash / total_value if total_value > 0 else 1.0
    watchlist_symbols = _target_weight_repair_watchlist(latest)
    benchmark = _compute_benchmark_excess(
        date=parsed_date,
        daily_return=daily_return,
        cash_ratio=cash_ratio,
        watchlist_symbols=watchlist_symbols,
    )
    benchmark_meta = dict(benchmark.get("benchmark_meta") or {})
    benchmark_meta["repair_source"] = repaired_fields["performance_source"]
    benchmark_meta["performance_repair"] = True
    benchmark_meta["source_record_version"] = source_version
    benchmark_meta["source_proof_reason"] = before_reason
    benchmark_meta["base_value"] = repaired_fields.get("base_value")
    benchmark_meta["traded_notional"] = repaired_fields.get("traded_notional")
    benchmark_meta["watchlist_symbols"] = watchlist_symbols
    benchmark_status = benchmark.get("benchmark_status", "failed")

    updated = deepcopy(latest)
    for field in (
        "total_value",
        "cash",
        "invested",
        "daily_return",
        "cumulative_return",
        "mdd",
        "position_count",
        "total_trades",
        "buy_count",
        "sell_count",
        "order_submit_count",
        "fill_count",
        "raw_fill_rate",
        "effective_fill_rate",
        "turnover",
    ):
        value = repaired_fields.get(field)
        if value is not None:
            updated[field] = value
    updated["same_universe_excess"] = benchmark.get("same_universe_excess")
    updated["exposure_matched_excess"] = benchmark.get("exposure_matched_excess")
    updated["cash_adjusted_excess"] = benchmark.get("cash_adjusted_excess")
    updated["benchmark_meta"] = benchmark_meta
    updated["benchmark_status"] = benchmark_status
    updated["record_version"] = source_version + 1
    updated["schema_version"] = max(_coerce_int_or_zero(updated.get("schema_version") or 2), 2)
    updated["evidence_mode"] = "pilot_paper"
    updated["session_mode"] = "pilot_paper"
    updated["pilot_authorized"] = True
    updated["execution_backed"] = True
    updated["promotion_eligible"] = False
    updated["promotion_exclusion_reason"] = "target_weight_repaired_performance_not_promotable"

    warnings = list(updated.get("cross_validation_warnings") or [])
    repair_warning = "target_weight_pilot_evidence_repaired_from_execution_snapshot"
    if repair_warning not in warnings:
        warnings.append(repair_warning)
    updated["cross_validation_warnings"] = warnings

    after_valid, after_reason = _target_weight_record_proof_status(
        candidate_id,
        updated,
        allow_repaired_performance=True,
    )
    if not after_valid:
        report["reason"] = f"target_weight_pilot_evidence_repair_still_invalid: {after_reason}"
        report["repaired_fields"] = repaired_fields
        report["proof_status_after"] = {"valid": after_valid, "reason": after_reason}
        return finish(report, fail=True)
    promotion_valid, promotion_reason = _target_weight_record_proof_status(candidate_id, updated)

    _append_jsonl(jsonl_path, updated)
    report.update({
        "status": "repaired",
        "reason": "target_weight_pilot_evidence_repaired",
        "repaired": True,
        "appended_record_version": updated["record_version"],
        "repaired_fields": {
            key: repaired_fields.get(key)
            for key in (
                "total_value",
                "cash",
                "invested",
                "daily_return",
                "cumulative_return",
                "mdd",
                "position_count",
                "total_trades",
                "buy_count",
                "sell_count",
                "order_submit_count",
                "fill_count",
                "raw_fill_rate",
                "effective_fill_rate",
                "turnover",
            )
        },
        "benchmark_status": benchmark_status,
        "proof_status_after": {"valid": after_valid, "reason": after_reason},
        "promotion_status_after": {
            "eligible": promotion_valid,
            "reason": promotion_reason,
        },
    })
    return finish(report)


def verify_existing_pilot_evidence_record(plan: TargetWeightPlan) -> dict[str, Any]:
    from core.paper_evidence import _target_weight_record_proof_status

    record = _latest_existing_evidence_record(plan)
    result = {
        "checked": True,
        "valid": False,
        "date": plan.trade_day,
        "strategy": plan.candidate_id,
        "reason": "",
        "mismatches": [],
        "record_summary": {},
    }
    if record is None:
        result["reason"] = (
            "target_weight_existing_evidence_missing: "
            f"no canonical evidence record for {plan.candidate_id} {plan.trade_day}"
        )
        return result

    caps = record.get("pilot_caps_snapshot") or {}
    target_plan = caps.get("target_weight_plan") or {}
    target_execution = caps.get("target_weight_execution") or {}
    position_reconciliation = target_execution.get("position_reconciliation") or {}
    order_result_reconciliation = target_execution.get("order_result_reconciliation") or {}
    fill_reconciliation = target_execution.get("fill_reconciliation") or {}
    expected_target_plan = _target_weight_plan_evidence_snapshot(plan)

    result["record_summary"] = {
        "date": record.get("date"),
        "strategy": record.get("strategy"),
        "evidence_mode": record.get("evidence_mode"),
        "session_mode": record.get("session_mode"),
        "execution_backed": record.get("execution_backed"),
        "pilot_authorized": record.get("pilot_authorized"),
        "target_weight_execution_complete": target_execution.get("complete"),
        "params_hash": target_plan.get("params_hash") or target_execution.get("params_hash"),
        "benchmark_status": record.get("benchmark_status"),
        "same_universe_excess": record.get("same_universe_excess"),
        "exposure_matched_excess": record.get("exposure_matched_excess"),
        "cash_adjusted_excess": record.get("cash_adjusted_excess"),
        "daily_return": record.get("daily_return"),
        "total_value": record.get("total_value"),
    }

    checks = [
        ("record.strategy", record.get("strategy"), plan.candidate_id),
        ("record.evidence_mode", record.get("evidence_mode"), "pilot_paper"),
        ("record.session_mode", record.get("session_mode"), "pilot_paper"),
        ("record.execution_backed", record.get("execution_backed"), True),
        ("record.pilot_authorized", record.get("pilot_authorized"), True),
        ("target_weight_plan.candidate_id", target_plan.get("candidate_id"), plan.candidate_id),
        ("target_weight_plan.trade_day", target_plan.get("trade_day"), plan.trade_day),
        ("target_weight_plan.score_day", target_plan.get("score_day"), plan.score_day),
        ("target_weight_plan.params_hash", target_plan.get("params_hash"), plan.params_hash),
        (
            "target_weight_plan.targets",
            [normalize_symbol(symbol) for symbol in target_plan.get("targets", [])],
            [normalize_symbol(symbol) for symbol in expected_target_plan["targets"]],
        ),
        (
            "target_weight_plan.risk_off",
            target_plan.get("risk_off"),
            expected_target_plan["risk_off"],
        ),
        (
            "target_weight_plan.position_quantities_before",
            _normalized_quantities(target_plan.get("position_quantities_before")),
            expected_target_plan["position_quantities_before"],
        ),
        (
            "target_weight_plan.target_quantities_after",
            _normalized_quantities(target_plan.get("target_quantities_after")),
            expected_target_plan["target_quantities_after"],
        ),
        ("target_weight_execution.params_hash", target_execution.get("params_hash"), plan.params_hash),
        ("target_weight_execution.complete", target_execution.get("complete"), True),
        ("target_weight_execution.planned_orders", target_execution.get("planned_orders"), len(plan.orders)),
        ("target_weight_execution.idempotency_allowed", target_execution.get("idempotency_allowed"), True),
        (
            "target_weight_execution.execution_trade_day_allowed",
            target_execution.get("execution_trade_day_allowed"),
            True,
        ),
        (
            "target_weight_execution.execution_market_session_allowed",
            target_execution.get("execution_market_session_allowed"),
            True,
        ),
        (
            "target_weight_execution.pilot_authorization_snapshot_allowed",
            target_execution.get("pilot_authorization_snapshot_allowed"),
            True,
        ),
        ("target_weight_execution.preflight_refresh_complete", target_execution.get("preflight_refresh_complete"), True),
        ("target_weight_execution.pre_execution_complete", target_execution.get("pre_execution_complete"), True),
        ("target_weight_execution.liquidity_complete", target_execution.get("liquidity_complete"), True),
        ("target_weight_execution.pre_trade_risk_complete", target_execution.get("pre_trade_risk_complete"), True),
        ("target_weight_execution.order_count_complete", target_execution.get("order_count_complete"), True),
        ("target_weight_execution.order_result_complete", target_execution.get("order_result_complete"), True),
        ("target_weight_execution.order_complete", target_execution.get("order_complete"), True),
        (
            "target_weight_execution.order_result_reconciliation.complete",
            order_result_reconciliation.get("complete"),
            True,
        ),
        ("target_weight_execution.fill_complete", target_execution.get("fill_complete"), True),
        ("target_weight_execution.fill_reconciliation.complete", fill_reconciliation.get("complete"), True),
        ("target_weight_execution.position_reconciliation.complete", position_reconciliation.get("complete"), True),
    ]
    if "portfolio_drawdown_guard" in expected_target_plan or "portfolio_drawdown_guard" in target_plan:
        checks.append((
            "target_weight_plan.portfolio_drawdown_guard",
            _authorization_portfolio_drawdown_guard(target_plan.get("portfolio_drawdown_guard")),
            _authorization_portfolio_drawdown_guard(expected_target_plan.get("portfolio_drawdown_guard")),
        ))
    for field, actual, expected in checks:
        if actual != expected:
            result["mismatches"].append({
                "field": field,
                "expected": expected,
                "actual": actual,
            })
    numeric_checks = [
        (
            "target_weight_plan.target_exposure",
            target_plan.get("target_exposure"),
            expected_target_plan["target_exposure"],
            None,
        ),
        (
            "target_weight_plan.base_target_exposure",
            target_plan.get("base_target_exposure"),
            expected_target_plan["base_target_exposure"],
            None,
        ),
        (
            "target_weight_plan.gross_exposure_after",
            target_plan.get("gross_exposure_after"),
            expected_target_plan["gross_exposure_after"],
            _authorization_snapshot_money_tolerance(expected_target_plan["gross_exposure_after"]),
        ),
        (
            "target_weight_plan.max_order_notional",
            target_plan.get("max_order_notional"),
            expected_target_plan["max_order_notional"],
            _authorization_snapshot_money_tolerance(expected_target_plan["max_order_notional"]),
        ),
    ]
    for field, actual, expected, absolute_tolerance in numeric_checks:
        if not _numbers_match(actual, expected, absolute_tolerance=absolute_tolerance):
            result["mismatches"].append({
                "field": field,
                "expected": expected,
                "actual": actual,
                "tolerance": absolute_tolerance,
            })

    if not result["mismatches"]:
        proof_valid, proof_reason = _target_weight_record_proof_status(plan.candidate_id, record)
        if not proof_valid:
            result["mismatches"].append({
                "field": "target_weight_evidence_quality",
                "expected": "verified_target_weight_pilot_evidence",
                "actual": proof_reason,
            })

    if result["mismatches"]:
        preview = ", ".join(
            f"{item['field']} actual={item['actual']} expected={item['expected']}"
            for item in result["mismatches"][:5]
        )
        if len(result["mismatches"]) > 5:
            preview = f"{preview}, +{len(result['mismatches']) - 5} more"
        result["reason"] = f"target_weight_existing_evidence_invalid: {preview}"
        return result

    result["valid"] = True
    result["reason"] = "existing pilot_paper evidence verified"
    return result


def write_session_artifact(
    *,
    plan: TargetWeightPlan,
    pilot_check: Any,
    validation: Any,
    cap_preview: Any,
    cap_recommendation: dict[str, Any],
    liquidity_check: dict[str, Any],
    pre_trade_risk_check: dict[str, Any],
    execution: dict[str, Any],
    dry_run: bool,
    execution_trade_day_check: dict[str, Any] | None = None,
    execution_market_session_check: dict[str, Any] | None = None,
    pilot_authorization_snapshot_check: dict[str, Any] | None = None,
    execution_idempotency: dict[str, Any] | None = None,
    execution_lock: dict[str, Any] | None = None,
    execution_lock_release: dict[str, Any] | None = None,
    preflight_refresh: dict[str, Any] | None = None,
    fill_reconciliation: dict[str, Any] | None = None,
    shadow_evidence: dict[str, Any] | None = None,
    evidence_collection: dict[str, Any] | None = None,
    launch_artifacts: dict[str, Any] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "target_weight_rotation_pilot_session",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "dry_run": dry_run,
        "plan": plan.to_dict(),
        "pilot_check": _pilot_check_to_dict(pilot_check),
        "plan_validation": asdict(validation),
        "cap_preview": asdict(cap_preview),
        "cap_recommendation": cap_recommendation,
        "liquidity_check": liquidity_check,
        "pre_trade_risk_check": pre_trade_risk_check,
        "execution": execution,
        "execution_trade_day_check": (
            execution_trade_day_check or execution_trade_day_check_not_required()
        ),
        "execution_market_session_check": (
            execution_market_session_check or execution_market_session_check_not_required()
        ),
        "pilot_authorization_snapshot_check": (
            pilot_authorization_snapshot_check
            or _authorization_snapshot_not_required(
                "pilot authorization snapshot check not required"
            )
        ),
        "execution_idempotency": execution_idempotency or {"checked": False},
        "execution_lock": execution_lock or {"checked": False},
        "execution_lock_release": execution_lock_release or {"checked": False},
        "preflight_refresh": preflight_refresh or {"checked": False},
        "fill_reconciliation": fill_reconciliation or {"checked": False},
        "shadow_evidence": shadow_evidence or {"attempted": False, "recorded": False},
        "evidence_collection": evidence_collection or {"attempted": False, "recorded": False},
        "launch_artifacts": launch_artifacts or {"attempted": False},
        "live_safety": {
            "live_enabled": False,
            "note": "adapter refuses live mode; live gate remains canonical-artifact + paper-evidence driven",
        },
    }
    path = output_dir / f"target_weight_pilot_session_{plan.candidate_id}_{plan.trade_day}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _shadow_benchmark_status(plan: TargetWeightPlan) -> str:
    if plan.score_day is None:
        return "failed"
    if plan.diagnostics.get("missing_symbols"):
        return "provisional"
    return "final"


def record_shadow_evidence_for_plan(
    plan: TargetWeightPlan,
    *,
    validation: Any,
) -> Any:
    """Record dry-run plan readiness without creating promotable evidence."""
    from core.paper_evidence import append_shadow_plan_evidence

    benchmark_status = _shadow_benchmark_status(plan)
    missing_symbols = list(plan.diagnostics.get("missing_symbols", []))
    benchmark_meta = {
        "source": "target_weight_shadow_plan",
        "candidate_id": plan.candidate_id,
        "params_hash": plan.params_hash,
        "symbol_count": len(plan.symbols),
        "target_count": len(plan.targets),
        "orders_planned": len(plan.orders),
        "missing_symbols": missing_symbols,
        "benchmark_symbol": plan.diagnostics.get("benchmark_symbol"),
        "score_day": plan.score_day,
        "target_exposure": plan.target_exposure,
        "risk_off": plan.risk_off,
        "performance_excess_computed": False,
    }
    diagnostics = [{
        "ok": benchmark_status == "final",
        "text": "target_weight_dry_run_plan",
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "targets": plan.targets,
        "orders_planned": len(plan.orders),
        "pilot_validation_allowed": getattr(validation, "allowed", False),
        "pilot_validation_reason": getattr(validation, "reason", ""),
        "dry_run_only": True,
    }]

    return append_shadow_plan_evidence(
        strategy=plan.candidate_id,
        date=plan.trade_day,
        total_value=plan.nav,
        cash=plan.cash_before,
        invested=plan.market_value_before,
        position_count=plan.target_position_count,
        watchlist_symbols=plan.symbols,
        diagnostics=diagnostics,
        benchmark_status=benchmark_status,
        benchmark_meta=benchmark_meta,
    )


def append_target_weight_cap_section(
    runbook_path: Path,
    *,
    plan: TargetWeightPlan,
    cap_preview: Any,
    cap_recommendation: dict[str, Any],
) -> None:
    """Append target-weight-specific cap guidance to the generic pilot runbook."""
    minimum = cap_recommendation["minimum_caps"]
    suggested = cap_recommendation["suggested_caps"]
    suggested_preview = cap_recommendation["suggested_preview"]
    preview_status = "PASS" if getattr(cap_preview, "allowed", False) else "BLOCKED"
    suggested_status = "PASS" if suggested_preview.get("allowed") else "BLOCKED"
    liquidity_check = assess_plan_liquidity(plan)
    liquidity_status = "PASS" if liquidity_check["complete"] else "BLOCKED"
    pre_trade_risk_check = assess_plan_pre_trade_risk(plan)
    pre_trade_risk_status = "PASS" if pre_trade_risk_check["complete"] else "BLOCKED"
    existing = runbook_path.read_text(encoding="utf-8") if runbook_path.exists() else ""
    section = [
        "",
        "## Target-weight Cap Recommendation",
        f"- Planned orders: {len(plan.orders)}",
        f"- Target positions after rebalance: {plan.target_position_count}",
        f"- Max order notional: {plan.max_order_notional:,.0f}",
        f"- Gross exposure after rebalance: {plan.gross_exposure_after:,.0f}",
        f"- Current preview status: **{preview_status}** - {getattr(cap_preview, 'reason', '')}",
        f"- Suggested cap preview: **{suggested_status}** - {suggested_preview.get('reason', '')}",
        "",
        "Minimum caps for this dry-run plan:",
        f"- max_orders_per_day: {minimum['max_orders_per_day']}",
        f"- max_concurrent_positions: {minimum['max_concurrent_positions']}",
        f"- max_notional_per_trade: {minimum['max_notional_per_trade']:,}",
        f"- max_gross_exposure: {minimum['max_gross_exposure']:,}",
        "",
        "Suggested first-pilot caps:",
        f"- max_orders_per_day: {suggested['max_orders_per_day']}",
        f"- max_concurrent_positions: {suggested['max_concurrent_positions']}",
        f"- max_notional_per_trade: {suggested['max_notional_per_trade']:,}",
        f"- max_gross_exposure: {suggested['max_gross_exposure']:,}",
        "",
        "Liquidity preflight:",
        f"- status: **{liquidity_status}** - {liquidity_check['reason']}",
        f"- max_order_adv_pct: {liquidity_check['max_order_adv_pct']:.2f}",
        f"- lookback_days: {liquidity_check['lookback_days']}",
        "",
        "Pre-trade risk validation:",
        f"- status: **{pre_trade_risk_status}** - {pre_trade_risk_check.get('reason', 'not checked')}",
        f"- projected_cash_ratio_after_costs: {float(pre_trade_risk_check.get('projected_cash_ratio_after_costs') or 0):.2%}",
        f"- projected_investment_ratio_after_costs: {float(pre_trade_risk_check.get('projected_investment_ratio_after_costs') or 0):.2%}",
        f"- estimated_costs: {float((pre_trade_risk_check.get('cost_summary') or {}).get('total_explicit_costs') or 0):,.0f}",
        "",
        "Suggested enable command:",
        "```bash",
        cap_recommendation["enable_command"],
        "```",
        "",
        cap_recommendation["operator_note"],
    ]
    runbook_path.write_text(existing.rstrip() + "\n" + "\n".join(section) + "\n", encoding="utf-8")


def generate_launch_artifacts(
    candidate_id: str,
    *,
    include_runbook: bool = True,
    plan: TargetWeightPlan | None = None,
    cap_preview: Any | None = None,
    cap_recommendation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate launch-readiness artifacts after shadow/pilot planning."""
    from core.paper_pilot import (
        compute_launch_readiness,
        generate_launch_readiness_artifact,
        generate_pilot_runbook,
    )

    readiness = compute_launch_readiness(candidate_id)
    json_path, md_path = generate_launch_readiness_artifact(candidate_id)
    result: dict[str, Any] = {
        "attempted": True,
        "launch_readiness": {
            "json_path": str(json_path),
            "md_path": str(md_path),
            "infra_ready": readiness.get("infra_ready", False),
            "launch_ready": readiness.get("launch_ready", False),
            "clean_final_days_current": readiness.get("clean_final_days_current", 0),
            "clean_final_days_required": readiness.get("clean_final_days_required", 0),
            "shadow_days": readiness.get("shadow_days", 0),
            "strategy": readiness.get("strategy", candidate_id),
            "blocking_requirements": readiness.get("blocking_requirements", []),
        },
        "runbook_path": None,
    }
    if include_runbook:
        runbook_path = generate_pilot_runbook(candidate_id)
        if plan is not None and cap_preview is not None and cap_recommendation is not None:
            append_target_weight_cap_section(
                runbook_path,
                plan=plan,
                cap_preview=cap_preview,
                cap_recommendation=cap_recommendation,
            )
        result["runbook_path"] = str(runbook_path)
    return result


def _unique_reasons(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for reason in reasons:
        clean_reason = str(reason).strip()
        if not clean_reason or clean_reason in seen:
            continue
        seen.add(clean_reason)
        unique.append(clean_reason)
    return unique


def _plan_summary(plan: TargetWeightPlan) -> dict[str, Any]:
    return {
        "candidate_id": plan.candidate_id,
        "as_of_date": plan.as_of_date,
        "trade_day": plan.trade_day,
        "score_day": plan.score_day,
        "params_hash": plan.params_hash,
        "symbol_count": len(plan.symbols),
        "target_count": len(plan.targets),
        "targets": list(plan.targets),
        "order_count": len(plan.orders),
        "target_position_count": int(plan.target_position_count),
        "target_exposure": plan.target_exposure,
        "base_target_exposure": plan.base_target_exposure,
        "risk_off": plan.risk_off,
        "nav": plan.nav,
        "cash_before": plan.cash_before,
        "cash_after_estimate": plan.cash_after_estimate,
        "gross_exposure_after": plan.gross_exposure_after,
        "max_order_notional": plan.max_order_notional,
    }


def _normalize_diagnostic_date(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    try:
        return datetime.fromisoformat(clean.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return clean[:10]


def _plan_data_quality_symbols(plan: TargetWeightPlan) -> list[str]:
    symbols: set[str] = set(plan.symbols)
    symbols.update(plan.targets)
    symbols.update(plan.prices)
    symbols.update(order.symbol for order in plan.orders)
    symbols.update(
        symbol
        for symbol, quantity in (plan.position_quantities_before or {}).items()
        if int(quantity or 0) > 0
    )
    symbols.update(
        symbol
        for symbol, quantity in (plan.target_quantities_after or {}).items()
        if int(quantity or 0) > 0
    )
    return sorted(str(symbol) for symbol in symbols if str(symbol).strip())


def data_quality_check_not_available(
    reason: str = "target_weight_data_quality_not_checked",
) -> dict[str, Any]:
    return {
        "checked": False,
        "complete": False,
        "reason": reason,
        "trade_day": None,
        "score_day": None,
        "symbols_checked": 0,
        "required_symbols": [],
        "price_last_dates": {},
        "missing_price_last_date_symbols": [],
        "stale_price_symbols": [],
        "missing_symbols": [],
        "missing_position_symbols": [],
        "benchmark_symbol": None,
        "benchmark_last_date": None,
        "benchmark_stale": False,
        "violations": [reason],
        "warnings": [],
    }


def _check_display_status(check: dict[str, Any] | None) -> str:
    payload = check or {}
    if not payload.get("checked", True):
        return "NOT CHECKED"
    if payload.get("complete", payload.get("allowed", False)):
        return "PASS"
    return "BLOCKED"


def _check_passed(check: dict[str, Any] | None) -> bool:
    payload = check or {}
    if not payload.get("checked", False):
        return False
    if "allowed" in payload and not payload.get("allowed", False):
        return False
    if "complete" in payload and not payload.get("complete", False):
        return False
    return "allowed" in payload or "complete" in payload


def assess_plan_data_quality(plan: TargetWeightPlan) -> dict[str, Any]:
    """Validate target-weight price freshness diagnostics before operator action."""
    diagnostics = plan.diagnostics or {}
    price_last_dates = diagnostics.get("price_last_dates") or {}
    if not isinstance(price_last_dates, dict):
        price_last_dates = {}

    required_symbols = _plan_data_quality_symbols(plan)
    trade_day = _normalize_diagnostic_date(plan.trade_day)
    score_day = _normalize_diagnostic_date(plan.score_day)
    normalized_price_dates = {
        str(symbol): _normalize_diagnostic_date(day)
        for symbol, day in price_last_dates.items()
    }
    missing_symbols = sorted(str(symbol) for symbol in diagnostics.get("missing_symbols") or [])
    missing_position_symbols = sorted(
        str(symbol) for symbol in diagnostics.get("missing_position_symbols") or []
    )
    violations: list[str] = []
    warnings: list[str] = []

    if missing_symbols:
        violations.append(f"missing universe price data: {', '.join(missing_symbols[:10])}")
    if missing_position_symbols:
        violations.append(
            f"positions without price data: {', '.join(missing_position_symbols[:10])}"
        )
    if not normalized_price_dates:
        violations.append("missing price_last_dates diagnostics")

    missing_price_last_date_symbols = [
        symbol for symbol in required_symbols if symbol not in normalized_price_dates
    ]
    if missing_price_last_date_symbols:
        violations.append(
            "missing price freshness dates: "
            + ", ".join(missing_price_last_date_symbols[:10])
        )

    stale_price_symbols: dict[str, str] = {}
    invalid_price_date_symbols: list[str] = []
    for symbol in required_symbols:
        price_day = normalized_price_dates.get(symbol)
        if not price_day:
            if symbol in normalized_price_dates:
                invalid_price_date_symbols.append(symbol)
            continue
        if trade_day and price_day < trade_day:
            stale_price_symbols[symbol] = price_day
    if invalid_price_date_symbols:
        violations.append(
            "invalid price freshness dates: "
            + ", ".join(invalid_price_date_symbols[:10])
        )
    if stale_price_symbols:
        preview = ", ".join(
            f"{symbol}={day}" for symbol, day in list(stale_price_symbols.items())[:10]
        )
        violations.append(f"stale symbol price data: {preview}")

    benchmark_symbol = diagnostics.get("benchmark_symbol")
    benchmark_last_date = _normalize_diagnostic_date(diagnostics.get("benchmark_last_date"))
    benchmark_stale = False
    if benchmark_symbol and score_day:
        if not benchmark_last_date:
            violations.append(
                f"missing benchmark_last_date: benchmark={benchmark_symbol} score_day={score_day}"
            )
        elif benchmark_last_date < score_day:
            benchmark_stale = True
            violations.append(
                "stale benchmark price data: "
                f"benchmark={benchmark_symbol} latest={benchmark_last_date} score_day={score_day}"
            )
        elif trade_day and benchmark_last_date < trade_day:
            benchmark_stale = True
            violations.append(
                "benchmark_latest_before_trade_day: "
                f"benchmark={benchmark_symbol} latest={benchmark_last_date} trade_day={trade_day}"
            )

    complete = not violations
    reason = "target_weight_data_quality_passed"
    if not complete:
        preview = "; ".join(violations[:5])
        if len(violations) > 5:
            preview = f"{preview}; +{len(violations) - 5} more"
        reason = f"target_weight_data_quality_failed: {preview}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "trade_day": trade_day,
        "score_day": score_day,
        "symbols_checked": len(required_symbols),
        "required_symbols": required_symbols,
        "price_last_dates": {
            symbol: normalized_price_dates.get(symbol)
            for symbol in required_symbols
            if symbol in normalized_price_dates
        },
        "missing_price_last_date_symbols": missing_price_last_date_symbols,
        "stale_price_symbols": stale_price_symbols,
        "missing_symbols": missing_symbols,
        "missing_position_symbols": missing_position_symbols,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_last_date": benchmark_last_date,
        "benchmark_stale": benchmark_stale,
        "violations": violations,
        "warnings": warnings,
    }


def assess_plan_liquidity(
    plan: TargetWeightPlan,
    *,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    lookback_days: int = DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
) -> dict[str, Any]:
    liquidity = (plan.diagnostics or {}).get("liquidity") or {}
    symbols = liquidity.get("symbols") or {}
    if not symbols:
        return {
            "checked": True,
            "complete": False,
            "reason": "target_weight_liquidity_preflight_failed: missing liquidity diagnostics",
            "lookback_days": int(lookback_days),
            "max_order_adv_pct": float(max_order_adv_pct),
            "orders_checked": 0,
            "orders": [],
            "violations": ["missing liquidity diagnostics"],
        }

    orders: list[dict[str, Any]] = []
    violations: list[str] = []
    for order in plan.orders:
        symbol_liquidity = symbols.get(order.symbol) or {}
        avg_daily_value = symbol_liquidity.get("avg_daily_value")
        last_daily_value = symbol_liquidity.get("last_daily_value")
        item = {
            "symbol": order.symbol,
            "action": order.action,
            "quantity": int(order.quantity),
            "order_notional": float(order.notional),
            "lookback_observations": int(symbol_liquidity.get("observations", 0) or 0),
            "avg_daily_value": avg_daily_value,
            "last_daily_value": last_daily_value,
            "order_adv_pct": None,
            "complete": False,
            "reason": "",
        }
        if not symbol_liquidity.get("complete", False) or avg_daily_value is None or float(avg_daily_value) <= 0:
            item["reason"] = str(symbol_liquidity.get("reason") or "missing liquidity data")
            violations.append(f"{order.symbol}: {item['reason']}")
        else:
            order_adv_pct = float(order.notional) / float(avg_daily_value) * 100
            item["order_adv_pct"] = round(order_adv_pct, 4)
            if order_adv_pct > float(max_order_adv_pct):
                item["reason"] = (
                    f"order ADV {order_adv_pct:.2f}% > max {float(max_order_adv_pct):.2f}%"
                )
                violations.append(f"{order.symbol}: {item['reason']}")
            else:
                item["complete"] = True
                item["reason"] = "within liquidity cap"
        orders.append(item)

    complete = len(violations) == 0
    reason = "target_weight_liquidity_preflight_passed"
    if not complete:
        preview = "; ".join(violations[:5])
        if len(violations) > 5:
            preview = f"{preview}; +{len(violations) - 5} more"
        reason = f"target_weight_liquidity_preflight_failed: {preview}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "lookback_days": int(liquidity.get("lookback_days") or lookback_days),
        "max_order_adv_pct": float(max_order_adv_pct),
        "orders_checked": len(plan.orders),
        "orders": orders,
        "violations": violations,
    }


def failed_liquidity_preflight(
    plan: TargetWeightPlan,
    error: Exception,
    *,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    lookback_days: int = DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
) -> dict[str, Any]:
    return {
        "checked": True,
        "complete": False,
        "reason": f"target_weight_liquidity_preflight_failed: {error}",
        "lookback_days": int(lookback_days),
        "max_order_adv_pct": float(max_order_adv_pct),
        "orders_checked": len(plan.orders),
        "orders": [],
        "violations": [str(error)],
    }


def _avg_daily_volume_for_order(plan: TargetWeightPlan, order: Any) -> float | None:
    liquidity = (plan.diagnostics or {}).get("liquidity") or {}
    symbol_liquidity = (liquidity.get("symbols") or {}).get(order.symbol) or {}
    avg_daily_value = symbol_liquidity.get("avg_daily_value")
    if avg_daily_value is None or float(avg_daily_value) <= 0 or float(order.price) <= 0:
        return None
    return float(avg_daily_value) / float(order.price)


def _position_avg_price_for_order(plan: TargetWeightPlan, order: Any) -> float:
    avg_prices = (plan.diagnostics or {}).get("position_avg_prices_before") or {}
    avg_price = avg_prices.get(order.symbol)
    if avg_price is None or float(avg_price) <= 0:
        return float(order.price)
    return float(avg_price)


def refresh_paper_preflight_status(strategy: str, date: str) -> dict[str, Any]:
    """Refresh paper preflight so readiness uses the current notifier/runtime gates."""
    try:
        import core.paper_pilot as paper_pilot
        import core.paper_preflight as paper_preflight

        previous_runtime_dir = paper_preflight.RUNTIME_DIR
        paper_preflight.RUNTIME_DIR = paper_pilot.RUNTIME_DIR
        try:
            result = paper_preflight.run_preflight(strategy, date)
        finally:
            paper_preflight.RUNTIME_DIR = previous_runtime_dir
        preflight_complete = str(result.overall).lower() != "fail"
        reason = (
            "paper preflight refreshed"
            if preflight_complete
            else f"paper preflight failed: {'; '.join(result.block_reasons) or result.overall}"
        )
        return {
            "checked": True,
            "complete": preflight_complete,
            "reason": reason,
            "strategy": result.strategy,
            "date": result.date,
            "overall": result.overall,
            "entry_allowed": bool(result.entry_allowed),
            "runtime_state": result.runtime_state,
            "notifier_health": result.notifier_health,
            "pilot_authorized": bool(result.pilot_authorized),
            "blocking_requirements": list(result.blocking_requirements or []),
            "block_reasons": list(result.block_reasons or []),
        }
    except Exception as exc:
        logger.exception("target-weight readiness preflight refresh failed for {}", strategy)
        return {
            "checked": True,
            "complete": False,
            "reason": f"target_weight_preflight_refresh_failed: {exc}",
            "strategy": strategy,
            "date": date,
        }


def assess_plan_pre_trade_risk(
    plan: TargetWeightPlan,
    *,
    config: Any | None = None,
    risk_manager: Any | None = None,
) -> dict[str, Any]:
    if risk_manager is None:
        from config.config_loader import Config
        from core.risk_manager import RiskManager

        risk_config = config if hasattr(config, "risk_params") else Config.get()
        risk_manager = RiskManager(risk_config)

    risk_params = getattr(risk_manager, "risk_params", None)
    if risk_params is None:
        risk_params = getattr(getattr(risk_manager, "config", None), "risk_params", {}) or {}
    div = (risk_params or {}).get("diversification", {})
    max_position_ratio = float(div.get("max_position_ratio", 0.20))
    max_investment_ratio = float(div.get("max_investment_ratio", 0.70))
    min_cash_ratio = float(div.get("min_cash_ratio", 0.20))
    max_positions = int(div.get("max_positions", 10))

    cash = float(plan.cash_before)
    order_costs: list[dict[str, Any]] = []
    violations: list[str] = []
    warnings: list[str] = []
    total_commission = 0.0
    total_tax = 0.0
    total_slippage = 0.0
    total_capital_gains_tax = 0.0
    projected_position_prices: dict[str, float] = {}

    for order in plan.orders:
        action = str(order.action).upper()
        avg_daily_volume = _avg_daily_volume_for_order(plan, order)
        avg_price = _position_avg_price_for_order(plan, order) if action == "SELL" else None
        costs = risk_manager.calculate_transaction_costs(
            float(order.price),
            int(order.quantity),
            action,
            avg_daily_volume=avg_daily_volume,
            avg_price=avg_price,
        )
        commission = float(costs.get("commission", 0) or 0)
        tax = float(costs.get("tax", 0) or 0)
        capital_gains_tax = float(costs.get("capital_gains_tax", 0) or 0)
        slippage = float(costs.get("slippage", 0) or 0)
        execution_price = float(costs.get("execution_price", order.price) or order.price)
        projected_position_prices[order.symbol] = execution_price
        total_commission += commission
        total_tax += tax
        total_capital_gains_tax += capital_gains_tax
        total_slippage += slippage

        before_cash = cash
        item = {
            "symbol": order.symbol,
            "action": order.action,
            "quantity": int(order.quantity),
            "plan_price": float(order.price),
            "execution_price": execution_price,
            "avg_daily_volume": avg_daily_volume,
            "commission": commission,
            "tax": tax,
            "capital_gains_tax": capital_gains_tax,
            "slippage": slippage,
            "participation_rate": costs.get("participation_rate"),
            "slippage_multiplier": costs.get("slippage_multiplier"),
            "cash_before": round(before_cash, 2),
        }
        if action == "SELL":
            proceeds = execution_price * int(order.quantity) - commission - tax - capital_gains_tax
            cash += proceeds
            item["cash_delta"] = round(proceeds, 2)
            item["required_cash"] = 0.0
        else:
            required = execution_price * int(order.quantity) + commission
            item["required_cash"] = round(required, 2)
            item["cash_delta"] = round(-required, 2)
            if required > cash + 1e-6:
                violations.append(
                    f"{order.symbol}: required cash {required:,.0f} > projected cash {cash:,.0f}"
                )
            cash -= required
        item["cash_after"] = round(cash, 2)
        order_costs.append(item)

    expected_quantities = _expected_position_quantities(plan)
    position_values: dict[str, float] = {}
    missing_price_symbols: list[str] = []
    for symbol, quantity in expected_quantities.items():
        if int(quantity) <= 0:
            continue
        price = projected_position_prices.get(symbol, plan.prices.get(symbol))
        if price is None or float(price) <= 0:
            missing_price_symbols.append(symbol)
            continue
        position_values[symbol] = float(price) * int(quantity)

    for symbol in missing_price_symbols:
        violations.append(f"{symbol}: missing price for projected position risk check")

    projected_gross = sum(position_values.values())
    projected_total_value = cash + projected_gross
    if projected_total_value <= 0:
        violations.append("projected total value after costs is not positive")
        projected_cash_ratio = 0.0
        projected_investment_ratio = 1.0
    else:
        projected_cash_ratio = cash / projected_total_value
        projected_investment_ratio = projected_gross / projected_total_value

    target_position_count = sum(1 for value in position_values.values() if value > 0)
    if target_position_count > max_positions:
        violations.append(f"target positions {target_position_count} > max_positions {max_positions}")

    if projected_investment_ratio > max_investment_ratio + 1e-9:
        violations.append(
            f"projected investment ratio {projected_investment_ratio:.2%} > max {max_investment_ratio:.2%}"
        )
    if projected_cash_ratio < min_cash_ratio - 1e-9:
        violations.append(
            f"projected cash ratio {projected_cash_ratio:.2%} < min {min_cash_ratio:.2%}"
        )

    position_ratio_rows: list[dict[str, Any]] = []
    if projected_total_value > 0:
        for symbol, value in sorted(position_values.items()):
            ratio = value / projected_total_value
            valuation_price = projected_position_prices.get(symbol, plan.prices.get(symbol))
            position_ratio_rows.append({
                "symbol": symbol,
                "value": round(value, 2),
                "valuation_price": round(float(valuation_price), 4),
                "ratio": round(ratio, 6),
            })
            if ratio > max_position_ratio + 1e-9:
                violations.append(
                    f"{symbol}: projected position ratio {ratio:.2%} > max {max_position_ratio:.2%}"
                )

    complete = len(violations) == 0
    reason = "target_weight_pre_trade_risk_passed"
    if not complete:
        preview = "; ".join(violations[:5])
        if len(violations) > 5:
            preview = f"{preview}; +{len(violations) - 5} more"
        reason = f"target_weight_pre_trade_risk_failed: {preview}"

    return {
        "checked": True,
        "complete": complete,
        "reason": reason,
        "violations": violations,
        "warnings": warnings,
        "projected_cash_after_costs": round(cash, 2),
        "projected_cash_ratio_after_costs": round(projected_cash_ratio, 6),
        "projected_gross_exposure_after_costs": round(projected_gross, 2),
        "projected_investment_ratio_after_costs": round(projected_investment_ratio, 6),
        "projected_total_value_after_costs": round(projected_total_value, 2),
        "target_position_count": target_position_count,
        "projected_position_prices": {
            symbol: round(float(price), 4)
            for symbol, price in sorted(projected_position_prices.items())
        },
        "limits": {
            "max_position_ratio": max_position_ratio,
            "max_investment_ratio": max_investment_ratio,
            "min_cash_ratio": min_cash_ratio,
            "max_positions": max_positions,
        },
        "position_ratios": position_ratio_rows,
        "order_costs": order_costs,
        "cost_summary": {
            "commission": round(total_commission, 2),
            "tax": round(total_tax, 2),
            "capital_gains_tax": round(total_capital_gains_tax, 2),
            "slippage": round(total_slippage, 2),
            "total_explicit_costs": round(
                total_commission + total_tax + total_capital_gains_tax + total_slippage,
                2,
            ),
        },
    }


def failed_pre_trade_risk_validation(plan: TargetWeightPlan, error: Exception) -> dict[str, Any]:
    return {
        "checked": True,
        "complete": False,
        "reason": f"target_weight_pre_trade_risk_failed: {error}",
        "violations": [str(error)],
        "warnings": [],
        "projected_cash_after_costs": None,
        "projected_cash_ratio_after_costs": None,
        "projected_gross_exposure_after_costs": None,
        "projected_investment_ratio_after_costs": None,
        "projected_total_value_after_costs": None,
        "target_position_count": int(plan.target_position_count),
        "projected_position_prices": {},
        "limits": {},
        "position_ratios": [],
        "order_costs": [],
        "cost_summary": {},
    }


def _build_readiness_operator_commands(
    plan: TargetWeightPlan,
    cap_recommendation: dict[str, Any],
    execution_trade_day_check: dict[str, Any] | None = None,
    execution_market_session_check: dict[str, Any] | None = None,
    pilot_authorization_snapshot_check: dict[str, Any] | None = None,
    execute_block_reason: str | None = None,
) -> dict[str, str]:
    base = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {plan.candidate_id} --as-of-date {plan.as_of_date}"
    )
    repair_base = f"python tools/target_weight_rotation_pilot.py --candidate-id {plan.candidate_id}"
    execute_command = f"{base} --execute --collect-evidence"
    if execute_block_reason:
        execute_command = f"# blocked: {execute_block_reason}"
    elif execution_trade_day_check and not execution_trade_day_check.get("allowed", True):
        execute_command = (
            "# blocked: "
            f"{execution_trade_day_check.get('reason', 'execution trade day check failed')}"
        )
    elif (
        execution_market_session_check
        and execution_market_session_check.get("checked", False)
        and not execution_market_session_check.get("allowed", True)
    ):
        execute_command = (
            "# blocked: "
            f"{execution_market_session_check.get('reason', 'execution market session check failed')}"
        )
    elif (
        pilot_authorization_snapshot_check
        and pilot_authorization_snapshot_check.get("checked", False)
        and not pilot_authorization_snapshot_check.get("allowed", True)
    ):
        execute_command = (
            "# blocked: "
            f"{pilot_authorization_snapshot_check.get('reason', 'pilot authorization snapshot check failed')}"
        )
    return {
        "collect_shadow_days": (
            "python tools/target_weight_rotation_pilot.py "
            f"--candidate-id {plan.candidate_id} --shadow-days 3 --shadow-end-date {plan.as_of_date}"
        ),
        "rerun_readiness_audit": f"{base} --readiness-audit",
        "enable_suggested_caps": str(cap_recommendation.get("enable_command", "")).strip(),
        "execute_capped_paper": execute_command,
        "finalize_pilot_evidence": f"{repair_base} --finalize-pilot-evidence --finalize-date {plan.trade_day}",
        "repair_pilot_evidence": f"{repair_base} --repair-pilot-evidence --repair-date {plan.trade_day}",
    }


def _first_text(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    for item in items:
        text = str(item).strip()
        if text:
            return text
    return None


def _target_weight_finalize_first_invalid_reasons(invalid_reasons: Any) -> bool:
    if not isinstance(invalid_reasons, dict):
        return False
    return any(reason in REPAIRABLE_TARGET_WEIGHT_EVIDENCE_REASONS for reason in invalid_reasons)


def _readiness_execute_block_reason(
    *,
    ready_for_capped_pilot: bool,
    ready_for_cap_approval: bool,
    launch_readiness: dict[str, Any],
    validation: Any,
    execution_trade_day_check: dict[str, Any],
    execution_market_session_check: dict[str, Any],
    pilot_authorization_snapshot_check: dict[str, Any],
    blocking_reasons: list[str],
    next_action: str,
) -> str | None:
    if ready_for_capped_pilot:
        return None
    if (
        execution_trade_day_check.get("checked", False)
        and not execution_trade_day_check.get("allowed", False)
    ):
        return str(execution_trade_day_check.get("reason", "execution trade day check failed"))
    if (
        execution_market_session_check.get("checked", False)
        and not execution_market_session_check.get("allowed", False)
    ):
        return str(execution_market_session_check.get("reason", "execution market session check failed"))
    if not ready_for_cap_approval:
        return _first_text(blocking_reasons) or "readiness audit is not ready for cap approval"
    if not launch_readiness.get("pilot_authorization_present", False):
        return "pilot authorization is not active; enable suggested caps, then rerun readiness audit"
    if (
        pilot_authorization_snapshot_check.get("checked", False)
        and not pilot_authorization_snapshot_check.get("allowed", False)
    ):
        return str(pilot_authorization_snapshot_check.get("reason", "pilot authorization snapshot check failed"))
    if not launch_readiness.get("launch_ready", False):
        blocker = _first_text(launch_readiness.get("blocking_requirements"))
        return f"launch readiness is not ready: {blocker or 'requirements are not met'}"
    if not getattr(validation, "allowed", False):
        return str(getattr(validation, "reason", "pilot plan blocked"))
    return f"readiness audit is not READY_TO_EXECUTE; next action: {next_action}"


def _readiness_audit_execute_block_reason(readiness_audit: dict[str, Any] | None) -> str | None:
    if not readiness_audit or readiness_audit.get("ready_for_capped_pilot"):
        return None
    for key, default_reason in (
        ("execution_trade_day_check", "execution trade day check failed"),
        ("execution_market_session_check", "execution market session check failed"),
        ("pilot_authorization_snapshot_check", "pilot authorization snapshot check failed"),
    ):
        check = readiness_audit.get(key) or {}
        if check.get("checked", False) and not check.get("allowed", False):
            return str(check.get("reason", default_reason))
    blocker = _first_text(readiness_audit.get("blocking_reasons"))
    if blocker:
        return blocker
    next_action = str(readiness_audit.get("next_action") or "rerun readiness audit")
    return f"readiness audit is not READY_TO_EXECUTE; next action: {next_action}"


def build_pilot_readiness_audit(
    *,
    plan: TargetWeightPlan,
    pilot_check: Any,
    validation: Any,
    cap_preview: Any,
    cap_recommendation: dict[str, Any],
    preflight_refresh: dict[str, Any],
    launch_readiness: dict[str, Any],
    execution_idempotency: dict[str, Any],
    execution_trade_day_check: dict[str, Any],
    execution_market_session_check: dict[str, Any],
    pilot_authorization_snapshot_check: dict[str, Any],
    pre_execution_reconciliation: dict[str, Any],
    liquidity_check: dict[str, Any],
    pre_trade_risk_check: dict[str, Any],
    trading_mode: str,
) -> dict[str, Any]:
    """Combine launch, cap, duplicate, position, liquidity, and cost checks into one no-order audit."""
    suggested_preview = cap_recommendation.get("suggested_preview", {})
    blockers: list[str] = []
    warnings: list[str] = []
    data_quality_check = assess_plan_data_quality(plan)

    if trading_mode == "live":
        blockers.append("trading_mode: target-weight pilot requires paper mode, not live")

    if not data_quality_check.get("complete", False):
        blockers.append(
            "data_quality: "
            f"{data_quality_check.get('reason', 'target-weight data quality check failed')}"
        )
    for warning in data_quality_check.get("warnings") or []:
        warnings.append(f"data_quality: {warning}")

    if preflight_refresh.get("checked", False) and not preflight_refresh.get("complete", False):
        blockers.append(
            "preflight_refresh: "
            f"{preflight_refresh.get('reason', 'paper preflight refresh failed')}"
        )
    for blocker in launch_readiness.get("blocking_requirements", []) or []:
        blockers.append(f"launch_readiness: {blocker}")
    if not launch_readiness.get("infra_ready", False):
        blockers.append("launch_readiness: infrastructure requirements are not met")
    if not launch_readiness.get("pilot_authorization_present", False):
        blockers.append("pilot_authorization: no active capped pilot authorization")
    if not getattr(validation, "allowed", False):
        blockers.append(f"pilot_validation: {getattr(validation, 'reason', 'pilot plan blocked')}")
    if not execution_idempotency.get("allowed", False):
        blockers.append(
            "execution_idempotency: "
            f"{execution_idempotency.get('reason', 'previous pilot session found')}"
        )
    if execution_trade_day_check.get("checked", False) and not execution_trade_day_check.get("allowed", False):
        blockers.append(
            "execution_trade_day: "
            f"{execution_trade_day_check.get('reason', 'plan trade day does not match execution day')}"
        )
    if (
        execution_market_session_check.get("checked", False)
        and not execution_market_session_check.get("allowed", False)
    ):
        blockers.append(
            "execution_market_session: "
            f"{execution_market_session_check.get('reason', 'market session is closed')}"
        )
    if (
        pilot_authorization_snapshot_check.get("checked", False)
        and not pilot_authorization_snapshot_check.get("allowed", False)
    ):
        blockers.append(
            "pilot_authorization_snapshot: "
            f"{pilot_authorization_snapshot_check.get('reason', 'approved plan snapshot does not match current plan')}"
        )
    if not pre_execution_reconciliation.get("complete", False):
        blockers.append(
            "pre_execution_positions: "
            f"{pre_execution_reconciliation.get('reason', 'starting positions do not match plan')}"
        )
    if liquidity_check.get("checked", False) and not liquidity_check.get("complete", False):
        blockers.append(
            "liquidity_preflight: "
            f"{liquidity_check.get('reason', 'planned orders exceed liquidity cap')}"
        )
    if pre_trade_risk_check.get("checked", False) and not pre_trade_risk_check.get("complete", False):
        blockers.append(
            "pre_trade_risk: "
            f"{pre_trade_risk_check.get('reason', 'projected cost-adjusted plan breaches risk limits')}"
        )
    if suggested_preview and not suggested_preview.get("allowed", False):
        blockers.append(
            "suggested_caps: "
            f"{suggested_preview.get('reason', 'suggested caps do not satisfy plan')}"
        )

    if not getattr(cap_preview, "allowed", False):
        warnings.append(f"preview_caps: {getattr(cap_preview, 'reason', 'preview caps blocked')}")
    if not liquidity_check.get("checked", False):
        warnings.append(f"liquidity_preflight: {liquidity_check.get('reason', 'not checked')}")
    if not pre_trade_risk_check.get("checked", False):
        warnings.append(f"pre_trade_risk: {pre_trade_risk_check.get('reason', 'not checked')}")
    if len(plan.orders) == 0:
        warnings.append("plan_orders: no rebalance orders for this trade day")

    blockers = _unique_reasons(blockers)
    warnings = _unique_reasons(warnings)
    ready_for_cap_approval = (
        trading_mode != "live"
        and bool(preflight_refresh.get("complete", False))
        and bool(launch_readiness.get("infra_ready", False))
        and bool(execution_idempotency.get("allowed", False))
        and bool(execution_trade_day_check.get("allowed", False))
        and bool(pre_execution_reconciliation.get("complete", False))
        and bool(data_quality_check.get("complete", False))
        and bool(liquidity_check.get("complete", False))
        and bool(pre_trade_risk_check.get("complete", False))
        and bool(suggested_preview.get("allowed", False))
    )
    trade_day_passed = _check_passed(execution_trade_day_check)
    market_session_passed = _check_passed(execution_market_session_check)
    authorization_snapshot_passed = _check_passed(pilot_authorization_snapshot_check)
    ready_for_capped_pilot = (
        ready_for_cap_approval
        and bool(launch_readiness.get("launch_ready", False))
        and bool(getattr(validation, "allowed", False))
        and trade_day_passed
        and market_session_passed
        and authorization_snapshot_passed
        and not blockers
    )

    trade_day_mismatch = (
        execution_trade_day_check.get("checked", False)
        and not execution_trade_day_check.get("allowed", False)
    )
    market_session_closed = (
        execution_market_session_check.get("checked", False)
        and not execution_market_session_check.get("allowed", False)
    )
    if ready_for_capped_pilot:
        next_action = "execute capped paper pilot with --execute --collect-evidence"
    elif trade_day_mismatch:
        next_action = "rerun readiness audit with current market data before enabling or executing pilot"
    elif (
        market_session_closed
        and ready_for_cap_approval
        and bool(launch_readiness.get("launch_ready", False))
        and bool(getattr(validation, "allowed", False))
        and trade_day_passed
        and authorization_snapshot_passed
    ):
        next_action = "wait for KRX regular session, then rerun readiness audit before executing pilot"
    elif ready_for_cap_approval:
        next_action = "enable pilot with suggested caps, then rerun readiness audit"
    else:
        next_action = "resolve blocking requirements before enabling or executing pilot"

    execute_block_reason = _readiness_execute_block_reason(
        ready_for_capped_pilot=ready_for_capped_pilot,
        ready_for_cap_approval=ready_for_cap_approval,
        launch_readiness=launch_readiness,
        validation=validation,
        execution_trade_day_check=execution_trade_day_check,
        execution_market_session_check=execution_market_session_check,
        pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
        blocking_reasons=blockers,
        next_action=next_action,
    )

    return {
        "artifact_type": "target_weight_rotation_pilot_readiness_audit",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": plan.candidate_id,
        "trade_day": plan.trade_day,
        "ready_for_cap_approval": ready_for_cap_approval,
        "ready_for_capped_pilot": ready_for_capped_pilot,
        "next_action": next_action,
        "blocking_reasons": blockers,
        "warning_reasons": warnings,
        "operator_commands": _build_readiness_operator_commands(
            plan,
            cap_recommendation,
            execution_trade_day_check=execution_trade_day_check,
            execution_market_session_check=execution_market_session_check,
            pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
            execute_block_reason=execute_block_reason,
        ),
        "plan_summary": _plan_summary(plan),
        "pilot_check": _pilot_check_to_dict(pilot_check),
        "plan_validation": asdict(validation),
        "cap_preview": asdict(cap_preview),
        "cap_recommendation": cap_recommendation,
        "preflight_refresh": preflight_refresh,
        "launch_readiness": launch_readiness,
        "execution_idempotency": execution_idempotency,
        "execution_trade_day_check": execution_trade_day_check,
        "execution_market_session_check": execution_market_session_check,
        "pilot_authorization_snapshot_check": pilot_authorization_snapshot_check,
        "pre_execution_reconciliation": pre_execution_reconciliation,
        "data_quality_check": data_quality_check,
        "liquidity_check": liquidity_check,
        "pre_trade_risk_check": pre_trade_risk_check,
        "no_order_safety": {
            "orders_submitted": False,
            "shadow_evidence_recorded": False,
            "pilot_evidence_recorded": False,
            "pilot_session_written": False,
            "pilot_entry_audit_may_be_written": True,
            "audit_artifacts_only": True,
        },
    }


def build_target_weight_experiment_manifest(
    *,
    plan: TargetWeightPlan,
    cap_recommendation: dict[str, Any],
    readiness_audit: dict[str, Any] | None = None,
    target_pilot_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
) -> dict[str, Any]:
    """target-weight 후보의 60영업일 paper 운용 기준 manifest를 만든다."""
    commands = dict((readiness_audit or {}).get("operator_commands") or {})
    if not commands:
        commands = _build_readiness_operator_commands(
            plan,
            cap_recommendation,
            execute_block_reason=_readiness_audit_execute_block_reason(readiness_audit),
        )
    manifest = {
        "artifact_type": "target_weight_paper_experiment_manifest",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": plan.candidate_id,
        "mode": "capped_paper_pilot",
        "live_enabled": False,
        "objective": (
            "target-weight 후보를 제한된 paper pilot으로 운용하며 "
            "실제 주문 기반 60영업일 승격 증거를 누적한다."
        ),
        "target_pilot_days": int(target_pilot_days),
        "plan_summary": _plan_summary(plan),
        "candidate_snapshot": {
            "params_hash": plan.params_hash,
            "symbols": list(plan.symbols),
            "benchmark_symbol": (plan.diagnostics or {}).get("benchmark_symbol"),
            "score_day": plan.score_day,
            "trade_day": plan.trade_day,
            "target_exposure": plan.target_exposure,
            "base_target_exposure": plan.base_target_exposure,
            "risk_off": plan.risk_off,
        },
        "evidence_policy": {
            "shadow_clean_days_required": 3,
            "pilot_paper_days_required": int(target_pilot_days),
            "promotable_evidence_mode": "pilot_paper",
            "required_provenance": {
                "execution_backed": True,
                "evidence_mode": "pilot_paper",
                "session_mode": "pilot_paper",
                "pilot_authorized": True,
            },
            "target_weight_execution_required": {
                "params_hash_match": True,
                "execution_trade_day_allowed": True,
                "execution_market_session_allowed": True,
                "pilot_authorization_snapshot_allowed": True,
                "preflight_refresh_complete": True,
                "pre_execution_positions_complete": True,
                "liquidity_complete": True,
                "pre_trade_risk_complete": True,
                "order_count_complete": True,
                "order_result_complete": True,
                "order_complete": True,
                "order_result_reconciliation_complete": True,
                "fill_complete": True,
                "fill_reconciliation_complete": True,
                "position_reconciliation_complete": True,
            },
            "blocked_evidence": [
                "shadow_bootstrap",
                "legacy record without provenance",
                "partial or halted execution",
                "duplicate completed execution rerun",
                "position drift before or after execution",
            ],
        },
        "risk_controls": {
            "pilot_caps": cap_recommendation.get("suggested_caps", {}),
            "cap_buffer_pct": cap_recommendation.get("buffer_pct"),
            "liquidity_max_order_adv_pct": float(max_order_adv_pct),
            "pre_trade_cost_check": True,
            "execution_market_session_check": True,
            "idempotency_check": True,
            "live_mode_refused": True,
        },
        "operator_commands": commands,
        "current_decision": {
            "ready_for_cap_approval": bool(
                (readiness_audit or {}).get("ready_for_cap_approval", False)
            ),
            "ready_for_capped_pilot": bool(
                (readiness_audit or {}).get("ready_for_capped_pilot", False)
            ),
            "next_action": (readiness_audit or {}).get(
                "next_action",
                "run readiness audit before enabling pilot",
            ),
            "blocking_reasons": list((readiness_audit or {}).get("blocking_reasons") or []),
            "warning_reasons": list((readiness_audit or {}).get("warning_reasons") or []),
            "data_quality_check": (readiness_audit or {}).get("data_quality_check"),
        },
        "no_order_safety": {
            "orders_submitted": False,
            "paper_evidence_recorded": False,
            "pilot_session_written": False,
            "manifest_only": True,
        },
    }
    manifest["manifest_hash"] = _stable_manifest_hash(manifest)
    return manifest


def write_target_weight_experiment_manifest(
    manifest: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_id = str(manifest["candidate_id"])
    trade_day = str((manifest.get("plan_summary") or {}).get("trade_day") or "unknown")
    path = output_dir / f"target_weight_paper_experiment_manifest_{candidate_id}_{trade_day}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def summarize_target_weight_evidence_progress(
    candidate_id: str,
    *,
    target_days: int = TARGET_WEIGHT_PILOT_TARGET_DAYS,
) -> dict[str, Any]:
    """target-weight 후보의 pilot evidence 누적 상태를 운영 요약용으로 집계한다."""
    from core.paper_evidence import (
        _is_promotable_paper_evidence,
        _target_weight_record_proof_status,
        get_canonical_records,
    )

    records = get_canonical_records(candidate_id)
    verified_dates: set[str] = set()
    invalid_dates: set[str] = set()
    invalid_reasons: dict[str, int] = {}
    repaired_dates: set[str] = set()
    shadow_dates: set[str] = set()
    non_promotable_dates: set[str] = set()
    all_dates: set[str] = set()

    for record in records:
        date = str(record.get("date") or "")
        if date:
            all_dates.add(date)
        if (
            record.get("evidence_mode") == "shadow_bootstrap"
            or record.get("session_mode") == "shadow_bootstrap"
        ):
            if date:
                shadow_dates.add(date)
            continue
        if not _is_promotable_paper_evidence(record):
            if date:
                non_promotable_dates.add(date)
            continue

        valid, reason = _target_weight_record_proof_status(candidate_id, record)
        if valid:
            if date:
                verified_dates.add(date)
        else:
            if reason == "target_weight_repaired_performance_not_promotable":
                repair_valid, _ = _target_weight_record_proof_status(
                    candidate_id,
                    record,
                    allow_repaired_performance=True,
                )
                if repair_valid and date:
                    repaired_dates.add(date)
            if date:
                invalid_dates.add(date)
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1

    verified_days = len(verified_dates)
    remaining_days = max(int(target_days) - verified_days, 0)
    progress_ratio = verified_days / int(target_days) if target_days else 0.0
    return {
        "candidate_id": candidate_id,
        "target_days": int(target_days),
        "verified_pilot_days": verified_days,
        "remaining_pilot_days": remaining_days,
        "progress_ratio": round(progress_ratio, 4),
        "shadow_days": len(shadow_dates),
        "invalid_execution_days": len(invalid_dates),
        "invalid_reasons": invalid_reasons,
        "repaired_pilot_days": len(repaired_dates),
        "non_promotable_days": len(non_promotable_dates),
        "total_canonical_records": len(records),
        "latest_record_date": max(all_dates) if all_dates else None,
        "latest_verified_pilot_date": max(verified_dates) if verified_dates else None,
        "latest_repaired_pilot_date": max(repaired_dates) if repaired_dates else None,
        "latest_shadow_date": max(shadow_dates) if shadow_dates else None,
        "ready_for_promotion_day_count": verified_days >= int(target_days),
    }


def build_target_weight_daily_ops_summary(
    *,
    audit: dict[str, Any],
    experiment_manifest: dict[str, Any],
    evidence_progress: dict[str, Any],
) -> dict[str, Any]:
    """readiness audit와 evidence progress를 하루 운영 판단용 artifact로 묶는다."""
    data_quality_check = audit.get("data_quality_check") or data_quality_check_not_available()
    execution_market_session_check = (
        audit.get("execution_market_session_check")
        or execution_market_session_check_not_required()
    )
    pilot_authorization_snapshot_check = audit.get(
        "pilot_authorization_snapshot_check"
    ) or _authorization_snapshot_not_required(
        "pilot authorization snapshot check not required"
    )
    execution_trade_day_check = audit.get("execution_trade_day_check") or execution_trade_day_check_not_required()
    audit_operator_commands = dict(audit.get("operator_commands") or {})
    candidate_id = str(audit["candidate_id"])
    trade_day = str(audit.get("trade_day") or "")

    def command_scope_issues(
        command: str,
        *,
        require_trade_day: bool,
        required_flags: tuple[str, ...] = (),
    ) -> list[str]:
        return target_weight_command_scope_issues(
            {"candidate_id": candidate_id, "trade_day": trade_day},
            command,
            require_trade_day=require_trade_day,
            required_flags=required_flags,
        )

    audit_enable_command = str(
        audit_operator_commands.get("enable_suggested_caps") or ""
    ).strip()
    enable_command_ready = (
        bool(audit_enable_command)
        and not audit_enable_command.lstrip().startswith("# blocked:")
    )
    enable_command_scope_issues = (
        command_scope_issues(
            audit_enable_command,
            require_trade_day=False,
            required_flags=("--enable",),
        )
        if enable_command_ready
        else []
    )
    enable_command_ready = enable_command_ready and not enable_command_scope_issues
    enable_command_issue_reason = ""
    if not enable_command_ready:
        enable_command_issue_reason = (
            "daily_ops_enable_command_unavailable: "
            + (audit_enable_command or "missing enable_suggested_caps command")
        )
        if enable_command_scope_issues:
            enable_command_issue_reason += "; " + "; ".join(enable_command_scope_issues)
    audit_execute_command = str(
        audit_operator_commands.get("execute_capped_paper") or ""
    ).strip()
    execute_command_ready = (
        bool(audit_execute_command)
        and not audit_execute_command.lstrip().startswith("# blocked:")
    )
    execute_command_scope_issues = (
        command_scope_issues(
            audit_execute_command,
            require_trade_day=True,
            required_flags=("--execute", "--collect-evidence"),
        )
        if execute_command_ready
        else []
    )
    execute_command_ready = execute_command_ready and not execute_command_scope_issues
    execute_command_issue_reason = ""
    if not execute_command_ready:
        execute_command_issue_reason = (
            "daily_ops_execute_command_unavailable: "
            + (audit_execute_command or "missing execute_capped_paper command")
        )
        if execute_command_scope_issues:
            execute_command_issue_reason += "; " + "; ".join(execute_command_scope_issues)
    execution_ready_checks_passed = (
        _check_passed(data_quality_check)
        and _check_passed(execution_trade_day_check)
        and _check_passed(execution_market_session_check)
        and _check_passed(pilot_authorization_snapshot_check)
    )
    capped_launch_ready = (
        bool((audit.get("launch_readiness") or {}).get("launch_ready", False))
        and bool((audit.get("plan_validation") or {}).get("allowed", False))
        and _check_passed(execution_trade_day_check)
        and _check_passed(pilot_authorization_snapshot_check)
        and _check_passed(data_quality_check)
    )
    blocking_reason_text = " ".join(
        str(reason).lower() for reason in audit.get("blocking_reasons") or []
    )
    duplicate_execution_blocked = any(
        needle in blocking_reason_text
        for needle in (
            "execution_idempotency",
            "duplicate_execution",
            "duplicate execution",
        )
    )
    latest_record_date = str(evidence_progress.get("latest_record_date") or "")
    latest_verified_pilot_date = str(evidence_progress.get("latest_verified_pilot_date") or "")
    latest_repaired_pilot_date = str(evidence_progress.get("latest_repaired_pilot_date") or "")
    try:
        invalid_execution_days = int(evidence_progress.get("invalid_execution_days") or 0)
    except (TypeError, ValueError):
        invalid_execution_days = 0
    pilot_evidence_recorded_today = (
        latest_verified_pilot_date == trade_day
        and duplicate_execution_blocked
    )
    pilot_evidence_repaired_today = (
        latest_repaired_pilot_date == trade_day
        and latest_verified_pilot_date != trade_day
        and duplicate_execution_blocked
    )
    pilot_evidence_invalid_today = (
        latest_record_date == trade_day
        and latest_verified_pilot_date != trade_day
        and latest_repaired_pilot_date != trade_day
        and invalid_execution_days > 0
        and duplicate_execution_blocked
    )
    if pilot_evidence_repaired_today:
        status = "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE"
        next_step = "오늘 pilot_paper 실행 증거는 복구 보존됐지만 promotion 카운트에서는 제외; 다음 KRX 영업일 fresh readiness 점검"
    elif pilot_evidence_invalid_today:
        status = "PILOT_EVIDENCE_INVALID"
        invalid_reasons = evidence_progress.get("invalid_reasons") or {}
        if _target_weight_finalize_first_invalid_reasons(invalid_reasons):
            next_step = "오늘 pilot_paper 증거 품질 미확정; final benchmark/portfolio evidence 확정 후 daily ops 재점검"
        else:
            next_step = "오늘 pilot_paper 증거 품질 실패; benchmark/portfolio evidence 복구 후 daily ops 재점검"
    elif pilot_evidence_recorded_today:
        status = "PILOT_EVIDENCE_RECORDED"
        next_step = "오늘 pilot_paper 증거 기록 완료; 다음 KRX 영업일 fresh readiness와 cap 재승인 점검"
    elif (
        audit.get("ready_for_capped_pilot")
        and execution_ready_checks_passed
        and execute_command_ready
    ):
        status = "READY_TO_EXECUTE"
        next_step = "승인된 cap으로 capped paper 실행"
    elif audit.get("ready_for_capped_pilot") and execution_ready_checks_passed:
        status = "BLOCKED"
        next_step = "실행 명령이 차단 또는 누락됨; readiness audit 재생성 후 재점검"
    elif not _check_passed(data_quality_check):
        status = "BLOCKED"
        next_step = "target-weight 데이터 품질 진단 해소 후 readiness 재점검"
    elif (
        audit.get("ready_for_cap_approval")
        and capped_launch_ready
        and execution_market_session_check.get("checked", False)
        and not execution_market_session_check.get("allowed", False)
        and enable_command_ready
    ):
        status = "WAITING_FOR_MARKET_SESSION"
        next_step = "KRX 정규장 시간에 readiness audit 재실행 후 capped paper 실행"
    elif audit.get("ready_for_cap_approval") and enable_command_ready:
        status = "READY_TO_ENABLE_CAPS"
        next_step = "추천 cap 승인 후 readiness audit 재실행"
    elif audit.get("ready_for_cap_approval"):
        status = "BLOCKED"
        next_step = "cap 승인 명령이 차단 또는 누락됨; readiness audit 재생성 후 재점검"
    else:
        status = "BLOCKED"
        next_step = "차단 사유 해소 후 shadow/readiness 재점검"

    plan = audit.get("plan_summary") or {}
    liquidity = audit.get("liquidity_check") or {}
    pre_trade_risk = audit.get("pre_trade_risk_check") or {}
    raw_blocking_reasons = list(audit.get("blocking_reasons") or [])
    if (
        status == "BLOCKED"
        and audit.get("ready_for_capped_pilot")
        and execution_ready_checks_passed
        and execute_command_issue_reason
    ):
        raw_blocking_reasons.append(execute_command_issue_reason)
    elif (
        status == "BLOCKED"
        and audit.get("ready_for_cap_approval")
        and enable_command_issue_reason
    ):
        raw_blocking_reasons.append(enable_command_issue_reason)
    post_evidence_diagnostics: list[str] = []
    decision_blocking_reasons = raw_blocking_reasons
    next_operator_trade_day: str | None = None
    not_before_date: str | None = None
    premature_run_guard: str | None = None
    if pilot_evidence_recorded_today or pilot_evidence_repaired_today:
        next_operator_trade_day = _next_kr_market_business_day(trade_day)
        not_before_date = next_operator_trade_day
        premature_run_guard = "target_weight_future_as_of_date_blocked"
        post_evidence_diagnostics = [
            reason
            for reason in raw_blocking_reasons
            if str(reason).startswith("execution_idempotency:")
            or str(reason).startswith("pilot_authorization_snapshot:")
            or str(reason).startswith("pilot_validation: max_orders_per_day")
        ]
        decision_blocking_reasons = [
            reason
            for reason in raw_blocking_reasons
            if reason not in post_evidence_diagnostics
        ]
    operator_commands = audit_operator_commands
    operator_commands.setdefault(
        "finalize_pilot_evidence",
        (
            "python tools/target_weight_rotation_pilot.py "
            f"--candidate-id {audit['candidate_id']} "
            f"--finalize-pilot-evidence --finalize-date {audit['trade_day']}"
        ),
    )
    operator_commands.setdefault(
        "repair_pilot_evidence",
        (
            "python tools/target_weight_rotation_pilot.py "
            f"--candidate-id {audit['candidate_id']} "
            f"--repair-pilot-evidence --repair-date {audit['trade_day']}"
        ),
    )
    if pilot_evidence_invalid_today:
        invalid_reasons = evidence_progress.get("invalid_reasons") or {}
        if _target_weight_finalize_first_invalid_reasons(invalid_reasons):
            operator_commands["repair_pilot_evidence"] = (
                "# fallback: use only if finalize cannot produce promotable proof; "
                + operator_commands["repair_pilot_evidence"]
            )
        operator_commands["enable_suggested_caps"] = (
            f"# blocked: pilot_paper evidence invalid for {audit['trade_day']}; "
            "finalize or repair evidence before changing pilot caps"
        )
        operator_commands["execute_capped_paper"] = (
            f"# blocked: pilot_paper evidence invalid for {audit['trade_day']}; "
            "finalize benchmark/portfolio evidence before counting the day"
        )
    elif pilot_evidence_repaired_today:
        operator_commands["enable_suggested_caps"] = (
            f"# blocked: repaired pilot_paper evidence already recorded for {audit['trade_day']}; "
            f"rerun readiness audit for {next_operator_trade_day}"
        )
        operator_commands["execute_capped_paper"] = (
            f"# blocked: repaired pilot_paper evidence already recorded for {audit['trade_day']}"
        )
        operator_commands["finalize_pilot_evidence"] = (
            f"# blocked: repaired pilot_paper evidence already appended for {audit['trade_day']}"
        )
        operator_commands["repair_pilot_evidence"] = (
            f"# blocked: repaired pilot_paper evidence already appended for {audit['trade_day']}"
        )
        next_base = _base_no_order_command(
            candidate_id=str(audit["candidate_id"]),
            as_of_date=next_operator_trade_day,
        )
        operator_commands["next_daily_ops_summary"] = f"{next_base} --daily-ops-summary"
        operator_commands["next_readiness_audit"] = f"{next_base} --readiness-audit"
    elif pilot_evidence_recorded_today:
        operator_commands["enable_suggested_caps"] = (
            f"# blocked: pilot_paper evidence already recorded for {audit['trade_day']}; "
            f"rerun readiness audit for {next_operator_trade_day}"
        )
        operator_commands["execute_capped_paper"] = (
            f"# blocked: pilot_paper evidence already recorded for {audit['trade_day']}"
        )
        operator_commands["finalize_pilot_evidence"] = (
            f"# blocked: pilot_paper evidence already finalized for {audit['trade_day']}"
        )
        next_base = _base_no_order_command(
            candidate_id=str(audit["candidate_id"]),
            as_of_date=next_operator_trade_day,
        )
        operator_commands["next_daily_ops_summary"] = f"{next_base} --daily-ops-summary"
        operator_commands["next_readiness_audit"] = f"{next_base} --readiness-audit"
    enable_allowed_statuses = {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}
    enable_command = str(operator_commands.get("enable_suggested_caps") or "").strip()
    if status == "BLOCKED" and audit.get("ready_for_cap_approval") and enable_command_issue_reason:
        operator_commands["enable_suggested_caps"] = f"# blocked: {enable_command_issue_reason}"
    elif (
        status not in enable_allowed_statuses
        and enable_command
        and not enable_command.lstrip().startswith("# blocked:")
    ):
        operator_commands["enable_suggested_caps"] = (
            f"# blocked: daily_ops_summary.status == {status}; "
            "READY_TO_ENABLE_CAPS 전 cap 변경 금지"
        )
    if status != "READY_TO_EXECUTE":
        execute_command = str(operator_commands.get("execute_capped_paper") or "").strip()
        if (
            status == "BLOCKED"
            and audit.get("ready_for_capped_pilot")
            and execution_ready_checks_passed
            and execute_command_issue_reason
        ):
            operator_commands["execute_capped_paper"] = f"# blocked: {execute_command_issue_reason}"
        elif not execute_command.lstrip().startswith("# blocked:"):
            block_reason = _first_text(decision_blocking_reasons) or f"{status}: {next_step}"
            operator_commands["execute_capped_paper"] = f"# blocked: {block_reason}"
    summary = {
        "artifact_type": "target_weight_daily_ops_summary",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": audit["candidate_id"],
        "trade_day": audit["trade_day"],
        "next_operator_trade_day": next_operator_trade_day,
        "status": status,
        "next_step": next_step,
        "evidence_progress": evidence_progress,
        "decision": {
            "ready_for_cap_approval": bool(audit.get("ready_for_cap_approval")),
            "ready_for_capped_pilot": bool(audit.get("ready_for_capped_pilot")),
            "readiness_next_action": audit.get("next_action", ""),
            "blocking_reasons": decision_blocking_reasons,
            "warning_reasons": list(audit.get("warning_reasons") or []),
            "post_evidence_diagnostics": post_evidence_diagnostics,
            "execution_trade_day_check": execution_trade_day_check,
            "execution_market_session_check": execution_market_session_check,
            "pilot_authorization_snapshot_check": pilot_authorization_snapshot_check,
            "data_quality_check": data_quality_check,
        },
        "risk_snapshot": {
            "orders": plan.get("order_count", 0),
            "target_positions": plan.get("target_position_count", 0),
            "max_order_notional": plan.get("max_order_notional", 0),
            "gross_exposure_after": plan.get("gross_exposure_after", 0),
            "data_quality_status": _check_display_status(data_quality_check),
            "data_quality_complete": bool(data_quality_check.get("complete", False)),
            "data_quality_reason": data_quality_check.get("reason", "not checked"),
            "price_symbols_checked": int(data_quality_check.get("symbols_checked", 0) or 0),
            "liquidity_complete": bool(liquidity.get("complete", False)),
            "liquidity_reason": liquidity.get("reason", "not checked"),
            "pre_trade_risk_complete": bool(pre_trade_risk.get("complete", False)),
            "pre_trade_risk_reason": pre_trade_risk.get("reason", "not checked"),
            "execution_trade_day_checked": bool(execution_trade_day_check.get("checked", False)),
            "execution_trade_day_status": _check_display_status(execution_trade_day_check),
            "execution_trade_day_allowed": bool(execution_trade_day_check.get("allowed", False)),
            "execution_trade_day_reason": execution_trade_day_check.get("reason", "not checked"),
            "execution_market_session_checked": bool(
                execution_market_session_check.get("checked", False)
            ),
            "execution_market_session_status": _check_display_status(execution_market_session_check),
            "execution_market_session_allowed": bool(execution_market_session_check.get("allowed", False)),
            "execution_market_session_reason": execution_market_session_check.get("reason", "not checked"),
            "pilot_authorization_snapshot_checked": bool(
                pilot_authorization_snapshot_check.get("checked", False)
            ),
            "pilot_authorization_snapshot_status": _check_display_status(
                pilot_authorization_snapshot_check
            ),
            "pilot_authorization_snapshot_allowed": bool(
                pilot_authorization_snapshot_check.get("allowed", False)
            ),
            "pilot_authorization_snapshot_reason": pilot_authorization_snapshot_check.get(
                "reason",
                "not checked",
            ),
        },
        "data_quality_snapshot": data_quality_check,
        "operator_commands": operator_commands,
        "manifest_hash": experiment_manifest.get("manifest_hash"),
        "no_order_safety": {
            "orders_submitted": False,
            "shadow_evidence_recorded": False,
            "pilot_evidence_recorded": False,
            "pilot_session_written": False,
            "summary_only": True,
        },
    }
    if not_before_date:
        summary["not_before_date"] = not_before_date
    if premature_run_guard:
        summary["premature_run_guard"] = premature_run_guard
    summary["summary_hash"] = _stable_manifest_hash(summary)
    return summary


def render_target_weight_daily_ops_markdown(summary: dict[str, Any]) -> str:
    progress = summary["evidence_progress"]
    decision = summary["decision"]
    risk = summary["risk_snapshot"]
    data_quality = (
        summary.get("data_quality_snapshot")
        or decision.get("data_quality_check")
        or data_quality_check_not_available()
    )
    data_quality_status = _check_display_status(data_quality)
    commands = summary.get("operator_commands", {})
    execution_day = decision.get("execution_trade_day_check") or execution_trade_day_check_not_required()
    market_session = (
        decision.get("execution_market_session_check")
        or execution_market_session_check_not_required()
    )
    authorization_snapshot = decision.get(
        "pilot_authorization_snapshot_check"
    ) or _authorization_snapshot_not_required(
        "pilot authorization snapshot check not required"
    )
    lines = [
        "# Target-weight Daily Ops Summary",
        "",
        f"- Candidate: `{summary['candidate_id']}`",
        f"- Trade day: `{summary['trade_day']}`",
        f"- Execution day (KST): `{execution_day.get('execution_day', 'N/A')}`",
        f"- Execution time (KST): `{market_session.get('execution_time', 'N/A')}`",
        f"- Next operator trade day: `{summary.get('next_operator_trade_day') or 'N/A'}`",
    ]
    if summary.get("not_before_date"):
        lines.append(f"- Not before date: `{summary.get('not_before_date')}`")
    if summary.get("premature_run_guard"):
        lines.append(f"- Premature run guard: `{summary.get('premature_run_guard')}`")
    lines.extend([
        f"- Status: **{summary['status']}**",
        f"- Next step: {summary['next_step']}",
        "",
        "## Evidence Progress",
        (
            f"- Verified pilot days: "
            f"{progress['verified_pilot_days']}/{progress['target_days']} "
            f"({progress['progress_ratio']:.0%})"
        ),
        f"- Remaining pilot days: {progress['remaining_pilot_days']}",
        f"- Shadow days: {progress['shadow_days']}",
        f"- Repaired non-promotable pilot days: {progress.get('repaired_pilot_days', 0)}",
        f"- Invalid execution days: {progress['invalid_execution_days']}",
        f"- Latest verified pilot date: {progress.get('latest_verified_pilot_date') or 'N/A'}",
        f"- Latest repaired pilot date: {progress.get('latest_repaired_pilot_date') or 'N/A'}",
        "",
        "## Risk Snapshot",
        f"- Orders: {risk['orders']}",
        f"- Target positions: {risk['target_positions']}",
        f"- Max order notional: {float(risk['max_order_notional'] or 0):,.0f}",
        f"- Gross exposure after: {float(risk['gross_exposure_after'] or 0):,.0f}",
        f"- Liquidity: {'PASS' if risk['liquidity_complete'] else 'BLOCKED'} - {risk['liquidity_reason']}",
        (
            f"- Pre-trade risk: "
            f"{'PASS' if risk['pre_trade_risk_complete'] else 'BLOCKED'} - "
            f"{risk['pre_trade_risk_reason']}"
        ),
        (
            f"- Execution day check: "
            f"{_check_display_status(execution_day)} - "
            f"{execution_day.get('reason', 'not checked')}"
        ),
        (
            f"- Market session check: "
            f"{_check_display_status(market_session)} - "
            f"{market_session.get('reason', 'not checked')}"
        ),
        (
            f"- Pilot auth snapshot: "
            f"{_check_display_status(authorization_snapshot)} - "
            f"{authorization_snapshot.get('reason', 'not checked')}"
        ),
        (
            f"- Data quality: "
            f"{data_quality_status} - {data_quality.get('reason', 'not checked')}"
        ),
        "",
        "## Data Quality",
        f"- Status: {data_quality_status}",
        f"- Reason: {data_quality.get('reason', 'not checked')}",
        f"- Price symbols checked: {data_quality.get('symbols_checked', 0)}",
        f"- Trade day: {data_quality.get('trade_day') or 'N/A'}",
        f"- Benchmark: {data_quality.get('benchmark_symbol') or 'N/A'}",
        f"- Benchmark latest: {data_quality.get('benchmark_last_date') or 'N/A'}",
        f"- Missing price date symbols: {', '.join(data_quality.get('missing_price_last_date_symbols') or []) or 'none'}",
        f"- Stale price symbols: {', '.join((data_quality.get('stale_price_symbols') or {}).keys()) or 'none'}",
        "",
        "## Blocking Reasons",
    ])
    lines.extend([f"- {reason}" for reason in decision.get("blocking_reasons") or []] or ["- none"])
    lines.extend(["", "## Post-evidence Diagnostics"])
    lines.extend(
        [f"- {reason}" for reason in decision.get("post_evidence_diagnostics") or []]
        or ["- none"]
    )
    lines.extend(["", "## Warnings"])
    lines.extend([f"- {reason}" for reason in decision.get("warning_reasons") or []] or ["- none"])
    lines.extend([
        "",
        "## Operator Commands",
        "",
        "### Collect Shadow Days",
        "```bash",
        commands.get("collect_shadow_days", ""),
        "```",
        "",
        "### Rerun Readiness Audit",
        "```bash",
        commands.get("rerun_readiness_audit", ""),
        "```",
        "",
    ])
    if commands.get("next_daily_ops_summary") or commands.get("next_readiness_audit"):
        lines.extend([
            "### Next Daily Ops Summary",
            "```bash",
            commands.get("next_daily_ops_summary", ""),
            "```",
            "",
            "### Next Readiness Audit",
            "```bash",
            commands.get("next_readiness_audit", ""),
            "```",
            "",
        ])
    lines.extend([
        "### Enable Suggested Caps",
        "```bash",
        commands.get("enable_suggested_caps", ""),
        "```",
        "",
        "### Finalize Pilot Evidence",
        "```bash",
        commands.get("finalize_pilot_evidence", ""),
        "```",
        "",
        "### Repair Pilot Evidence",
        "```bash",
        commands.get("repair_pilot_evidence", ""),
        "```",
        "",
        "### Execute Capped Paper",
        "```bash",
        commands.get("execute_capped_paper", ""),
        "```",
        "",
        "## Safety",
        "- No orders are submitted by this summary.",
        "- This summary does not imply live eligibility.",
    ])
    return "\n".join(lines) + "\n"


def write_target_weight_daily_ops_summary(
    summary: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_id = str(summary["candidate_id"])
    trade_day = str(summary["trade_day"])
    stem = f"target_weight_daily_ops_summary_{candidate_id}_{trade_day}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_target_weight_daily_ops_markdown(summary), encoding="utf-8")
    return json_path, md_path


def _safe_path_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"_", "-"} else "_"
        for char in str(value)
    ).strip("_") or "target_weight"


def _base_no_order_command(
    *,
    candidate_id: str,
    as_of_date: str | None = None,
    raw_symbols: str | None = None,
) -> str:
    command = f"python tools/target_weight_rotation_pilot.py --candidate-id {candidate_id}"
    if as_of_date:
        command += f" --as-of-date {as_of_date}"
    if raw_symbols:
        command += f' --symbols "{raw_symbols}"'
    return command


def build_no_order_operation_failure_artifact(
    *,
    mode: str,
    candidate_id: str,
    error: Exception,
    as_of_date: str | None = None,
    raw_symbols: str | None = None,
) -> dict[str, Any]:
    """주문 없는 운영 점검이 plan 생성 전에 막혀도 blocker artifact를 남긴다."""
    base_command = _base_no_order_command(
        candidate_id=candidate_id,
        as_of_date=as_of_date,
        raw_symbols=raw_symbols,
    )
    reason = f"target_weight_{mode}_blocked: {error}"
    return {
        "artifact_type": "target_weight_no_order_operation_failure",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "candidate_id": candidate_id,
        "as_of_date": as_of_date,
        "status": "BLOCKED",
        "reason": reason,
        "blocking_reasons": [reason],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
        "operator_commands": {
            "daily_ops_summary": f"{base_command} --daily-ops-summary",
            "readiness_audit": f"{base_command} --readiness-audit",
            "check_promotion_artifacts": "python tools/evaluate_and_promote.py --check-only",
        },
        "no_order_safety": {
            "orders_submitted": False,
            "shadow_evidence_recorded": False,
            "pilot_evidence_recorded": False,
            "pilot_session_written": False,
            "failure_artifact_only": True,
        },
    }


def render_no_order_operation_failure_markdown(payload: dict[str, Any]) -> str:
    commands = payload.get("operator_commands") or {}
    error = payload.get("error") or {}
    lines = [
        "# Target-weight No-order Operation Failure",
        "",
        f"- Candidate: `{payload.get('candidate_id')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Status: **{payload.get('status', 'BLOCKED')}**",
        f"- Reason: {payload.get('reason', '')}",
        f"- Error type: `{error.get('type', 'unknown')}`",
        f"- Error message: {error.get('message', '')}",
        "",
        "## Blocking Reasons",
    ]
    lines.extend([f"- {reason}" for reason in payload.get("blocking_reasons") or []] or ["- none"])
    lines.extend([
        "",
        "## Operator Commands",
        "",
        "### Check Promotion Artifacts",
        "```bash",
        commands.get("check_promotion_artifacts", ""),
        "```",
        "",
        "### Daily Ops Summary",
        "```bash",
        commands.get("daily_ops_summary", ""),
        "```",
        "",
        "### Readiness Audit",
        "```bash",
        commands.get("readiness_audit", ""),
        "```",
        "",
        "## No-order Safety",
        "- orders_submitted: false",
        "- pilot_evidence_recorded: false",
        "- pilot_session_written: false",
    ])
    return "\n".join(lines) + "\n"


def write_no_order_operation_failure_artifacts(
    payload: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = _safe_path_component(str(payload.get("candidate_id") or "target_weight"))
    mode = _safe_path_component(str(payload.get("mode") or "operation"))
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    stem = f"target_weight_{mode}_failure_{candidate}_{stamp}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_no_order_operation_failure_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _print_no_order_failure(
    *,
    label: str,
    payload: dict[str, Any],
    artifact_path: Path,
    report_path: Path,
) -> None:
    print(f"\nTarget-weight {label}")
    print(f"  candidate: {payload['candidate_id']}")
    print(f"  status: BLOCKED")
    print(f"  blocker: {payload['reason']}")
    print(f"  artifact: {artifact_path}")
    print(f"  report: {report_path}")
    print("  orders_submitted: false")


def _pilot_readiness_audit_path_stem(audit: dict[str, Any]) -> str:
    return f"target_weight_pilot_readiness_audit_{audit['candidate_id']}_{audit['trade_day']}"


def _audit_display_status(audit: dict[str, Any]) -> str:
    trade_day = audit.get("execution_trade_day_check") or execution_trade_day_check_not_required()
    market_session = (
        audit.get("execution_market_session_check")
        or execution_market_session_check_not_required()
    )
    authorization_snapshot = audit.get(
        "pilot_authorization_snapshot_check"
    ) or _authorization_snapshot_not_required(
        "pilot authorization snapshot check not required"
    )
    if (
        audit.get("ready_for_capped_pilot")
        and _check_passed(trade_day)
        and _check_passed(market_session)
        and _check_passed(authorization_snapshot)
    ):
        return "READY"
    if (
        audit.get("ready_for_cap_approval")
        and bool((audit.get("launch_readiness") or {}).get("launch_ready", False))
        and bool((audit.get("plan_validation") or {}).get("allowed", False))
        and _check_passed(trade_day)
        and _check_passed(authorization_snapshot)
        and market_session.get("checked", False)
        and not market_session.get("allowed", False)
    ):
        return "WAITING_FOR_MARKET_SESSION"
    if audit.get("ready_for_cap_approval"):
        return "CAP_APPROVAL_READY"
    return "BLOCKED"


def render_pilot_readiness_audit_markdown(audit: dict[str, Any]) -> str:
    plan = audit["plan_summary"]
    launch = audit["launch_readiness"]
    caps = audit["cap_recommendation"]["suggested_caps"]
    data_quality = audit.get("data_quality_check") or data_quality_check_not_available()
    data_quality_status = _check_display_status(data_quality)
    liquidity = audit.get("liquidity_check", {})
    pre_trade_risk = audit.get("pre_trade_risk_check", {})
    execution_day = audit.get("execution_trade_day_check") or execution_trade_day_check_not_required()
    market_session = (
        audit.get("execution_market_session_check")
        or execution_market_session_check_not_required()
    )
    authorization_snapshot = audit.get(
        "pilot_authorization_snapshot_check"
    ) or _authorization_snapshot_not_required(
        "pilot authorization snapshot check not required"
    )
    commands = audit.get("operator_commands", {})
    lines = [
        "# Target-weight Pilot Readiness Audit",
        "",
        f"- Candidate: `{audit['candidate_id']}`",
        f"- Trade day: `{audit['trade_day']}`",
        f"- Generated: `{audit['generated_at']}`",
        f"- Status: **{_audit_display_status(audit)}**",
        f"- Next action: {audit['next_action']}",
        "",
        "## Plan",
        f"- Score day: `{plan['score_day']}`",
        f"- Execution day (KST): `{execution_day.get('execution_day', 'N/A')}`",
        f"- Execution time (KST): `{market_session.get('execution_time', 'N/A')}`",
        (
            f"- Execution day check: "
            f"{_check_display_status(execution_day)} - "
            f"{execution_day.get('reason', 'not checked')}"
        ),
        (
            f"- Market session check: "
            f"{_check_display_status(market_session)} - "
            f"{market_session.get('reason', 'not checked')}"
        ),
        (
            f"- Pilot auth snapshot: "
            f"{_check_display_status(authorization_snapshot)} - "
            f"{authorization_snapshot.get('reason', 'not checked')}"
        ),
        f"- Targets: {', '.join(plan['targets']) if plan['targets'] else '(none)'}",
        f"- Orders: {plan['order_count']}",
        f"- Target positions: {plan['target_position_count']}",
        f"- Max order notional: {plan['max_order_notional']:,.0f}",
        f"- Gross exposure after rebalance: {plan['gross_exposure_after']:,.0f}",
        f"- Target exposure: {plan['target_exposure']:.2%}",
        "",
        "## Data Quality",
        f"- Status: {data_quality_status}",
        f"- Reason: {data_quality.get('reason', 'not checked')}",
        f"- Price symbols checked: {data_quality.get('symbols_checked', 0)}",
        f"- Trade day: {data_quality.get('trade_day') or 'N/A'}",
        f"- Benchmark: {data_quality.get('benchmark_symbol') or 'N/A'}",
        f"- Benchmark latest: {data_quality.get('benchmark_last_date') or 'N/A'}",
        f"- Missing price date symbols: {', '.join(data_quality.get('missing_price_last_date_symbols') or []) or 'none'}",
        f"- Stale price symbols: {', '.join((data_quality.get('stale_price_symbols') or {}).keys()) or 'none'}",
        "",
        "## Launch Readiness",
        f"- Clean final days: {launch['clean_final_days_current']}/{launch['clean_final_days_required']}",
        f"- Infrastructure ready: {'YES' if launch['infra_ready'] else 'NO'}",
        f"- Pilot authorization present: {'YES' if launch.get('pilot_authorization_present') else 'NO'}",
        f"- Launch ready: {'YES' if launch['launch_ready'] else 'NO'}",
        f"- Runtime state: `{launch.get('runtime_state', 'unknown')}`",
        "",
        "## Suggested Caps",
        f"- max_orders_per_day: {caps['max_orders_per_day']}",
        f"- max_concurrent_positions: {caps['max_concurrent_positions']}",
        f"- max_notional_per_trade: {caps['max_notional_per_trade']:,}",
        f"- max_gross_exposure: {caps['max_gross_exposure']:,}",
        "",
        "## Liquidity Preflight",
        f"- Status: {'PASS' if liquidity.get('complete') else 'BLOCKED'}",
        f"- Max order ADV: {float(liquidity.get('max_order_adv_pct', 0.0)):.2f}%",
        f"- Lookback days: {liquidity.get('lookback_days', 'unknown')}",
        f"- Reason: {liquidity.get('reason', 'not checked')}",
        "",
        "## Pre-trade Risk",
        f"- Status: {'PASS' if pre_trade_risk.get('complete') else 'BLOCKED'}",
        f"- Projected cash after costs: {float(pre_trade_risk.get('projected_cash_after_costs') or 0):,.0f}",
        f"- Projected cash ratio: {float(pre_trade_risk.get('projected_cash_ratio_after_costs') or 0):.2%}",
        f"- Projected investment ratio: {float(pre_trade_risk.get('projected_investment_ratio_after_costs') or 0):.2%}",
        f"- Estimated costs: {float((pre_trade_risk.get('cost_summary') or {}).get('total_explicit_costs') or 0):,.0f}",
        f"- Reason: {pre_trade_risk.get('reason', 'not checked')}",
        "",
        "## Blocking Reasons",
    ]
    blockers = audit.get("blocking_reasons") or []
    lines.extend([f"- {reason}" for reason in blockers] or ["- none"])
    lines.extend(["", "## Warnings"])
    warnings = audit.get("warning_reasons") or []
    lines.extend([f"- {reason}" for reason in warnings] or ["- none"])
    lines.extend([
        "",
        "## Operator Commands",
        "",
        "### Collect Shadow Days",
        "```bash",
        commands.get("collect_shadow_days", ""),
        "```",
        "",
        "### Rerun Readiness Audit",
        "```bash",
        commands.get("rerun_readiness_audit", ""),
        "```",
        "",
        "### Enable Suggested Caps",
        "```bash",
        commands.get("enable_suggested_caps", ""),
        "```",
        "",
        "### Execute Capped Paper",
        "```bash",
        commands.get("execute_capped_paper", ""),
        "```",
        "",
        "## No-order Safety",
        "- orders_submitted: false",
        "- shadow_evidence_recorded: false",
        "- pilot_evidence_recorded: false",
        "- pilot_session_written: false",
        "",
        "This audit is an operator checkpoint. It does not imply live eligibility.",
    ])
    return "\n".join(lines) + "\n"


def write_pilot_readiness_audit_artifact(
    audit: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_pilot_readiness_audit_path_stem(audit)}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def write_pilot_readiness_audit_report(
    audit: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_pilot_readiness_audit_path_stem(audit)}.md"
    path.write_text(render_pilot_readiness_audit_markdown(audit), encoding="utf-8")
    return path


def run_pilot_readiness_audit(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    as_of_date: str | None = None,
    cash: float | None = None,
    preview_caps: dict[str, int] | None = None,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    allow_rerun: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
    execution_now: datetime | None = None,
) -> dict[str, Any]:
    """Build a no-order readiness decision for the next capped target-weight pilot."""
    from config.config_loader import Config
    from core.paper_pilot import check_pilot_entry, compute_launch_readiness

    _require_actual_paper_cash(cash, context="readiness audit")
    _require_not_future_as_of_date(
        as_of_date,
        context="readiness audit",
        now=execution_now,
    )
    config = config or Config.get()
    plan = build_plan(
        candidate_id=candidate_id,
        raw_symbols=raw_symbols,
        as_of_date=as_of_date,
        cash=cash,
        config=config,
        collector=collector,
    )
    _require_requested_as_of_trade_day(
        plan,
        as_of_date,
        context="readiness audit",
    )
    preflight_refresh = refresh_paper_preflight_status(plan.candidate_id, plan.trade_day)
    pilot_check = check_pilot_entry(
        plan.candidate_id,
        candidate_notional=plan.max_order_notional,
        as_of_date=plan.trade_day,
    )
    validation = validate_plan_against_pilot(plan, pilot_check)
    pilot_authorization_snapshot_check = validate_pilot_authorization_snapshot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan, preview_caps)
    cap_recommendation = recommend_pilot_caps(plan)
    launch_readiness = compute_launch_readiness(plan.candidate_id, as_of_date=plan.trade_day)
    execution_idempotency = check_execution_idempotency(plan, allow_rerun=allow_rerun)
    execution_trade_day_check = validate_execution_trade_day(plan, now=execution_now)
    execution_market_session_check = validate_execution_market_session(
        plan,
        config=config,
        now=execution_now,
    )
    try:
        liquidity_check = assess_plan_liquidity(plan, max_order_adv_pct=max_order_adv_pct)
    except Exception as exc:
        logger.exception("target-weight readiness liquidity preflight failed for {}", plan.candidate_id)
        liquidity_check = failed_liquidity_preflight(
            plan,
            exc,
            max_order_adv_pct=max_order_adv_pct,
        )
    try:
        pre_trade_risk_check = assess_plan_pre_trade_risk(plan, config=config)
    except Exception as exc:
        logger.exception("target-weight readiness pre-trade risk validation failed for {}", plan.candidate_id)
        pre_trade_risk_check = failed_pre_trade_risk_validation(plan, exc)
    try:
        pre_execution_reconciliation = reconcile_plan_starting_positions(
            plan,
            _load_positions(plan.candidate_id),
        )
    except Exception as exc:
        logger.exception(
            "target-weight readiness pre-execution position reconciliation failed for {}",
            plan.candidate_id,
        )
        pre_execution_reconciliation = failed_starting_position_reconciliation(plan, exc)

    trading_mode = str(getattr(config, "trading", {}).get("mode", "paper"))
    audit = build_pilot_readiness_audit(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        cap_recommendation=cap_recommendation,
        preflight_refresh=preflight_refresh,
        launch_readiness=launch_readiness,
        execution_idempotency=execution_idempotency,
        execution_trade_day_check=execution_trade_day_check,
        execution_market_session_check=execution_market_session_check,
        pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
        pre_execution_reconciliation=pre_execution_reconciliation,
        liquidity_check=liquidity_check,
        pre_trade_risk_check=pre_trade_risk_check,
        trading_mode=trading_mode,
    )
    artifact_path = write_pilot_readiness_audit_artifact(audit, output_dir=output_dir)
    report_path = write_pilot_readiness_audit_report(audit, output_dir=output_dir)
    experiment_manifest = build_target_weight_experiment_manifest(
        plan=plan,
        cap_recommendation=cap_recommendation,
        readiness_audit=audit,
        max_order_adv_pct=max_order_adv_pct,
    )
    experiment_manifest_path = write_target_weight_experiment_manifest(
        experiment_manifest,
        output_dir=output_dir,
    )
    return {
        "plan": plan,
        "audit": audit,
        "artifact_path": artifact_path,
        "report_path": report_path,
        "experiment_manifest": experiment_manifest,
        "experiment_manifest_path": experiment_manifest_path,
    }


def run_daily_ops_summary(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    as_of_date: str | None = None,
    cash: float | None = None,
    preview_caps: dict[str, int] | None = None,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    allow_rerun: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
    execution_now: datetime | None = None,
) -> dict[str, Any]:
    """readiness audit, manifest, evidence progress를 하루 운영 요약으로 저장한다."""
    _require_actual_paper_cash(cash, context="daily ops summary")
    readiness = run_pilot_readiness_audit(
        candidate_id=candidate_id,
        raw_symbols=raw_symbols,
        as_of_date=as_of_date,
        cash=cash,
        preview_caps=preview_caps,
        max_order_adv_pct=max_order_adv_pct,
        allow_rerun=allow_rerun,
        output_dir=output_dir,
        config=config,
        collector=collector,
        execution_now=execution_now,
    )
    evidence_progress = summarize_target_weight_evidence_progress(
        readiness["audit"]["candidate_id"],
    )
    summary = build_target_weight_daily_ops_summary(
        audit=readiness["audit"],
        experiment_manifest=readiness["experiment_manifest"],
        evidence_progress=evidence_progress,
    )
    summary_path, summary_report_path = write_target_weight_daily_ops_summary(
        summary,
        output_dir=output_dir,
    )
    return {
        **readiness,
        "evidence_progress": evidence_progress,
        "daily_ops_summary": summary,
        "daily_ops_summary_path": summary_path,
        "daily_ops_summary_report_path": summary_report_path,
    }


def write_shadow_bootstrap_artifact(
    *,
    candidate_id: str,
    start_date: str,
    end_date: str,
    requested_dates: list[str],
    results: list[dict[str, Any]],
    target_unique_trade_days: int | None = None,
    launch_artifacts: dict[str, Any] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    covered_trade_days = {
        item.get("trade_day")
        for item in results
        if item.get("status") in {"recorded", "already_recorded"} and item.get("trade_day")
    }
    summary = {
        "requested_dates": len(requested_dates),
        "recorded": sum(1 for item in results if item.get("status") == "recorded"),
        "already_recorded": sum(1 for item in results if item.get("status") == "already_recorded"),
        "duplicate_trade_day": sum(1 for item in results if item.get("status") == "duplicate_trade_day"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "unique_trade_days": len({item.get("trade_day") for item in results if item.get("trade_day")}),
        "covered_unique_trade_days": len(covered_trade_days),
    }
    if target_unique_trade_days is not None:
        summary["target_unique_trade_days"] = int(target_unique_trade_days)
        summary["target_met"] = len(covered_trade_days) >= int(target_unique_trade_days)
    payload = {
        "artifact_type": "target_weight_rotation_shadow_bootstrap",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "candidate_id": candidate_id,
        "start_date": start_date,
        "end_date": end_date,
        "requested_dates": requested_dates,
        "summary": summary,
        "results": results,
        "launch_artifacts": launch_artifacts or {"attempted": False},
        "live_safety": {
            "live_enabled": False,
            "note": "shadow bootstrap records non-promotable evidence only; execution-backed pilot evidence still requires explicit pilot authorization",
        },
    }
    path = output_dir / f"target_weight_shadow_bootstrap_{candidate_id}_{start_date}_{end_date}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def run_shadow_bootstrap(
    *,
    start_date: str,
    end_date: str,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    cash: float | None = None,
    preview_caps: dict[str, int] | None = None,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    target_unique_trade_days: int | None = None,
    max_scan_weekdays: int | None = None,
    generate_readiness_artifacts: bool = True,
    generate_runbook: bool = True,
    allow_rerun: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
    execution_now: datetime | None = None,
) -> dict[str, Any]:
    """Record non-promotable target-weight shadow evidence over a date range."""
    from config.config_loader import Config
    from core.paper_evidence import get_canonical_records
    from core.paper_pilot import check_pilot_entry

    _require_not_future_as_of_date(
        end_date,
        context="shadow bootstrap",
        now=execution_now,
    )
    config = config or Config.get()
    requested_dates = _date_range(start_date, end_date)
    prebuilt_plans: dict[str, TargetWeightPlan] = {}
    target_unique = int(target_unique_trade_days) if target_unique_trade_days is not None else None
    scan_count = len(requested_dates)
    if target_unique is not None:
        if target_unique <= 0:
            raise ValueError("target_unique_trade_days must be positive")
        if max_scan_weekdays is not None and max_scan_weekdays <= 0:
            raise ValueError("max_scan_weekdays must be positive")
        default_scan_count = max(len(requested_dates), target_unique * DEFAULT_SHADOW_SCAN_MULTIPLIER)
        scan_count = int(max_scan_weekdays) if max_scan_weekdays is not None else default_scan_count
        scan_count = max(scan_count, target_unique)
        scan_dates = _recent_weekday_dates(end_date, scan_count)
        selected: list[tuple[str, TargetWeightPlan]] = []
        selected_trade_days: set[str] = set()
        for as_of in reversed(scan_dates):
            try:
                plan = build_plan(
                    candidate_id=candidate_id,
                    raw_symbols=raw_symbols,
                    as_of_date=as_of,
                    cash=cash,
                    config=config,
                    collector=collector,
                )
            except Exception:
                logger.exception("target-weight shadow bootstrap scan failed for {}", as_of)
                continue
            if plan.trade_day in selected_trade_days:
                continue
            selected_trade_days.add(plan.trade_day)
            selected.append((as_of, plan))
            if len(selected) >= target_unique:
                break
        selected.sort(key=lambda item: (item[1].trade_day, item[0]))
        requested_dates = [as_of for as_of, _ in selected]
        prebuilt_plans = {as_of: plan for as_of, plan in selected}
    existing_dates_by_strategy: dict[str, set[str]] = {}
    seen_trade_days: set[str] = set()
    results: list[dict[str, Any]] = []
    latest_plan: TargetWeightPlan | None = None
    latest_cap_preview: Any | None = None
    latest_cap_recommendation: dict[str, Any] | None = None

    for as_of in requested_dates:
        try:
            plan = prebuilt_plans.get(as_of)
            if plan is None:
                plan = build_plan(
                    candidate_id=candidate_id,
                    raw_symbols=raw_symbols,
                    as_of_date=as_of,
                    cash=cash,
                    config=config,
                    collector=collector,
                )
            evidence_strategy = plan.candidate_id
            if evidence_strategy not in existing_dates_by_strategy:
                existing_dates_by_strategy[evidence_strategy] = {
                    record.get("date")
                    for record in get_canonical_records(evidence_strategy)
                    if record.get("date")
                }
            existing_dates = existing_dates_by_strategy[evidence_strategy]

            if plan.trade_day in seen_trade_days:
                results.append({
                    "as_of_date": as_of,
                    "candidate_id": evidence_strategy,
                    "trade_day": plan.trade_day,
                    "status": "duplicate_trade_day",
                    "reason": "as_of_date mapped to a trade day already processed in this batch",
                })
                continue
            seen_trade_days.add(plan.trade_day)

            pilot_check = check_pilot_entry(
                evidence_strategy,
                candidate_notional=plan.max_order_notional,
                as_of_date=plan.trade_day,
            )
            validation = validate_plan_against_pilot(plan, pilot_check)
            cap_preview = preview_plan_against_caps(plan, preview_caps)
            cap_recommendation = recommend_pilot_caps(plan)
            liquidity_check = assess_plan_liquidity(plan, max_order_adv_pct=max_order_adv_pct)
            pre_trade_risk_check = assess_plan_pre_trade_risk(plan, config=config)
            latest_plan = plan
            latest_cap_preview = cap_preview
            latest_cap_recommendation = cap_recommendation

            if plan.trade_day in existing_dates:
                status = "already_recorded"
                recorded = False
            else:
                evidence = record_shadow_evidence_for_plan(plan, validation=validation)
                recorded = evidence is not None
                status = "recorded" if recorded else "already_recorded"
                existing_dates.add(plan.trade_day)

            results.append({
                "as_of_date": as_of,
                "candidate_id": evidence_strategy,
                "trade_day": plan.trade_day,
                "score_day": plan.score_day,
                "status": status,
                "recorded": recorded,
                "targets": plan.targets,
                "orders_planned": len(plan.orders),
                "max_order_notional": plan.max_order_notional,
                "gross_exposure_after": plan.gross_exposure_after,
                "pilot_validation": asdict(validation),
                "cap_preview": asdict(cap_preview),
                "cap_recommendation": cap_recommendation,
                "liquidity_check": liquidity_check,
                "pre_trade_risk_check": pre_trade_risk_check,
                "plan": plan.to_dict(),
            })
        except Exception as exc:
            logger.exception("target-weight shadow bootstrap failed for {}", as_of)
            results.append({
                "as_of_date": as_of,
                "status": "failed",
                "error": str(exc),
            })

    if target_unique is not None:
        covered_trade_days = {
            item.get("trade_day")
            for item in results
            if item.get("status") in {"recorded", "already_recorded"} and item.get("trade_day")
        }
        if len(covered_trade_days) < target_unique:
            results.append({
                "status": "failed",
                "reason": (
                    f"target unique trade days not met: "
                    f"{len(covered_trade_days)}/{target_unique} "
                    f"within {scan_count} scanned weekdays"
                ),
                "target_unique_trade_days": target_unique,
                "covered_unique_trade_days": len(covered_trade_days),
            })

    launch_artifacts = {"attempted": False}
    if generate_readiness_artifacts:
        artifact_candidate_id = latest_plan.candidate_id if latest_plan is not None else candidate_id
        launch_artifacts = generate_launch_artifacts(
            artifact_candidate_id,
            include_runbook=generate_runbook,
            plan=latest_plan,
            cap_preview=latest_cap_preview,
            cap_recommendation=latest_cap_recommendation,
        )

    artifact_start_date = requested_dates[0] if requested_dates else start_date
    artifact_end_date = requested_dates[-1] if requested_dates else end_date
    artifact_path = write_shadow_bootstrap_artifact(
        candidate_id=latest_plan.candidate_id if latest_plan is not None else candidate_id,
        start_date=artifact_start_date,
        end_date=artifact_end_date,
        requested_dates=requested_dates,
        results=results,
        target_unique_trade_days=target_unique,
        launch_artifacts=launch_artifacts,
        output_dir=output_dir,
    )

    covered_trade_days = {
        item.get("trade_day")
        for item in results
        if item.get("status") in {"recorded", "already_recorded"} and item.get("trade_day")
    }
    summary = {
        "requested_dates": len(requested_dates),
        "recorded": sum(1 for item in results if item.get("status") == "recorded"),
        "already_recorded": sum(1 for item in results if item.get("status") == "already_recorded"),
        "duplicate_trade_day": sum(1 for item in results if item.get("status") == "duplicate_trade_day"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "covered_unique_trade_days": len(covered_trade_days),
    }
    if target_unique is not None:
        summary["target_unique_trade_days"] = target_unique
        summary["target_met"] = len(covered_trade_days) >= target_unique
    return {
        "summary": summary,
        "results": results,
        "start_date": artifact_start_date,
        "end_date": artifact_end_date,
        "requested_dates": requested_dates,
        "launch_artifacts": launch_artifacts,
        "artifact_path": artifact_path,
    }


def run_pilot(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    as_of_date: str | None = None,
    cash: float | None = None,
    execute: bool = False,
    collect_evidence: bool = False,
    record_shadow_evidence: bool = False,
    preview_caps: dict[str, int] | None = None,
    max_order_adv_pct: float = DEFAULT_MAX_ORDER_ADV_PCT,
    generate_readiness_artifacts: bool = True,
    generate_runbook: bool = True,
    allow_rerun: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
    execution_now: datetime | None = None,
) -> dict[str, Any]:
    from config.config_loader import Config
    from core.paper_pilot import check_pilot_entry, save_pilot_session_artifact

    if execute and record_shadow_evidence:
        raise ValueError("record_shadow_evidence is only valid for dry-run sessions")
    if execute or collect_evidence:
        _require_actual_paper_cash(cash, context="execution or pilot evidence collection")
    if collect_evidence and not execute:
        raise ValueError(
            "target_weight_collect_evidence_requires_execute: "
            "pilot_paper evidence collection requires --execute --collect-evidence"
        )
    _require_not_future_as_of_date(
        as_of_date,
        context="pilot adapter",
        now=execution_now,
    )

    config = config or Config.get()
    plan = build_plan(
        candidate_id=candidate_id,
        raw_symbols=raw_symbols,
        as_of_date=as_of_date,
        cash=cash,
        config=config,
        collector=collector,
    )

    preflight_refresh = {
        "checked": False,
        "complete": True,
        "reason": "paper preflight refresh not required for dry-run",
    }
    if execute:
        preflight_refresh = refresh_paper_preflight_status(plan.candidate_id, plan.trade_day)

    pilot_check = check_pilot_entry(
        candidate_id,
        candidate_notional=plan.max_order_notional,
        as_of_date=plan.trade_day,
    )
    validation = validate_plan_against_pilot(plan, pilot_check)
    pilot_authorization_snapshot_check = validate_pilot_authorization_snapshot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan, preview_caps)
    cap_recommendation = recommend_pilot_caps(plan)
    try:
        liquidity_check = assess_plan_liquidity(plan, max_order_adv_pct=max_order_adv_pct)
    except Exception as exc:
        logger.exception("target-weight liquidity preflight failed for {}", plan.candidate_id)
        liquidity_check = failed_liquidity_preflight(
            plan,
            exc,
            max_order_adv_pct=max_order_adv_pct,
        )
    try:
        pre_trade_risk_check = assess_plan_pre_trade_risk(plan, config=config)
    except Exception as exc:
        logger.exception("target-weight pre-trade risk validation failed for {}", plan.candidate_id)
        pre_trade_risk_check = failed_pre_trade_risk_validation(plan, exc)
    dry_run = not execute

    execution_trade_day_check = execution_trade_day_check_not_required()
    execution_market_session_check = execution_market_session_check_not_required()
    if execute:
        execution_trade_day_check = validate_execution_trade_day(plan, now=execution_now)
        execution_market_session_check = validate_execution_market_session(
            plan,
            config=config,
            now=execution_now,
        )

    execution_idempotency = None
    execution_lock = None
    execution_lock_release = None
    if (
        execute
        and validation.allowed
        and execution_trade_day_check["allowed"]
        and execution_market_session_check["allowed"]
        and pilot_authorization_snapshot_check["allowed"]
        and preflight_refresh["complete"]
    ):
        execution_idempotency = check_execution_idempotency(
            plan,
            allow_rerun=allow_rerun,
        )
        if execution_idempotency["allowed"]:
            execution_session_id = make_execution_session_id(plan, now=execution_now)
            execution_lock = acquire_execution_lock(
                plan,
                execution_session_id=execution_session_id,
            )
            if not execution_lock["allowed"]:
                execution_idempotency = {
                    **execution_idempotency,
                    "allowed": False,
                    "reason": execution_lock["reason"],
                    "execution_lock": execution_lock,
                }

    pre_execution_reconciliation = None
    if (
        execute
        and execution_trade_day_check["allowed"]
        and execution_market_session_check["allowed"]
        and pilot_authorization_snapshot_check["allowed"]
        and preflight_refresh["complete"]
        and execution_idempotency
        and execution_idempotency["allowed"]
    ):
        pre_execution_reconciliation = load_starting_position_reconciliation(plan)

    execution_session_id: str | None = None
    if execute and not preflight_refresh["complete"]:
        execution = blocked_execution_for_preflight_refresh(plan, preflight_refresh)
    elif execute and not validation.allowed:
        execution = blocked_execution_for_pilot_validation(plan, validation)
    elif execute and not execution_trade_day_check["allowed"]:
        execution = blocked_execution_for_trade_day_mismatch(plan, execution_trade_day_check)
    elif execute and not execution_market_session_check["allowed"]:
        execution = blocked_execution_for_market_session(plan, execution_market_session_check)
    elif execute and not pilot_authorization_snapshot_check["allowed"]:
        execution = blocked_execution_for_authorization_snapshot_mismatch(
            plan,
            pilot_authorization_snapshot_check,
        )
    elif execute and execution_idempotency and not execution_idempotency["allowed"]:
        execution = blocked_execution_for_duplicate_execution(plan, execution_idempotency)
    elif execute and pre_execution_reconciliation and not pre_execution_reconciliation["complete"]:
        execution = blocked_execution_for_pre_execution_drift(plan, pre_execution_reconciliation)
    elif execute and not liquidity_check["complete"]:
        execution = blocked_execution_for_liquidity(plan, liquidity_check)
    elif execute and not pre_trade_risk_check["complete"]:
        execution = blocked_execution_for_pre_trade_risk(plan, pre_trade_risk_check)
    else:
        execution_session_id = execution_session_id or (
            make_execution_session_id(plan, now=execution_now) if execute else None
        )
        execution = execute_plan(
            plan,
            config=config,
            dry_run=dry_run,
            pilot_validation=validation,
            preflight_refresh=preflight_refresh,
            execution_trade_day_check=execution_trade_day_check,
            execution_market_session_check=execution_market_session_check,
            pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
            execution_idempotency=execution_idempotency,
            allow_rerun=allow_rerun,
            pre_execution_reconciliation=pre_execution_reconciliation,
            liquidity_check=liquidity_check,
            pre_trade_risk_check=pre_trade_risk_check,
            max_order_adv_pct=max_order_adv_pct,
            execution_session_id=execution_session_id,
        )
    execution_session_id = str(execution.get("execution_session_id") or execution_session_id or "")
    if execution.get("pre_execution_reconciliation") is not None:
        pre_execution_reconciliation = execution["pre_execution_reconciliation"]

    fill_reconciliation = None
    position_reconciliation = None
    if (
        execute
        and validation.allowed
        and execution_trade_day_check["allowed"]
        and execution_market_session_check["allowed"]
        and pilot_authorization_snapshot_check["allowed"]
        and preflight_refresh["complete"]
        and (execution_idempotency is None or execution_idempotency["allowed"])
        and (pre_execution_reconciliation is None or pre_execution_reconciliation["complete"])
        and liquidity_check["complete"]
        and pre_trade_risk_check["complete"]
    ):
        try:
            fill_reconciliation = reconcile_plan_fills(
                plan,
                load_paper_trade_fills(
                    plan,
                    execution_session_id=execution_session_id or None,
                ),
                execution_session_id=execution_session_id or None,
            )
        except Exception as exc:
            logger.exception("target-weight fill reconciliation failed for {}", plan.candidate_id)
            fill_reconciliation = failed_fill_reconciliation(
                plan,
                exc,
                execution_session_id=execution_session_id or None,
            )
        try:
            position_reconciliation = reconcile_plan_positions(
                plan,
                _load_positions(plan.candidate_id),
            )
        except Exception as exc:
            logger.exception("target-weight position reconciliation failed for {}", plan.candidate_id)
            position_reconciliation = failed_position_reconciliation(plan, exc)
    execution_evidence = summarize_execution_for_evidence(
        plan,
        execution,
        execution_trade_day_check=execution_trade_day_check,
        execution_market_session_check=execution_market_session_check,
        pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
        execution_idempotency=execution_idempotency,
        preflight_refresh=preflight_refresh,
        pre_execution_reconciliation=pre_execution_reconciliation,
        liquidity_check=liquidity_check,
        pre_trade_risk_check=pre_trade_risk_check,
        fill_reconciliation=fill_reconciliation,
        position_reconciliation=position_reconciliation,
    )
    evidence_collection = {"attempted": False, "recorded": False}

    if execute:
        order_submission_reached = _execution_reached_order_submission(execution)
        evidence_caps_snapshot = build_pilot_evidence_caps_snapshot(
            plan,
            validation,
            execution,
            execution_trade_day_check=execution_trade_day_check,
            execution_market_session_check=execution_market_session_check,
            pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
            execution_idempotency=execution_idempotency,
            preflight_refresh=preflight_refresh,
            pre_execution_reconciliation=pre_execution_reconciliation,
            liquidity_check=liquidity_check,
            pre_trade_risk_check=pre_trade_risk_check,
            fill_reconciliation=fill_reconciliation,
            position_reconciliation=position_reconciliation,
        )
        pilot_session = {
            "active": True,
            "session_mode": "pilot_paper",
            "evidence_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": evidence_caps_snapshot,
            "orders_planned": len(plan.orders),
            "orders_executed": execution.get("executed", 0),
            "order_submission_reached": order_submission_reached,
            "execution_complete": execution_evidence["complete"],
            "evidence_collectible": execution_evidence["complete"],
            "evidence_block_reason": "" if execution_evidence["complete"] else execution_evidence["reason"],
            "target_weight_execution": execution_evidence,
        }
        if (
            validation.allowed
            and execution_trade_day_check["allowed"]
            and execution_market_session_check["allowed"]
            and pilot_authorization_snapshot_check["allowed"]
            and preflight_refresh["complete"]
            and (execution_idempotency is None or execution_idempotency["allowed"])
            and (execution_evidence["complete"] or order_submission_reached)
        ):
            save_pilot_session_artifact(
                strategy=candidate_id,
                date=plan.trade_day,
                pilot_session=pilot_session,
            )
        execution_lock_release = release_execution_lock(execution_lock)

        if collect_evidence:
            evidence_collection = {
                "attempted": True,
                "recorded": False,
                "status": "blocked",
                "reason": execution_evidence["reason"],
                "target_weight_execution": execution_evidence,
            }
            if execution_evidence["complete"]:
                from core.paper_evidence import collect_daily_evidence

                evidence_record = collect_daily_evidence(
                    strategy=candidate_id,
                    mode="paper",
                    account_key=candidate_id,
                    date=datetime.strptime(plan.trade_day, "%Y-%m-%d"),
                    watchlist_symbols=plan.symbols,
                    evidence_mode="pilot_paper",
                    pilot_authorized=True,
                    pilot_caps_snapshot=evidence_caps_snapshot,
                )
                if evidence_record is not None:
                    evidence_collection.update({
                        "recorded": True,
                        "status": "recorded",
                        "reason": "pilot_paper evidence recorded",
                    })
                else:
                    existing_evidence = verify_existing_pilot_evidence_record(plan)
                    if existing_evidence["valid"]:
                        evidence_collection.update({
                            "recorded": False,
                            "status": "already_recorded",
                            "reason": "existing pilot_paper evidence verified",
                            "existing_evidence": existing_evidence,
                        })
                    else:
                        evidence_collection.update({
                            "recorded": False,
                            "status": "blocked",
                            "reason": existing_evidence["reason"],
                            "existing_evidence": existing_evidence,
                        })

    shadow_evidence_record = None
    shadow_evidence_summary = {"attempted": False, "recorded": False}
    if dry_run and record_shadow_evidence:
        shadow_evidence_record = record_shadow_evidence_for_plan(plan, validation=validation)
        shadow_evidence_summary = {
            "attempted": True,
            "recorded": shadow_evidence_record is not None,
            "date": plan.trade_day,
            "evidence_mode": "shadow_bootstrap",
            "reason": "recorded" if shadow_evidence_record is not None else "already recorded",
        }

    launch_artifacts = {"attempted": False}
    if dry_run and record_shadow_evidence and generate_readiness_artifacts:
        launch_artifacts = generate_launch_artifacts(
            plan.candidate_id,
            include_runbook=generate_runbook,
            plan=plan,
            cap_preview=cap_preview,
            cap_recommendation=cap_recommendation,
        )

    artifact_path = write_session_artifact(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        cap_recommendation=cap_recommendation,
        liquidity_check=liquidity_check,
        pre_trade_risk_check=pre_trade_risk_check,
        execution=execution,
        dry_run=dry_run,
        execution_trade_day_check=execution_trade_day_check,
        execution_market_session_check=execution_market_session_check,
        pilot_authorization_snapshot_check=pilot_authorization_snapshot_check,
        execution_idempotency=execution_idempotency,
        execution_lock=execution_lock,
        execution_lock_release=execution_lock_release,
        preflight_refresh=preflight_refresh,
        fill_reconciliation=fill_reconciliation,
        shadow_evidence=shadow_evidence_summary,
        evidence_collection=evidence_collection,
        launch_artifacts=launch_artifacts,
        output_dir=output_dir,
    )

    return {
        "plan": plan,
        "pilot_check": pilot_check,
        "validation": validation,
        "cap_preview": cap_preview,
        "cap_recommendation": cap_recommendation,
        "preflight_refresh": preflight_refresh,
        "liquidity_check": liquidity_check,
        "pre_trade_risk_check": pre_trade_risk_check,
        "execution": execution,
        "execution_trade_day_check": execution_trade_day_check,
        "execution_market_session_check": execution_market_session_check,
        "pilot_authorization_snapshot_check": pilot_authorization_snapshot_check,
        "execution_idempotency": execution_idempotency,
        "execution_lock": execution_lock,
        "execution_lock_release": execution_lock_release,
        "fill_reconciliation": fill_reconciliation,
        "execution_evidence": execution_evidence,
        "evidence_collection": evidence_collection,
        "shadow_evidence": shadow_evidence_record,
        "shadow_evidence_summary": shadow_evidence_summary,
        "launch_artifacts": launch_artifacts,
        "artifact_path": artifact_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Target-weight rotation paper pilot adapter")
    parser.add_argument("--candidate-id", default=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID)
    parser.add_argument("--symbols", help="Comma-separated universe override")
    parser.add_argument("--as-of-date", help="YYYY-MM-DD; defaults to latest available data before now")
    parser.add_argument("--cash", type=float, help="Override starting cash for planning")
    parser.add_argument("--execute", action="store_true", help="Submit paper orders. Default is dry-run.")
    parser.add_argument("--collect-evidence", action="store_true", help="Collect pilot_paper evidence after execution.")
    parser.add_argument(
        "--readiness-audit",
        action="store_true",
        help="Write a no-order target-weight capped pilot readiness audit artifact.",
    )
    parser.add_argument(
        "--daily-ops-summary",
        action="store_true",
        help="Write a no-order daily operator summary with readiness, risk, and evidence progress.",
    )
    parser.add_argument(
        "--repair-pilot-evidence",
        action="store_true",
        help="Append a no-order repaired pilot_paper evidence record from stored target-weight execution proof.",
    )
    parser.add_argument("--repair-date", help="YYYY-MM-DD evidence date to repair with --repair-pilot-evidence.")
    parser.add_argument(
        "--finalize-pilot-evidence",
        action="store_true",
        help="Append a no-order finalized pilot_paper evidence record when final benchmark data is available.",
    )
    parser.add_argument("--finalize-date", help="YYYY-MM-DD evidence date to finalize with --finalize-pilot-evidence.")
    parser.add_argument(
        "--record-shadow-evidence",
        action="store_true",
        help="On dry-run, append non-promotable shadow_bootstrap evidence for launch readiness.",
    )
    parser.add_argument(
        "--shadow-start-date",
        help="Run multi-date shadow bootstrap from YYYY-MM-DD; implies dry-run shadow evidence.",
    )
    parser.add_argument(
        "--shadow-days",
        type=int,
        help=(
            "Auto-select recent weekdays until N unique resolved trade days are "
            "covered for shadow bootstrap; optionally anchor with --shadow-end-date."
        ),
    )
    parser.add_argument(
        "--shadow-end-date",
        help="Run shadow bootstrap through YYYY-MM-DD; required with --shadow-start-date or optional with --shadow-days.",
    )
    parser.add_argument(
        "--skip-readiness-artifacts",
        action="store_true",
        help="Skip launch readiness JSON/MD generation when shadow evidence is recorded.",
    )
    parser.add_argument(
        "--skip-runbook",
        action="store_true",
        help="Skip pilot runbook generation when readiness artifacts are generated.",
    )
    parser.add_argument(
        "--allow-rerun",
        action="store_true",
        help=(
            "Explicitly allow recovery rerun only for incomplete/interrupted "
            "same-candidate/trade-day pilot sessions."
        ),
    )
    parser.add_argument("--preview-max-orders", type=int, help="Proposed pilot cap preview: max orders/day.")
    parser.add_argument("--preview-max-positions", type=int, help="Proposed pilot cap preview: max concurrent positions.")
    parser.add_argument("--preview-max-notional", type=int, help="Proposed pilot cap preview: max notional/trade.")
    parser.add_argument("--preview-max-exposure", type=int, help="Proposed pilot cap preview: max gross exposure.")
    parser.add_argument(
        "--max-order-adv-pct",
        type=float,
        default=DEFAULT_MAX_ORDER_ADV_PCT,
        help="Block target-weight pilot orders above this percent of recent average daily traded value.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    if args.cash is not None:
        cash_blocked_modes = []
        if args.readiness_audit:
            cash_blocked_modes.append("--readiness-audit")
        if args.daily_ops_summary:
            cash_blocked_modes.append("--daily-ops-summary")
        if args.execute:
            cash_blocked_modes.append("--execute")
        if args.collect_evidence:
            cash_blocked_modes.append("--collect-evidence")
        if args.repair_pilot_evidence:
            cash_blocked_modes.append("--repair-pilot-evidence")
        if args.finalize_pilot_evidence:
            cash_blocked_modes.append("--finalize-pilot-evidence")
        if cash_blocked_modes:
            parser.error(
                "--cash cannot be combined with "
                f"{', '.join(cash_blocked_modes)}; use actual paper account cash"
            )
    if args.collect_evidence and not args.execute:
        parser.error("--collect-evidence requires --execute; evidence must be tied to a completed paper execution")
    evidence_maintenance_modes = [args.repair_pilot_evidence, args.finalize_pilot_evidence]
    if sum(1 for enabled in evidence_maintenance_modes if enabled) > 1:
        parser.error("--repair-pilot-evidence and --finalize-pilot-evidence are mutually exclusive")
    if (args.repair_pilot_evidence or args.finalize_pilot_evidence) and (
        args.readiness_audit
        or args.daily_ops_summary
        or args.execute
        or args.collect_evidence
        or args.record_shadow_evidence
        or args.shadow_start_date is not None
        or args.shadow_end_date is not None
        or args.shadow_days is not None
    ):
        parser.error(
            "evidence maintenance cannot be combined with readiness, daily ops, "
            "execution, evidence collection, or shadow bootstrap modes"
        )
    if args.repair_pilot_evidence and not args.repair_date:
        parser.error("--repair-date is required with --repair-pilot-evidence")
    if args.repair_date and not args.repair_pilot_evidence:
        parser.error("--repair-date is only used with --repair-pilot-evidence")
    if args.repair_pilot_evidence and args.as_of_date:
        parser.error("--as-of-date is not used with --repair-pilot-evidence; use --repair-date")
    if args.finalize_pilot_evidence and not args.finalize_date:
        parser.error("--finalize-date is required with --finalize-pilot-evidence")
    if args.finalize_date and not args.finalize_pilot_evidence:
        parser.error("--finalize-date is only used with --finalize-pilot-evidence")
    if args.finalize_pilot_evidence and args.as_of_date:
        parser.error("--as-of-date is not used with --finalize-pilot-evidence; use --finalize-date")

    from database.models import init_database

    init_database()
    preview_caps = build_preview_caps(
        max_orders=args.preview_max_orders,
        max_positions=args.preview_max_positions,
        max_notional=args.preview_max_notional,
        max_exposure=args.preview_max_exposure,
    )

    shadow_batch = (
        args.shadow_start_date is not None
        or args.shadow_end_date is not None
        or args.shadow_days is not None
    )
    if shadow_batch:
        if args.daily_ops_summary:
            parser.error("--daily-ops-summary cannot be combined with shadow bootstrap batch options")
        if args.readiness_audit:
            parser.error("--readiness-audit cannot be combined with shadow bootstrap batch options")
        if args.execute or args.collect_evidence:
            parser.error("shadow bootstrap date range cannot be combined with --execute or --collect-evidence")
        if args.as_of_date:
            parser.error("--as-of-date is not used with shadow bootstrap batch options")
        try:
            shadow_start_date, shadow_end_date, requested_dates = resolve_shadow_batch_range(
                shadow_start_date=args.shadow_start_date,
                shadow_end_date=args.shadow_end_date,
                shadow_days=args.shadow_days,
            )
        except NO_ORDER_OPERATION_ERRORS as exc:
            parser.error(str(exc))

        batch = run_shadow_bootstrap(
            candidate_id=args.candidate_id,
            raw_symbols=args.symbols,
            start_date=shadow_start_date,
            end_date=shadow_end_date,
            cash=args.cash,
            target_unique_trade_days=args.shadow_days,
            generate_readiness_artifacts=not args.skip_readiness_artifacts,
            generate_runbook=not args.skip_runbook,
            preview_caps=preview_caps,
            max_order_adv_pct=args.max_order_adv_pct,
            output_dir=Path(args.output_dir),
        )
        summary = batch["summary"]
        print("\nTarget-weight shadow bootstrap")
        print(f"  candidate: {args.candidate_id}")
        print(f"  range: {batch.get('start_date', shadow_start_date)} ~ {batch.get('end_date', shadow_end_date)}")
        if args.shadow_days is not None:
            selected_dates = batch.get("requested_dates") or requested_dates
            print(f"  auto-selected weekdays: {', '.join(selected_dates)}")
        print(
            "  summary: "
            f"recorded={summary['recorded']} "
            f"already_recorded={summary['already_recorded']} "
            f"duplicate_trade_day={summary['duplicate_trade_day']} "
            f"failed={summary['failed']} "
            f"covered_unique_trade_days={summary['covered_unique_trade_days']}"
        )
        if args.shadow_days is not None:
            print(
                "  target: "
                f"unique_trade_days={summary['covered_unique_trade_days']}/{summary['target_unique_trade_days']} "
                f"met={'YES' if summary['target_met'] else 'NO'}"
            )
        shadow_incomplete = summary["failed"] > 0 or (
            args.shadow_days is not None and not summary.get("target_met", False)
        )
        if batch["launch_artifacts"].get("attempted"):
            readiness = batch["launch_artifacts"]["launch_readiness"]
            print(
                "  readiness: "
                f"clean={readiness['clean_final_days_current']}/{readiness['clean_final_days_required']} "
                f"infra={'YES' if readiness['infra_ready'] else 'NO'} "
                f"launch={'YES' if readiness['launch_ready'] else 'NO'}"
            )
            print(f"  readiness artifact: {readiness['json_path']}")
            if batch["launch_artifacts"].get("runbook_path"):
                print(f"  runbook: {batch['launch_artifacts']['runbook_path']}")
        if shadow_incomplete:
            print("  status: BLOCKED - shadow bootstrap incomplete")
        else:
            print("  status: OK")
        print(f"  artifact: {batch['artifact_path']}")
        if shadow_incomplete:
            raise SystemExit(1)
        return

    if args.repair_pilot_evidence:
        try:
            result = repair_target_weight_pilot_evidence(
                candidate_id=args.candidate_id,
                repair_date=args.repair_date,
                output_dir=Path(args.output_dir),
            )
        except ValueError as exc:
            print("\nTarget-weight pilot evidence repair")
            print(f"  candidate: {args.candidate_id}")
            print(f"  repair_date: {args.repair_date}")
            print(f"  status: BLOCKED - {exc}")
            raise SystemExit(1)

        print("\nTarget-weight pilot evidence repair")
        print(f"  candidate: {result['candidate_id']}")
        print(f"  repair_date: {result['repair_date']}")
        print(f"  status: {result['status']}")
        print(f"  reason: {result['reason']}")
        if result.get("repaired"):
            print(f"  appended_record_version: {result['appended_record_version']}")
            print(f"  proof: {result['proof_status_after'].get('reason')}")
        print(f"  artifact: {result['artifact_path']}")
        print(f"  report: {result['report_path']}")
        return

    if args.finalize_pilot_evidence:
        try:
            result = finalize_target_weight_pilot_evidence(
                candidate_id=args.candidate_id,
                finalize_date=args.finalize_date,
                output_dir=Path(args.output_dir),
            )
        except ValueError as exc:
            print("\nTarget-weight pilot evidence finalize")
            print(f"  candidate: {args.candidate_id}")
            print(f"  finalize_date: {args.finalize_date}")
            print(f"  status: BLOCKED - {exc}")
            raise SystemExit(1)

        print("\nTarget-weight pilot evidence finalize")
        print(f"  candidate: {result['candidate_id']}")
        print(f"  finalize_date: {result['finalize_date']}")
        print(f"  status: {result['status']}")
        print(f"  reason: {result['reason']}")
        if result.get("finalized"):
            print(f"  appended_record_version: {result['appended_record_version']}")
            print(f"  proof: {result['proof_status_after'].get('reason')}")
        print(f"  artifact: {result['artifact_path']}")
        print(f"  report: {result['report_path']}")
        if result["status"] == "waiting_for_final_benchmark":
            raise SystemExit(1)
        return

    if args.daily_ops_summary:
        if args.readiness_audit or args.execute or args.collect_evidence or args.record_shadow_evidence:
            parser.error(
                "--daily-ops-summary cannot be combined with --readiness-audit, "
                "--execute, --collect-evidence, or --record-shadow-evidence"
            )

        try:
            result = run_daily_ops_summary(
                candidate_id=args.candidate_id,
                raw_symbols=args.symbols,
                as_of_date=args.as_of_date,
                cash=args.cash,
                allow_rerun=args.allow_rerun,
                preview_caps=preview_caps,
                max_order_adv_pct=args.max_order_adv_pct,
                output_dir=Path(args.output_dir),
            )
        except NO_ORDER_OPERATION_ERRORS as exc:
            failure = build_no_order_operation_failure_artifact(
                mode="daily_ops_summary",
                candidate_id=args.candidate_id,
                as_of_date=args.as_of_date,
                raw_symbols=args.symbols,
                error=exc,
            )
            artifact_path, report_path = write_no_order_operation_failure_artifacts(
                failure,
                output_dir=Path(args.output_dir),
            )
            _print_no_order_failure(
                label="daily ops summary",
                payload=failure,
                artifact_path=artifact_path,
                report_path=report_path,
            )
            raise SystemExit(1)
        summary = result["daily_ops_summary"]
        progress = summary["evidence_progress"]
        print("\nTarget-weight daily ops summary")
        print(f"  candidate: {summary['candidate_id']}")
        print(f"  trade_day: {summary['trade_day']}")
        execution_day = summary["decision"].get("execution_trade_day_check", {})
        print(
            "  execution day: "
            f"{execution_day.get('execution_day', 'N/A')} "
            f"({_check_display_status(execution_day)})"
        )
        market_session = summary["decision"].get("execution_market_session_check", {})
        print(
            "  market session: "
            f"{market_session.get('execution_time', 'N/A')} "
            f"({_check_display_status(market_session)})"
        )
        authorization_snapshot = summary["decision"].get(
            "pilot_authorization_snapshot_check",
            {},
        )
        print(f"  status: {summary['status']}")
        if (
            summary["status"] == "PILOT_EVIDENCE_RECORDED"
            and authorization_snapshot.get("checked", False)
            and not authorization_snapshot.get("allowed", True)
        ):
            print(
                "  pilot auth snapshot: DIAGNOSTIC - "
                "same-day evidence already recorded; refresh readiness/caps next business day"
            )
        else:
            print(
                "  pilot auth snapshot: "
                f"{_check_display_status(authorization_snapshot)} - "
                f"{authorization_snapshot.get('reason', 'not checked')}"
            )
        print(
            "  evidence: "
            f"verified={progress['verified_pilot_days']}/{progress['target_days']} "
            f"remaining={progress['remaining_pilot_days']} "
            f"shadow={progress['shadow_days']} "
            f"repaired={progress.get('repaired_pilot_days', 0)} "
            f"invalid={progress['invalid_execution_days']}"
        )
        print(f"  next: {summary['next_step']}")
        for reason in summary["decision"]["blocking_reasons"][:8]:
            print(f"  blocker: {reason}")
        remaining = len(summary["decision"]["blocking_reasons"]) - 8
        if remaining > 0:
            print(f"  blocker: +{remaining} more")
        print(f"  readiness artifact: {result['artifact_path']}")
        print(f"  experiment manifest: {result['experiment_manifest_path']}")
        print(f"  summary artifact: {result['daily_ops_summary_path']}")
        print(f"  summary report: {result['daily_ops_summary_report_path']}")
        if summary["status"] in {
            "BLOCKED",
            "WAITING_FOR_MARKET_SESSION",
            "PILOT_EVIDENCE_INVALID",
        }:
            raise SystemExit(1)
        return

    if args.readiness_audit:
        if args.execute or args.collect_evidence or args.record_shadow_evidence:
            parser.error(
                "--readiness-audit cannot be combined with --execute, "
                "--collect-evidence, or --record-shadow-evidence"
            )

        try:
            result = run_pilot_readiness_audit(
                candidate_id=args.candidate_id,
                raw_symbols=args.symbols,
                as_of_date=args.as_of_date,
                cash=args.cash,
                allow_rerun=args.allow_rerun,
                preview_caps=preview_caps,
                max_order_adv_pct=args.max_order_adv_pct,
                output_dir=Path(args.output_dir),
            )
        except NO_ORDER_OPERATION_ERRORS as exc:
            failure = build_no_order_operation_failure_artifact(
                mode="readiness_audit",
                candidate_id=args.candidate_id,
                as_of_date=args.as_of_date,
                raw_symbols=args.symbols,
                error=exc,
            )
            artifact_path, report_path = write_no_order_operation_failure_artifacts(
                failure,
                output_dir=Path(args.output_dir),
            )
            _print_no_order_failure(
                label="pilot readiness audit",
                payload=failure,
                artifact_path=artifact_path,
                report_path=report_path,
            )
            raise SystemExit(1)
        audit = result["audit"]
        plan_summary = audit["plan_summary"]
        launch = audit["launch_readiness"]
        cap_rec = audit["cap_recommendation"]
        suggested_caps = cap_rec["suggested_caps"]
        status = _audit_display_status(audit)
        print("\nTarget-weight pilot readiness audit")
        print(f"  candidate: {audit['candidate_id']}")
        print(f"  trade_day: {audit['trade_day']} score_day: {plan_summary['score_day']}")
        execution_day = audit.get("execution_trade_day_check", {})
        print(
            "  execution day: "
            f"{execution_day.get('execution_day', 'N/A')} "
            f"({_check_display_status(execution_day)})"
        )
        market_session = audit.get("execution_market_session_check", {})
        print(
            "  market session: "
            f"{market_session.get('execution_time', 'N/A')} "
            f"({_check_display_status(market_session)})"
        )
        authorization_snapshot = audit.get("pilot_authorization_snapshot_check", {})
        print(
            "  pilot auth snapshot: "
            f"{_check_display_status(authorization_snapshot)} - "
            f"{authorization_snapshot.get('reason', 'not checked')}"
        )
        print(f"  status: {status}")
        print(
            "  launch: "
            f"clean={launch['clean_final_days_current']}/{launch['clean_final_days_required']} "
            f"infra={'YES' if launch['infra_ready'] else 'NO'} "
            f"auth={'YES' if launch.get('pilot_authorization_present') else 'NO'} "
            f"launch={'YES' if launch['launch_ready'] else 'NO'}"
        )
        print(
            "  plan: "
            f"orders={plan_summary['order_count']} "
            f"positions={plan_summary['target_position_count']} "
            f"max_order={plan_summary['max_order_notional']:,.0f} "
            f"gross_exposure={plan_summary['gross_exposure_after']:,.0f}"
        )
        print(
            "  suggested caps: "
            f"orders={suggested_caps['max_orders_per_day']} "
            f"positions={suggested_caps['max_concurrent_positions']} "
            f"notional={suggested_caps['max_notional_per_trade']:,} "
            f"exposure={suggested_caps['max_gross_exposure']:,}"
        )
        print(f"  pilot validation: {audit['plan_validation']['reason']}")
        print(f"  idempotency: {audit['execution_idempotency']['reason']}")
        print(f"  positions: {audit['pre_execution_reconciliation']['reason']}")
        liquidity = audit["liquidity_check"]
        print(
            "  liquidity: "
            f"{'PASS' if liquidity['complete'] else 'BLOCKED'} - {liquidity['reason']}"
        )
        pre_trade_risk = audit["pre_trade_risk_check"]
        print(
            "  pre-trade risk: "
            f"{'PASS' if pre_trade_risk['complete'] else 'BLOCKED'} - {pre_trade_risk['reason']}"
        )
        for reason in audit["blocking_reasons"][:8]:
            print(f"  blocker: {reason}")
        remaining = len(audit["blocking_reasons"]) - 8
        if remaining > 0:
            print(f"  blocker: +{remaining} more")
        for reason in audit["warning_reasons"][:5]:
            print(f"  warning: {reason}")
        print(f"  next: {audit['next_action']}")
        if audit.get("operator_commands", {}).get("enable_suggested_caps"):
            print("  enable command: see report")
        print(f"  artifact: {result['artifact_path']}")
        print(f"  report: {result['report_path']}")
        print(f"  experiment manifest: {result['experiment_manifest_path']}")
        if not audit["ready_for_cap_approval"] or status == "WAITING_FOR_MARKET_SESSION":
            raise SystemExit(1)
        return

    result = run_pilot(
        candidate_id=args.candidate_id,
        raw_symbols=args.symbols,
        as_of_date=args.as_of_date,
        cash=args.cash,
        execute=args.execute,
        collect_evidence=args.collect_evidence,
        record_shadow_evidence=args.record_shadow_evidence,
        generate_readiness_artifacts=not args.skip_readiness_artifacts,
        generate_runbook=not args.skip_runbook,
        allow_rerun=args.allow_rerun,
        preview_caps=preview_caps,
        max_order_adv_pct=args.max_order_adv_pct,
        output_dir=Path(args.output_dir),
    )

    plan = result["plan"]
    validation = result["validation"]
    cap_preview = result["cap_preview"]
    cap_recommendation = result["cap_recommendation"]
    suggested_caps = cap_recommendation["suggested_caps"]
    print("\nTarget-weight pilot adapter")
    print(f"  candidate: {plan.candidate_id}")
    print(f"  trade_day: {plan.trade_day} score_day: {plan.score_day}")
    print(f"  targets: {', '.join(plan.targets) if plan.targets else '(none)'}")
    print(f"  orders: {len(plan.orders)} max_order={plan.max_order_notional:,.0f}")
    print(f"  pilot: {'ALLOWED' if validation.allowed else 'BLOCKED'} - {validation.reason}")
    print(f"  cap preview: {'PASS' if cap_preview.allowed else 'BLOCKED'} - {cap_preview.reason}")
    liquidity = result.get("liquidity_check", {"complete": True, "reason": "liquidity preflight not checked"})
    print(f"  liquidity: {'PASS' if liquidity['complete'] else 'BLOCKED'} - {liquidity['reason']}")
    pre_trade_risk = result.get(
        "pre_trade_risk_check",
        {"complete": True, "reason": "pre-trade risk validation not checked"},
    )
    print(
        "  pre-trade risk: "
        f"{'PASS' if pre_trade_risk['complete'] else 'BLOCKED'} - {pre_trade_risk['reason']}"
    )
    print(
        "  suggested caps: "
        f"orders={suggested_caps['max_orders_per_day']} "
        f"positions={suggested_caps['max_concurrent_positions']} "
        f"notional={suggested_caps['max_notional_per_trade']:,} "
        f"exposure={suggested_caps['max_gross_exposure']:,}"
    )
    if args.execute:
        fidelity_status = "OK" if result["execution_evidence"].get("complete") else "BLOCKED"
        print(f"  execution fidelity: {fidelity_status} - {result['execution_evidence'].get('reason', '')}")
    if result["evidence_collection"].get("attempted"):
        evidence_status = result["evidence_collection"].get("status", "unknown")
        evidence_reason = result["evidence_collection"].get("reason", "")
        print(f"  pilot evidence: {evidence_status} - {evidence_reason}")
    if result["shadow_evidence_summary"].get("attempted"):
        status = "recorded" if result["shadow_evidence_summary"].get("recorded") else "already recorded"
        print(f"  shadow evidence: {status}")
    if result["launch_artifacts"].get("attempted"):
        readiness = result["launch_artifacts"]["launch_readiness"]
        print(
            "  readiness: "
            f"infra={'YES' if readiness['infra_ready'] else 'NO'} "
            f"launch={'YES' if readiness['launch_ready'] else 'NO'}"
        )
        print(f"  readiness artifact: {readiness['json_path']}")
        if result["launch_artifacts"].get("runbook_path"):
            print(f"  runbook: {result['launch_artifacts']['runbook_path']}")
    print(f"  artifact: {result['artifact_path']}")
    if args.execute and not result["execution_evidence"].get("complete", False):
        raise SystemExit(1)
    if result["evidence_collection"].get("status") == "blocked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
