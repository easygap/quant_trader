#!/usr/bin/env python3
"""Durable, prospective paper validation; no orders or operational DB writes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.paper_validation import collect_paper_validation, read_paper_validation


def main(argv=None):
    parser = argparse.ArgumentParser(description="장기 모의운용 전진 관측. 실계좌 승격·주문·운영 DB 쓰기 없음.")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="현재 시각으로 종가·입출금·비교지수 관측 추가")
    collect.add_argument("--db-path", type=Path, help="SQLite 원천 (mode=ro로만 조회)")
    collect.add_argument("--config-path", type=Path)
    show = commands.add_parser("status", help="저장 결과만 조회 (네트워크·DB 접근 없음)")
    for subcommand in (collect, show):
        subcommand.add_argument("--output-dir", type=Path)
        subcommand.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (collect_paper_validation(args.db_path, config_path=args.config_path, output_dir=args.output_dir)
                  if args.command == "collect" else read_paper_validation(args.output_dir))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print(f"전진 모의운용 검증: {result['status']} · 연구 전용 · 실계좌 자동 승격 없음")
            for row in result["cohorts"]:
                print(f"{row['basket']} [{row['status']}] {row['return_observations']}/{row['minimum_observations']} 관측수익률 · 시작 {row['started_at']}")
                if row["issues"]:
                    print("  " + ", ".join(row["issues"]))
            for issue in result["issues"]:
                print(issue)
        return 2 if result["status"] in {"unavailable", "corrupt"} else 0
    except (ValueError, OSError) as exc:
        print(f"paper validation failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
