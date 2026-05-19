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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PROMOTION_METADATA_PATH = Path("reports/promotion/run_metadata.json")
KST = timezone(timedelta(hours=9))
REPORTS_DIR = Path("reports")
TARGET_WEIGHT_BASE_STRATEGIES = frozenset({"target_weight_rotation"})
TARGET_WEIGHT_SUGGESTED_CAP_FIELDS = (
    "max_orders_per_day",
    "max_concurrent_positions",
    "max_notional_per_trade",
    "max_gross_exposure",
)
TARGET_WEIGHT_MONEY_CAP_FIELDS = frozenset({
    "max_notional_per_trade",
    "max_gross_exposure",
})


def _target_weight_cap_envelope_violations(
    requested_caps: dict,
    cap_recommendation: dict,
) -> list[str]:
    suggested_caps = cap_recommendation.get("suggested_caps") or {}
    minimum_caps = cap_recommendation.get("minimum_caps") or {}
    try:
        rounding_step = int(cap_recommendation.get("rounding_step") or 0)
    except (TypeError, ValueError):
        rounding_step = 0
    if not suggested_caps:
        return ["readiness suggested caps missing"]

    violations: list[str] = []
    for field in TARGET_WEIGHT_SUGGESTED_CAP_FIELDS:
        requested = requested_caps.get(field)
        minimum = minimum_caps.get(field)
        suggested = suggested_caps.get(field)
        if requested is None:
            violations.append(f"{field}: requested missing")
            continue
        if minimum is not None and requested < minimum:
            violations.append(
                f"{field}: requested={requested} below_minimum={minimum}"
            )
        tolerance = rounding_step if field in TARGET_WEIGHT_MONEY_CAP_FIELDS else 0
        upper_bound = suggested + tolerance if suggested is not None else None
        if upper_bound is not None and requested > upper_bound:
            violations.append(
                f"{field}: requested={requested} above_suggested={suggested}"
                f" tolerance={tolerance}"
            )
    return violations


def _load_default_status_strategy(*, reports_dir: str | Path | None = None) -> str | None:
    base = Path(reports_dir) if reports_dir is not None else Path(REPORTS_DIR)
    path = base / "current_blockers.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    runbook = payload.get("operator_runbook")
    if isinstance(runbook, dict):
        primary = str(runbook.get("primary_strategy") or "").strip()
        if primary:
            return primary
        priority = runbook.get("current_priority_action")
        if isinstance(priority, dict):
            strategy = str(priority.get("strategy") or "").strip()
            if strategy:
                return strategy

    next_actions = payload.get("next_actions")
    if isinstance(next_actions, list):
        for action in next_actions:
            if not isinstance(action, dict):
                continue
            strategy = str(action.get("strategy") or "").strip()
            if strategy:
                return strategy
    return None


