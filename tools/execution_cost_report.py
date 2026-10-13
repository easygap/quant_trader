"""Read execution-cost observations without creating or migrating a database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.execution_analytics import execution_cost_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="SQLite ledger to inspect in read-only mode")
    parser.add_argument("--account-key")
    parser.add_argument("--mode", choices=["paper", "live"])
    parser.add_argument("--since", help="ISO date or timestamp, interpreted in KST if naive")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--out", type=Path, help="Optional JSON report; default: stdout only")
    args = parser.parse_args()
    report = execution_cost_report(db_path=args.db, account_key=args.account_key,
                                   mode=args.mode, since=args.since, limit=args.limit)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
