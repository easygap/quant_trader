"""tools/pilot_util.py — 순수 유틸(해시·수치 변환·근사 비교) 단위 테스트.

모놀리스에서 분리한 함수들. 분리 전 전용 테스트가 없었으므로 커버리지를 추가한다.
"""
from tools import pilot_util as pu


class TestStableManifestHash:
    def test_deterministic_regardless_of_key_order(self):
        assert pu.stable_manifest_hash({"a": 1, "b": 2}) == pu.stable_manifest_hash({"b": 2, "a": 1})

    def test_different_payload_different_hash(self):
        assert pu.stable_manifest_hash({"a": 1}) != pu.stable_manifest_hash({"a": 2})

    def test_returns_hex_sha256(self):
        h = pu.stable_manifest_hash({"x": 1})
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)

    def test_handles_non_json_values(self):
        # default=str 로 직렬화되므로 datetime 등도 안전
        from datetime import datetime
        h = pu.stable_manifest_hash({"t": datetime(2024, 1, 1)})
        assert len(h) == 64


class TestNumbersMatch:
    def test_exact(self):
        assert pu.numbers_match(1.0, 1.0)

    def test_within_default_tolerance(self):
        assert pu.numbers_match(1.0, 1.0 + 1e-7)

    def test_outside_tolerance(self):
        assert not pu.numbers_match(1.0, 1.1)

    def test_absolute_tolerance(self):
        assert pu.numbers_match(100.0, 105.0, absolute_tolerance=10.0)
        assert not pu.numbers_match(100.0, 120.0, absolute_tolerance=10.0)

    def test_non_numeric_falls_back_to_eq(self):
        assert pu.numbers_match("abc", "abc")
        assert not pu.numbers_match("abc", "def")


class TestCoerce:
    def test_float_ok(self):
        assert pu.coerce_float_or_none("2.5") == 2.5

    def test_float_bad_is_none(self):
        assert pu.coerce_float_or_none("x") is None
        assert pu.coerce_float_or_none(None) is None

    def test_float_nonfinite_is_none(self):
        assert pu.coerce_float_or_none(float("nan")) is None
        assert pu.coerce_float_or_none(float("inf")) is None

    def test_int_ok(self):
        assert pu.coerce_int_or_zero("7") == 7
        assert pu.coerce_int_or_zero(3.9) == 3

    def test_int_bad_is_zero(self):
        assert pu.coerce_int_or_zero("x") == 0
        assert pu.coerce_int_or_zero(None) == 0


def test_backcompat_aliases():
    for name in ("_stable_manifest_hash", "_numbers_match", "_coerce_float_or_none", "_coerce_int_or_zero"):
        assert hasattr(pu, name)
