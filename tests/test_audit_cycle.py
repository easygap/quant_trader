"""일일 바스켓 사이클 회귀 테스트 (2026-09-23 점검).

- 휴장일 실행은 매매 없이 스냅샷만 남긴다(추석 평일에도 일일 태스크는 돈다).
- 결측 보충은 실제로 호출돼야 한다 — 8/27 도입 이래 정의되지 않은 이름(baskets_cfg)
  때문에 매 사이클 실패했지만 경고 로그로만 남아 한 달간 아무도 몰랐다. 함수가 아니라
  배선을 테스트한다.
- 보충은 계정이 생기기 전 날을 채우지 않고, 입금 경계를 실제 측정 시각으로 잡는다.
"""

from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _args(dry_run=False, force_rebalance=False):
    return SimpleNamespace(
        basket="kr_test", dry_run=dry_run, confirm_live=False,
        force_rebalance=force_rebalance,
    )


@pytest.fixture
def cycle(monkeypatch):
    """사이클 협력자를 모킹하고 (fake_rebalancer, 호출 순서 기록)을 돌려준다."""
    from config.config_loader import Config

    Config._instance = None
    order = []
    fake_rb = MagicMock()
    fake_rb.portfolio_mgr.initial_capital = 300_000
    fake_rb.get_status_report.side_effect = lambda: order.append("status") or "status"
    fake_rb.should_rebalance.return_value = (True, "드리프트")
    fake_rb.plan_rebalance.return_value = []
    fake_rb.plan_risk_exits.return_value = []
    monkeypatch.setattr(
        "core.basket_rebalancer.BasketRebalancer", MagicMock(return_value=fake_rb)
    )
    monkeypatch.setattr("core.notifier.Notifier", MagicMock())
    monkeypatch.setattr("database.repositories.get_trade_history", lambda **kw: [])
    calls = {}

    def _fake_backfill(config, account_key, initial_capital, since, until, mode="paper"):
        order.append("backfill")
        calls["backfill"] = dict(
            account_key=account_key, initial_capital=initial_capital,
            since=since, until=until, mode=mode,
        )
        return []

    monkeypatch.setattr("core.snapshot_backfill.backfill_account", _fake_backfill)
    return fake_rb, order, calls


# ------------------------------------------------------------------ 휴장일

def test_holiday_run_skips_trading_but_saves_snapshot(cycle, monkeypatch):
    import main

    fake_rb, _, _ = cycle
    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: True)

    main.run_rebalance(_args())

    fake_rb.plan_risk_exits.assert_not_called()
    fake_rb.should_rebalance.assert_not_called()
    fake_rb.plan_rebalance.assert_not_called()
    fake_rb.execute.assert_not_called()
    fake_rb.save_daily_nav_snapshot.assert_called_once()


def test_holiday_run_can_be_forced(cycle, monkeypatch):
    import main

    fake_rb, _, _ = cycle
    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: True)

    main.run_rebalance(_args(force_rebalance=True))

    fake_rb.plan_risk_exits.assert_called_once()
    fake_rb.plan_rebalance.assert_called_once()


def test_trading_day_runs_normal_path(cycle, monkeypatch):
    import main

    fake_rb, _, _ = cycle
    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: False)

    main.run_rebalance(_args())

    fake_rb.plan_risk_exits.assert_called_once()
    fake_rb.plan_rebalance.assert_called_once()


def test_market_closed_helper_uses_calendar():
    import main
    from config.config_loader import Config

    Config._instance = None
    cfg = Config.get()
    assert main._market_closed_today(cfg, datetime(2026, 9, 24, 10, 7)) is True   # 추석
    assert main._market_closed_today(cfg, datetime(2026, 9, 28, 10, 7)) is False  # 대체 아님
    assert main._market_closed_today(cfg, datetime(2026, 9, 26, 10, 7)) is True   # 토요일


# ------------------------------------------------------------------ 결측 보충 배선

