"""Run observational collection once; daily runtime hooks use the same durable lock."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.research_operations import read_research_operations, run_research_operations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Collect even if already attempted today")
    parser.add_argument("--status", action="store_true", help="Read saved status without collection")
    parser.add_argument("--trigger", default="manual")
    args = parser.parse_args()
    report = read_research_operations() if args.status else run_research_operations(force=args.force, trigger=args.trigger)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if report["status"] in {"attention", "failed", "unavailable", "interrupted", "stale", "invalid"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
