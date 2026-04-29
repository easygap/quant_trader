#!/usr/bin/env python3
"""
Paper Shadow Bootstrap CLI

entry submit 없이 evidence만 수집하는 shadow mode.
blocked_insufficient_evidence / no-evidence 전략이 증거를 축적하여
bootstrap paradox를 해소한다.

Usage:
    # 단일 일자 shadow collect
    python tools/paper_bootstrap.py --strategy rotation --mode shadow --date 2026-04-06

    # 기간 shadow collect
    python tools/paper_bootstrap.py --strategy scoring --mode shadow --from 2026-04-01 --to 2026-04-06

실주문 제출: 0회. signal/benchmark/evidence/anomaly/weekly/promotion만 수집.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(description="Paper Shadow Bootstrap")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--mode", choices=["shadow"], default="shadow",
                        help="bootstrap mode (현재 shadow만 지원)")
    parser.add_argument("--date", help="YYYY-MM-DD (단일 일자)")
    parser.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    from core.paper_runtime import is_paper_trade_allowed

    from core.strategy_universe import is_paper_eligible
    if not is_paper_eligible(args.strategy):
        print("ERROR: %s is not a paper-eligible strategy (disabled/backtest-only)" % args.strategy)
        sys.exit(1)

    if not is_paper_trade_allowed(args.strategy, "shadow_collect"):
        print("ERROR: shadow_collect not allowed for %s" % args.strategy)
        sys.exit(1)

    if args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d")]
    elif args.from_date and args.to_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d")
        end = datetime.strptime(args.to_date, "%Y-%m-%d")
        dates = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
    else:
        dates = [datetime.now()]

    run_shadow_bootstrap(args.strategy, dates)


def run_shadow_bootstrap(strategy: str, dates: list):
    """shadow mode: evidence collect + finalize. 주문 제출 없음."""
    from core.paper_evidence import (
        collect_daily_evidence, finalize_daily_evidence,
        generate_weekly_summary, get_canonical_records,
    )

    print("\n=== Shadow Bootstrap: %s ===" % strategy)
    print("  Mode: shadow (no order submit)")
    print("  Dates: %d business days" % len(dates))

    watchlist = _get_watchlist()
    stats = {"collected": 0, "finalized": 0, "skipped": 0, "order_submits": 0}

    for dt in dates:
        date_pm = dt.replace(hour=15, minute=35)
        # collect (idempotent, shadow mode)
        r = collect_daily_evidence(
            strategy=strategy, mode="paper", account_key=strategy,
            date=date_pm, watchlist_symbols=watchlist,
            evidence_mode="shadow_bootstrap",
        )
        if r:
            stats["collected"] += 1
            print("  [COLLECT] %s bench=%s excess=%s" % (
                r.date, r.benchmark_status,
                r.same_universe_excess if r.same_universe_excess is not None else "null",
            ))
        else:
            stats["skipped"] += 1

        # finalize previous day
        if len(dates) > 1:
            prev = date_pm - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            f = finalize_daily_evidence(
                strategy=strategy, mode="paper", account_key=strategy,
                date=prev, watchlist_symbols=watchlist,
            )
            if f and f.record_version > 1:
                stats["finalized"] += 1

    # summary
    records = get_canonical_records(strategy)
    print("\n=== Shadow Bootstrap Summary ===")
    print("  Collected: %d" % stats["collected"])
    print("  Finalized: %d" % stats["finalized"])
    print("  Skipped: %d" % stats["skipped"])
    print("  Order Submits: %d (shadow mode)" % stats["order_submits"])
    print("  Total Canonical Records: %d" % len(records))

    # weekly summary if enough data
    if records:
        last_date = records[-1]["date"]
        ws = generate_weekly_summary(strategy, week_end_date=last_date)
        if ws:
            print("  Weekly Summary: %s" % ws)

    return stats


def _get_watchlist():
    try:
        from config.config_loader import Config
        from core.watchlist_manager import WatchlistManager
        return WatchlistManager(Config.get()).resolve()
    except Exception as e:
        print("WARNING: watchlist fail: %s" % e)
        return []


if __name__ == "__main__":
    main()
