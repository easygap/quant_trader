"""장중 재스캔·쿨다운 해제 재스캔 후보의 신선도 회귀 테스트 (감사: runtime-rescan-candidates-never-stale).

두 재스캔 경로의 진입 후보에 timestamp가 없어 30분 경과 후보 폐기가 적용되지
않았고(기본값 now), _signal_at이 없어 신호→주문 지연도 기록되지 않았다. 쿨다운
해제 후보에는 시장 국면 스케일도 없었다. 네트워크는 호출하지 않는다.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

T0 = datetime(2026, 9, 23, 10, 0)


def _sample_ohlcv(days=60):
    rng = np.random.default_rng(11)
    dates = pd.date_range(end=T0, periods=days, freq="B")
    prices = 50000 * np.cumprod(1 + rng.normal(0.0002, 0.01, days))
    return pd.DataFrame({
        "open": prices, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices, "volume": rng.integers(100_000, 1_000_000, days),
    }, index=dates)


class _BuyStrategy:
    def generate_signal(self, df, symbol=None):
        return {"signal": "BUY", "close": 50_000, "atr": 1_000, "score": 80}


def _freeze(monkeypatch, moment):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr("core.scheduler.datetime", _Frozen)


@pytest.fixture
def scheduler(monkeypatch):
    from core.scheduler import Scheduler

    sample = _sample_ohlcv()

    class FakeCollector:
        def fetch_stock(self, symbol, start=None, end=None):
            return sample.copy()

    monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)
    monkeypatch.setattr(
        "core.market_regime.check_market_regime",
        lambda config, collector=None: {"allow_buys": True, "position_scale": 0.7},
    )
    monkeypatch.setattr(
        "core.scheduler.WatchlistManager",
        lambda cfg: SimpleNamespace(resolve=lambda: ["005930"]),
    )
    monkeypatch.setattr(
        "core.scheduler.get_position", lambda symbol, account_key="", mode="paper": None,
    )

    s = Scheduler(strategy_name="scoring")
    # live 모드로 두어 paper runtime/preflight 가드(별도 테스트 대상)를 건너뛴다.
    s._mode = "live"
    s._ledger_mode = "live"
    s._entry_candidates = []
    s._get_strategy = lambda: _BuyStrategy()
    s._maybe_record_dashboard_signal = lambda *a, **kw: None
    s.discord = MagicMock()
    s.portfolio = SimpleNamespace(
        get_portfolio_summary=lambda: {
            "total_value": 10_000_000, "cash": 10_000_000, "current_value": 0,
        }
    )
    s.blackswan = SimpleNamespace(get_recovery_scale=lambda: 1.0)
    s.executed = []

    class _Executor:
        def execute_buy(self, **kwargs):
            s.executed.append(kwargs)
            return {"success": True, "symbol": kwargs["symbol"]}

    s._get_or_create_executor = lambda: _Executor()
    return s


def test_intraday_rescan_candidate_carries_timestamp_and_signal_time(monkeypatch, scheduler):
    _freeze(monkeypatch, T0)
    scheduler._rescan_for_new_entries()

    assert len(scheduler._entry_candidates) == 1
    candidate = scheduler._entry_candidates[0]
    assert candidate["timestamp"] == T0
    assert candidate["_signal_at"] == T0
    assert candidate["market_regime_scale"] == 0.7


def test_post_cooldown_candidate_carries_timestamp_and_regime_scale(monkeypatch, scheduler):
    _freeze(monkeypatch, T0)
    scheduler._market_regime_scale = 0.5

    scheduler._run_post_cooldown_rescan()

    assert len(scheduler._entry_candidates) == 1
    candidate = scheduler._entry_candidates[0]
    assert candidate["timestamp"] == T0
    assert candidate["_signal_at"] == T0
    assert candidate["market_regime_scale"] == 0.5


@pytest.mark.parametrize("rescan", ["_rescan_for_new_entries", "_run_post_cooldown_rescan"])
def test_rescan_candidate_older_than_30_minutes_is_discarded(monkeypatch, scheduler, rescan):
    _freeze(monkeypatch, T0)
    getattr(scheduler, rescan)()
    assert scheduler._entry_candidates

    _freeze(monkeypatch, T0 + timedelta(minutes=31))
    scheduler._execute_entry_candidates()

    assert scheduler.executed == []
    assert scheduler._entry_candidates == []


def test_fresh_rescan_candidate_records_signal_latency(monkeypatch, scheduler):
    _freeze(monkeypatch, T0)
    scheduler._rescan_for_new_entries()

    _freeze(monkeypatch, T0 + timedelta(minutes=10))
    scheduler._execute_entry_candidates()

    assert len(scheduler.executed) == 1
    assert scheduler.executed[0]["signal_at"] == T0
