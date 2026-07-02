"""core/cycle_observability.py — 사이클 관측성 단위 테스트.

find_snapshot_gaps / format_gap_alert 는 순수 함수(외부 상태 없음).
record_cycle_event / detect_snapshot_gaps_for_account 는 DB를 쓰므로 격리 DB에서 검증.
"""
from datetime import date, datetime

from core.cycle_observability import (
    find_snapshot_gaps,
    format_gap_alert,
    record_cycle_event,
)


class TestFindSnapshotGaps:
    def test_no_gaps(self):
        days = [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
        assert find_snapshot_gaps(days, days) == []

    def test_finds_missing_day(self):
        days = [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
        snaps = [date(2026, 6, 10), date(2026, 6, 12)]
        assert find_snapshot_gaps(days, snaps) == [date(2026, 6, 11)]

    def test_sorted_output(self):
        days = [date(2026, 6, 12), date(2026, 6, 10), date(2026, 6, 11)]
        gaps = find_snapshot_gaps(days, [])
        assert gaps == [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]

    def test_normalizes_datetime(self):
        # datetime과 date가 섞여도 날짜로 정규화해 비교
        days = [datetime(2026, 6, 10, 10, 0), date(2026, 6, 11)]
        snaps = [date(2026, 6, 10)]
        assert find_snapshot_gaps(days, snaps) == [date(2026, 6, 11)]

    def test_extra_snapshots_ignored(self):
        # 검사 대상이 아닌 날의 스냅샷은 무시(gap 판정에 영향 없음)
        days = [date(2026, 6, 11)]
        snaps = [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
        assert find_snapshot_gaps(days, snaps) == []


class TestFormatGapAlert:
    def test_includes_today_flag(self):
        msg = format_gap_alert("kr_x", [date(2026, 6, 26)], today=date(2026, 6, 26))
        assert "오늘 포함" in msg
        assert "kr_x" in msg
        assert "1일" in msg

    def test_prior_gap_no_today_flag(self):
        msg = format_gap_alert("kr_x", [date(2026, 6, 26)], today=date(2026, 6, 29))
        assert "오늘 포함" not in msg
        assert "재실행 권장" in msg

    def test_truncates_many_gaps(self):
        gaps = [date(2026, 6, d) for d in range(1, 10)]  # 9일
        msg = format_gap_alert("kr_x", gaps, today=date(2026, 6, 10))
        assert "9일" in msg          # 총 개수
        assert "외 4일" in msg       # 최근 5개만 표기, 나머지 4일 요약


class TestRecordCycleEvent:
    def test_writes_event_row(self):
        from database.models import OperationEvent, get_session, init_database

        init_database()
        ok = record_cycle_event(
            "CYCLE_START", "테스트 사이클 시작", strategy="basket_rebalance:test_x", mode="paper",
        )
        assert ok is True
        session = get_session()
        try:
            row = (
                session.query(OperationEvent)
                .filter(OperationEvent.strategy == "basket_rebalance:test_x")
                .filter(OperationEvent.event_type == "CYCLE_START")
                .order_by(OperationEvent.id.desc())
                .first()
            )
            assert row is not None
            assert row.message == "테스트 사이클 시작"
        finally:
            session.close()

    def test_never_raises_on_bad_input(self):
        # 관측 실패가 사이클을 막으면 안 된다 — 예외 대신 False 반환
        assert record_cycle_event("CYCLE_START", None) in (True, False)


class TestDetectSnapshotGapsForAccount:
    """DB 스냅샷 + 영업일 판정을 엮어 결측 영업일을 찾는 수집 함수."""

    def _add_snap(self, session, acct, d):
        from database.models import PortfolioSnapshot
        session.add(PortfolioSnapshot(
            account_key=acct, date=datetime(d.year, d.month, d.day),
            total_value=1_000_000, cash=0, invested=1_000_000,
        ))

    def test_detects_missing_trading_day(self, monkeypatch):
        import core.cycle_observability as co
        from database.models import get_session, init_database

        init_database()
        acct = "basket_rebalance:test_gap_x"
        session = get_session()
        try:
            # 6/30(화) 결측, 나머지는 스냅샷 있음
            for d in (date(2026, 6, 29), date(2026, 7, 1), date(2026, 7, 2)):
                self._add_snap(session, acct, d)
            session.commit()
        finally:
            session.close()

        class _FakeTH:
            def __init__(self, cfg):
                pass

            def is_trading_day(self, d):
                return d.weekday() < 5  # 월~금만 영업일(휴장일 무시 — 테스트 단순화)

        monkeypatch.setattr("core.trading_hours.TradingHours", _FakeTH)
        gaps = co.detect_snapshot_gaps_for_account(
            config=None, account_key=acct, today=date(2026, 7, 2),
        )
        assert date(2026, 6, 30) in gaps
        assert date(2026, 6, 29) not in gaps
        assert date(2026, 7, 2) not in gaps

    def test_no_snapshots_returns_empty(self, monkeypatch):
        # 운영 전(스냅샷 0건)이면 gap 판정 대상 아님 → 빈 목록
        import core.cycle_observability as co
        from database.models import init_database

        init_database()

        class _FakeTH:
            def __init__(self, cfg):
                pass

            def is_trading_day(self, d):
                return d.weekday() < 5

        monkeypatch.setattr("core.trading_hours.TradingHours", _FakeTH)
        gaps = co.detect_snapshot_gaps_for_account(
            config=None, account_key="basket_rebalance:never_ran", today=date(2026, 7, 2),
        )
        assert gaps == []

    def test_pre_operation_days_not_flagged(self, monkeypatch):
        # 운영 시작(첫 스냅샷) 이전 영업일은 결측이 아니다
        import core.cycle_observability as co
        from database.models import get_session, init_database

        init_database()
        acct = "basket_rebalance:test_start_clamp"
        session = get_session()
        try:
            # 첫 스냅샷이 7/1 → 6/29·6/30은 운영 전이라 결측 아님
            self._add_snap(session, acct, date(2026, 7, 1))
            self._add_snap(session, acct, date(2026, 7, 2))
            session.commit()
        finally:
            session.close()

        class _FakeTH:
            def __init__(self, cfg):
                pass

            def is_trading_day(self, d):
                return d.weekday() < 5

        monkeypatch.setattr("core.trading_hours.TradingHours", _FakeTH)
        gaps = co.detect_snapshot_gaps_for_account(
            config=None, account_key=acct, today=date(2026, 7, 2),
        )
        assert gaps == []  # 6/29, 6/30은 첫 스냅샷(7/1) 이전이라 제외
