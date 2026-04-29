#!/usr/bin/env python3
"""
Paper Evidence Pipeline CLI

Usage:
    # 단일 일자 evidence 수집
    python tools/run_paper_evidence_pipeline.py --strategy scoring --date 2026-04-02

    # provisional → final benchmark 승격
    python tools/run_paper_evidence_pipeline.py --strategy scoring --finalize --date 2026-04-02

    # 최근 N영업일 backfill + discrepancy report
    python tools/run_paper_evidence_pipeline.py --strategy scoring --backfill 20

    # 주간 요약 생성
    python tools/run_paper_evidence_pipeline.py --strategy scoring --weekly-summary --date 2026-04-02

    # 60일 승격 패키지 생성
    python tools/run_paper_evidence_pipeline.py --strategy scoring --generate-package

Note:
    - approved_strategies.json은 절대 수정하지 않습니다.
    - promotion package는 recommendation만 제공합니다.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main():
    parser = argparse.ArgumentParser(
        description="Paper Evidence Pipeline"
    )
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--finalize", action="store_true", help="provisional→final benchmark 승격")
    parser.add_argument("--backfill", type=int, metavar="N", help="최근 N영업일 backfill + discrepancy report")
    parser.add_argument("--weekly-summary", action="store_true")
    parser.add_argument("--generate-package", action="store_true")
    parser.add_argument("--quality-report", action="store_true", help="evidence 품질 report 생성")
    args = parser.parse_args()

    from database.models import init_database
    init_database()

    ran = False

    if args.backfill:
        run_backfill(args.strategy, args.backfill)
        ran = True

    if args.date and args.finalize:
        run_finalize(args.strategy, args.date)
        ran = True
    elif args.date:
        run_single_day(args.strategy, args.date)
        ran = True

    if args.weekly_summary:
        run_weekly_summary(args.strategy, args.date)
        ran = True

    if args.generate_package:
        run_promotion_package(args.strategy)
        ran = True

    if getattr(args, 'quality_report', False):
        run_quality_report(args.strategy)
        ran = True

    if not ran:
        parser.print_help()
        sys.exit(1)


def _get_watchlist():
    try:
        from config.config_loader import Config
        from core.watchlist_manager import WatchlistManager
        return WatchlistManager(Config.get()).resolve()
    except Exception as e:
        print("WARNING: watchlist fail, empty: %s" % e)
        return []


def run_single_day(strategy: str, date_str: str):
    from core.paper_evidence import collect_daily_evidence

    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print("ERROR: bad date format: %s" % date_str)
        sys.exit(1)

    result = collect_daily_evidence(
        strategy=strategy, mode="paper", account_key=strategy,
        date=date, watchlist_symbols=_get_watchlist(),
    )
    if result is None:
        print("SKIP: %s already recorded" % date_str)
    else:
        print("OK: %s day=%d bench=%s status=%s" % (date_str, result.day_number, result.benchmark_status, result.status))
        for a in result.anomalies:
            print("  ANOMALY [%s] %s: %s" % (a["severity"], a["type"], a.get("detail", "")))
        if result.cross_validation_warnings:
            for w in result.cross_validation_warnings:
                print("  XV-WARN: %s" % w)


def run_finalize(strategy: str, date_str: str):
    from core.paper_evidence import finalize_daily_evidence

    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print("ERROR: bad date format: %s" % date_str)
        sys.exit(1)

    result = finalize_daily_evidence(
        strategy=strategy, mode="paper", account_key=strategy,
        date=date, watchlist_symbols=_get_watchlist(),
    )
    if result is None:
        print("SKIP: %s already final or no record" % date_str)
    else:
        print("OK: finalized %s bench=%s v%d" % (date_str, result.benchmark_status, result.record_version))


def run_backfill(strategy: str, n_days: int):
    """최근 N영업일 backfill + finalize + discrepancy report."""
    from core.paper_evidence import collect_daily_evidence, finalize_daily_evidence, get_canonical_records

    watchlist = _get_watchlist()
    today = datetime.now()
    dates = []
    d = today
    while len(dates) < n_days:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # skip weekends
            dates.append(d)

    dates.reverse()
    stats = {"total": 0, "new": 0, "finalized": 0, "skipped": 0, "anomalies": 0, "xv_warnings": 0}

    for date in dates:
        stats["total"] += 1
        # collect (idempotent)
        r = collect_daily_evidence(
            strategy=strategy, mode="paper", account_key=strategy,
            date=date, watchlist_symbols=watchlist,
        )
        if r:
            stats["new"] += 1
            stats["anomalies"] += len(r.anomalies)
            stats["xv_warnings"] += len(r.cross_validation_warnings)
        # try finalize
        f = finalize_daily_evidence(
            strategy=strategy, mode="paper", account_key=strategy,
            date=date, watchlist_symbols=watchlist,
        )
        if f and f.record_version > 1:
            stats["finalized"] += 1
        if r is None and f is None:
            stats["skipped"] += 1

    # generate discrepancy report
    records = get_canonical_records(strategy)
    bench_final = sum(1 for r in records if r.get("benchmark_status") == "final")
    bench_prov = sum(1 for r in records if r.get("benchmark_status") == "provisional")
    bench_fail = sum(1 for r in records if r.get("benchmark_status") == "failed")
    total_rec = len(records)

    print("\n=== Backfill Summary: %s (last %d bdays) ===" % (strategy, n_days))
    print("  Processed: %d days" % stats["total"])
    print("  New records: %d" % stats["new"])
    print("  Finalized: %d" % stats["finalized"])
    print("  Skipped (existing): %d" % stats["skipped"])
    print("  Anomalies: %d" % stats["anomalies"])
    print("  Cross-validation warnings: %d" % stats["xv_warnings"])
    print("\n  Total canonical records: %d" % total_rec)
    print("  Benchmark: final=%d, provisional=%d, failed=%d" % (bench_final, bench_prov, bench_fail))
    if total_rec > 0:
        print("  Benchmark final ratio: %.1f%%" % (bench_final / total_rec * 100))

    # restart recovery count
    recovery_days = sum(1 for r in records if r.get("restart_recovery_count", 0) > 0)
    print("  Days with restart recovery: %d" % recovery_days)


def run_weekly_summary(strategy: str, date_str):
    from core.paper_evidence import generate_weekly_summary
    path = generate_weekly_summary(strategy, week_end_date=date_str)
    if path:
        print("OK: weekly summary -> %s" % path)
    else:
        print("SKIP: no evidence data")


def run_promotion_package(strategy: str):
    from core.paper_evidence import generate_promotion_package
    pkg_path, cl_path = generate_promotion_package(strategy)
    if pkg_path:
        print("OK: promotion evidence -> %s" % pkg_path)
        print("OK: approval checklist -> %s" % cl_path)
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        print("\n  Recommendation: %s" % pkg["recommendation"])
        print("  Period: %s" % pkg["period"])
        print("  Days: %d" % pkg["total_days"])
        print("  Benchmark: final=%d prov=%d fail=%d (%.0f%%)" % (
            pkg.get("benchmark_final_days", 0),
            pkg.get("benchmark_provisional_days", 0),
            pkg.get("benchmark_failed_days", 0),
            pkg.get("benchmark_final_ratio", 0) * 100,
        ))
        if pkg.get("block_reasons"):
            print("  Block reasons: %s" % ", ".join(pkg["block_reasons"]))
    else:
        print("SKIP: no evidence data")


def run_quality_report(strategy: str):
    from core.paper_evidence import generate_evidence_quality_report
    report, path = generate_evidence_quality_report(strategy)
    if path:
        print("OK: evidence quality report -> %s" % path)
        print("\n  Period: %s" % report["period"])
        print("  Total days: %d" % report["total_days"])
        print("  Benchmark non-null ratio: %.1f%%" % (report["benchmark_non_null_ratio"] * 100))
        conv = report["provisional_to_final_conversion"]
        print("  Provisional→Final conversion: %.1f%% (final=%d, prov=%d, fail=%d)" % (
            conv["conversion_ratio"] * 100, conv["final_days"],
            conv["provisional_days"], conv["failed_days"],
        ))
        cdist = report["final_completeness_distribution"]
        print("  Final completeness: min=%.2f, max=%.2f, avg=%.2f (n=%d)" % (
            cdist["min"] or 0, cdist["max"] or 0, cdist["avg"] or 0, cdist["count"],
        ))
        print("  Cross-validation mismatches: %d" % report["cross_validation_mismatch_count"])
        print("  Restart recovery count: %d" % report["restart_recovery_count"])
        print("  Anomaly rate: %.1f%%" % (report["anomaly_rate"] * 100))
        if report["anomaly_type_breakdown"]:
            print("  Anomaly breakdown: %s" % json.dumps(report["anomaly_type_breakdown"], ensure_ascii=False))
    else:
        print("SKIP: no evidence data")


if __name__ == "__main__":
    main()
