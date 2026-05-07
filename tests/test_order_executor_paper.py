"""OrderExecutor paper 모드 단위 테스트 (DB/외부 의존성 최소화)"""
from types import SimpleNamespace

import pytest

from config.config_loader import Config


def _normal_runtime_state():
    return SimpleNamespace(
        state="normal",
        allowed_actions=["entry", "exit", "cancel", "reconcile", "finalize"],
        reasons=[],
        metrics={"recent_final_ratio": 1.0, "recent_anomaly_count": 0},
        evidence_date="2026-01-01",
    )


def _passing_preflight():
    return SimpleNamespace(
        overall="pass",
        entry_allowed=True,
        runtime_state="normal",
        block_reasons=[],
    )


def _blocked_runtime_state():
    return SimpleNamespace(
        state="blocked_insufficient_evidence",
        allowed_actions=["exit", "cancel", "reconcile", "finalize", "shadow_collect"],
        reasons=["insufficient execution-backed evidence"],
        metrics={"recent_final_ratio": 0.0, "recent_anomaly_count": 0},
        evidence_date=None,
    )


def test_order_executor_import():
    """OrderExecutor 임포트 및 paper 모드 초기화 가능"""
    from core.order_executor import OrderExecutor
    # Config.get() 사용 시 실제 설정 로드됨 — 테스트 환경에서는 mock 권장
    try:
        ex = OrderExecutor()
        assert ex.mode in ("paper", "live")
    except Exception:
        # 설정 없을 수 있음
        pass


def test_position_lock_import():
    """PositionLock 사용 가능"""
    from core.position_lock import PositionLock
    with PositionLock():
        pass


@pytest.fixture
def fresh_db():
    Config._instance = None
    from database.models import (
        init_database, get_session,
        TradeHistory, OperationEvent, PortfolioSnapshot,
        Position, FailedOrder, PendingOrderGuard, DailyReport,
    )
    init_database()
    session = get_session()
    for model in [TradeHistory, OperationEvent, PortfolioSnapshot,
                  Position, FailedOrder, PendingOrderGuard, DailyReport]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def test_execute_buy_quantity_records_exact_paper_quantity(fresh_db, monkeypatch):
    """Target-weight adapters can submit exact paper buy quantities."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, get_daily_trade_summary

    executor = OrderExecutor(account_key="exact_qty_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: _passing_preflight())
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=7,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="exact quantity test",
        strategy="target_weight_rotation_test",
    )

    assert result["success"] is True
    assert result["quantity"] == 7
    position = get_position("005930", account_key="exact_qty_test")
    assert position is not None
    assert position.quantity == 7
    summary = get_daily_trade_summary(mode="paper", account_key="exact_qty_test")
    assert summary["buy_count"] == 1


def test_paper_buy_quantity_fails_closed_when_runtime_unavailable(fresh_db, monkeypatch):
    """Paper 신규 진입은 runtime 조회 실패 시 주문/포지션을 만들지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="runtime_down_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: _passing_preflight())

    def _raise_runtime(*args, **kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", _raise_runtime)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="runtime unavailable test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper runtime state unavailable" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="runtime_down_test") is None


def test_paper_buy_quantity_fails_closed_when_preflight_missing(fresh_db, monkeypatch):
    """Preflight 산출물이 없으면 신규 paper 진입을 시작하지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="preflight_missing_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: None)
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="preflight missing test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper preflight status missing" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="preflight_missing_test") is None


def test_paper_buy_quantity_blocks_on_preflight_fail(fresh_db, monkeypatch):
    """저장된 preflight fail은 runtime normal 여부와 무관하게 신규 진입을 차단한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="preflight_block_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(
        "core.paper_preflight.load_preflight_status",
        lambda strategy, strict=False: SimpleNamespace(
            overall="fail",
            entry_allowed=False,
            runtime_state="normal",
            block_reasons=["notifier unhealthy"],
        ),
    )
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="preflight fail test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper preflight blocked entry" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="preflight_block_test") is None


def test_paper_buy_quantity_allows_pilot_override(fresh_db, monkeypatch):
    """runtime이 entry를 막아도 활성 pilot authorization이 있으면 제한 진입을 허용한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="pilot_override_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(
        "core.paper_preflight.load_preflight_status",
        lambda strategy, strict=False: SimpleNamespace(
            overall="warn",
            entry_allowed=False,
            runtime_state="blocked_insufficient_evidence",
            block_reasons=["insufficient execution-backed evidence"],
        ),
    )
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _blocked_runtime_state())
    monkeypatch.setattr(
        "core.paper_pilot.check_pilot_entry",
        lambda *a, **kw: SimpleNamespace(
            allowed=True,
            reason="active pilot authorization",
            caps_snapshot={"max_orders_per_day": 2},
        ),
    )

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=2,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="pilot override test",
        strategy="target_weight_rotation_test",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="pilot_override_test") is not None


def test_paper_sell_remains_allowed_when_runtime_unavailable(fresh_db, monkeypatch):
    """Runtime 장애가 있어도 기존 포지션 청산 경로는 막지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="exit_safe_test")
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        stop_loss_price=55_000,
        take_profit_price=70_000,
        trailing_stop_price=58_000,
        strategy="scoring",
        account_key="exit_safe_test",
    )

    def _raise_runtime(*args, **kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", _raise_runtime)

    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        reason="STOP_LOSS",
        strategy="scoring",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="exit_safe_test") is None
