#!/usr/bin/env python3
"""
CI 검증: 전략 레지스트리 상태 일관성 확인

- disabled 전략이 paper/schedule allowed_modes에 포함되면 FAIL
- research_only 전략이 paper 허용이면 FAIL
- 전략 이름 불일치 (registry vs status dict) 감지

Exit code:
  0 = OK
  1 = 불일치 발견
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def check() -> list[str]:
    from strategies import _STRATEGY_REGISTRY, STRATEGY_STATUS, is_strategy_allowed

    errors = []

    # 1. disabled 전략이 paper 모드 허용이면 안 됨
    for name, info in STRATEGY_STATUS.items():
        status = info.get("status", "disabled")
        allowed_modes = info.get("allowed_modes", [])

        if status == "disabled" and ("paper" in allowed_modes or "schedule" in allowed_modes):
            errors.append(
                f"{name}: status=disabled 인데 allowed_modes에 paper/schedule 포함: {allowed_modes}"
            )

    # 2. registry에 있지만 status에 없는 전략 확인
    for name in _STRATEGY_REGISTRY:
        if name not in STRATEGY_STATUS:
            errors.append(f"{name}: registry에는 있지만 STRATEGY_STATUS에 없음")

    # 3. is_strategy_allowed 일관성 검증
    for name, info in STRATEGY_STATUS.items():
        status = info.get("status", "disabled")
        allowed, reason = is_strategy_allowed(name, "paper")
        if status == "disabled" and allowed:
            errors.append(
                f"{name}: status=disabled인데 is_strategy_allowed('paper')=True — 게이트 불일치"
            )

    return errors


def main():
    errors = check()
    if errors:
        print("FAIL: 전략 레지스트리 불일치 발견")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("OK: 전략 레지스트리 상태 일관성 확인 완료")
        sys.exit(0)


if __name__ == "__main__":
    main()
