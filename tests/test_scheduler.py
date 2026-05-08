"""
Scheduler 장전/장중/장마감 시뮬레이션 테스트.
- 네트워크·KIS API 호출을 모킹하여 단위 테스트로 실행 가능.
"""

from datetime import datetime

import pandas as pd

import pytest


def _sample_ohlcv(days=60):
    import numpy as np
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
    returns = np.random.normal(0.0002, 0.015, days)
    prices = 50000 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": prices * (1 + np.random.uniform(-0.01, 0.01, days)),
        "high": prices * (1 + np.random.uniform(0, 0.02, days)),
        "low": prices * (1 - np.random.uniform(0, 0.02, days)),
        "close": prices,
        "volume": np.random.randint(100000, 3000000, days),
    }, index=dates)
    df.index.name = "date"
    return df


class _MockConfig:
    """테스트용 설정: watchlist만 주고 나머지는 최소."""
    watchlist = ["005930"]
    trading = {"mode": "paper", "auto_entry": False}


def test_scheduler_pre_market_runs_without_network(monkeypatch):
    """장전 준비: DataCollector를 모킹해 네트워크 없이 _run_pre_market 완료."""
    from core.scheduler import Scheduler

    sample = _sample_ohlcv(60)

    class FakeCollector:
        def fetch_korean_stock(self, symbol, start=None, end=None):
            return sample.copy()

        def fetch_stock(self, symbol, start=None, end=None):
            return self.fetch_korean_stock(symbol, start, end)

    monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)
    scheduler = Scheduler(strategy_name="scoring")
    if not scheduler.config.watchlist:
        scheduler.config._settings.setdefault("watchlist", {})["symbols"] = ["005930"]

    scheduler._run_pre_market()
    assert True


def test_scheduler_monitoring_runs_without_api(monkeypatch):
    """장중 모니터링: 포지션 없음 + KIS get_current_price 모킹으로 _run_monitoring 완료."""
    from core.scheduler import Scheduler

    def fake_get_all():
        return []

    monkeypatch.setattr("core.scheduler.get_all_positions", fake_get_all)

    class FakeKIS:
        def __init__(self, account_no=None, *args, **kwargs):
            pass

        def get_current_price(self, symbol):
            return None

        def get_rate_limit_stats(self):
            return {
                "requests_last_60s": 0,
                "minute_utilization_pct": 0.0,
                "max_per_min": 300,
            }

    monkeypatch.setattr("api.kis_api.KISApi", FakeKIS)
    scheduler = Scheduler(strategy_name="scoring")

    scheduler._run_monitoring()
    assert True


def test_scheduler_post_market_runs():
    """장마감: _run_post_market 호출 시 DB 저장·디스코드 시도만 하고 예외 없이 완료."""
    from core.scheduler import Scheduler

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._run_post_market()
    assert True


def test_scheduler_startup_recovery_paper_mode_no_crash(monkeypatch):
    """재시작 복구: paper 모드에서 KIS 없이 startup_recovery가 예외 없이 끝난다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("core.scheduler.get_pending_failed_orders", lambda: [])
    scheduler = Scheduler(strategy_name="scoring")
    scheduler.startup_recovery()
    assert True


def test_scheduler_startup_recovery_reports_open_order_lookup_failure(monkeypatch):
    """live 재시작 복구에서 KIS 미체결 조회 실패를 0건처럼 조용히 넘기지 않는다."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.scheduler import Scheduler

    class FakeExecutor:
        def __init__(self, config=None, account_key=""):
            self.last_open_order_reconcile_status = {
                "checked": False,
                "reason": "kis_open_orders_query_failed",
                "orders": [],
            }

        def reconcile_open_orders_after_crash(self):
            return []

    monkeypatch.setattr("core.scheduler.get_pending_failed_orders", lambda: [])
    monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)
    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler.discord = MagicMock()
        scheduler.portfolio = SimpleNamespace(sync_with_broker=lambda auto_correct=True: None)
        scheduler.trading_hours = SimpleNamespace(is_market_open=lambda: False)
        scheduler._restart_recovery_count = 0

        scheduler.startup_recovery()

        assert scheduler._restart_recovery_count == 1
        scheduler.discord.send_message.assert_called()
        message = scheduler.discord.send_message.call_args.args[0]
        assert "KIS 미체결 조회 실패" in message
        assert scheduler.discord.send_message.call_args.kwargs["critical"] is True
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_scheduler_skips_next_cycle_after_overrun(monkeypatch):
    from core.scheduler import Scheduler

    scheduler = Scheduler(strategy_name="scoring")
    scheduler.monitor_interval = 1
    scheduler.auto_entry = True
    scheduler._entry_candidates = [{"symbol": "005930", "price": 50000}]

    def slow_entry():
        import time
        time.sleep(1.1)

    monkeypatch.setattr(scheduler, "_execute_entry_candidates", slow_entry)
    monkeypatch.setattr(scheduler, "_check_exit_signals", lambda: None)
    scheduler._run_monitoring()

    assert scheduler._skip_next_monitor_cycle is True
    assert scheduler._should_monitor() is False
