#!/usr/bin/env python3
"""Import or inspect curated hypotheses; no news collection or trading side effects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.research_evidence import import_research_evidence, read_evidence_journal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="출처·반증 조건이 있는 수동 조사 기록. 주문을 만들지 않습니다.")
    parser.add_argument("--journal-dir", help="기록 폴더 (기본 data/research_evidence)")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("import", help="JSON을 새로운 관측 시각과 버전으로 추가")
    add.add_argument("source", type=Path)
    show = commands.add_parser("list", help="해당 시점에 관측한 최신 버전 조회")
    show.add_argument("--as-of", help="시간대가 있는 ISO 시각. 예: 2026-10-13T11:00:00+09:00")
    show.add_argument("--symbol", action="append", help="연결 근거가 기록된 종목만 조회 (반복 가능)")
    show.add_argument("--limit", type=int, default=50)
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_research_evidence(args.source, args.journal_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        result = read_evidence_journal(args.journal_dir, as_of=args.as_of, symbols=args.symbol, limit=args.limit)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"수동 조사 기록 {result['total']}개 · {result['status']} · {result['as_of']}")
            print("공개 시각은 작성자가 입력한 정보입니다. 자동 뉴스 수집·매매 신호가 아닙니다.")
            for entry in result["entries"]:
                print(f"[{entry['status']}] {entry['title']} (v{entry['version']}, 관측 {entry['observed_at']})")
                print(f"  가설: {entry['thesis']}")
                print(f"  반증: {entry['invalidation_condition']}")
                if entry["issues"]:
                    print(f"  점검: {', '.join(entry['issues'])}")
            for issue in result["issues"]:
                print(f"기록 오류: {issue}")
        return 2 if result["issues"] else 0
    except (OSError, ValueError) as exc:
        print(f"research evidence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
