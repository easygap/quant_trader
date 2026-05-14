#!/usr/bin/env python3
"""
Paper Pilot Control CLI

Usage:
    # pilot 활성화
    python tools/paper_pilot_control.py --strategy scoring --enable \\
        --from 2026-04-07 --to 2026-04-11 \\
        --max-orders 2 --max-notional 1000000 --max-exposure 3000000 \\
        --reason "collect first 5 pilot days"

    # pilot 상태 확인
    python tools/paper_pilot_control.py --strategy scoring --status

    # pilot 비활성화
    python tools/paper_pilot_control.py --strategy scoring --disable --reason "stop pilot"

    # 사전 조건 확인
    python tools/paper_pilot_control.py --strategy rotation --check-prerequisites
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PROMOTION_METADATA_PATH = Path("reports/promotion/run_metadata.json")
TARGET_WEIGHT_BASE_STRATEGIES = frozenset({"target_weight_rotation"})


def main():
    parser = argparse.ArgumentParser(description="Paper Pilot Control")
    parser.add_argument("--strategy", required=True)
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--enable", action="store_true")
    action_group.add_argument("--disable", action="store_true")
    action_group.add_argument("--status", action="store_true")
    action_group.add_argument("--check-prerequisites", action="store_true")
    parser.add_argument("--from", dest="valid_from")
    parser.add_argument("--to", dest="valid_to")
    parser.add_argument("--max-orders", type=int, default=2)
    parser.add_argument("--max-positions", type=int, default=2)
    parser.add_argument("--max-notional", type=int, default=1_000_000)
    parser.add_argument("--max-exposure", type=int, default=3_000_000)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    if args.enable:
        if not args.valid_from or not args.valid_to:
            print("ERROR: --from and --to required for --enable")
            sys.exit(1)
        run_enable(args)
    elif args.disable:
        run_disable(args)
    elif args.check_prerequisites:
        run_check_prerequisites(args.strategy)
    elif args.status:
        run_status(args.strategy)
    else:
        parser.print_help()


def run_enable(args):
    from core.paper_pilot import enable_pilot, check_pilot_prerequisites

    # prerequisites check
    ok, reason = check_pilot_prerequisites(args.strategy)
    if not ok:
        print(f"PREREQUISITE FAIL: {reason}")
        print("Pilot을 활성화하려면 먼저 shadow bootstrap으로 prerequisites를 충족하세요.")
        sys.exit(1)

    try:
        target_weight_audit = _target_weight_enable_guard(args)
        target_weight_plan_snapshot = None
        if target_weight_audit is not None:
            target_weight_plan_snapshot = target_weight_audit.get("target_weight_plan_snapshot")
        auth = enable_pilot(
            strategy=args.strategy,
            valid_from=args.valid_from,
            valid_to=args.valid_to,
            max_orders=args.max_orders,
            max_positions=args.max_positions,
            max_notional=args.max_notional,
            max_exposure=args.max_exposure,
            reason=args.reason,
            target_weight_plan_snapshot=target_weight_plan_snapshot,
        )
        print(f"\nPilot ENABLED: {args.strategy}")
        print(f"  Period: {auth.valid_from} ~ {auth.valid_to}")
        print(f"  Max orders/day: {auth.max_orders_per_day}")
        print(f"  Max positions: {auth.max_concurrent_positions}")
        print(f"  Max notional/trade: {auth.max_notional_per_trade:,}")
        print(f"  Max exposure: {auth.max_gross_exposure:,}")
        print(f"  Reason: {auth.operator_reason}")
        if target_weight_audit is not None:
            print("  Target-weight readiness audit: PASS")
            print(f"    artifact: {target_weight_audit['artifact_path']}")
            print(f"    report: {target_weight_audit['report_path']}")
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def _load_promotion_metadata(path: str | Path | None = None) -> dict | None:
    metadata_path = Path(path) if path is not None else PROMOTION_METADATA_PATH
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _is_target_weight_strategy_for_enable(
    strategy: str,
    canonical_metadata: dict | None = None,
) -> bool:
    strategy_name = str(strategy)
    if strategy_name.startswith("target_weight_"):
        return True
    metadata = canonical_metadata if canonical_metadata is not None else _load_promotion_metadata()
    specs = metadata.get("strategy_specs") if isinstance(metadata, dict) else None
    if not isinstance(specs, list):
        return False
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("candidate_id") != strategy_name:
            continue
        base_strategy = spec.get("base_strategy") or spec.get("strategy")
        candidate_id = spec.get("candidate_id")
        return (
            isinstance(base_strategy, str)
            and base_strategy in TARGET_WEIGHT_BASE_STRATEGIES
        ) or (
            isinstance(candidate_id, str)
            and candidate_id.startswith("target_weight_")
        )
    return False


def _target_weight_enable_guard(args):
    if not _is_target_weight_strategy_for_enable(args.strategy):
        return None

    from tools.target_weight_rotation_pilot import (
        build_pilot_authorization_snapshot,
        build_preview_caps,
        run_pilot_readiness_audit,
    )

    requested_caps = build_preview_caps(
        max_orders=args.max_orders,
        max_positions=args.max_positions,
        max_notional=args.max_notional,
        max_exposure=args.max_exposure,
    )
    result = run_pilot_readiness_audit(
        candidate_id=args.strategy,
        as_of_date=args.valid_from,
        preview_caps=requested_caps,
    )
    audit = result["audit"]
    cap_preview = audit["cap_preview"]
    if not audit.get("ready_for_cap_approval", False):
        reason = "; ".join(audit.get("blocking_reasons") or ["unknown blocker"])
        raise ValueError(
            "target-weight readiness audit blocked pilot enable: "
            f"{reason}. Report: {result['report_path']}"
        )
    if not cap_preview.get("allowed", False):
        raise ValueError(
            "requested target-weight pilot caps do not satisfy the current plan: "
            f"{cap_preview.get('reason', 'cap preview blocked')}. "
            f"Use the suggested caps in {result['report_path']}"
        )
    plan = result.get("plan")
    if plan is not None:
        result["target_weight_plan_snapshot"] = build_pilot_authorization_snapshot(
            plan,
            readiness_audit=audit,
        )
    return result


def run_disable(args):
    from core.paper_pilot import disable_pilot
    disable_pilot(args.strategy, args.reason)
    print(f"Pilot DISABLED: {args.strategy}")


def run_status(strategy):
    from core.paper_pilot import get_active_pilot, check_pilot_entry

    auth = get_active_pilot(strategy)
    if auth is None:
        print(f"\nNo active pilot for {strategy}")
    else:
        print(f"\nActive Pilot: {strategy}")
        print(f"  Period: {auth.valid_from} ~ {auth.valid_to}")
        print(f"  Max orders/day: {auth.max_orders_per_day}")
        print(f"  Max positions: {auth.max_concurrent_positions}")
        print(f"  Max notional/trade: {auth.max_notional_per_trade:,}")
        print(f"  Max exposure: {auth.max_gross_exposure:,}")

    check = check_pilot_entry(strategy)
    print(f"\n  Entry Check: {'ALLOWED' if check.allowed else 'BLOCKED'}")
    print(f"  Reason: {check.reason}")
    if check.remaining_orders is not None:
        print(f"  Remaining orders: {check.remaining_orders}")
    if check.remaining_exposure is not None:
        print(f"  Remaining exposure: {check.remaining_exposure:,}")


def run_check_prerequisites(strategy):
    from core.paper_pilot import check_pilot_prerequisites
    ok, reason = check_pilot_prerequisites(strategy)
    print(f"\nPrerequisites for {strategy}: {'MET' if ok else 'NOT MET'}")
    print(f"  {reason}")


if __name__ == "__main__":
    main()