def main():
    parser = argparse.ArgumentParser(description="Paper Pilot Control")
    parser.add_argument("--strategy")
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

    if args.status and not args.strategy:
        args.strategy = _load_default_status_strategy()
        if args.strategy:
            print(f"INFO: --strategy omitted; using current blockers primary strategy: {args.strategy}")
    if (args.enable or args.disable or args.check_prerequisites or args.status) and not args.strategy:
        parser.error("--strategy is required unless --status can resolve current_blockers primary strategy")

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
    cap_violations = _target_weight_cap_envelope_violations(
        requested_caps,
        audit.get("cap_recommendation") or {},
    )
    if cap_violations:
        raise ValueError(
            "requested target-weight pilot caps must stay within readiness cap envelope: "
            + "; ".join(cap_violations)
            + f". Use the suggested caps in {result['report_path']}"
        )
    plan = result.get("plan")
    if plan is None:
        raise ValueError(
            "target-weight readiness plan missing: "
            f"pilot enable requires a plan snapshot from {result['report_path']}"
        )
    plan_candidate_id = str(getattr(plan, "candidate_id", "") or "").strip()
    if plan_candidate_id != args.strategy:
        raise ValueError(
            "target-weight readiness plan candidate mismatch: "
            f"strategy={args.strategy} plan_candidate_id={plan_candidate_id or 'missing'}. "
            f"Rerun enable with the matching plan in {result['report_path']}"
        )
    audit_trade_day = str(audit.get("trade_day") or "").strip()
    if audit_trade_day != args.valid_from:
        raise ValueError(
            "target-weight readiness audit trade day mismatch: "
            f"valid_from={args.valid_from} audit_trade_day={audit_trade_day or 'missing'}. "
            f"Rerun enable with the audit trade day in {result['report_path']}"
        )
    plan_trade_day = str(getattr(plan, "trade_day", "") or "").strip()
    if plan_trade_day and plan_trade_day != args.valid_from:
        raise ValueError(
            "target-weight readiness plan trade day mismatch: "
            f"valid_from={args.valid_from} plan_trade_day={plan_trade_day}. "
            f"Rerun enable with the plan trade day in {result['report_path']}"
        )
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

    is_target_weight = _is_target_weight_strategy_for_enable(strategy)
    check = check_pilot_entry(strategy)
    entry_label = "Core Entry Check" if is_target_weight else "Entry Check"
    print(f"\n  {entry_label}: {'ALLOWED' if check.allowed else 'BLOCKED'}")
    print(f"  Reason: {check.reason}")
    if check.remaining_orders is not None:
        print(f"  Remaining orders: {check.remaining_orders}")
    if check.remaining_exposure is not None:
        print(f"  Remaining exposure: {check.remaining_exposure:,}")
    if is_target_weight:
        _print_target_weight_daily_ops_status(strategy)


def _load_latest_target_weight_daily_ops(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> dict | None:
    base = Path(reports_dir)
    prefix = f"target_weight_daily_ops_summary_{strategy}_"
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)
    candidates = sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob(f"{prefix}*.json")
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("artifact_type") != "target_weight_daily_ops_summary":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if not _daily_ops_trade_day_is_available(payload):
            continue
        payload["source_path"] = str(path)
        return payload
    return None


