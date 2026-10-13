"""Publish the market observation radar; no order or operating DB path is used."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from core.market_research import (build_market_research, collect_market_research, load_research_config,
                                  read_market_research, research_directory, save_market_research)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--output-dir", default=str(research_directory()))
    parser.add_argument("--prices-dir", help="Offline CSV directory: SYMBOL.csv, with date/close[/volume] columns")
    parser.add_argument("--csv-price-basis", choices=("unknown", "adjusted", "unadjusted"), default="unknown")
    parser.add_argument("--now", help="Reproducible observation time; naive timestamps use Asia/Seoul")
    args = parser.parse_args()
    if args.now and not args.prices_dir:
        parser.error("--now is only permitted with --prices-dir for an explicitly marked offline replay")
    config = load_research_config(args.config)
    previous = read_market_research(Path(args.output_dir) / "latest.json", now=args.now)
    if args.prices_dir:
        prices, sources = {}, {}
        for spec in [config["benchmark"]] + config.get("universe", []):
            symbol = spec["symbol"]
            path = Path(args.prices_dir) / f"{symbol}.csv"
            if path.exists():
                prices[symbol] = pd.read_csv(path)
                sources[symbol] = {"name": "offline_csv", "adjusted": {"unknown": None, "adjusted": True, "unadjusted": False}[args.csv_price_basis]}
        report = build_market_research(config, prices, sources=sources, generated_at=args.now, previous=previous)
    else:
        report = collect_market_research(config, now=args.now, previous=previous)
    paths = save_market_research(report, args.output_dir)
    print(json.dumps({"status": report["status"], "as_of": report["as_of"], "coverage": report["coverage"], **paths}, ensure_ascii=False))


if __name__ == "__main__":
    main()
