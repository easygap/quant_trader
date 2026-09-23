"""KRX 거래일 기준 evidence 기록·staleness 회귀 테스트.

감사 두 건을 함께 고정한다(같이 배포해야 하는 쌍):
- runtime-premarket-finalize-holiday-fabrication: 장전 '전일 finalize'가 주말만
  건너뛰어 평일 휴장일(추석·대체공휴일)의 가짜 real_paper evidence를 만들고, 실제
  직전 거래일은 finalize하지 않았다.
- runtime-paper-runtime-weekday-stale: paper_runtime이 평일 수로 staleness를 세서
  연휴 뒤 첫 거래일에 증거가 밀렸다고 오판해 신규 진입을 막았다.

거래일 달력은 테스트가 고정한다 — 운영 holidays.yaml이 교정돼도(예: 2026-09-28
대체공휴일 오표기 의심) 이 테스트의 기대값이 흔들리지 않게.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from loguru import logger

CHUSEOK_2026_WITH_0928 = {
    "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-28",
    "2026-10-03", "2026-10-05", "2026-10-09",
}


@pytest.fixture
def calendar(monkeypatch):
    holder = {"holidays": set(CHUSEOK_2026_WITH_0928)}
    monkeypatch.setattr("core.trading_hours._load_holidays", lambda: set(holder["holidays"]))
    return holder


@pytest.fixture
def captured_warnings():
    messages = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _scheduler():
    from config.config_loader import Config
    from core.scheduler import Scheduler
    from core.trading_hours import TradingHours

    s = Scheduler.__new__(Scheduler)
    s.strategy_name = "scoring"
    s._mode = "paper"
    s.config = Config.get()
    s.trading_hours = TradingHours(s.config)
    s._pilot_session = {
        "active": False, "pilot_authorized": False,
        "pilot_caps_snapshot": {}, "session_mode": "normal_paper",
        "evidence_mode": "real_paper",
    }
    return s


@pytest.fixture
def evidence_calls(monkeypatch):
    calls = {"finalize": [], "collect": []}
    monkeypatch.setattr(
        "core.paper_evidence.finalize_daily_evidence",
        lambda **kw: calls["finalize"].append(kw) or None,
    )
    monkeypatch.setattr(
        "core.paper_evidence.collect_daily_evidence",
        lambda **kw: calls["collect"].append(kw) or None,
    )
    monkeypatch.setattr(
        "core.scheduler.WatchlistManager",
        lambda cfg: SimpleNamespace(resolve=lambda: ["005930"]),
    )
    return calls


def _frozen_datetime(frozen):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    return _Frozen


def test_premarket_finalize_after_chuseok_targets_last_trading_day(
    monkeypatch, calendar, evidence_calls
):
    """9/29 장전 finalize는 휴장일 9/28이 아니라 직전 거래일 9/23을 확정한다."""
    monkeypatch.setattr(
        "core.scheduler.datetime", _frozen_datetime(datetime(2026, 9, 29, 8, 55))
    )

    class _StopCollector:
        def __init__(self, *a, **kw):
            raise RuntimeError("테스트: 장전 분석 단계는 여기서 멈춘다")

    monkeypatch.setattr("core.data_collector.DataCollector", _StopCollector)

    _scheduler()._run_pre_market()

    assert [c["date"].date() for c in evidence_calls["finalize"]] == [date(2026, 9, 23)]


@pytest.mark.parametrize(
    "now, holidays, expected",
    [
        (datetime(2026, 9, 29, 8, 55), CHUSEOK_2026_WITH_0928, date(2026, 9, 23)),
        (datetime(2026, 9, 29, 8, 55), CHUSEOK_2026_WITH_0928 - {"2026-09-28"}, date(2026, 9, 28)),
        (datetime(2026, 10, 6, 8, 55), CHUSEOK_2026_WITH_0928, date(2026, 10, 2)),
        (datetime(2026, 10, 12, 8, 55), CHUSEOK_2026_WITH_0928, date(2026, 10, 8)),
        (datetime(2026, 9, 22, 8, 55), CHUSEOK_2026_WITH_0928, date(2026, 9, 21)),
    ],
)
def test_previous_trading_day_skips_weekday_holidays(calendar, now, holidays, expected):
    calendar["holidays"] = set(holidays)
    assert _scheduler()._previous_trading_day(now).date() == expected


def test_finalize_refuses_holiday_without_raising(calendar, evidence_calls, captured_warnings):
    """비거래일 finalize는 경고만 남기고 None — 가짜 real_paper 기록을 만들지 않는다."""
    assert _scheduler()._finalize_evidence_for(datetime(2026, 9, 28)) is None
    assert evidence_calls["finalize"] == []
    assert any("2026-09-28" in m and "KRX 거래일이 아니" in m for m in captured_warnings)


def test_post_market_collect_refuses_holiday(calendar, evidence_calls, captured_warnings):
    assert _scheduler()._collect_post_market_evidence(datetime(2026, 9, 24, 15, 40)) is None
    assert evidence_calls["collect"] == []
    assert any("2026-09-24" in m for m in captured_warnings)


def test_post_market_collect_runs_on_trading_day(calendar, evidence_calls):
    _scheduler()._collect_post_market_evidence(datetime(2026, 9, 23, 15, 40))
    assert [c["date"].date() for c in evidence_calls["collect"]] == [date(2026, 9, 23)]
    assert evidence_calls["collect"][0]["evidence_mode"] == "real_paper"


@pytest.mark.parametrize(
    "start, end, expected",
    [
        ("2026-09-23", "2026-09-29", 1),
        ("2026-10-02", "2026-10-06", 1),
        ("2026-04-10", "2026-04-15", 3),  # 휴장일 없는 구간은 평일 수와 같다
        ("2026-09-23", "2026-09-23", 0),
    ],
)
def test_runtime_staleness_counts_krx_trading_days(calendar, start, end, expected):
    from core.paper_runtime import _trading_days_between

    assert _trading_days_between(start, end) == expected


def test_runtime_state_is_not_stale_after_chuseok(calendar, monkeypatch, tmp_path):
    """9/23 증거로 9/29에 평가하면 신선하다(예전엔 평일 4일로 세서 신규 진입 차단)."""
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    from core.paper_evidence import _append_jsonl

    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    assert pr.PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS == 1

    strategy = "audit_calendar_s"
    path = tmp_path / "paper_evidence" / f"daily_evidence_{strategy}.jsonl"
    for day_number, day in enumerate(["2026-09-21", "2026-09-22", "2026-09-23"], start=1):
        _append_jsonl(path, {
            "date": day, "day_number": day_number, "strategy": strategy,
            "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
            "daily_return": 0.1, "cumulative_return": 0.3, "mdd": -1.0,
            "position_count": 2, "total_trades": 1,
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {"completeness": 1.0}, "raw_fill_rate": 1.0,
            "reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
            "duplicate_blocked_count": 0, "restart_recovery_count": 0,
            "anomalies": [], "cross_validation_warnings": [], "status": "normal",
            "record_version": 2, "schema_version": 2, "diagnostics": [],
        })

    state = pr.get_paper_runtime_state(strategy, as_of_date="2026-09-29")

    assert state.metrics["evidence_stale_trading_days"] == 1
    assert state.metrics["evidence_fresh"] is True
    assert not any("stale_evidence" in r for r in state.reasons)
    assert state.state == "normal"
    assert "entry" in state.allowed_actions


def test_pilot_business_days_warns_when_calendar_unavailable(monkeypatch, captured_warnings):
    """달력 로드 실패는 조용히 평일 수로 떨어지지 않고 경고를 남긴다."""
    import core.trading_hours as th
    from core.paper_pilot import _business_days_between

    class _Broken:
        def __init__(self, *a, **kw):
            raise RuntimeError("holidays.yaml 손상")

    monkeypatch.setattr(th, "TradingHours", _Broken)

    assert _business_days_between("2026-09-23", "2026-09-29") == 4
    assert any("KRX 거래일 달력 로드 실패" in m for m in captured_warnings)