def _load_target_weight_current_blockers_run_guard(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> dict:
    path = Path(reports_dir) / "current_blockers.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    candidates: list[dict] = []
    next_actions = payload.get("next_actions")
    if isinstance(next_actions, list):
        candidates.extend(
            action
            for action in next_actions
            if isinstance(action, dict) and action.get("strategy") == strategy
        )

    runbook = payload.get("operator_runbook")
    if isinstance(runbook, dict) and runbook.get("primary_strategy") == strategy:
        priority = runbook.get("current_priority_action")
        if isinstance(priority, dict):
            candidates.append(priority)
        sequence = runbook.get("sequence")
        if isinstance(sequence, list):
            candidates.extend(step for step in sequence if isinstance(step, dict))

    for candidate in candidates:
        not_before = candidate.get("not_before_date")
        if not_before and not _not_before_date_pending(not_before):
            continue
        guard = {
            key: candidate[key]
            for key in ("not_before_date", "premature_run_guard")
            if candidate.get(key)
        }
        if guard:
            return guard
    return {}


def _current_kst_date() -> str:
    return datetime.now(KST).date().isoformat()


def _not_before_date_pending(not_before_date: str | None, *, current_date: str | None = None) -> bool:
    if not not_before_date:
        return False
    try:
        target = datetime.strptime(str(not_before_date), "%Y-%m-%d").date()
        current = datetime.strptime(current_date or _current_kst_date(), "%Y-%m-%d").date()
    except ValueError:
        return True
    return target > current


def _daily_ops_trade_day_is_available(payload: dict, *, current_date: str | None = None) -> bool:
    trade_day = str(payload.get("trade_day") or "").strip()
    if not trade_day:
        return False
    today = current_date or _current_kst_date()
    try:
        trade_date = datetime.strptime(trade_day, "%Y-%m-%d").date()
        current = datetime.strptime(today, "%Y-%m-%d").date()
        return trade_date <= current
    except ValueError:
        return False


def _print_target_weight_daily_ops_status(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> None:
    summary = _load_latest_target_weight_daily_ops(strategy, reports_dir=reports_dir)
    command = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --daily-ops-summary"
    )
    if summary is None:
        print("\n  Target-weight Daily Ops: MISSING")
        print(f"  Run: {command}")
        return

    progress = summary.get("evidence_progress") or {}
    decision = summary.get("decision") or {}
    diagnostics = decision.get("post_evidence_diagnostics") or []
    operator_commands = summary.get("operator_commands") or {}
    execute_command = operator_commands.get("execute_capped_paper") or ""
    finalize_command = operator_commands.get("finalize_pilot_evidence") or ""
    repair_command = operator_commands.get("repair_pilot_evidence") or ""
    next_daily_ops_command = operator_commands.get("next_daily_ops_summary") or ""
    next_readiness_command = operator_commands.get("next_readiness_audit") or ""
    next_operator_trade_day = summary.get("next_operator_trade_day")
    run_guard = _load_target_weight_current_blockers_run_guard(
        strategy,
        reports_dir=reports_dir,
    )
    not_before_date = (
        run_guard.get("not_before_date")
        or summary.get("not_before_date")
        or (
            next_operator_trade_day
            if (
                summary.get("status") == "PILOT_EVIDENCE_RECORDED"
                and _not_before_date_pending(next_operator_trade_day)
            )
            else None
        )
    )
    premature_run_guard = run_guard.get("premature_run_guard") or summary.get(
        "premature_run_guard"
    )
    print("\n  Target-weight Daily Ops:")
    print(f"    Status: {summary.get('status', 'unknown')}")
    print(f"    Trade day: {summary.get('trade_day', 'N/A')}")
    if next_operator_trade_day:
        print(f"    Next operator trade day: {next_operator_trade_day}")
    if not_before_date:
        print(f"    Not before date: {not_before_date}")
    if premature_run_guard:
        print(f"    Premature run guard: {premature_run_guard}")
    print(
        "    Verified pilot days: "
        f"{progress.get('verified_pilot_days', 0)}/{progress.get('target_days', 'N/A')}"
    )
    evidence_breakdown = []
    if "shadow_days" in progress:
        evidence_breakdown.append(f"shadow={progress.get('shadow_days', 0)}")
    if "repaired_pilot_days" in progress:
        evidence_breakdown.append(f"repaired={progress.get('repaired_pilot_days', 0)}")
    if "invalid_execution_days" in progress:
        evidence_breakdown.append(f"invalid={progress.get('invalid_execution_days', 0)}")
    if evidence_breakdown:
        print(f"    Evidence breakdown: {' '.join(evidence_breakdown)}")
    print(f"    Next: {summary.get('next_step', 'N/A')}")
    if diagnostics:
        print(f"    Post-evidence diagnostics: {len(diagnostics)}")
    if execute_command:
        if str(execute_command).lstrip().startswith("# blocked:"):
            print("    Adapter execution: BLOCKED by daily ops")
        else:
            print("    Adapter execution: follow daily ops READY_TO_EXECUTE command only")
        print(f"    Execute command: {execute_command}")
    if finalize_command:
        print(f"    Finalize evidence command: {finalize_command}")
    if repair_command:
        print(f"    Repair evidence command: {repair_command}")
    if next_daily_ops_command:
        print(f"    Next daily ops command: {next_daily_ops_command}")
    if next_readiness_command:
        print(f"    Next readiness command: {next_readiness_command}")
    print(f"    Source: {summary.get('source_path')}")


def run_check_prerequisites(strategy):
    from core.paper_pilot import check_pilot_prerequisites
    ok, reason = check_pilot_prerequisites(strategy)
    print(f"\nPrerequisites for {strategy}: {'MET' if ok else 'NOT MET'}")
    print(f"  {reason}")


if __name__ == "__main__":
    main()
