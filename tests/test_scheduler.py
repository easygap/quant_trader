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
        def get_current_price(self, symbol):
            return None

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


def test_quant_auto_entry_env_overrides_config(monkeypatch):
    """QUANT_AUTO_ENTRY 환경변수가 config의 auto_entry=false를 true로 오버라이드."""
    from core.scheduler import Scheduler

    monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
    scheduler = Scheduler(strategy_name="scoring")
    assert scheduler.auto_entry is True


def test_quant_auto_entry_env_false_keeps_false(monkeypatch):
    """QUANT_AUTO_ENTRY=false면 auto_entry=false 유지."""
    from core.scheduler import Scheduler

    monkeypatch.setenv("QUANT_AUTO_ENTRY", "false")
    scheduler = Scheduler(strategy_name="scoring")
    assert scheduler.auto_entry is False


def test_quant_auto_entry_env_absent_uses_config():
    """QUANT_AUTO_ENTRY 미설정 시 config 기본값(false) 사용."""
    import os
    from core.scheduler import Scheduler

    os.environ.pop("QUANT_AUTO_ENTRY", None)
    scheduler = Scheduler(strategy_name="scoring")
    assert scheduler.auto_entry is False
