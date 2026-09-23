"""스케줄러 휴장일 자동 갱신 호출부 회귀 테스트 (감사: runtime-holidays-autoupdate-reverts-verified-calendar).

스케줄러는 holidays.yaml을 실행 위치(CWD) 기준으로 찾아, 다른 폴더에서 띄우면 파일이
'없다'고 보고 매일 갱신했다. 그리고 pykrx 휴장일 조회가 안 되는 환경(현재 설치 버전에
get_market_trading_date_by_date 없음)에서는 갱신기가 대체 목록으로 검증된 달력을
덮어쓸 수 있었다. 이 테스트는 호출부만 고정한다(갱신기 자체 수정은 별도).
실제 holidays.yaml은 건드리지 않는다 — 갱신 함수는 전부 대역이다.
"""

import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import pytest
from loguru import logger

import core.scheduler as scheduler_mod


@pytest.fixture
def captured_warnings():
    messages = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def update_calls(monkeypatch):
    calls = []

    def fake_update(path=None, **kwargs):
        calls.append(path)
        return path

    monkeypatch.setattr("core.holidays_updater.update_holidays_yaml", fake_update)
    return calls


def _scheduler(monkeypatch):
    s = scheduler_mod.Scheduler.__new__(scheduler_mod.Scheduler)
    s.config = None
    s.rebuilt = []
    monkeypatch.setattr(scheduler_mod, "TradingHours", lambda config: s.rebuilt.append(config) or "th")
    s.trading_hours = "old"
    return s


def _stale_file(tmp_path, days_old=100):
    path = tmp_path / "holidays.yaml"
    path.write_text("holidays: []\n", encoding="utf-8")
    old = time.time() - days_old * 86400
    os.utime(path, (old, old))
    return path


def test_holidays_path_is_resolved_from_project_root():
    project_root = Path(scheduler_mod.__file__).resolve().parents[1]
    assert scheduler_mod._HOLIDAYS_PATH == project_root / "config" / "holidays.yaml"
    assert scheduler_mod._HOLIDAYS_PATH.is_absolute()


def test_other_working_directory_does_not_trigger_update(
    monkeypatch, tmp_path, update_calls
):
    """다른 폴더에서 실행해도 최신 달력 파일을 찾아 갱신하지 않는다."""
    fresh = tmp_path / "project" / "holidays.yaml"
    fresh.parent.mkdir()
    fresh.write_text("holidays: []\n", encoding="utf-8")
    monkeypatch.setattr(scheduler_mod, "_HOLIDAYS_PATH", fresh)
    monkeypatch.setattr(scheduler_mod, "_pykrx_holiday_source_available", lambda: (True, ""))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    s = _scheduler(monkeypatch)
    s._maybe_update_holidays()

    assert update_calls == []
    assert s.trading_hours == "old"


def test_stale_calendar_is_not_overwritten_when_pykrx_unavailable(
    monkeypatch, tmp_path, update_calls, captured_warnings
):
    stale = _stale_file(tmp_path)
    monkeypatch.setattr(scheduler_mod, "_HOLIDAYS_PATH", stale)
    monkeypatch.setattr(
        scheduler_mod,
        "_pykrx_holiday_source_available",
        lambda: (False, "pykrx.stock.get_market_trading_date_by_date 없음"),
    )

    s = _scheduler(monkeypatch)
    s._maybe_update_holidays()

    assert update_calls == []
    assert s.rebuilt == []
    assert any("휴장일 자동 갱신 생략" in m for m in captured_warnings)


def test_stale_calendar_updates_project_root_path_when_pykrx_available(
    monkeypatch, tmp_path, update_calls
):
    stale = _stale_file(tmp_path)
    monkeypatch.setattr(scheduler_mod, "_HOLIDAYS_PATH", stale)
    monkeypatch.setattr(scheduler_mod, "_pykrx_holiday_source_available", lambda: (True, ""))

    s = _scheduler(monkeypatch)
    s._maybe_update_holidays()

    assert update_calls == [stale]
    assert s.rebuilt == [None]
    assert s.trading_hours == "th"


@pytest.mark.parametrize("pykrx_ok, expected_calls", [(False, 0), (True, 1)])
def test_new_year_trigger_also_respects_pykrx_availability(
    monkeypatch, tmp_path, update_calls, pykrx_ok, expected_calls
):
    """연초(1/1~1/7) 트리거도 pykrx를 쓸 수 없으면 대체 목록으로 덮어쓰지 않는다."""
    last_year = tmp_path / "holidays.yaml"
    last_year.write_text("holidays: []\n", encoding="utf-8")
    december = datetime(2026, 12, 30, 12, 0).timestamp()
    os.utime(last_year, (december, december))
    monkeypatch.setattr(scheduler_mod, "_HOLIDAYS_PATH", last_year)
    monkeypatch.setattr(
        scheduler_mod, "_pykrx_holiday_source_available", lambda: (pykrx_ok, "" if pykrx_ok else "없음"),
    )

    class _NewYear(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 2, 8, 0)

    monkeypatch.setattr(scheduler_mod, "datetime", _NewYear)

    _scheduler(monkeypatch)._maybe_update_holidays()

    assert len(update_calls) == expected_calls


def _fake_pykrx(monkeypatch, *, with_api):
    stock = types.ModuleType("pykrx.stock")
    if with_api:
        stock.get_market_trading_date_by_date = lambda start, end: None
    package = types.ModuleType("pykrx")
    package.stock = stock
    monkeypatch.setitem(sys.modules, "pykrx", package)
    monkeypatch.setitem(sys.modules, "pykrx.stock", stock)


def test_pykrx_probe_detects_missing_trading_date_api(monkeypatch):
    _fake_pykrx(monkeypatch, with_api=False)
    available, reason = scheduler_mod._pykrx_holiday_source_available()
    assert available is False
    assert "get_market_trading_date_by_date" in reason


def test_pykrx_probe_accepts_available_api(monkeypatch):
    _fake_pykrx(monkeypatch, with_api=True)
    assert scheduler_mod._pykrx_holiday_source_available() == (True, "")
