"""
Paper Evidence Pipeline — 수동 실행 도구

사용: python tools/run_paper_evidence_pipeline.py --strategy rotation --date 2026-04-02
      python tools/run_paper_evidence_pipeline.py --strategy rotation --weekly
      python tools/run_paper_evidence_pipeline.py --strategy rotation --promotion-package

기능:
1. --date: 특정 날짜의 DailyEvidence를 수동 생성 (스케줄러 밖에서 실행)
2. --weekly: 주간 요약 마크다운 생성
3. --promotion-package: 60일 승격 패키지 생성
4. --status: 현재 evidence 상태 확인
"""
import sys, os, json, argparse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="INFO")


def show_status(strategy: str):
    """현재 evidence 상태 확인."""
    from core.paper_evidence import load_all_evidence, load_anomalies, EVIDENCE_DIR

    evidence = load_all_evidence(strategy)
    anomalies = [a for a in load_anomalies() if a.get("strategy") == strategy]

    print(f"\n{'='*60}")
    print(f"  Paper Evidence 상태: {strategy}")
    print(f"{'='*60}")
    print(f"  기록 일수: {len(evidence)}")
    if evidence:
        first = evidence[0]
        last = evidence[-1]
        print(f"  첫 기록: {first['date']} (Day {first['day_number']})")
        print(f"  마지막: {last['date']} (Day {last['day_number']})")
        print(f"  누적 수익: {last['cumulative_return']:.2f}%")
        print(f"  포트폴리오: {last['portfolio_value']:,.0f}원")
        print(f"  MDD: {last['drawdown']:.2f}%")
    print(f"  Anomaly 총 건수: {len(anomalies)}")
    critical = sum(1 for a in anomalies if a.get("severity") == "critical")
    print(f"  Critical anomaly: {critical}건")
    print(f"  파일: {EVIDENCE_DIR / f'daily_evidence_{strategy}.jsonl'}")
    print(f"{'='*60}\n")


def generate_weekly_summary(strategy: str):
    """주간 요약 마크다운 생성."""
    from core.paper_evidence import load_all_evidence, load_anomalies, EVIDENCE_DIR

    evidence = load_all_evidence(strategy)
    if not evidence:
        print("Evidence 데이터 없음")
        return

    # 최근 5일
    recent = evidence[-5:] if len(evidence) >= 5 else evidence
    anomalies = [a for a in load_anomalies() if a.get("strategy") == strategy]

    md = f"# Paper Weekly Summary — {strategy}\n\n"
    md += f"**기간**: {recent[0]['date']} ~ {recent[-1]['date']} (Day {recent[0]['day_number']}~{recent[-1]['day_number']})\n\n"

    md += "## 일별 요약\n\n"
    md += "| Day | Date | Return% | Cum% | MDD% | Positions | Value |\n"
    md += "|-----|------|---------|------|------|-----------|-------|\n"
    for e in recent:
        md += f"| {e['day_number']} | {e['date']} | {e['absolute_return']:.2f} | {e['cumulative_return']:.2f} | {e['drawdown']:.2f} | {e['n_positions']} | {e['portfolio_value']:,.0f} |\n"

    md += f"\n## Anomalies ({len(anomalies)}건)\n\n"
    if anomalies:
        for a in anomalies[-5:]:
            md += f"- [{a['severity']}] {a['rule']}: {a['detail']}\n"
    else:
        md += "없음\n"

    # 저장
    out_path = EVIDENCE_DIR / f"weekly_summary_{strategy}_{date.today().isoformat()}.md"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"주간 요약 생성: {out_path}")
    print(md)


def generate_promotion(strategy: str):
    """60일 승격 패키지 생성."""
    from core.paper_evidence import generate_promotion_package

    pkg = generate_promotion_package(strategy)
    print(json.dumps(pkg, indent=2, ensure_ascii=False, default=str))

    if pkg.get("all_gates_passed"):
        print("\n  추천: promote_to_live_candidate")
    else:
        failed = [g["name"] for g in pkg.get("approval_gates", []) if not g["passed"]]
        print(f"\n  미통과 게이트: {failed}")
        print(f"  추천: maintain_provisional")


def main():
    parser = argparse.ArgumentParser(description="Paper Evidence Pipeline")
    parser.add_argument("--strategy", required=True, help="전략명 (rotation, scoring)")
    parser.add_argument("--status", action="store_true", help="현재 상태 확인")
    parser.add_argument("--weekly", action="store_true", help="주간 요약 생성")
    parser.add_argument("--promotion-package", action="store_true", help="승격 패키지 생성")
    args = parser.parse_args()

    if args.status:
        show_status(args.strategy)
    elif args.weekly:
        generate_weekly_summary(args.strategy)
    elif args.promotion_package:
        generate_promotion(args.strategy)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
