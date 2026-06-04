"""target-weight pilot의 순수 유틸 함수 — 해시·수치 변환·근사 비교.

target_weight_rotation_pilot.py 모놀리스에서 외부 의존이 전혀 없는(stdlib만 쓰는) 순수
헬퍼만 분리한 모듈이다. 단독 테스트가 쉽고, 동작은 기존과 100% 동일하다. 모놀리스는 이
모듈을 `_접두어` 이름으로 re-import해 하위 호환을 유지한다.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def stable_manifest_hash(payload: dict[str, Any]) -> str:
    """dict를 정렬·고정 직렬화해 SHA-256 해시. 키 순서·공백에 무관하게 안정적."""
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def numbers_match(actual: Any, expected: Any, *, absolute_tolerance: float | None = None) -> bool:
    """두 값이 (부동소수 오차 허용 하에) 같은지. 숫자 변환 실패 시 == 비교로 폴백."""
    try:
        actual_num = float(actual)
        expected_num = float(expected)
    except (TypeError, ValueError):
        return actual == expected
    tolerance = max(1e-6, abs(expected_num) * 1e-9)
    if absolute_tolerance is not None:
        tolerance = max(tolerance, float(absolute_tolerance))
    return abs(actual_num - expected_num) <= tolerance


def coerce_float_or_none(value: Any) -> float | None:
    """float로 변환. 실패하거나 비유한값(NaN/Inf)이면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def coerce_int_or_zero(value: Any) -> int:
    """int로 변환. 실패하면 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 하위 호환: 모놀리스가 쓰던 _접두어 이름을 별칭으로 노출 ──
_stable_manifest_hash = stable_manifest_hash
_numbers_match = numbers_match
_coerce_float_or_none = coerce_float_or_none
_coerce_int_or_zero = coerce_int_or_zero
