"""
Scheduler 장전/장중/장마감 시뮬레이션 테스트.
- 네트워크·KIS API 호출을 모킹하여 단위 테스트로 실행 가능.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

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


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeKIS:
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

    def fake_get_all(account_key=None, mode="paper"):
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


def test_live_monitoring_blocks_new_entries_when_broker_sync_fails(monkeypatch):
    """live 장중 KIS↔DB 동기화가 미통과하면 신규 진입만 보류하고 exit 점검은 유지한다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("api.kis_api.KISApi", _FakeKIS)
    monkeypatch.setattr("core.scheduler.PositionLock", _NoopLock)

    scheduler = Scheduler(strategy_name="scoring")
    monkeypatch.setattr(
        scheduler.config, "get_account_no", lambda strategy="": "12345678-01"
    )
    old_mode = scheduler.config.trading.get("mode")
    exit_checked = {"value": False}
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler.auto_entry = True
        scheduler._entry_candidates = [{"symbol": "005930", "price": 50000}]
        scheduler.portfolio = SimpleNamespace(
            sync_with_broker=lambda: {
                "ok": False,
                "mismatches": [{"symbol": "005930"}],
                "message": "포지션 불일치 1건",
            }
        )
        scheduler.blackswan = SimpleNamespace(
            consume_cooldown_ended_flag=lambda: False,
            is_on_cooldown=lambda: False,
        )
        scheduler.discord = MagicMock()
        scheduler._log_monitoring_watchlist_preflight = lambda: None
        scheduler._maybe_recheck_market_regime = lambda: None
        scheduler._execute_entry_candidates = lambda: pytest.fail(
            "broker sync failure must block entry execution"
        )
        scheduler._rescan_for_new_entries = lambda: pytest.fail(
            "broker sync failure must block entry rescan"
        )
        scheduler._check_exit_signals = lambda kis=None: exit_checked.__setitem__("value", True)
        scheduler._update_dynamic_stop_losses = lambda: None
        scheduler._publish_dashboard_runtime_state = lambda kis=None: None

        scheduler._run_monitoring()

        assert exit_checked["value"] is True
        assert scheduler._last_broker_sync_ok is False
        assert "포지션 불일치" in scheduler._last_broker_sync_message
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_live_monitoring_allows_entries_after_broker_sync_ok(monkeypatch):
    """live 장중 KIS↔DB 동기화가 통과하면 신규 진입 실행과 재스캔을 진행한다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("api.kis_api.KISApi", _FakeKIS)
    monkeypatch.setattr("core.scheduler.PositionLock", _NoopLock)

    scheduler = Scheduler(strategy_name="scoring")
    monkeypatch.setattr(
        scheduler.config, "get_account_no", lambda strategy="": "12345678-01"
    )
    old_mode = scheduler.config.trading.get("mode")
    calls = {"entry": 0, "rescan": 0, "exit": 0}
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler.auto_entry = True
        scheduler._entry_candidates = [{"symbol": "005930", "price": 50000}]
        scheduler.portfolio = SimpleNamespace(
            sync_with_broker=lambda: {"ok": True, "mismatches": [], "message": "일치"}
        )
        scheduler.blackswan = SimpleNamespace(
            consume_cooldown_ended_flag=lambda: False,
            is_on_cooldown=lambda: False,
        )
        scheduler.discord = MagicMock()
        scheduler._log_monitoring_watchlist_preflight = lambda: None
        scheduler._maybe_recheck_market_regime = lambda: None
        scheduler._execute_entry_candidates = lambda: calls.__setitem__("entry", calls["entry"] + 1)
        scheduler._rescan_for_new_entries = lambda: calls.__setitem__("rescan", calls["rescan"] + 1)
        scheduler._check_exit_signals = lambda kis=None: calls.__setitem__("exit", calls["exit"] + 1)
        scheduler._update_dynamic_stop_losses = lambda: None
        scheduler._publish_dashboard_runtime_state = lambda kis=None: None

        scheduler._run_monitoring()

        assert calls == {"entry": 1, "rescan": 1, "exit": 1}
        assert scheduler._last_broker_sync_ok is True
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_monitoring_checks_existing_exits_before_new_entries(monkeypatch):
    """급락 첫 사이클에서 기존 노출 점검이 신규 BUY보다 먼저다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("api.kis_api.KISApi", _FakeKIS)
    monkeypatch.setattr("core.scheduler.PositionLock", _NoopLock)

    scheduler = Scheduler(strategy_name="scoring")
    calls = []
    scheduler.auto_entry = True
    scheduler._entry_candidates = [{"symbol": "005930", "price": 50_000}]
    scheduler.blackswan = SimpleNamespace(
        consume_cooldown_ended_flag=lambda: False,
        is_on_cooldown=lambda: False,
    )
    scheduler.discord = MagicMock()
    scheduler._log_monitoring_watchlist_preflight = lambda: None
    scheduler._maybe_recheck_market_regime = lambda: None
    scheduler._check_exit_signals = lambda kis=None: calls.append("exit")
    scheduler._execute_entry_candidates = lambda: calls.append("entry")
    scheduler._update_dynamic_stop_losses = lambda: None
    scheduler._rescan_for_new_entries = lambda: calls.append("rescan")
    scheduler._publish_dashboard_runtime_state = lambda kis=None: None

    scheduler._run_monitoring()

    assert calls == ["exit", "entry", "rescan"]


