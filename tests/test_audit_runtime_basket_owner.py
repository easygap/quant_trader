"""스케줄러는 바스켓을 거래하지 않는다 (감사: runtime-scheduler-basket-premarket-divergent).

예전 스케줄러는 장전(08:50~09:00)에 바스켓 리밸런싱을 실행했다. live 주문은 거래
시간 가드에 전부 거부되고, paper는 전일 종가로 체결됐으며, CLI 사이클의 손절·하루
1회 거래 가드·스냅샷 보충이 빠져 있었다. 이제 바스켓 실행 경로는 일일 CLI
(main.py --mode rebalance) 하나뿐이고, 스케줄러는 그 사실을 로그로만 남긴다.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from loguru import logger


def _sample_ohlcv(days=60):
    rng = np.random.default_rng(7)
    dates = pd.date_range(end=datetime.now(), periods=days, freq="B")
    prices = 50000 * np.cumprod(1 + rng.normal(0.0002, 0.01, days))
    return pd.DataFrame({
        "open": prices, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices, "volume": rng.integers(100_000, 1_000_000, days),
    }, index=dates)


@pytest.fixture
def captured_warnings():
    messages = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


def test_pre_market_never_builds_or_executes_a_basket(monkeypatch, captured_warnings):
    """live 모드 장전 단계에서도 BasketRebalancer를 만들지 않고, 실행 경로를 로그로 알린다."""
    from core.scheduler import Scheduler

    constructed = []

    class FakeRebalancer:
        @staticmethod
        def get_enabled_baskets():
            return ["kr_diversified_hold", "kr_pocket"]

        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))

    sample = _sample_ohlcv()

    class FakeCollector:
        def fetch_stock(self, symbol, start=None, end=None):
            return sample.copy()

        def check_source_consistency(self, mode="paper"):
            return []

        def has_kis_fallback_symbols(self):
            return []

    class FakeStrategy:
        def generate_signal(self, df, symbol=None):
            return {"signal": "HOLD", "score": 0, "close": float(df["close"].iloc[-1])}

    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
    monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)
    monkeypatch.setattr(
        "core.market_regime.check_market_regime",
        lambda config, collector=None: {"allow_buys": True, "position_scale": 1.0},
    )
    monkeypatch.setattr(
        "core.scheduler.WatchlistManager",
        lambda config: SimpleNamespace(resolve=lambda: ["005930"]),
    )

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._mode = "live"
    scheduler._live_gate_validated = True
    scheduler.discord = MagicMock()
    scheduler._get_strategy = lambda: FakeStrategy()
    scheduler._maybe_record_dashboard_signal = lambda *a, **kw: None

    scheduler._run_pre_market()

    assert constructed == []
    owner_logs = [m for m in captured_warnings if "--mode rebalance" in m]
    assert len(owner_logs) == 1
    assert "kr_diversified_hold" in owner_logs[0] and "kr_pocket" in owner_logs[0]
    assert "거래하지 않습니다" in owner_logs[0]


def test_no_enabled_baskets_logs_nothing(monkeypatch, captured_warnings):
    from core.scheduler import Scheduler

    class FakeRebalancer:
        @staticmethod
        def get_enabled_baskets():
            return []

    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
    Scheduler.__new__(Scheduler)._log_basket_execution_owner()

    assert not [m for m in captured_warnings if "--mode rebalance" in m]


def test_scheduler_has_no_basket_execution_path():
    """스케줄러 소스에 바스켓 주문 실행 경로가 되살아나지 않게 고정한다."""
    from core.scheduler import Scheduler

    assert not hasattr(Scheduler, "_run_basket_rebalance_check")
    source = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("plan_rebalance(", ".execute(orders", "save_daily_nav_snapshot("):
        assert forbidden not in source


def test_baskets_yaml_header_names_the_cli_as_only_path():
    header = (Path(__file__).resolve().parents[1] / "config" / "baskets.yaml").read_text(
        encoding="utf-8"
    ).split("baskets:", 1)[0]

    assert "스케줄러 장전 단계에서 실행" not in header
    assert "python main.py --mode rebalance" in header
    assert "바스켓을 거래하지 않습니다" in header