def test_backfill_is_wired_with_resolved_capital_before_status(cycle, monkeypatch):
    import main

    _, order, calls = cycle
    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: False)

    main.run_rebalance(_args())

    from datetime import timedelta
    from zoneinfo import ZoneInfo

    assert "backfill" in calls, "결측 보충이 호출되지 않았다"
    assert calls["backfill"]["initial_capital"] == 300_000.0
    assert calls["backfill"]["account_key"] == "basket_rebalance:kr_test"
    kst_today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    assert calls["backfill"]["until"] == kst_today - timedelta(days=1)
    # 오버레이 판단(상태 보고에서 계산·캐시)보다 먼저 보충돼야 한다
    assert order.index("backfill") < order.index("status")


def test_backfill_failure_is_recorded_as_event(cycle, monkeypatch):
    import main
    import core.cycle_observability as co

    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: False)

    def _boom(*a, **k):
        raise RuntimeError("시세 조회 실패")

    monkeypatch.setattr("core.snapshot_backfill.backfill_account", _boom)
    recorded = []
    monkeypatch.setattr(
        co, "record_event_once_per_day",
        lambda event_type, message, **kw: recorded.append((event_type, kw)) or True,
    )

    main.run_rebalance(_args())

    assert [e for e, _ in recorded] == ["SNAPSHOT_BACKFILL_FAILED"]
    assert recorded[0][1]["strategy"] == "basket_rebalance:kr_test"


def test_backfill_not_called_on_dry_run(cycle, monkeypatch):
    import main

    _, _, calls = cycle
    monkeypatch.setattr(main, "_market_closed_today", lambda config, now: False)
    main.run_rebalance(_args(dry_run=True))
    assert "backfill" not in calls


# ------------------------------------------------------------------ 보충 범위·입금 경계

def _add_trade(key, day, symbol="069500", qty=1, price=100_000.0):
    from database.models import TradeHistory, get_session, init_database

    init_database()
    s = get_session()
    try:
        s.add(TradeHistory(
            account_key=key, symbol=symbol, action="BUY", price=price, quantity=qty,
            total_amount=price * qty, mode="paper",
            executed_at=datetime.combine(day, time(10, 7)),
        ))
        s.commit()
    finally:
        s.close()


def test_backfill_does_not_fill_days_before_account_existed(monkeypatch):
    """계정 첫 활동일(7/10) 이전의 평일은 '결측'이 아니다."""
    import core.snapshot_backfill as sb
    from database.repositories import save_portfolio_snapshot

    key = "basket_rebalance:t_preinception"
    _add_trade(key, date(2026, 7, 10))
    for d in (date(2026, 7, 10), date(2026, 7, 13), date(2026, 7, 15)):
        save_portfolio_snapshot(
            total_value=300_000, cash=200_000, invested=100_000,
            account_key=key, snapshot_date=datetime.combine(d, time()), mode="paper",
        )
    monkeypatch.setattr(sb, "historical_mark", lambda symbol, day: 100_000.0)

    filled = sb.backfill_account(
        None, key, 300_000, date(2026, 7, 1), date(2026, 7, 15), dry_run=True,
    )

    assert [f["date"] for f in filled] == [date(2026, 7, 14)]


def test_backfill_returns_nothing_for_account_without_activity():
    import core.snapshot_backfill as sb

    assert sb.backfill_account(
        None, "basket_rebalance:t_no_activity", 300_000,
        date(2026, 9, 1), date(2026, 9, 22), dry_run=True,
    ) == []


def test_deposit_after_previous_snapshot_is_not_booked_as_return():
    """금 10:07 스냅샷 이후 17:19 입금 → 월요일 복원분의 수익률은 0이어야 한다."""
    import core.snapshot_backfill as sb
    from database.repositories import record_cash_flow, save_portfolio_snapshot

    key = "basket_rebalance:t_flow_after"
    fri, mon = date(2026, 9, 18), date(2026, 9, 21)
    save_portfolio_snapshot(
        total_value=300_000, cash=300_000, invested=0, cumulative_return=0.0,
        account_key=key, snapshot_date=datetime.combine(fri, time()), mode="paper",
        measured_at=datetime.combine(fri, time(10, 7)),
    )
    record_cash_flow(100_000, account_key=key, occurred_at=datetime.combine(fri, time(17, 19)))

    snap = sb.reconstruct_snapshot(None, key, mon, 300_000)

    assert snap["total_value"] == 400_000
    assert snap["cumulative_return"] == pytest.approx(0.0, abs=1e-9)
    assert snap["daily_return"] == pytest.approx(0.0, abs=1e-9)
    assert snap["mdd"] == pytest.approx(0.0, abs=1e-9)


