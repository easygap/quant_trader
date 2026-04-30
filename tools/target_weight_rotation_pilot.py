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


def execute_plan(plan: TargetWeightPlan, *, config: Any | None = None, dry_run: bool = True) -> dict[str, Any]:
    from config.config_loader import Config

    config = config or Config.get()
    if config.trading.get("mode") == "live":
        raise ValueError("target-weight pilot adapter refuses live mode")

    results = {"executed": 0, "skipped": 0, "failed": 0, "details": []}
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

    for order in plan.orders:
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
            results["details"].append({
                "order": asdict(order),
                "status": status,
                "result": res,
            })
        except Exception as exc:
            logger.exception("target-weight order failed: {}", order.symbol)
            results["failed"] += 1
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
    execution: dict[str, Any],
    dry_run: bool,
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
        "execution": execution,
        "live_safety": {
            "live_enabled": False,
            "note": "adapter refuses live mode; live gate remains canonical-artifact + paper-evidence driven",
        },
    }
    path = output_dir / f"target_weight_pilot_session_{plan.candidate_id}_{plan.trade_day}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def run_pilot(
    *,
    candidate_id: str = DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
    raw_symbols: str | None = None,
    as_of_date: str | None = None,
    cash: float | None = None,
    execute: bool = False,
    collect_evidence: bool = False,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    config: Any | None = None,
    collector: Any | None = None,
) -> dict[str, Any]:
    from config.config_loader import Config
    from core.paper_pilot import check_pilot_entry, save_pilot_session_artifact

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

    artifact_path = write_session_artifact(
        plan=plan,
        pilot_check=pilot_check,
        validation=validation,
        execution=execution,
        dry_run=dry_run,
        output_dir=output_dir,
    )

    return {
        "plan": plan,
        "pilot_check": pilot_check,
        "validation": validation,
        "execution": execution,
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
        output_dir=Path(args.output_dir),
    )

    plan = result["plan"]
    validation = result["validation"]
    print("\nTarget-weight pilot adapter")
    print(f"  candidate: {plan.candidate_id}")
    print(f"  trade_day: {plan.trade_day} score_day: {plan.score_day}")
    print(f"  targets: {', '.join(plan.targets) if plan.targets else '(none)'}")
    print(f"  orders: {len(plan.orders)} max_order={plan.max_order_notional:,.0f}")
    print(f"  pilot: {'ALLOWED' if validation.allowed else 'BLOCKED'} - {validation.reason}")
    print(f"  artifact: {result['artifact_path']}")


if __name__ == "__main__":
    main()
