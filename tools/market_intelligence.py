"""Collect actual public disclosure/flow evidence; never opens the operating DB."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.market_intelligence import collect_market_intelligence, load_intelligence_config, save_market_intelligence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    report = collect_market_intelligence(load_intelligence_config(args.config))
    paths = save_market_intelligence(report, args.output_dir)
    print(json.dumps({"status": report["status"], "generated_at": report["generated_at"], "expected_session": report["expected_session"],
                      "disclosures": report["disclosures"]["coverage"], "investor_flows": report["investor_flows"]["coverage"], **paths}, ensure_ascii=False))
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