def test_live_entry_loop_halts_when_order_requires_reconcile(monkeypatch):
    """live 주문 체결 확인이 보류되면 같은 루프의 남은 신규 진입을 중단한다."""
    from core.scheduler import Scheduler

    sample = _sample_ohlcv(60)
    calls = []
    op_events = []

    class FakeCollector:
        def fetch_stock(self, symbol, start=None, end=None):
            return sample.copy()

    class FakeStrategy:
        def generate_signal(self, df, symbol=None):
            return {"signal": "BUY", "close": 50_000, "atr": 1_000, "score": 82}

    class FakeExecutor:
        def execute_buy(self, **kwargs):
            calls.append(kwargs["symbol"])
            if kwargs["symbol"] == "005930":
                return {
                    "success": False,
                    "requires_reconcile": True,
                    "order_pending": True,
                    "symbol": "005930",
                    "reason": "실전 주문은 접수됐지만 체결 확인 전이라 DB 반영을 보류했습니다.",
                    "order_id": "local-1",
                    "broker_order_id": "kis-1",
                    "order_status": "ACKED",
                    "execution_check": {"reason": "live_fill_unconfirmed"},
                }
            return {"success": True, "symbol": kwargs["symbol"]}

    monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)
    monkeypatch.setattr(
        "core.market_regime.check_market_regime",
        lambda config, collector: {"allow_buys": True, "position_scale": 1.0},
    )
    monkeypatch.setattr(
        "core.scheduler.get_position",
        lambda symbol, account_key="", mode="paper": None,
    )
    monkeypatch.setattr(
        "core.scheduler._log_op",
        lambda *args, **kwargs: op_events.append((args, kwargs)),
    )

    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler._entry_candidates = [
            {"symbol": "005930", "price": 50_000},
            {"symbol": "000660", "price": 120_000},
        ]
        scheduler.portfolio = SimpleNamespace(
            get_portfolio_summary=lambda: {
                "total_value": 1_000_000,
                "cash": 1_000_000,
                "current_value": 0,
            }
        )
        scheduler.blackswan = SimpleNamespace(get_recovery_scale=lambda: 1.0)
        scheduler.discord = MagicMock()
        scheduler._get_or_create_executor = lambda: FakeExecutor()
        scheduler._get_strategy = lambda: FakeStrategy()

        scheduler._execute_entry_candidates()

        assert calls == ["005930"]
        assert scheduler._entry_candidates == [{"symbol": "000660", "price": 120_000}]
        assert scheduler._last_broker_sync_ok is False
        assert "pending reconcile" in scheduler._last_broker_sync_message
        scheduler.discord.send_trade_alert.assert_not_called()
        scheduler.discord.send_message.assert_called_once()
        assert scheduler.discord.send_message.call_args.kwargs["critical"] is True
        assert op_events
        assert op_events[-1][0][0] == "LIVE_RECONCILE_BLOCK"
        assert op_events[-1][1]["detail"]["remaining_candidates"] == 1
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_entry_candidate_revalidation_failure_holds_candidate_without_order(monkeypatch):
    """시그널 재검증 실패 시 오래된 후보로 주문하지 않고 다음 루프까지 보류한다."""
    from core.scheduler import Scheduler

    calls = []
    op_events = []

    class FailingCollector:
        def fetch_stock(self, symbol, start=None, end=None):
            raise RuntimeError("temporary data outage")

    class FakeStrategy:
        def generate_signal(self, df, symbol=None):
            pytest.fail("데이터 재조회 실패 시 전략 신호 계산까지 진행하면 안 됨")

    class FakeExecutor:
        def execute_buy(self, **kwargs):
            calls.append(kwargs)
            return {"success": True, "symbol": kwargs["symbol"]}

    monkeypatch.setattr("core.data_collector.DataCollector", FailingCollector)
    monkeypatch.setattr(
        "core.market_regime.check_market_regime",
        lambda config, collector: {"allow_buys": True, "position_scale": 1.0},
    )
    monkeypatch.setattr(
        "core.scheduler.get_position",
        lambda symbol, account_key="", mode="paper": None,
    )
    monkeypatch.setattr(
        "core.scheduler._log_op",
        lambda *args, **kwargs: op_events.append((args, kwargs)),
    )

    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    candidate = {
        "symbol": "005930",
        "price": 50_000,
        "timestamp": datetime.now(),
        "reason": "pre-market candidate",
    }
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler._entry_candidates = [candidate]
        scheduler.portfolio = SimpleNamespace(
            get_portfolio_summary=lambda: {
                "total_value": 1_000_000,
                "cash": 1_000_000,
                "current_value": 0,
            }
        )
        scheduler.blackswan = SimpleNamespace(get_recovery_scale=lambda: 1.0)
        scheduler.discord = MagicMock()
        scheduler._get_or_create_executor = lambda: FakeExecutor()
        scheduler._get_strategy = lambda: FakeStrategy()

        scheduler._execute_entry_candidates()

        assert calls == []
        assert scheduler._entry_candidates == [candidate]
        assert op_events
        assert op_events[-1][0][0] == "ENTRY_REVALIDATION_BLOCK"
        assert op_events[-1][1]["symbol"] == "005930"
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_live_monitoring_skips_rescan_after_entry_requires_reconcile(monkeypatch):
    """live 신규 진입 실행 중 체결 확인이 보류되면 같은 루프 재스캔도 막는다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("api.kis_api.KISApi", _FakeKIS)
    monkeypatch.setattr("core.scheduler.PositionLock", _NoopLock)

    scheduler = Scheduler(strategy_name="scoring")
    monkeypatch.setattr(
        scheduler.config, "get_account_no", lambda strategy="": "12345678-01"
    )
    old_mode = scheduler.config.trading.get("mode")
    calls = {"entry": 0, "rescan": 0, "exit": 0}
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler.auto_entry = True
        scheduler._entry_candidates = [{"symbol": "005930", "price": 50000}]
        scheduler.portfolio = SimpleNamespace(
            sync_with_broker=lambda: {"ok": True, "mismatches": [], "message": "일치"}
        )
        scheduler.blackswan = SimpleNamespace(
            consume_cooldown_ended_flag=lambda: False,
            is_on_cooldown=lambda: False,
        )
        scheduler.discord = MagicMock()
        scheduler._log_monitoring_watchlist_preflight = lambda: None
        scheduler._maybe_recheck_market_regime = lambda: None

        def mark_reconcile_block():
            calls["entry"] += 1
            scheduler._last_broker_sync_ok = False
            scheduler._last_broker_sync_message = "live order pending reconcile: 005930"

        scheduler._execute_entry_candidates = mark_reconcile_block
        scheduler._rescan_for_new_entries = lambda: calls.__setitem__("rescan", calls["rescan"] + 1)
        scheduler._check_exit_signals = lambda kis=None: calls.__setitem__("exit", calls["exit"] + 1)
        scheduler._update_dynamic_stop_losses = lambda: None
        scheduler._publish_dashboard_runtime_state = lambda kis=None: None

        scheduler._run_monitoring()

        assert calls == {"entry": 1, "rescan": 0, "exit": 1}
        assert scheduler._last_broker_sync_ok is False
        assert "pending reconcile" in scheduler._last_broker_sync_message
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_exit_signals_skip_invalid_current_price(monkeypatch):
    """현재가가 0/누락이면 갭다운·블랙스완·손절 판단과 매도 주문을 모두 보류한다."""
    from core.scheduler import Scheduler

    op_events = []
    position = SimpleNamespace(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        bought_at=None,
    )

    class FakeKIS:
        def get_current_price(self, symbol):
            return {"price": 0, "prev_close": 60_000}

    class FakeExecutor:
        def check_stop_loss_take_profit(self, *args, **kwargs):
            pytest.fail("invalid price must not reach stop-loss check")

        def execute_sell(self, *args, **kwargs):
            pytest.fail("invalid price must not submit sell order")

    monkeypatch.setattr(
        "core.scheduler.get_all_positions",
        lambda account_key=None, mode="paper": [position],
    )
    monkeypatch.setattr(
        "core.scheduler._log_op",
        lambda *args, **kwargs: op_events.append((args, kwargs)),
    )

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._get_or_create_executor = lambda: FakeExecutor()
    scheduler.blackswan = SimpleNamespace(
        check_stock=lambda *args, **kwargs: pytest.fail(
            "invalid price must not reach black-swan check"
        )
    )
    scheduler.discord = MagicMock()

    scheduler._check_exit_signals(kis=FakeKIS())

    assert op_events
    assert op_events[-1][0][0] == "PRICE_DATA_BLOCK"
    scheduler.discord.send_message.assert_not_called()
    scheduler.discord.send_trade_alert.assert_not_called()


def test_exit_signals_records_missing_current_price(monkeypatch):
    """브로커 현재가 응답이 없을 때도 조용히 넘기지 않고 차단 이벤트를 남긴다."""
    from core.scheduler import Scheduler

    op_events = []
    position = SimpleNamespace(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        bought_at=None,
    )

    class MissingPriceKIS:
        def get_current_price(self, symbol):
            return None

    class FakeExecutor:
        def check_stop_loss_take_profit(self, *args, **kwargs):
            pytest.fail("missing price must not reach stop-loss check")

        def execute_sell(self, *args, **kwargs):
            pytest.fail("missing price must not submit sell order")

    monkeypatch.setattr(
        "core.scheduler.get_all_positions",
        lambda account_key=None, mode="paper": [position],
    )
    monkeypatch.setattr(
        "core.scheduler._log_op",
        lambda *args, **kwargs: op_events.append((args, kwargs)),
    )

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._get_or_create_executor = lambda: FakeExecutor()
    scheduler.blackswan = SimpleNamespace(
        check_stock=lambda *args, **kwargs: pytest.fail(
            "missing price must not reach black-swan check"
        )
    )
    scheduler.discord = MagicMock()

    scheduler._check_exit_signals(kis=MissingPriceKIS())

    assert op_events
    assert op_events[-1][0][0] == "PRICE_DATA_BLOCK"
    assert "현재가 미수신" in op_events[-1][0][1]
    scheduler.discord.send_message.assert_not_called()
    scheduler.discord.send_trade_alert.assert_not_called()


def test_blackswan_preempts_gap_exit_and_activates_cooldown(monkeypatch):
    """-5% 이하 급락은 gap continue 전에 BlackSwan cooldown을 켜야 한다."""
    from core.blackswan_detector import BlackSwanDetector
    from core.scheduler import Scheduler

    position = SimpleNamespace(
        symbol="005930",
        avg_price=100_000,
        quantity=3,
        bought_at=None,
    )

    class CrashPriceKIS:
        def get_current_price(self, symbol):
            return {"price": 94_000, "prev_close": 100_000, "open": 94_000}

    sell_calls = []

    class FakeExecutor:
        def execute_sell(self, symbol, price, **kwargs):
            sell_calls.append({"symbol": symbol, "price": price, **kwargs})
            return {"success": True}

        def check_stop_loss_take_profit(self, *args, **kwargs):
            pytest.fail("BlackSwan 청산 후 일반 stop 점검으로 진행하면 안 됨")

    detector_config = SimpleNamespace(
        risk_params={
            "blackswan": {
                "single_stock_threshold": -0.05,
                "portfolio_threshold": -0.03,
                "consecutive_days": 3,
                "consecutive_threshold": -0.02,
                "cooldown_minutes": 60,
                "recovery_minutes": 120,
                "recovery_scale": 0.5,
            }
        },
        trading={},
    )

    monkeypatch.setattr(
        "core.scheduler.get_all_positions",
        lambda account_key=None, mode="paper": [position],
    )
    op_events = []
    monkeypatch.setattr(
        "core.scheduler._log_op",
        lambda *args, **kwargs: op_events.append((args, kwargs)),
    )

    scheduler = Scheduler(strategy_name="scoring")
    old_gap = scheduler.config.risk_params.get("gap_risk")
    try:
        scheduler.config.risk_params["gap_risk"] = {
            "enabled": True,
            "gap_down_threshold": -0.03,
        }
        scheduler.blackswan = BlackSwanDetector(detector_config)
        scheduler._get_or_create_executor = lambda: FakeExecutor()
        scheduler.discord = MagicMock()

        scheduler._check_exit_signals(kis=CrashPriceKIS())

        assert scheduler.blackswan.is_on_cooldown() is True
        assert len(sell_calls) == 1
        assert sell_calls[0]["reason"] == "블랙스완 긴급 매도"
        assert op_events and op_events[0][0][0] == "BLACKSWAN"
        messages = [call.args[0] for call in scheduler.discord.send_message.call_args_list]
        assert any("블랙스완 발동" in message for message in messages)
        assert all("갭다운 청산" not in message for message in messages)
    finally:
        if old_gap is None:
            scheduler.config.risk_params.pop("gap_risk", None)
        else:
            scheduler.config.risk_params["gap_risk"] = old_gap


def test_intraday_decline_is_not_misclassified_as_gap_down(monkeypatch):
    """시가가 보합이면 장중 -4%를 gap 긴급 청산으로 오인하지 않는다."""
    from core.scheduler import Scheduler

    position = SimpleNamespace(
        symbol="005930",
        avg_price=100_000,
        quantity=3,
        bought_at=None,
    )

    class IntradayDropKIS:
        def get_current_price(self, symbol):
            return {
                "price": 96_000,
                "prev_close": 100_000,
                "open": 100_000,
            }

    class FakeExecutor:
        def execute_sell(self, *args, **kwargs):
            pytest.fail("장중 하락만으로 gap 청산 주문을 내면 안 됨")

        def check_stop_loss_take_profit(self, *args, **kwargs):
            return {"action": None}

    monkeypatch.setattr(
        "core.scheduler.get_all_positions",
        lambda account_key=None, mode="paper": [position],
    )

    scheduler = Scheduler(strategy_name="scoring")
    old_gap = scheduler.config.risk_params.get("gap_risk")
    try:
        scheduler.config.risk_params["gap_risk"] = {
            "enabled": True,
            "gap_down_threshold": -0.03,
        }
        scheduler._get_or_create_executor = lambda: FakeExecutor()
        scheduler.discord = MagicMock()

        scheduler._check_exit_signals(kis=IntradayDropKIS())

        assert scheduler.blackswan.is_on_cooldown() is False
        scheduler.discord.send_message.assert_not_called()
        scheduler.discord.send_trade_alert.assert_not_called()
    finally:
        if old_gap is None:
            scheduler.config.risk_params.pop("gap_risk", None)
        else:
            scheduler.config.risk_params["gap_risk"] = old_gap


def test_scheduler_post_market_runs():
    """장마감: _run_post_market 호출 시 DB 저장·디스코드 시도만 하고 예외 없이 완료."""
    from core.scheduler import Scheduler

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._run_post_market()
    assert True


def test_collect_snapshot_prices_fails_if_any_position_price_is_missing(monkeypatch):
    """일부 종목의 가격 누락을 평균단가 폴백으로 숨기지 않는다."""
    from types import SimpleNamespace

    from core.scheduler import Scheduler

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.strategy_name = "scoring"
    scheduler._ledger_mode = "paper"
    scheduler.config = SimpleNamespace(get_account_no=lambda key: "00000000-00")
    monkeypatch.setattr(
        "core.scheduler.get_all_positions",
        lambda account_key=None, mode="paper": [
            SimpleNamespace(symbol="005930"),
            SimpleNamespace(symbol="000660"),
        ],
    )

    class FakeKIS:
        def __init__(self, account_no=None):
            self.account_no = account_no

        def get_current_price(self, symbol):
            return {"price": 55000} if symbol == "005930" else None

    monkeypatch.setattr("api.kis_api.KISApi", FakeKIS)

    with pytest.raises(RuntimeError, match="000660 현재가"):
        scheduler._collect_snapshot_prices()


def test_post_market_stops_when_validated_snapshot_cannot_be_saved():
    """스냅샷 실패 뒤 부정확한 일일 리포트를 계속 생성하지 않는다."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.scheduler import Scheduler

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.strategy_name = "scoring"
    scheduler.portfolio = MagicMock()
    scheduler.portfolio.save_daily_snapshot.return_value = False
    scheduler.discord = MagicMock()
    scheduler.config = SimpleNamespace(trading={"mode": "paper"})
    scheduler._collect_snapshot_prices = lambda: {"005930": 55000}

    scheduler._run_post_market()

    scheduler.portfolio.get_portfolio_summary.assert_not_called()
    scheduler.discord.send_daily_report.assert_not_called()


