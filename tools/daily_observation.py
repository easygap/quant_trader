"""Run paper close valuation and research collection, without order execution."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.daily_observation import read_daily_observation, run_daily_observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Read saved status without collection or DB access")
    args = parser.parse_args()
    report = read_daily_observation() if args.status else run_daily_observation()
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] in {"completed", "awaiting_close", "busy"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
