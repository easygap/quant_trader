"""바스켓 paper 운영 평가 CLI — DB에서 운영 데이터를 수집해 승격 판정을 출력한다.

사용:
    .venv\\Scripts\\python.exe tools/basket_paper_evaluation.py
    .venv\\Scripts\\python.exe tools/basket_paper_evaluation.py --min-days 60 --out reports/basket_eval.md

판정 로직·기준 근거는 core/basket_evaluation.py 및 docs/BASKET_PAPER_EVALUATION.md 참고.
수집·판정은 바스켓 live gate(core/live_readiness.py)와 동일한 경로
(collect_basket_paper_evaluation)를 공유한다 — CLI가 보여주는 판정이 곧 게이트가
보는 판정이다. 데이터가 쌓이는 즉시(매일) 실행해도 안전하다(기간 미충족이면 WAIT).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="바스켓 paper 운영 평가 (승격 판정)")
    parser.add_argument("--min-days", type=int, default=60, help="필요 운영 영업일 수 (기본 60)")
    parser.add_argument("--out", type=str, default=None, help="평가 리포트 저장 경로 (Markdown)")
    args = parser.parse_args()

    from core.basket_evaluation import (
        collect_basket_paper_evaluation,
        format_evaluation_report,
    )

    result, basket_label = collect_basket_paper_evaluation(min_days=args.min_days)
    report = format_evaluation_report(result, basket_name=basket_label)
    print(report)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")
        logger.info("평가 리포트 저장: {}", out)

    # exit code: WAIT/PASS=0(정상 흐름), FAIL_REVIEW=1(운영자 확인 필요)
    return 1 if result["verdict"] == "FAIL_REVIEW" else 0


if __name__ == "__main__":
    raise SystemExit(main())
