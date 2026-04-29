#!/usr/bin/env python3
"""
Paper Preflight CLI

Usage:
    python tools/paper_preflight.py --strategy scoring --date 2026-04-06
    python tools/paper_preflight.py --strategy rotation --date 2026-04-06 --send-test-notification
    python tools/paper_preflight.py --all --date 2026-04-06
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Paper Preflight Check")
    parser.add_argument("--strategy", help="전략 이름")
    parser.add_argument("--all", action="store_true", help="전체 전략")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--with-pilot-check", action="store_true",
                        help="pilot authorization + prerequisites 상세 표시")
    parser.add_argument("--send-test-notification", action="store_true",
                        help="Discord test notification 실제 발송")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    from core.paper_preflight import run_preflight, _save_session_bootstrap
    from datetime import datetime

    date = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.all:
        strategies = _discover_strategies()
        results = []
        for s in strategies:
            r = run_preflight(s, date, args.send_test_notification)
            results.append(r)
            _print_result(r, with_pilot=args.with_pilot_check)
        if results:
            path = _save_session_bootstrap(date, results)
            print(f"\nSession bootstrap: {path}")
    elif args.strategy:
        r = run_preflight(args.strategy, date, args.send_test_notification)
        _print_result(r, with_pilot=args.with_pilot_check or True)
    else:
        parser.print_help()
        sys.exit(1)


def _discover_strategies():
    """Canonical strategy universe: STRATEGY_STATUS에서 paper 허용만."""
    from core.strategy_universe import get_paper_strategy_names
    return get_paper_strategy_names()


def _print_result(r, with_pilot=False):
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(r.overall, "❓")

    # entry가 pilot으로만 허용되는지 판단
    entry_mode = "BLOCKED"
    if r.entry_allowed:
        if r.pilot_authorized and r.runtime_state == "blocked_insufficient_evidence":
            entry_mode = "PILOT ONLY"
        else:
            entry_mode = "NORMAL"

    print(f"\n{'=' * 60}")
    print(f"  {icon}  Preflight: {r.strategy} ({r.date})")
    print(f"  Overall: {r.overall.upper()}")
    print(f"  Entry Allowed: {entry_mode}")
    print(f"  Runtime State: {r.runtime_state}")
    print(f"{'=' * 60}")

    # Registry status
    try:
        from strategies import STRATEGY_STATUS
        reg = STRATEGY_STATUS.get(r.strategy, {})
        print(f"\n  Registry Status: {reg.get('status', 'unknown')}")
    except Exception:
        pass

    print(f"\n  Evidence Date: {r.evidence_date or 'N/A'}")
    print(f"  Freshness: {r.evidence_freshness}")
    print(f"  Eligible/Quarantined: {r.eligible_records}/{r.quarantined_records}")
    print(f"  Has Real Evidence: {'YES' if r.has_real_evidence else 'NO'}")
    print(f"  Real Paper Days: {r.real_paper_days}")
    print(f"  Shadow Days: {r.shadow_days}")
    print(f"  Benchmark Final Ratio: {r.benchmark_final_ratio}")
    print(f"  Excess Non-Null Ratio: {r.excess_non_null_ratio}")
    print(f"  Notifier: {r.notifier_health}")
    print(f"  Positions/Pending: {r.open_positions}/{r.pending_orders}")

    # Pilot section
    if with_pilot:
        print(f"\n  --- Pilot Authorization ---")
        print(f"  Pilot Authorized: {'YES' if r.pilot_authorized else 'NO'}")
        if r.pilot_authorized:
            print(f"  Remaining Orders: {r.pilot_remaining_orders}")
            exposure_str = f"{r.pilot_remaining_exposure:,}" if r.pilot_remaining_exposure is not None else "N/A"
            print(f"  Remaining Exposure: {exposure_str}")

        # prerequisites for rotation-like strategies
        try:
            from core.paper_pilot import check_pilot_prerequisites, get_active_pilot
            prereq_ok, prereq_reason = check_pilot_prerequisites(r.strategy)
            print(f"  Shadow Prerequisites: {'MET' if prereq_ok else 'NOT MET'}")
            if not prereq_ok:
                print(f"    → {prereq_reason}")
            auth = get_active_pilot(r.strategy)
            if auth:
                print(f"  Active Pilot: {auth.valid_from} ~ {auth.valid_to}")
                print(f"    max_orders/day: {auth.max_orders_per_day}")
                print(f"    max_positions: {auth.max_concurrent_positions}")
                print(f"    max_notional/trade: {auth.max_notional_per_trade:,}")
                print(f"    max_exposure: {auth.max_gross_exposure:,}")
                print(f"    reason: {auth.operator_reason}")
            else:
                print(f"  Active Pilot: NONE")
        except Exception as e:
            print(f"  Pilot check error: {e}")

        # why entry allowed/blocked
        if entry_mode == "PILOT ONLY":
            print(f"\n  WHY ALLOWED: runtime blocked ({r.runtime_state}), pilot override active")
        elif entry_mode == "NORMAL":
            print(f"\n  WHY ALLOWED: runtime state=normal, no pilot needed")
        else:
            reasons = r.block_reasons or ["unknown"]
            print(f"\n  WHY BLOCKED: {'; '.join(reasons)}")
            if not r.pilot_authorized:
                print(f"    → pilot authorization 없음 또는 조건 미충족")

    print("\n  Checks:")
    for c in r.checks:
        s_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(c["status"], "?")
        print(f"    {s_icon} {c['name']}: {c['detail']}")

    if r.block_reasons:
        print("\n  Block Reasons:")
        for br in r.block_reasons:
            print(f"    - {br}")

    if r.operator_actions:
        print("\n  Operator Actions:")
        for a in r.operator_actions:
            print(f"    - {a}")

    # Launch readiness summary
    lr_icon = "\u2705" if r.launch_ready else "\u274c"
    infra_icon = "\u2705" if r.infra_ready else "\u23f3"
    print(f"\n  --- Launch Readiness ---")
    print(f"  {lr_icon} Launch Ready: {'YES' if r.launch_ready else 'NO'}")
    print(f"  {infra_icon} Infra Ready: {'YES' if r.infra_ready else 'NO'}")
    print(f"  Clean Final Days: {r.clean_final_days} (remaining: {r.remaining_clean_days})")
    if r.blocking_requirements:
        for b in r.blocking_requirements:
            print(f"    \u274c {b}")

    if r.quarantined_records > 0:
        print(f"\n  Legacy: {r.quarantined_records} v1 record(s) quarantined (runtime/promotion에 미반영)")

    print()


if __name__ == "__main__":
    main()
