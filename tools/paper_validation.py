#!/usr/bin/env python3
"""Durable, prospective paper validation; no orders or operational DB writes."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.paper_validation import collect_paper_validation, read_paper_validation


def _status_label(value):
    return {
        "observed": "관측 성과", "insufficient_sample": "관측 부족",
        "continuity_unverified": "전체 기간 연결 미확인", "linked": "수익률 연결 확인",
        "unlinked": "연결 보류", "awaiting_observations": "종가 관측 대기",
        "unavailable": "자료 없음", "corrupt": "기록 확인 필요", "observing": "관측 누적 중",
        "awaiting_forward": "새 종가 관측 대기", "data_gaps": "관측 결측",
        "ready_for_review": "표본 요건 충족 · 성과 검토 필요", "inactive": "활성 검증 없음",
    }.get(value, value or "미확인")


def _percent(value, unit="%"):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:,.2f}{unit}"
    return "—"


def _period(performance):
    performance = performance or {}
    return f"{performance.get('start_date') or '—'} ~ {performance.get('end_date') or '—'}"


def _performance_text(performance):
    performance = performance or {}
    return (f"입출금 조정 수익률 {_percent(performance.get('twr_pct'))} · "
            f"비교지수 {_percent(performance.get('benchmark_return_pct'))} · "
            f"차이 {_percent(performance.get('excess_return_pct'), '%p')} · "
            f"최대 낙폭 {_percent(performance.get('drawdown_pct'))}")


def _change_reasons(values):
    labels = {"first_registration": "최초 등록", "config_changed": "운용 설정 변경",
              "code_changed": "운용 코드 변경", "protocol_changed": "검증 기준 변경",
              "source_database_changed": "원천 원장 변경"}
    return " · ".join(labels.get(value, value) for value in values or []) or "변경 사유 없음"


def _print_performance_history(histories):
    for history in histories:
        print(f"\n{history.get('basket') or history.get('account_key')} · 전체 누적 성과 "
              f"[{_status_label(history.get('status'))}]")
        print(f"  {_period(history.get('performance'))} · 종가 {history.get('observations', 0)}개 / "
              f"수익 구간 {history.get('return_observations', 0)}개 · 버전 {history.get('version_count', 0)}개")
        disconnected = history.get("status") == "continuity_unverified"
        print("  " + _performance_text(None if disconnected else history.get("performance")))
        coverage = history.get("coverage") or {}
        print(f"  종가 결측 {len(coverage.get('missing_sessions') or [])}일 · "
              f"비교지수 결측 {len(coverage.get('benchmark_missing_sessions') or [])}일")
        if disconnected:
            print("  전체 기간을 연결할 근거가 부족해 누적 수익률을 보류합니다.")
            for index, segment in enumerate(history.get("segments") or [], 1):
                print(f"  연속 구간 {index} · {_period(segment.get('performance'))} · "
                      f"수익 구간 {segment.get('return_observations', 0)}개 · "
                      + _performance_text(segment.get("performance")))
        for issue in history.get("issues") or []:
            print(f"  확인 사항: {issue}")
        versions = history.get("versions") or []
        labels = {version["cohort_id"]: f"버전 {index}" for index, version in enumerate(versions, 1)}
        print("  버전별 성과 (종료된 버전 포함):")
        for version in versions:
            label = labels[version["cohort_id"]]
            fingerprint = version.get("fingerprint") or {}
            print(f"    {label} · {'현재' if version.get('active') else '종료'} · "
                  f"코드 {str(fingerprint.get('code') or '—')[:10]} · "
                  f"{_period(version.get('performance'))} · 종가 {version.get('observations', 0)}개 / "
                  f"수익 구간 {version.get('return_observations', 0)}개")
            print(f"      {_change_reasons(version.get('registration_reason'))} · "
                  + _performance_text(version.get("performance")))
            for issue in version.get("issues") or []:
                print(f"      확인 사항: {issue}")
        comparisons = history.get("comparisons") or []
        print(f"  변경 전후 비교: {len(comparisons)}건")
        for comparison in comparisons:
            print(f"    {labels.get(comparison.get('before_cohort_id'), '이전 버전')} → "
                  f"{labels.get(comparison.get('after_cohort_id'), '다음 버전')} · "
                  f"{comparison.get('changed_at') or '—'} · "
                  f"{_change_reasons(comparison.get('change_reasons'))}")
            transition = comparison.get("transition") or {}
            print(f"      변경 경계 구간: {_status_label(transition.get('status'))} · {_period(transition)}")
            if transition.get("status") == "linked":
                print("      " + _performance_text(transition))
            window = comparison.get("matched_window") or {}
            print(f"      동일 길이 비교: {_status_label(window.get('status'))} · "
                  f"각 {window.get('intervals', 0)}개 수익 구간 (변경 경계 구간 제외)")
            if window.get("status") == "observed":
                for key, label in (("before", "변경 전"), ("after", "변경 후")):
                    print(f"        {label} {_period(window.get(key))} · " + _performance_text(window.get(key)))
                print(f"        변경 후 − 변경 전: 수익률 {_percent(window.get('twr_change_pp'), '%p')} · "
                      f"비교지수 대비 차이 {_percent(window.get('excess_change_pp'), '%p')}")
            for issue in (transition.get("issues") or []) + (window.get("issues") or []):
                print(f"      확인 사항: {issue}")
    if histories:
        print("\n누적 성과와 버전별 전진 검증 표본 요건은 따로 관리합니다. "
              "변경 전후 비교는 시장 상황도 달라지는 관측 비교이며, 코드 변경의 효과나 우열을 뜻하지 않습니다.")


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
            print(f"전진 모의운용 검증: {_status_label(result['status'])} · 연구 전용 · 실계좌 자동 승격 없음")
            _print_performance_history(result.get("performance_history") or [])
            print("\n버전별 전진 검증 표본:")
            for row in result["cohorts"]:
                print(f"{row['basket']} [{_status_label(row['status'])}] "
                      f"{row['return_observations']}/{row['minimum_observations']}개 수익 구간 · 시작 {row['started_at']}")
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
