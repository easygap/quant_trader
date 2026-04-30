#!/usr/bin/env python3
"""Paper/pilot adapter for the canonical target-weight rotation candidate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
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


def _split_symbols(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    symbols = [part.strip() for part in raw.replace("\n", ",").split(",")]
    return [symbol for symbol in symbols if symbol]


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


def write_session_artifact(
    *,
    plan: TargetWeightPlan,
    pilot_check: Any,
    validation: Any,
    cap_preview: Any,
    execution: dict[str, Any],
    dry_run: bool,
    shadow_evidence: dict[str, Any] | None = None,
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
        "execution": execution,
        "shadow_evidence": shadow_evidence or {"attempted": False, "recorded": False},
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


def generate_launch_artifacts(
    candidate_id: str,
    *,
    include_runbook: bool = True,
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
        result["runbook_path"] = str(runbook_path)
    return result


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
    dry_run = not execute
    if execute and not validation.allowed:
        raise ValueError(f"pilot plan blocked: {validation.reason}")

    execution = execute_plan(plan, config=config, dry_run=dry_run)

    if execute:
        pilot_session = {
            "active": True,
            "session_mode": "pilot_paper",
            "evidence_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": validation.caps_snapshot,
            "orders_planned": len(plan.orders),
            "orders_executed": execution.get("executed", 0),
        }
        save_pilot_session_artifact(
            strategy=candidate_id,
            date=plan.trade_day,
            pilot_session=pilot_session,
        )

        if collect_evidence:
            from core.paper_evidence import collect_daily_evidence

            collect_daily_evidence(
                strategy=candidate_id,
                mode="paper",
                account_key=candidate_id,
                date=datetime.strptime(plan.trade_day, "%Y-%m-%d"),
                watchlist_symbols=plan.symbols,
                evidence_mode="pilot_paper",
                pilot_authorized=True,
                pilot_caps_snapshot=validation.caps_snapshot,
            )

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
        )

    artifact_path = write_session_artifact(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        cap_preview=cap_preview,
        execution=execution,
        dry_run=dry_run,
        shadow_evidence=shadow_evidence_summary,
        launch_artifacts=launch_artifacts,
        output_dir=output_dir,
    )

    return {
        "plan": plan,
        "pilot_check": pilot_check,
        "validation": validation,
        "cap_preview": cap_preview,
        "execution": execution,
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
        "--skip-readiness-artifacts",
        action="store_true",
        help="Skip launch readiness JSON/MD generation when --record-shadow-evidence is used.",
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
        preview_caps=build_preview_caps(
            max_orders=args.preview_max_orders,
            max_positions=args.preview_max_positions,
            max_notional=args.preview_max_notional,
            max_exposure=args.preview_max_exposure,
        ),
        output_dir=Path(args.output_dir),
    )

    plan = result["plan"]
    validation = result["validation"]
    cap_preview = result["cap_preview"]
    print("\nTarget-weight pilot adapter")
    print(f"  candidate: {plan.candidate_id}")
    print(f"  trade_day: {plan.trade_day} score_day: {plan.score_day}")
    print(f"  targets: {', '.join(plan.targets) if plan.targets else '(none)'}")
    print(f"  orders: {len(plan.orders)} max_order={plan.max_order_notional:,.0f}")
    print(f"  pilot: {'ALLOWED' if validation.allowed else 'BLOCKED'} - {validation.reason}")
    print(f"  cap preview: {'PASS' if cap_preview.allowed else 'BLOCKED'} - {cap_preview.reason}")
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


if __name__ == "__main__":
    main()
