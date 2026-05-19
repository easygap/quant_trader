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
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.target_weight_commands import command_scope_issues as target_weight_command_scope_issues

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
    recovery_command = _pilot_entry_recovery_command(strategy, check.reason)
    if recovery_command:
        recovery_label = (
            "Core recovery command" if is_target_weight else "Recovery command"
        )
        print(f"  {recovery_label}: {recovery_command}")
    if check.remaining_orders is not None:
        print(f"  Remaining orders: {check.remaining_orders}")
    if check.remaining_exposure is not None:
        print(f"  Remaining exposure: {check.remaining_exposure:,}")
    if is_target_weight:
        _print_target_weight_daily_ops_status(strategy)


def _pilot_entry_recovery_command(strategy: str, reason: object) -> str | None:
    reason_text = str(reason or "").lower()
    if "--send-test-notification" not in reason_text and "notifier" not in reason_text:
        return None
    return (
        "python tools/paper_preflight.py "
        f"--strategy {strategy} --with-pilot-check --send-test-notification"
    )


def _load_latest_target_weight_daily_ops(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> dict | None:
    candidates = _target_weight_daily_ops_summary_paths(strategy, reports_dir=reports_dir)
    valid_candidates: list[tuple[tuple[str, float], dict]] = []
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
        if not _daily_ops_summary_hash_is_valid(payload):
            continue
        if not _daily_ops_trade_day_is_available(payload):
            continue
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        sanitized = _sanitize_target_weight_daily_ops_summary(payload)
        if sanitized is not None:
            valid_candidates.append((_daily_ops_trade_day_sort_key(payload, path), sanitized))
    if valid_candidates:
        return max(valid_candidates, key=lambda item: item[0])[1]
    return None


def _target_weight_daily_ops_summary_paths(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> list[Path]:
    base = Path(reports_dir)
    prefix = f"target_weight_daily_ops_summary_{strategy}_"
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)
    return sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob(f"{prefix}*.json")
            if not path.name.startswith("target_weight_daily_ops_summary_failure_")
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _target_weight_daily_ops_failure_paths(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> list[Path]:
    base = Path(reports_dir)
    prefix = f"target_weight_daily_ops_summary_failure_{strategy}_"
    search_dirs = [base]
    paper_runtime_dir = base / "paper_runtime"
    if paper_runtime_dir != base:
        search_dirs.append(paper_runtime_dir)
    return sorted(
        {
            path
            for search_dir in search_dirs
            for path in search_dir.glob(f"{prefix}*.json")
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _daily_ops_artifact_generated_timestamp(payload: dict) -> float:
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        return 0.0
    try:
        return datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _daily_ops_artifact_source_mtime(payload: dict) -> float:
    try:
        return float(payload.get("source_mtime") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _daily_ops_artifact_time_key(payload: dict) -> tuple[float, float]:
    source_mtime = _daily_ops_artifact_source_mtime(payload)
    source_path = str(payload.get("source_path") or "").strip()
    if not source_mtime and source_path:
        try:
            source_mtime = Path(source_path).stat().st_mtime
        except OSError:
            source_mtime = 0.0
    generated_ts = _daily_ops_artifact_generated_timestamp(payload)
    return (generated_ts or source_mtime, source_mtime)


def _target_weight_daily_ops_failure_sort_key(
    payload: dict,
    path: Path,
) -> tuple[float, float]:
    sortable = dict(payload)
    sortable["source_path"] = _artifact_source_path(path)
    sortable["source_mtime"] = path.stat().st_mtime
    return _daily_ops_artifact_time_key(sortable)


def _load_latest_target_weight_daily_ops_failure(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> dict | None:
    candidates: list[tuple[tuple[float, float], dict]] = []
    for path in _target_weight_daily_ops_failure_paths(strategy, reports_dir=reports_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("candidate_id") != strategy:
            continue
        artifact_type = str(payload.get("artifact_type") or "")
        mode = str(payload.get("mode") or "")
        if artifact_type == "target_weight_no_order_operation_failure":
            if mode != "daily_ops_summary":
                continue
        elif artifact_type != "target_weight_daily_ops_summary_failure":
            continue
        payload = dict(payload)
        payload["source_path"] = _artifact_source_path(path)
        payload["source_mtime"] = path.stat().st_mtime
        candidates.append(
            (_target_weight_daily_ops_failure_sort_key(payload, path), payload)
        )
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _target_weight_daily_ops_failure_reason(payload: dict) -> str:
    reason = str(payload.get("reason") or "").strip()
    if reason:
        return reason
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
    elif error:
        return str(error)
    blocking_reasons = payload.get("blocking_reasons")
    if isinstance(blocking_reasons, list) and blocking_reasons:
        return str(blocking_reasons[0])
    return "unknown"


def _target_weight_daily_ops_failure_error(payload: dict) -> str:
    error = payload.get("error")
    if not isinstance(error, dict):
        return str(error or "").strip()
    error_type = str(error.get("type") or "").strip()
    message = str(error.get("message") or "").strip()
    if error_type and message:
        return f"{error_type}: {message}"
    return error_type or message


def _target_weight_daily_ops_failure_command(payload: dict, fallback: str) -> str:
    commands = payload.get("operator_commands")
    if isinstance(commands, dict):
        command = str(commands.get("daily_ops_summary") or "").strip()
        if command:
            return command
    return fallback


def _daily_ops_failure_is_newer_than_summary(failure: dict | None, summary: dict) -> bool:
    if not failure:
        return False
    return _daily_ops_artifact_time_key(failure) > _daily_ops_artifact_time_key(summary)


def _target_weight_daily_ops_integrity_warnings(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> list[str]:
    warnings: list[str] = []
    for path in _target_weight_daily_ops_summary_paths(strategy, reports_dir=reports_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"{path}: unreadable {exc.__class__.__name__}")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"{path}: top-level JSON is not an object")
            continue
        if payload.get("artifact_type") != "target_weight_daily_ops_summary":
            continue
        if payload.get("candidate_id") != strategy:
            continue
        if payload.get("schema_version") != 1:
            warnings.append(f"{path}: schema_version mismatch")
        if not _daily_ops_summary_hash_is_valid(payload):
            warnings.append(f"{path}: summary_hash mismatch or missing")
        if not str(payload.get("trade_day") or "").strip():
            warnings.append(f"{path}: trade_day missing")
    return warnings


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


def _load_target_weight_current_blockers_priority_action(
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

    regenerate_command = _target_weight_current_blockers_regenerate_command(payload)
    runbook = payload.get("operator_runbook")

    def with_regenerate_command(action: dict) -> dict:
        result = dict(action)
        result["regenerate_current_blockers_command"] = regenerate_command
        return result

    if isinstance(runbook, dict) and runbook.get("primary_strategy") == strategy:
        priority = runbook.get("current_priority_action")
        if isinstance(priority, dict) and priority.get("strategy") == strategy:
            return with_regenerate_command(priority)

    next_actions = payload.get("next_actions")
    if isinstance(next_actions, list):
        for action in next_actions:
            if isinstance(action, dict) and action.get("strategy") == strategy:
                return with_regenerate_command(action)
    return {}


def _load_target_weight_promotion_artifact_freshness(
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
    freshness = payload.get("promotion_artifact_freshness")
    if not isinstance(freshness, dict):
        return {}
    return freshness


def _print_promotion_artifact_freshness(freshness: dict) -> None:
    if not freshness:
        return

    status = str(freshness.get("status") or "UNKNOWN").strip() or "UNKNOWN"
    age_days = freshness.get("age_days")
    max_age_days = freshness.get("max_age_days")
    age_label = ""
    if age_days is not None and max_age_days is not None:
        age_label = f" (age={age_days}/{max_age_days} days)"
    elif age_days is not None:
        age_label = f" (age={age_days} days)"
    print(f"    Promotion artifact freshness: {status}{age_label}")

    warning = str(freshness.get("warning") or "").strip()
    if warning:
        print(f"    Promotion freshness warning: {warning}")

    status_upper = status.upper()
    if status_upper in {"STALE", "FUTURE", "UNKNOWN"}:
        print(
            "    Promotion freshness gate: "
            "BLOCKED until canonical artifact is refreshed"
        )

    should_print_commands = (
        status_upper in {"AGING", "STALE", "FUTURE", "UNKNOWN"} or bool(warning)
    )
    if should_print_commands:
        check_command = str(freshness.get("check_command") or "").strip()
        refresh_command = str(freshness.get("refresh_command") or "").strip()
        if check_command:
            print(f"    Promotion freshness check command: {check_command}")
        if refresh_command:
            print(f"    Promotion freshness refresh command: {refresh_command}")


def _target_weight_current_blockers_regenerate_command(
    payload: dict | None = None,
) -> str:
    command = "python tools/evaluate_and_promote.py --current-blockers"
    runbook = payload.get("operator_runbook") if isinstance(payload, dict) else None
    if isinstance(runbook, dict):
        commands = runbook.get("commands")
        if isinstance(commands, dict):
            command = str(
                commands.get("regenerate_current_blockers") or command
            ).strip()
    return command


def _target_weight_current_blockers_priority_issue(
    strategy: str,
    priority_action: dict,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> tuple[str | None, str]:
    if priority_action:
        return None, str(
            priority_action.get("regenerate_current_blockers_command")
            or _target_weight_current_blockers_regenerate_command()
        ).strip()

    path = Path(reports_dir) / "current_blockers.json"
    if not path.exists():
        return (
            "current_blockers.json missing",
            _target_weight_current_blockers_regenerate_command(),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (
            f"current_blockers.json unreadable: {exc.__class__.__name__}",
            _target_weight_current_blockers_regenerate_command(),
        )
    if not isinstance(payload, dict):
        return (
            "current_blockers.json invalid",
            _target_weight_current_blockers_regenerate_command(),
        )

    command = _target_weight_current_blockers_regenerate_command(payload)
    runbook = payload.get("operator_runbook")
    if isinstance(runbook, dict):
        primary = str(runbook.get("primary_strategy") or "").strip()
        if primary and primary != strategy:
            return (
                "primary_strategy mismatch "
                f"current_blockers={primary} status={strategy}",
                command,
            )

    return f"priority action missing for strategy={strategy}", command


def _path_leaf(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").split("/")[-1]


def _artifact_source_path(path: Path) -> str:
    return path.as_posix()


def _target_weight_priority_action_warnings(
    summary: dict,
    priority_action: dict,
) -> list[str]:
    warnings: list[str] = []
    if not priority_action:
        return warnings

    latest_status = str(summary.get("status") or "").strip()
    priority_status = str(priority_action.get("daily_ops_status") or "").strip()
    if priority_status and latest_status and priority_status != latest_status:
        warnings.append(
            f"daily_ops_status priority={priority_status} latest={latest_status}"
        )

    latest_trade_day = str(summary.get("trade_day") or "").strip()
    priority_trade_day = str(priority_action.get("daily_ops_trade_day") or "").strip()
    if (
        priority_trade_day
        and latest_trade_day
        and priority_trade_day != latest_trade_day
    ):
        warnings.append(
            f"daily_ops_trade_day priority={priority_trade_day} latest={latest_trade_day}"
        )

    latest_source = _path_leaf(summary.get("source_path"))
    priority_source = _path_leaf(priority_action.get("source_path"))
    if priority_source and latest_source and priority_source != latest_source:
        warnings.append(
            f"source_path priority={priority_source} latest={latest_source}"
        )
    return warnings


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


def _command_is_blocked(command: object) -> bool:
    return str(command or "").lstrip().startswith("# blocked:")


def _target_weight_operator_next_action(
    summary: dict,
    *,
    not_before_date: str | None,
    premature_run_guard: str | None,
    priority_command: str,
    priority_scheduled_command: str,
    enable_command: str,
    execute_command: str,
    next_daily_ops_command: str,
    next_readiness_command: str,
) -> str | None:
    status = str(summary.get("status") or "").strip()
    if _not_before_date_pending(not_before_date):
        guard = str(premature_run_guard or "not_before_date").strip()
        return (
            f"WAIT until {not_before_date}: {guard}; "
            "do not run the scheduled command early"
        )
    if priority_command and not _command_is_blocked(priority_command):
        return f"RUN current blockers priority command: {priority_command}"
    if priority_scheduled_command:
        return f"RUN no-order scheduled priority check: {priority_scheduled_command}"
    if status == "READY_TO_EXECUTE" and execute_command and not _command_is_blocked(execute_command):
        return f"RUN capped paper command from daily ops: {execute_command}"
    if (
        status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}
        and enable_command
        and not _command_is_blocked(enable_command)
    ):
        return f"RUN cap approval command from daily ops: {enable_command}"
    if next_daily_ops_command:
        return f"RUN no-order daily ops check: {next_daily_ops_command}"
    if next_readiness_command:
        return f"RUN no-order readiness check: {next_readiness_command}"
    return None


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


def _daily_ops_trade_day_sort_key(payload: dict, path: Path) -> tuple[str, float, float]:
    source_mtime = path.stat().st_mtime
    payload_with_source = {
        **payload,
        "source_path": _artifact_source_path(path),
        "source_mtime": source_mtime,
    }
    artifact_ts = _daily_ops_artifact_time_key(payload_with_source)[0]
    return (str(payload.get("trade_day") or ""), artifact_ts, source_mtime)


def _stable_daily_ops_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _daily_ops_summary_hash_is_valid(payload: dict) -> bool:
    summary_hash = str(payload.get("summary_hash") or "").strip()
    if not summary_hash:
        return False
    normalized = dict(payload)
    normalized.pop("summary_hash", None)
    return _stable_daily_ops_hash(normalized) == summary_hash


def _target_weight_enable_blocker(payload: dict, command: str | None = None) -> str | None:
    status = str(payload.get("status") or "")
    trade_day = str(payload.get("trade_day") or "").strip() or "UNKNOWN"
    next_trade_day = str(payload.get("next_operator_trade_day") or "").strip()
    next_hint = next_trade_day or "next KRX business day"
    if status == "PILOT_EVIDENCE_RECORDED":
        return (
            f"# blocked: pilot_paper evidence already recorded for {trade_day}; "
            f"rerun readiness audit for {next_hint}"
        )
    if status == "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE":
        return (
            f"# blocked: repaired pilot_paper evidence already recorded for {trade_day}; "
            f"rerun readiness audit for {next_hint}"
        )
    if status == "PILOT_EVIDENCE_INVALID":
        return (
            f"# blocked: pilot_paper evidence invalid for {trade_day}; "
            "finalize or repair evidence before changing pilot caps"
        )
    if status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}:
        return None
    if str(command or "").strip():
        return (
            f"# blocked: daily_ops_summary.status == {status}; "
            "READY_TO_ENABLE_CAPS 전 cap 변경 금지"
        )
    return None


def _ready_to_execute_trade_day_is_current(payload: dict) -> bool:
    trade_day = str(payload.get("trade_day") or "").strip()
    if not trade_day:
        return False
    try:
        trade_date = datetime.strptime(trade_day, "%Y-%m-%d").date()
        current = datetime.strptime(_current_kst_date(), "%Y-%m-%d").date()
    except ValueError:
        return False
    return trade_date == current


def _target_weight_command_scope_issues(
    payload: dict,
    command: str,
    *,
    require_trade_day: bool,
    required_flags: tuple[str, ...],
) -> list[str]:
    return target_weight_command_scope_issues(
        payload,
        command,
        require_trade_day=require_trade_day,
        required_flags=required_flags,
    )


def _sanitize_target_weight_daily_ops_summary(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    sanitized = dict(payload)
    operator_commands = dict(sanitized.get("operator_commands") or {})
    status = sanitized.get("status")
    enable_command = str(operator_commands.get("enable_suggested_caps") or "")
    enable_blocker = _target_weight_enable_blocker(sanitized, enable_command)
    if enable_blocker:
        operator_commands["enable_suggested_caps"] = enable_blocker
    elif status in {"READY_TO_ENABLE_CAPS", "WAITING_FOR_MARKET_SESSION"}:
        enable_issues = _target_weight_command_scope_issues(
            sanitized,
            enable_command,
            require_trade_day=False,
            required_flags=("--enable",),
        )
        if enable_issues:
            operator_commands["enable_suggested_caps"] = (
                "# blocked: daily_ops_enable_command_unavailable: "
                + "; ".join(enable_issues)
            )

    execute_command = str(operator_commands.get("execute_capped_paper") or "")
    if status == "READY_TO_EXECUTE" and not _ready_to_execute_trade_day_is_current(sanitized):
        operator_commands["execute_capped_paper"] = (
            "# blocked: daily_ops_summary.trade_day is stale; "
            "rerun daily ops summary for the current KRX business day"
        )
    elif status == "READY_TO_EXECUTE":
        execute_issues = _target_weight_command_scope_issues(
            sanitized,
            execute_command,
            require_trade_day=True,
            required_flags=("--execute", "--collect-evidence"),
        )
        if execute_issues:
            operator_commands["execute_capped_paper"] = (
                "# blocked: daily_ops_execute_command_unavailable: "
                + "; ".join(execute_issues)
            )
    sanitized["operator_commands"] = operator_commands
    return sanitized


def _print_target_weight_daily_ops_status(
    strategy: str,
    *,
    reports_dir: str | Path = REPORTS_DIR,
) -> None:
    summary = _load_latest_target_weight_daily_ops(strategy, reports_dir=reports_dir)
    latest_failure = _load_latest_target_weight_daily_ops_failure(
        strategy,
        reports_dir=reports_dir,
    )
    integrity_warnings = _target_weight_daily_ops_integrity_warnings(
        strategy,
        reports_dir=reports_dir,
    )
    promotion_freshness = _load_target_weight_promotion_artifact_freshness(
        reports_dir=reports_dir,
    )
    command = (
        "python tools/target_weight_rotation_pilot.py "
        f"--candidate-id {strategy} --daily-ops-summary"
    )
    if summary is None:
        status_label = (
            "FAILED"
            if latest_failure
            else ("INVALID" if integrity_warnings else "MISSING")
        )
        print(f"\n  Target-weight Daily Ops: {status_label}")
        _print_promotion_artifact_freshness(promotion_freshness)
        for warning in integrity_warnings[:3]:
            print(f"  Integrity warning: {warning}")
        if latest_failure:
            print(f"  Failure source: {latest_failure.get('source_path')}")
            print(
                "  Failure reason: "
                f"{_target_weight_daily_ops_failure_reason(latest_failure)}"
            )
            failure_error = _target_weight_daily_ops_failure_error(latest_failure)
            if failure_error:
                print(f"  Failure error: {failure_error}")
            command = _target_weight_daily_ops_failure_command(latest_failure, command)
        print(f"  Run: {command}")
        return

    progress = summary.get("evidence_progress") or {}
    decision = summary.get("decision") or {}
    diagnostics = decision.get("post_evidence_diagnostics") or []
    operator_commands = summary.get("operator_commands") or {}
    enable_command = operator_commands.get("enable_suggested_caps") or ""
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
    priority_action = _load_target_weight_current_blockers_priority_action(
        strategy,
        reports_dir=reports_dir,
    )
    not_before_date = (
        run_guard.get("not_before_date")
        or summary.get("not_before_date")
        or (
            next_operator_trade_day
            if (
                summary.get("status")
                in {
                    "PILOT_EVIDENCE_RECORDED",
                    "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
                }
                and _not_before_date_pending(next_operator_trade_day)
            )
            else None
        )
    )
    premature_run_guard = run_guard.get("premature_run_guard") or summary.get(
        "premature_run_guard"
    )
    if (
        not premature_run_guard
        and not_before_date
        and summary.get("status")
        in {
            "PILOT_EVIDENCE_RECORDED",
            "PILOT_EVIDENCE_REPAIRED_NON_PROMOTABLE",
        }
    ):
        premature_run_guard = "target_weight_future_as_of_date_blocked"
    print("\n  Target-weight Daily Ops:")
    _print_promotion_artifact_freshness(promotion_freshness)
    for warning in integrity_warnings[:3]:
        print(f"    Integrity warning: {warning}")
    if _daily_ops_failure_is_newer_than_summary(latest_failure, summary):
        print("    Failure warning: latest daily ops failure is newer than loaded summary")
        print(f"    Failure source: {latest_failure.get('source_path')}")
        print(
            "    Failure reason: "
            f"{_target_weight_daily_ops_failure_reason(latest_failure)}"
        )
        print(
            "    Failure recovery command: "
            f"{_target_weight_daily_ops_failure_command(latest_failure, command)}"
        )
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
    priority_issue, regenerate_command = _target_weight_current_blockers_priority_issue(
        strategy,
        priority_action,
        reports_dir=reports_dir,
    )
    if priority_issue:
        print(f"    Current blockers warning: {priority_issue}")
        print(f"    Regenerate current blockers command: {regenerate_command}")
    priority_warnings = _target_weight_priority_action_warnings(
        summary,
        priority_action,
    )
    if priority_warnings:
        print(
            "    Current blockers warning: stale priority action; "
            + "; ".join(priority_warnings)
        )
        regenerate_command = str(
            priority_action.get("regenerate_current_blockers_command")
            or "python tools/evaluate_and_promote.py --current-blockers"
        ).strip()
        print(f"    Regenerate current blockers command: {regenerate_command}")
    priority_desc = str(priority_action.get("desc") or "").strip()
    priority_command = str(priority_action.get("command") or "").strip()
    priority_scheduled_command = str(
        priority_action.get("scheduled_command") or ""
    ).strip()
    priority_follow_up = str(
        priority_action.get("scheduled_follow_up") or priority_action.get("follow_up") or ""
    ).strip()
    if priority_desc or priority_command or priority_scheduled_command:
        print(f"    Current blockers priority: {priority_desc or 'N/A'}")
        priority_evidence = []
        if "target_days" in priority_action:
            priority_evidence.append(
                "verified="
                f"{priority_action.get('verified_pilot_days', 0)}/{priority_action.get('target_days')}"
            )
        elif "verified_pilot_days" in priority_action:
            priority_evidence.append(
                f"verified={priority_action.get('verified_pilot_days', 0)}"
            )
        for key, label in (
            ("remaining_pilot_days", "remaining"),
            ("shadow_days", "shadow"),
            ("repaired_pilot_days", "repaired"),
            ("invalid_execution_days", "invalid"),
        ):
            if key in priority_action:
                priority_evidence.append(f"{label}={priority_action.get(key, 0)}")
        if priority_evidence:
            print(f"    Priority evidence: {' '.join(priority_evidence)}")
        if priority_command:
            print(f"    Priority command: {priority_command}")
        if priority_scheduled_command and priority_scheduled_command != priority_command:
            print(f"    Scheduled priority command: {priority_scheduled_command}")
        if priority_follow_up and priority_follow_up not in {
            priority_command,
            priority_scheduled_command,
        }:
            print(f"    Priority follow-up: {priority_follow_up}")
    operator_next_action = _target_weight_operator_next_action(
        summary,
        not_before_date=not_before_date,
        premature_run_guard=premature_run_guard,
        priority_command=priority_command,
        priority_scheduled_command=priority_scheduled_command,
        enable_command=str(enable_command),
        execute_command=str(execute_command),
        next_daily_ops_command=str(next_daily_ops_command),
        next_readiness_command=str(next_readiness_command),
    )
    if operator_next_action:
        print(f"    Operator next action: {operator_next_action}")
    if diagnostics:
        print(f"    Post-evidence diagnostics: {len(diagnostics)}")
    if enable_command:
        if str(enable_command).lstrip().startswith("# blocked:"):
            print("    Cap approval: BLOCKED by daily ops")
        else:
            print("    Cap approval: follow daily ops READY_TO_ENABLE_CAPS command only")
        print(f"    Enable cap command: {enable_command}")
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
