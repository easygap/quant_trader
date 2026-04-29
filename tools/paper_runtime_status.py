#!/usr/bin/env python3
"""
Paper Runtime Status CLI

Usage:
    # 단일 전략 상태 조회
    python tools/paper_runtime_status.py --strategy scoring

    # 전체 전략 상태 조회
    python tools/paper_runtime_status.py --all

    # 수동 freeze
    python tools/paper_runtime_status.py --strategy scoring --freeze --reason "investigating anomaly"

    # 수동 unfreeze
    python tools/paper_runtime_status.py --strategy scoring --unfreeze --reason "anomaly resolved"

    # audit trail 출력
    python tools/paper_runtime_status.py --strategy scoring --audit

Note:
    canonical promotion bundle / live eligibility는 절대 수정하지 않습니다.
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Paper Runtime Status")
    parser.add_argument("--strategy", help="전략 이름")
    parser.add_argument("--all", action="store_true", help="전체 전략 상태")
    parser.add_argument("--freeze", action="store_true", help="수동 freeze")
    parser.add_argument("--unfreeze", action="store_true", help="수동 unfreeze")
    parser.add_argument("--reason", help="freeze/unfreeze 이유")
    parser.add_argument("--audit", action="store_true", help="audit trail 출력")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    if args.freeze and args.strategy:
        run_freeze(args.strategy, args.reason or "manual freeze via CLI")
    elif args.unfreeze and args.strategy:
        run_unfreeze(args.strategy, args.reason or "manual unfreeze via CLI")
    elif args.audit and args.strategy:
        run_audit(args.strategy)
    elif args.all:
        run_all()
    elif args.strategy:
        run_single(args.strategy)
    else:
        parser.print_help()
        sys.exit(1)


def _print_state(state):
    """RuntimeState를 콘솔에 출력."""
    from core.paper_runtime import ALLOWED_ACTIONS

    state_icon = {
        "research_disabled": "⛔",
        "normal": "✅",
        "degraded": "⚠️",
        "frozen": "🔒",
        "blocked_insufficient_evidence": "📊",
    }.get(state.state, "❓")

    print(f"\n{'=' * 60}")
    print(f"  {state_icon}  Strategy: {state.strategy}")
    print(f"  State: {state.state.upper()}")
    print(f"  Evaluated: {state.evaluated_at[:19]}")
    print(f"{'=' * 60}")

    if state.reasons:
        print("\n  Block/Freeze Reasons:")
        for r in state.reasons:
            print(f"    - {r}")

    print(f"\n  Evidence Date: {state.evidence_date or 'N/A'}")
    print(f"  Last Final Benchmark: {state.last_final_benchmark_date or 'N/A'}")

    if state.manual_freeze:
        print(f"  Manual Freeze: YES — {state.manual_freeze_reason}")

    m = state.metrics
    if m:
        print(f"\n  Metrics:")
        print(f"    Total Days: {m.get('total_days', 'N/A')}")
        print(f"    Excess Non-Null Ratio: {m.get('excess_non_null_ratio', 'N/A')}")
        print(f"    Recent Final Ratio: {m.get('recent_final_ratio', 'N/A')}")
        print(f"    Recent Anomaly Count: {m.get('recent_anomaly_count', 'N/A')}")

    if state.last_anomalies:
        print(f"\n  Recent Anomalies:")
        for a in state.last_anomalies:
            print(f"    [{a.get('date', '')}] {a.get('severity', '')} {a.get('type', '')}: {a.get('detail', '')}")

    print(f"\n  Eligible Actions: {', '.join(state.allowed_actions) or 'NONE'}")
    print()


def run_single(strategy: str):
    from core.paper_runtime import get_paper_runtime_state
    state = get_paper_runtime_state(strategy)
    _print_state(state)


def run_all():
    from core.paper_runtime import get_paper_runtime_state
    from core.strategy_universe import get_paper_strategy_names

    strategies = get_paper_strategy_names()

    if not strategies:
        print("등록된 paper 전략 없음")
        return

    for strategy in sorted(strategies):
        state = get_paper_runtime_state(strategy)
        _print_state(state)


def run_freeze(strategy: str, reason: str):
    from core.paper_runtime import manual_freeze
    state = manual_freeze(strategy, reason)
    print(f"FROZEN: {strategy} — {reason}")
    _print_state(state)


def run_unfreeze(strategy: str, reason: str):
    from core.paper_runtime import manual_unfreeze
    state = manual_unfreeze(strategy, reason)
    print(f"UNFROZEN: {strategy} — {reason}")
    _print_state(state)


def run_audit(strategy: str):
    from core.paper_runtime import generate_runtime_audit
    path = generate_runtime_audit(strategy)
    if path:
        print(f"Audit report: {path}")
        print(path.read_text(encoding="utf-8"))
    else:
        print(f"No audit history for {strategy}")


if __name__ == "__main__":
    main()