def test_reconstructed_row_is_measured_at_end_of_its_day():
    """복원 행의 측정 시각이 저장 시각(다음 날)이면, 다음 날 아침 입금이 빠진다."""
    import core.snapshot_backfill as sb
    from database.models import PortfolioSnapshot, get_session
    from database.repositories import (
        get_cash_flow_total_between,
        record_cash_flow,
        save_portfolio_snapshot,
    )

    key = "basket_rebalance:t_flow_before_cycle"
    fri, mon, tue = date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)
    save_portfolio_snapshot(
        total_value=300_000, cash=300_000, invested=0, cumulative_return=0.0,
        account_key=key, snapshot_date=datetime.combine(fri, time()), mode="paper",
        measured_at=datetime.combine(fri, time(10, 7)),
    )
    record_cash_flow(100_000, account_key=key, occurred_at=datetime.combine(tue, time(8, 0)))

    filled = sb.backfill_account(None, key, 300_000, mon, mon)
    assert [f["date"] for f in filled] == [mon]

    s = get_session()
    try:
        row = s.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.account_key == key,
            PortfolioSnapshot.date == datetime.combine(mon, time()),
        ).one()
        measured = row.created_at
        assert row.reconstructed is True
        assert row.cumulative_return == pytest.approx(0.0, abs=1e-9)
    finally:
        s.close()
    assert measured.date() == mon
    # 화요일 사이클의 유입 구간(월 복원 행 측정 시각, 화 10:07]에 화 08:00 입금이 들어가야 한다
    assert get_cash_flow_total_between(
        key, measured, datetime.combine(tue, time(10, 7)),
    ) == pytest.approx(100_000)


# ------------------------------------------------------------------ 중복 억제 이벤트

def test_event_once_per_day_dedupes_same_cause():
    from core.cycle_observability import record_event_once_per_day

    kw = dict(strategy="basket_rebalance:t_dedupe", mode="paper")
    assert record_event_once_per_day("ORDER_REJECTED", "거부 A", symbol="005930",
                                     dedupe_key="사유 A", **kw) is True
    assert record_event_once_per_day("ORDER_REJECTED", "거부 A", symbol="005930",
                                     dedupe_key="사유 A", **kw) is False
    # 원인이 다르면 따로 남는다
    assert record_event_once_per_day("ORDER_REJECTED", "거부 B", symbol="005930",
                                     dedupe_key="사유 B", **kw) is True
    assert record_event_once_per_day("ORDER_REJECTED", "거부 A", symbol="035720",
                                     dedupe_key="사유 A", **kw) is True


# ------------------------------------------------------------------ 커버리지 게이트

def test_coverage_gate_uses_measured_days_not_reconstructed():
    """보충이 매번 100%로 메우면 게이트가 '사이클이 실제로 돌았는가'를 못 본다."""
    from core.basket_evaluation import evaluate_basket_paper_operation

    r = evaluate_basket_paper_operation(
        operation_start=date(2026, 6, 10), today=date(2026, 9, 10),
        trading_days_total=60, snapshot_days=60, reconstructed_days=6,
        pending_failed_orders=0, total_costs=0, initial_capital=10_000_000,
    )
    assert r["snapshot_coverage"] == pytest.approx(1.0)   # 표시용 전체 커버리지는 그대로
    assert r["measured_coverage"] == pytest.approx(0.9)
    assert r["verdict"] == "FAIL_REVIEW"
    assert any("실측 스냅샷 커버리지" in i and "사후 복원 6일 제외" in i for i in r["issues"])


def test_coverage_gate_passes_with_few_reconstructed_days():
    from core.basket_evaluation import evaluate_basket_paper_operation

    r = evaluate_basket_paper_operation(
        operation_start=date(2026, 6, 10), today=date(2026, 9, 10),
        trading_days_total=60, snapshot_days=60, reconstructed_days=2,
        pending_failed_orders=0, total_costs=0, initial_capital=10_000_000,
    )
    assert r["verdict"] == "PASS_CANDIDATE"