def test_scheduler_startup_recovery_paper_mode_no_crash(monkeypatch):
    """재시작 복구: paper 모드에서 KIS 없이 startup_recovery가 예외 없이 끝난다."""
    from core.scheduler import Scheduler

    monkeypatch.setattr("core.scheduler.get_pending_failed_orders", lambda: [])
    scheduler = Scheduler(strategy_name="scoring")
    scheduler.startup_recovery()
    assert True


def test_scheduler_startup_recovery_reports_open_order_lookup_failure(monkeypatch):
    """live 재시작 복구에서 KIS 미체결 조회 실패 시 자동 보정을 건너뛴다."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.scheduler import Scheduler

    class FakeExecutor:
        def __init__(self, config=None, account_key="", *, live_gate_validated=False):
            self.last_open_order_reconcile_status = {
                "checked": False,
                "reason": "kis_open_orders_query_failed",
                "orders": [],
            }

        def reconcile_open_orders_after_crash(self):
            return []

    monkeypatch.setattr("core.scheduler.get_pending_failed_orders", lambda: [])
    monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)
    op_events = []
    sync_calls = []

    def fake_log_op(*args, **kwargs):
        op_events.append((args, kwargs))

    def fake_sync_with_broker(auto_correct=True):
        sync_calls.append(auto_correct)
        return {"ok": True, "mismatches": [], "corrected": [], "message": "일치"}

    monkeypatch.setattr("core.scheduler._log_op", fake_log_op)
    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler.discord = MagicMock()
        scheduler.portfolio = SimpleNamespace(sync_with_broker=fake_sync_with_broker)
        scheduler.trading_hours = SimpleNamespace(is_market_open=lambda: False)
        scheduler._restart_recovery_count = 0

        scheduler.startup_recovery()

        assert sync_calls == [False]
        assert scheduler._restart_recovery_count == 1
        scheduler.discord.send_message.assert_called()
        message = scheduler.discord.send_message.call_args.args[0]
        assert "KIS 미체결 조회 실패" in message
        assert scheduler.discord.send_message.call_args.kwargs["critical"] is True
        assert op_events
        detail = op_events[-1][1]["detail"]
        assert detail["broker_sync_ok"] is True
        assert detail["broker_sync_mode"] == "check_only_auto_correct_skipped"
        assert detail["broker_sync_auto_correct"] is False
        assert detail["broker_sync_skip_reason"] == "kis_open_orders_query_failed"
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_live_scheduler_passes_gate_validation_to_order_executor(monkeypatch):
    """live Scheduler가 생성한 OrderExecutor에 live gate 통과 상태를 전달한다."""
    from core.scheduler import Scheduler

    captured = {}

    class FakeExecutor:
        def __init__(self, config=None, account_key="", *, live_gate_validated=False):
            captured["account_key"] = account_key
            captured["live_gate_validated"] = live_gate_validated

    monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)

    scheduler = Scheduler(strategy_name="scoring")
    scheduler._mode = "live"
    scheduler._live_gate_validated = True

    executor = scheduler._get_or_create_executor()
    assert executor is scheduler._order_executor
    assert executor.blackswan is scheduler.blackswan
    assert captured == {
        "account_key": "scoring",
        "live_gate_validated": True,
    }


def test_live_scheduler_basket_rebalance_uses_basket_gate_and_scope(monkeypatch):
    """live 장전 바스켓 리밸런싱은 바스켓별 승인 단위로 gate/account/order tag를 맞춘다."""
    from core.scheduler import Scheduler

    calls = {}

    class FakeRebalancer:
        @staticmethod
        def get_enabled_baskets():
            return ["test_basket"]

        def __init__(self, basket_name, config, account_key="", execution_strategy=""):
            calls["basket_name"] = basket_name
            calls["config"] = config
            calls["account_key"] = account_key
            calls["execution_strategy"] = execution_strategy
            self.portfolio_mgr = SimpleNamespace(
                sync_with_broker=MagicMock(return_value={"ok": True, "message": "일치"})
            )
            calls["sync_mock"] = self.portfolio_mgr.sync_with_broker

        def should_rebalance(self):
            calls["should_rebalance"] = True
            return True, "드리프트"

        def plan_rebalance(self):
            calls["plan_rebalance"] = True
            return ["order"]

        def execute(self, orders, dry_run=False, live_confirmed=False):
            calls["execute"] = {
                "orders": orders,
                "dry_run": dry_run,
                "live_confirmed": live_confirmed,
            }
            return {"executed": 1, "skipped": 0, "failed": 0}

    gate = MagicMock(return_value=[])
    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
    monkeypatch.setattr("core.scheduler.check_live_readiness_gate", gate)

    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler._live_gate_validated = True
        scheduler.discord = MagicMock()

        scheduler._run_basket_rebalance_check()

        gate.assert_called_once_with(scheduler.config, "basket_rebalance:test_basket")
        assert calls["basket_name"] == "test_basket"
        assert calls["account_key"] == "basket_rebalance:test_basket"
        assert calls["execution_strategy"] == "basket_rebalance:test_basket"
        calls["sync_mock"].assert_called_once_with()
        assert calls["should_rebalance"] is True
        assert calls["plan_rebalance"] is True
        assert calls["execute"] == {
            "orders": ["order"],
            "dry_run": False,
            "live_confirmed": True,
        }
        scheduler.discord.send_message.assert_called_once()
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_live_scheduler_basket_rebalance_gate_failure_blocks_before_plan(monkeypatch):
    """live 바스켓 전용 gate가 실패하면 리밸런서 생성과 주문 계획까지 가지 않는다."""
    from core.scheduler import Scheduler

    class FakeRebalancer:
        @staticmethod
        def get_enabled_baskets():
            return ["test_basket"]

        def __init__(self, *args, **kwargs):
            pytest.fail("live basket gate failure must block before rebalancer init")

    gate = MagicMock(return_value=["promotion evidence 부족"])
    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
    monkeypatch.setattr("core.scheduler.check_live_readiness_gate", gate)

    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler._live_gate_validated = True
        scheduler.discord = MagicMock()

        scheduler._run_basket_rebalance_check()

        gate.assert_called_once_with(scheduler.config, "basket_rebalance:test_basket")
        scheduler.discord.send_message.assert_called_once()
        assert scheduler.discord.send_message.call_args.kwargs["critical"] is True
        assert "promotion evidence 부족" in scheduler.discord.send_message.call_args.args[0]
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_live_scheduler_basket_rebalance_sync_failure_blocks_orders(monkeypatch):
    """live 바스켓 리밸런싱 전 브로커 동기화가 실패하면 주문 판단과 실행을 생략한다."""
    from core.scheduler import Scheduler

    calls = {}

    class FakeRebalancer:
        @staticmethod
        def get_enabled_baskets():
            return ["test_basket"]

        def __init__(self, basket_name, config, account_key="", execution_strategy=""):
            calls["account_key"] = account_key
            calls["execution_strategy"] = execution_strategy
            self.portfolio_mgr = SimpleNamespace(
                sync_with_broker=MagicMock(
                    return_value={"ok": False, "message": "position mismatch"}
                )
            )

        def should_rebalance(self):
            pytest.fail("sync failure must block before should_rebalance")

        def plan_rebalance(self):
            pytest.fail("sync failure must block before plan_rebalance")

        def execute(self, *args, **kwargs):
            pytest.fail("sync failure must block before execute")

    monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
    monkeypatch.setattr(
        "core.scheduler.check_live_readiness_gate",
        MagicMock(return_value=[]),
    )

    scheduler = Scheduler(strategy_name="scoring")
    old_mode = scheduler.config.trading.get("mode")
    try:
        scheduler.config.trading["mode"] = "live"
        scheduler._mode = "live"
        scheduler._live_gate_validated = True
        scheduler.discord = MagicMock()

        scheduler._run_basket_rebalance_check()

        assert calls["account_key"] == "basket_rebalance:test_basket"
        assert calls["execution_strategy"] == "basket_rebalance:test_basket"
        scheduler.discord.send_message.assert_called_once()
        assert scheduler.discord.send_message.call_args.kwargs["critical"] is True
        assert "position mismatch" in scheduler.discord.send_message.call_args.args[0]
    finally:
        scheduler.config.trading["mode"] = old_mode


def test_scheduler_skips_next_cycle_after_overrun(monkeypatch):
    from core.scheduler import Scheduler

    monkeypatch.setattr("api.kis_api.KISApi", _FakeKIS)
    scheduler = Scheduler(strategy_name="scoring")
    scheduler.monitor_interval = 1
    scheduler.auto_entry = True
    scheduler._entry_candidates = [{"symbol": "005930", "price": 50000}]
    scheduler.blackswan = SimpleNamespace(
        consume_cooldown_ended_flag=lambda: False,
        is_on_cooldown=lambda: False,
        status_snapshot=lambda: {},
    )
    scheduler.discord = MagicMock()

    def slow_entry():
        import time
        time.sleep(1.1)

    monkeypatch.setattr(scheduler, "_log_monitoring_watchlist_preflight", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_recheck_market_regime", lambda: None)
    monkeypatch.setattr(scheduler, "_execute_entry_candidates", slow_entry)
    monkeypatch.setattr(scheduler, "_check_exit_signals", lambda kis=None: None)
    monkeypatch.setattr(scheduler, "_update_dynamic_stop_losses", lambda: None)
    monkeypatch.setattr(scheduler, "_rescan_for_new_entries", lambda: None)
    monkeypatch.setattr(scheduler, "_publish_dashboard_runtime_state", lambda kis=None: None)
    scheduler._run_monitoring()

    assert scheduler._skip_next_monitor_cycle is True
    assert scheduler._should_monitor() is False
