"""수정 전후의 모든 일별 결과를 대조하고 정수 주 백테스트 실행 시간을 잰다.

로컬 가격 캐시를 사용하며 캐시가 없으면 공개 종가를 받는다. 주문은 실행하지 않는다.
기준 커밋을 읽을 수 있도록 해당 Git 이력이 필요하다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from tools import risk_overlay_backtest as research
from tools import risk_review as current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="9a7c145")
    parser.add_argument("--as-of", default="2026-09-17")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--output", default="reports/research/implementation_benchmark_20260922.json"
    )
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("반복 횟수는 1 이상이어야 합니다")
    old = types.ModuleType("risk_review_before")
    old.__file__ = str(ROOT / "tools/risk_review.py")
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:tools/risk_review.py"], cwd=ROOT
    ).decode("utf-8")
    # 사용자가 지정한 로컬 저장소의 기준 코드를 같은 프로세스에서 비교한다.
    exec(compile(source, old.__file__, "exec"), old.__dict__)  # noqa: S102
    research.AS_OF = args.as_of
    series = {s: research._fdr(s, "2014-01-01") for s in ("069500", "357870", "KS200")}
    panel = pd.DataFrame(series)
    policies = [
        research.Policy("static", "고정"),
        research.Policy("old_product", "기존", trend=True, dd=True),
        research.Policy("minimum", "변경", trend=True, dd=True, combination="minimum"),
    ]
    report = {
        "date": pd.Timestamp.now().date().isoformat(),
        "baseline_commit": args.baseline,
        "input_as_of": args.as_of,
        "input_sha256": {
            s: hashlib.sha256(v.to_csv().encode()).hexdigest()
            for s, v in series.items()
        },
        "python": sys.version,
        "rows": len(panel),
        "repetitions": args.repetitions,
        "policies": {},
    }
    for policy in policies:
        timings = {"before": [], "after": []}
        for repeat in range(args.repetitions):
            results = {}
            for name, module in (("before", old), ("after", current)):
                began = time.perf_counter()
                results[name] = module.integer_etf_simulation(
                    panel, policy, redirect=policy.name != "old_product"
                )
                timings[name].append(time.perf_counter() - began)
            pd.testing.assert_frame_equal(
                results["before"][0], results["after"][0], check_exact=True
            )
            assert results["before"][1] == results["after"][1]
        median = {name: statistics.median(values) for name, values in timings.items()}
        report["policies"][policy.name] = {
            "seconds": timings,
            "median_seconds": median,
            "speedup": median["before"] / median["after"],
            "all_daily_values_exactly_equal": True,
            "output_rows": len(results["after"][0]),
        }
        print(policy.name, report["policies"][policy.name], flush=True)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
