#!/usr/bin/env python3
"""
Paper Runtime Rebuild CLI

기존 evidence JSONL을 날짜별로 순회하며 runtime state를 재계산한다.
legacy v1 record를 식별하고 quarantine하여 정규화 전/후 비교를 보여준다.

Usage:
    python tools/rebuild_paper_runtime.py --strategy scoring
    python tools/rebuild_paper_runtime.py --strategy scoring --from 2026-04-01 --to 2026-04-06
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Paper Runtime Rebuild")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    from core.paper_runtime import (
        rebuild_runtime_history, generate_rebuild_report,
        get_paper_runtime_state, filter_runtime_eligible,
        classify_evidence_schema, CURRENT_SCHEMA_VERSION,
    )
    from core.paper_evidence import get_canonical_records

    # ── Rebuild ──
    history = rebuild_runtime_history(args.strategy, args.from_date, args.to_date)
    if not history:
        print("No evidence records found for %s" % args.strategy)
        sys.exit(0)

    # ── Print table ──
    print("\n=== Runtime Rebuild: %s ===" % args.strategy)
    print("%-12s %-8s %-7s %-12s %-10s %-8s %-8s %-30s %s" % (
        "Date", "Schema", "Legacy", "BenchStatus", "Excess", "Eligible", "Final%", "State", "Reason"))
    print("-" * 120)
    for h in history:
        ex = "%.4f" % h["same_universe_excess"] if h["same_universe_excess"] is not None else "null"
        print("%-12s v%-7d %-7s %-12s %-10s %-8d %-8s %-30s %s" % (
            h["date"], h["schema_version"],
            "Y" if h["is_legacy"] else "N",
            h["benchmark_status"], ex,
            h["eligible_so_far"],
            "%.0f%%" % (h["final_ratio_so_far"] * 100),
            h["runtime_state"], h["reason"],
        ))

    # ── Legacy vs normalized comparison ──
    all_records = get_canonical_records(args.strategy)
    eligible, quarantined = filter_runtime_eligible(all_records)

    print("\n=== Legacy Normalization Summary ===")
    print("  Total records: %d" % len(all_records))
    print("  Eligible (v%d+): %d" % (CURRENT_SCHEMA_VERSION, len(eligible)))
    print("  Quarantined (legacy): %d" % len(quarantined))
    for q in quarantined:
        print("    [v%d] %s — %s" % (
            classify_evidence_schema(q), q.get("date", "?"),
            "normalized" if q.get("_legacy_normalized") else "raw",
        ))

    # ── Current state (legacy 포함 vs 제외) ──
    state = get_paper_runtime_state(args.strategy)
    print("\n=== Current Runtime State (legacy excluded) ===")
    print("  State: %s" % state.state)
    print("  Eligible records: %d" % state.metrics.get("eligible_records", 0))
    print("  Quarantined: %d" % state.metrics.get("quarantined_records", 0))
    print("  Excess non-null ratio: %s" % state.metrics.get("excess_non_null_ratio", "N/A"))
    print("  Recent final ratio: %s" % state.metrics.get("recent_final_ratio", "N/A"))
    print("  Reasons: %s" % ("; ".join(state.reasons) or "none"))

    # ── Generate report ──
    report_path = generate_rebuild_report(args.strategy, history)
    print("\n  Report: %s" % report_path)


if __name__ == "__main__":
    main()
