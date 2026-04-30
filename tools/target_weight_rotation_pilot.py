#!/usr/bin/env python3
"""Paper/pilot adapter for the canonical target-weight rotation candidate."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from loguru import logger

from core.target_weight_rotation import (
    DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    TargetWeightPlan,
    build_target_weight_plan,
    load_canonical_target_weight_spec,
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


def _format_enable_command(plan: TargetWeightPlan, caps: dict[str, int]) -> str:
    return "\n".join([
        f"python tools/paper_pilot_control.py --strategy {plan.candidate_id} --enable \\",
        f"  --from {plan.trade_day} --to YYYY-MM-DD \\",
        (
            f"  --max-orders {caps['max_orders_per_day']} "
            f"--max-positions {caps['max_concurrent_positions']} "
            f"--max-notional {caps['max_notional_per_trade']} "
            f"--max-exposure {caps['max_gross_exposure']} \\"
        ),
        '  --reason "target-weight shadow dry-run matched suggested pilot caps"',
    ])


def recommend_pilot_caps(
    plan: TargetWeightPlan,
    *,
    buffer_pct: float = DEFAULT_CAP_BUFFER_PCT,
    rounding_step: int = DEFAULT_CAP_ROUNDING_STEP,
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
    return {
        "minimum_caps": minimum_caps,
        "suggested_caps": suggested_caps,
        "buffer_pct": buffer_pct,
        "rounding_step": rounding_step,
        "planned_orders": len(plan.orders),
        "target_position_count": int(plan.target_position_count),
        "max_order_notional": plan.max_order_notional,
        "gross_exposure_after": plan.gross_exposure_after,
        "suggested_preview": asdict(suggested_preview),
        "enable_command": _format_enable_command(plan, suggested_caps),
        "operator_note": (
            "Use suggested caps for the first capped paper pilot only after launch readiness "
            "requirements and preflight checks pass. The caps are based on the dry-run plan and "
            "do not imply live eligibility."
        ),
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
    return build_target_weight_plan(
        candidate_id=candidate_id,
        symbols=symbols,
        params=spec.params,
        cash=plan_cash,
        positions=positions,
        as_of_date=as_of_date,
        collector=collector,
    )


def execute_plan(
    plan: TargetWeightPlan,
    *,
    config: Any | None = None,
    dry_run: bool = True,
    stop_on_failure: bool = True,
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
    }
    if dry_run:
        for order in plan.orders:
            results["skipped"] += 1
            results["details"].append({
                "order": asdict(order),
                "status": "dry_run",
            })
        return results

    from core.order_executor import OrderExecutor
    from core.portfolio_manager import PortfolioManager

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
                res = executor.execute_sell(
                    symbol=order.symbol,
                    price=order.price,
                    quantity=order.quantity,
                    reason=order.reason,
                    strategy=plan.candidate_id,
                )
            else:
                available_cash = portfolio.get_available_cash()
                total_value = portfolio.get_total_value()
                res = executor.execute_buy_quantity(
                    symbol=order.symbol,
                    price=order.price,
                    quantity=order.quantity,
                    capital=total_value,
                    available_cash=available_cash,
                    reason=order.reason,
                    strategy=plan.candidate_id,
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


def summarize_execution_for_evidence(plan: TargetWeightPlan, execution: dict[str, Any]) -> dict[str, Any]:
    planned = len(plan.orders)
    executed = int(execution.get("executed", 0) or 0)
    failed = int(execution.get("failed", 0) or 0)
    skipped = int(execution.get("skipped", 0) or 0)
    halted = bool(execution.get("halted", False))
    complete = failed == 0 and skipped == 0 and not halted and executed == planned
    reason = "all planned target-weight orders executed"
    if not complete:
        reason = (
            "target_weight_execution_incomplete: "
            f"executed={executed}/{planned} failed={failed} skipped={skipped} halted={halted}"
        )
        halt_reason = execution.get("halt_reason")
        if halt_reason:
            reason = f"{reason}; halt_reason={halt_reason}"

    return {
        "complete": complete,
        "reason": reason,
        "planned_orders": planned,
        "executed_orders": executed,
        "failed_orders": failed,
        "skipped_orders": skipped,
        "halted": halted,
        "halt_reason": execution.get("halt_reason", ""),
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
) -> dict[str, Any]:
    caps = dict(getattr(validation, "caps_snapshot", None) or {})
    caps["target_weight_plan"] = {
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
    }
    caps["target_weight_execution"] = summarize_execution_for_evidence(plan, execution)
    return caps


def write_session_artifact(
    *,
    plan: TargetWeightPlan,
    pilot_check: Any,
    validation: Any,
    cap_preview: Any,
    cap_recommendation: dict[str, Any],
    execution: dict[str, Any],
    dry_run: bool,
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
        "execution": execution,
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
    target_unique_trade_days: int | None = None,
    max_scan_weekdays: int | None = None,
    generate_readiness_artifacts: bool = True,
    generate_runbook: bool = True,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
) -> dict[str, Any]:
    """Record non-promotable target-weight shadow evidence over a date range."""
    from config.config_loader import Config
    from core.paper_evidence import get_canonical_records
    from core.paper_pilot import check_pilot_entry

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
    generate_readiness_artifacts: bool = True,
    generate_runbook: bool = True,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
) -> dict[str, Any]:
    from config.config_loader import Config
    from core.paper_pilot import check_pilot_entry, save_pilot_session_artifact

    if execute and record_shadow_evidence:
        raise ValueError("record_shadow_evidence is only valid for dry-run sessions")

    config = config or Config.get()
    plan = build_plan(
        candidate_id=candidate_id,
        raw_symbols=raw_symbols,
        as_of_date=as_of_date,
        cash=cash,
        config=config,
        collector=collector,
    )

    pilot_check = check_pilot_entry(
        candidate_id,
        candidate_notional=plan.max_order_notional,
        as_of_date=as_of_date or plan.trade_day,
    )
    validation = validate_plan_against_pilot(plan, pilot_check)
    cap_preview = preview_plan_against_caps(plan, preview_caps)
    cap_recommendation = recommend_pilot_caps(plan)
    dry_run = not execute
    if execute and not validation.allowed:
        raise ValueError(f"pilot plan blocked: {validation.reason}")

    execution = execute_plan(plan, config=config, dry_run=dry_run)
    execution_evidence = summarize_execution_for_evidence(plan, execution)
    evidence_collection = {"attempted": False, "recorded": False}

    if execute:
        evidence_caps_snapshot = build_pilot_evidence_caps_snapshot(plan, validation, execution)
        pilot_session = {
            "active": True,
            "session_mode": "pilot_paper",
            "evidence_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": evidence_caps_snapshot,
            "orders_planned": len(plan.orders),
            "orders_executed": execution.get("executed", 0),
            "execution_complete": execution_evidence["complete"],
            "evidence_collectible": execution_evidence["complete"],
            "evidence_block_reason": "" if execution_evidence["complete"] else execution_evidence["reason"],
            "target_weight_execution": execution_evidence,
        }
        save_pilot_session_artifact(
            strategy=candidate_id,
            date=plan.trade_day,
            pilot_session=pilot_session,
        )

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
                evidence_collection.update({
                    "recorded": evidence_record is not None,
                    "status": "recorded" if evidence_record is not None else "already_recorded",
                    "reason": "pilot_paper evidence recorded"
                    if evidence_record is not None else "pilot_paper evidence already recorded",
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
        execution=execution,
        dry_run=dry_run,
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
        "execution": execution,
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
    parser.add_argument("--preview-max-orders", type=int, help="Proposed pilot cap preview: max orders/day.")
    parser.add_argument("--preview-max-positions", type=int, help="Proposed pilot cap preview: max concurrent positions.")
    parser.add_argument("--preview-max-notional", type=int, help="Proposed pilot cap preview: max notional/trade.")
    parser.add_argument("--preview-max-exposure", type=int, help="Proposed pilot cap preview: max gross exposure.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

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
        except ValueError as exc:
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
        preview_caps=preview_caps,
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
    print(
        "  suggested caps: "
        f"orders={suggested_caps['max_orders_per_day']} "
        f"positions={suggested_caps['max_concurrent_positions']} "
        f"notional={suggested_caps['max_notional_per_trade']:,} "
        f"exposure={suggested_caps['max_gross_exposure']:,}"
    )
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
    if result["evidence_collection"].get("status") == "blocked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
