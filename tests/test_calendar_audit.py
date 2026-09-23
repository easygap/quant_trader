"""휴장일 달력 회귀 테스트 (2026-09-23 점검).

달력 오류는 양방향으로 조용히 해롭다.
- 휴장일 누락 → 커버리지 분모가 부풀고 가짜 결측 경보가 난다(2026-07-17 사례).
- 거래일을 휴장으로 → 그날 스냅샷이 직전 거래일 행을 덮어써 결측이 숨는다
  (2026-09-28 사례 — 미래 날짜라 시장 데이터 대조 도구로는 잡을 수 없었다).
그래서 연도별 '평일 휴장' 집합을 손으로 검증한 값으로 고정한다. 달력을 고치려면
이 목록도 함께 고쳐야 CI를 통과한다.
"""

from datetime import date, datetime
from pathlib import Path

import yaml

from core import holidays_updater as hu

_YAML = Path(__file__).resolve().parent.parent / "config" / "holidays.yaml"

# 월력요항 기준으로 손 검증한 평일 휴장일(주말에 걸린 공휴일은 판정에 영향이 없어 뺐다)
VERIFIED_WEEKDAY_CLOSURES = {
    2026: {
        "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02",
        "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-03", "2026-07-17",
        "2026-08-17", "2026-09-24", "2026-09-25", "2026-10-05", "2026-10-09",
        "2026-12-25", "2026-12-31",
    },
    2027: {
        "2027-01-01", "2027-02-08", "2027-02-09", "2027-03-01", "2027-05-05",
        "2027-05-13", "2027-08-16", "2027-09-14", "2027-09-15", "2027-09-16",
        "2027-10-04", "2027-10-11", "2027-12-27", "2027-12-31",
    },
}


def _weekday_set(dates, year):
    out = set()
    for d in dates:
        s = str(d)[:10]
        if int(s[:4]) == year and date.fromisoformat(s).weekday() < 5:
            out.add(s)
    return out


def _yaml_dates():
    with open(_YAML, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("holidays", [])


def test_yaml_weekday_closures_match_verified_list():
    dates = _yaml_dates()
    for year, expected in VERIFIED_WEEKDAY_CLOSURES.items():
        assert _weekday_set(dates, year) == expected, f"{year}년 평일 휴장 집합이 검증 목록과 다름"


def test_fallback_weekday_closures_match_yaml():
    """fallback 표가 고치기 전 값으로 남아 있으면 자동 갱신이 고친 달력을 되돌린다."""
    dates = _yaml_dates()
    for year in VERIFIED_WEEKDAY_CLOSURES:
        assert _weekday_set(hu.FALLBACK_BY_YEAR[year], year) == _weekday_set(dates, year)


def test_chuseok_2026_saturday_overlap_has_no_substitute():
    from core.trading_hours import TradingHours

    th = TradingHours()
    assert th.is_trading_day(datetime(2026, 9, 24)) is False
    assert th.is_trading_day(datetime(2026, 9, 25)) is False
    assert th.is_trading_day(datetime(2026, 9, 28)) is True


# --------------------------------------------------------------- 자동 갱신 안전성

def _write(path, dates, header="# 교정 이력 주석\n# 두 번째 줄\n"):
    path.write_text(header + "\n" + yaml.safe_dump({"holidays": sorted(dates)}), encoding="utf-8")


def test_fallback_update_never_removes_existing_entries(tmp_path, monkeypatch):
    """pykrx가 실패하면 fallback이 사람이 고친 파일을 덮어쓰지 못해야 한다."""
    path = tmp_path / "holidays.yaml"
    _write(path, {"2026-07-17", "2026-09-24"})
    monkeypatch.setattr(hu, "_fetch_from_pykrx", lambda a, b: (set(), None))
    monkeypatch.setattr(hu, "FALLBACK_BY_YEAR", {2026: {"2026-01-01"}})

    hu.update_holidays_yaml(path=path, year_from=2026, year_to=2026)

    saved = set(str(d) for d in yaml.safe_load(path.read_text(encoding="utf-8"))["holidays"])
    assert {"2026-07-17", "2026-09-24", "2026-01-01"} <= saved


def test_update_preserves_header_comments(tmp_path, monkeypatch):
    path = tmp_path / "holidays.yaml"
    _write(path, {"2026-09-24"})
    monkeypatch.setattr(hu, "_fetch_from_pykrx", lambda a, b: (set(), None))

    hu.update_holidays_yaml(path=path, year_from=2026, year_to=2026)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 교정 이력 주석\n# 두 번째 줄\n")


def test_pykrx_verified_range_is_authoritative_but_future_is_kept(tmp_path, monkeypatch):
    """시장 데이터로 확인된 구간은 바로잡고, 확인되지 않은 미래 구간은 건드리지 않는다."""
    path = tmp_path / "holidays.yaml"
    # 01-27은 실제로는 개장한 날(틀린 항목), 09-24는 아직 확인 불가한 미래
    _write(path, {"2026-01-27", "2026-09-24", "2026-03-01"})
    monkeypatch.setattr(
        hu, "_fetch_from_pykrx", lambda a, b: ({"2026-02-16"}, date(2026, 9, 22)),
    )
    monkeypatch.setattr(hu, "FALLBACK_BY_YEAR", {2026: {"2026-09-25"}})

    hu.update_holidays_yaml(path=path, year_from=2026, year_to=2026)

    saved = set(str(d) for d in yaml.safe_load(path.read_text(encoding="utf-8"))["holidays"])
    assert "2026-01-27" not in saved      # 확인 구간의 틀린 평일 항목은 바로잡힌다
    assert "2026-02-16" in saved          # 확인 구간의 실제 휴장
    assert "2026-03-01" in saved          # 주말 항목은 그대로
    assert {"2026-09-24", "2026-09-25"} <= saved  # 미래 구간은 기존 ∪ fallback


def test_pykrx_derivation_stops_at_last_returned_session(monkeypatch):
    """pykrx에는 미래 영업일이 없다 — 마지막 거래일 뒤 평일을 휴장으로 찍으면 안 된다."""
    trading = {date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)}
    monkeypatch.setattr(hu, "_pykrx_trading_dates", lambda s, e: trading)

    holidays, verified = hu._fetch_from_pykrx(2026, 2026)

    assert verified == date(2026, 9, 23)
    assert all(d <= "2026-09-23" for d in holidays)
    assert "2026-09-24" not in holidays and "2026-12-01" not in holidays
