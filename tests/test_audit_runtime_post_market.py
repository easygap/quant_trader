"""스케줄러 장마감 실행 창·일간 수익률 회귀 테스트 (감사: runtime-post-market-window-and-zero-daily-return).

1) 장마감 분기가 hour==15일 때만 열려, 16시 이후 시작·재시작하면 그날 스냅샷·
   리포트·evidence·DB 백업이 조용히 사라졌다.
2) 스케줄러 일일 리포트의 일간 수익률이 0으로 하드코딩돼 있었다(CLI는 TWR 계산).
DB는 conftest가 격리한 임시 DB를 쓴다.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _ClosedHours:
    """장전·장중이 아닌 시각(장마감 분기만 가능)으로 고정한 TradingHours 대역."""

    def is_pre_market(self, dt=None):
        return False

    def is_market_open(self, dt=None):
        return False


def _phase_scheduler():
    from core.scheduler import Scheduler

    s = Scheduler.__new__(Scheduler)
    s.trading_hours = _ClosedHours()
    s._pre_market_done = True
    s._post_market_done = False
    s.post_market_runs = []
    s._run_post_market = lambda: s.post_market_runs.append(True)
    return s


@pytest.mark.parametrize("hour, minute", [(15, 35), (16, 10), (23, 5)])
def test_post_market_runs_once_at_or_after_1535(hour, minute):
    s = _phase_scheduler()
    now = datetime(2026, 9, 23, hour, minute)

    s._run_trading_day_phase(now)
    s._run_trading_day_phase(now + timedelta(minutes=1))

    assert s.post_market_runs == [True]
    assert s._post_market_done is True


def test_post_market_does_not_run_before_1535():
    s = _phase_scheduler()
    s._run_trading_day_phase(datetime(2026, 9, 23, 15, 34))
    assert s.post_market_runs == []
    assert s._post_market_done is False


def _seed_snapshots(account, prev_total, last_total, deposit):
    from database.repositories import record_cash_flow, save_portfolio_snapshot

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    save_portfolio_snapshot(
        total_value=prev_total, cash=prev_total, invested=0,
        account_key=account, snapshot_date=today - timedelta(days=1), mode="paper",
    )
    save_portfolio_snapshot(
        total_value=last_total, cash=last_total, invested=0,
        account_key=account, snapshot_date=today, mode="paper",
    )
    if deposit:
        # 직전 스냅샷 측정(created_at) 뒤의 입금 — TWR 분모에 더해 중화돼야 한다.
        record_cash_flow(deposit, account_key=account, note="audit deposit", mode="paper")


def _report_scheduler(account):
    from core.scheduler import Scheduler

    s = Scheduler.__new__(Scheduler)
    s.strategy_name = account
    s._ledger_mode = "paper"
    return s


def test_report_daily_return_uses_twr_with_deposit_neutralized():
    account = "audit_post_market_twr"
    _seed_snapshots(account, 10_000_000, 10_250_000, 100_000)

    expected = (10_250_000 / (10_000_000 + 100_000) - 1) * 100
    assert _report_scheduler(account)._report_daily_return() == pytest.approx(expected)


def test_report_daily_return_is_zero_with_single_snapshot():
    from database.repositories import save_portfolio_snapshot

    account = "audit_post_market_single"
    save_portfolio_snapshot(
        total_value=1_000_000, cash=1_000_000, invested=0, account_key=account, mode="paper",
    )
    assert _report_scheduler(account)._report_daily_return() == 0.0


def test_post_market_report_card_carries_the_twr_daily_return(monkeypatch):
    """배선 확인: 계산 함수가 있어도 리포트 카드에 실제로 실려야 한다."""
    from core.scheduler import Scheduler

    account = "audit_post_market_card"
    _seed_snapshots(account, 20_000_000, 19_800_000, 0)

    s = Scheduler.__new__(Scheduler)
    s.strategy_name = account
    s._ledger_mode = "paper"
    s._mode = "paper"
    s.config = SimpleNamespace(trading={"mode": "paper"}, risk_params={})
    s.discord = MagicMock()
    s.portfolio = MagicMock()
    s.portfolio.save_daily_snapshot.return_value = True
    s.portfolio.get_portfolio_summary.return_value = {
        "total_value": 19_800_000, "cash": 19_800_000, "realized_pnl": 0,
        "unrealized_pnl": 0, "total_return": -1.0, "mdd": -1.0, "position_count": 0,
    }
    s._collect_snapshot_prices = lambda: {}
    s._check_live_readiness = lambda: None
    s._collect_post_market_evidence = lambda date: None
    s._loop_metrics = SimpleNamespace(
        summary=lambda: {"total_loops": 0, "total_skips": 0}
    )
    monkeypatch.setattr(
        "core.scheduler.get_daily_trade_summary",
        lambda mode, account_key: {
            "total_trades": 0, "buy_count": 0, "sell_count": 0, "realized_pnl": 0,
            "total_commission": 0, "total_tax": 0, "winning_trades": 0, "losing_trades": 0,
        },
    )
    monkeypatch.setattr("core.scheduler.diagnose_live_post_market", lambda **kw: [])
    monkeypatch.setattr("core.scheduler.save_daily_report", lambda **kw: None)
    monkeypatch.setattr("database.backup.run_daily_backup", lambda config: None)
    # 금요일에 돌면 주간 리포트 파일을 쓰지 않게 막는다(실행 요일에 따라 결과가 바뀌지 않게).
    monkeypatch.setattr(
        "monitoring.paper_monitor.WeeklyReportGenerator",
        lambda *a, **kw: SimpleNamespace(generate=lambda weeks_back=1: {}, save=lambda report: None),
    )

    s._run_post_market()

    card = s.discord.send_daily_report.call_args.args[0]
    assert card["daily_return"] == pytest.approx(-1.0)
