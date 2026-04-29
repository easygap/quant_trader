#!/usr/bin/env python3
"""
Pilot Launch Readiness CLI

Usage:
    python tools/paper_launch_readiness.py --strategy scoring
    python tools/paper_launch_readiness.py --all
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Pilot Launch Readiness Check")
    parser.add_argument("--strategy", help="전략 이름")
    parser.add_argument("--all", action="store_true", help="전체 paper 전략")
    parser.add_argument("--generate-runbook", action="store_true",
                        help="runbook markdown도 생성")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    if args.all:
        from core.strategy_universe import get_paper_strategy_names
        strategies = get_paper_strategy_names()
    elif args.strategy:
        strategies = [args.strategy]
    else:
        parser.print_help()
        sys.exit(1)

    for s in strategies:
        _run_one(s, args.generate_runbook)


def _run_one(strategy: str, gen_runbook: bool):
    from core.paper_pilot import (
        compute_launch_readiness,
        generate_launch_readiness_artifact,
        generate_pilot_runbook,
    )

    lr = compute_launch_readiness(strategy)
    json_path, md_path = generate_launch_readiness_artifact(strategy)

    icon = "\u2705" if lr["launch_ready"] else "\u274c"
    infra = "\u2705" if lr["infra_ready"] else "\u23f3"

    print(f"\n{'=' * 60}")
    print(f"  {icon}  Launch Readiness: {strategy}")
    print(f"  Launch Ready: {'YES' if lr['launch_ready'] else 'NO'}")
    print(f"  {infra}  Infrastructure Ready: {'YES' if lr['infra_ready'] else 'NO'}")
    print(f"{'=' * 60}")

    print(f"\n  Runtime State: {lr['runtime_state']}")
    print(f"  Real Paper Days: {lr['real_paper_days']}")
    print(f"  Shadow Days: {lr['shadow_days']}")
    print(f"  Eligible / Quarantined: {lr['eligible_records']} / {lr['quarantined_records']}")

    print(f"\n  --- Checklist ---")
    _check("Clean final days",
           f"{lr['clean_final_days_current']}/{lr['clean_final_days_required']}",
           lr["clean_final_days_current"] >= lr["clean_final_days_required"])
    _check("Evidence fresh",
           lr["evidence_date"] or "N/A",
           lr["evidence_fresh"])
    bfr = f"{lr['benchmark_final_ratio']:.0%}" if lr['benchmark_final_ratio'] is not None else "N/A"
    _check("Benchmark final ratio", bfr, lr["benchmark_ready"])
    _check("Discord notifier",
           "configured" if lr["notifier_ready"] else "MISSING",
           lr["notifier_ready"])
    _check("Pilot authorization",
           "present" if lr["pilot_authorization_present"] else "absent",
           lr["pilot_authorization_present"])
    _check("Strategy eligible", strategy, lr["strategy_eligible"])

    if lr["blocking_requirements"]:
        print(f"\n  --- Blocking Requirements ---")
        for b in lr["blocking_requirements"]:
            print(f"    \u274c {b}")

    print(f"\n  Artifacts: {json_path}")
    print(f"             {md_path}")

    if gen_runbook:
        rb = generate_pilot_runbook(strategy)
        print(f"    Runbook: {rb}")

    print()


def _check(label, value, ok):
    icon = "\u2705" if ok else "\u274c"
    print(f"    {icon} {label}: {value}")


if __name__ == "__main__":
    main()
