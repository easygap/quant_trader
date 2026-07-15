"""전역 거래 HALT 킬스위치 회귀 테스트."""

import json
from types import SimpleNamespace

import pytest

from config.config_loader import Config


@pytest.fixture(autouse=True)
def isolated_trading_halt_state():
    """각 테스트가 append-only HALT 상태를 다음 테스트에 유출하지 않게 한다."""
    from database.models import (
        FailedOrder,
        OperationEvent,
        OrderRecord,
        PendingOrderGuard,
        Position,
        TradeHistory,
        get_session,
        init_database,
    )

    Config._instance = None
    init_database()
    models = (
        OperationEvent,
        OrderRecord,
        PendingOrderGuard,
        FailedOrder,
        TradeHistory,
        Position,
    )

    def clean():
        session = get_session()
        try:
            for model in models:
                session.query(model).delete()
            session.commit()
        finally:
            session.close()

    clean()
    yield
    clean()
    Config._instance = None


def test_trading_halt_repository_is_append_only_and_requires_confirmed_clear():
    from database.models import OperationEvent, get_session
    from database.repositories import (
        TRADING_HALT_CLEARED,
        TRADING_HALT_SET,
        TRADING_HALT_STRATEGY,
        clear_trading_halt,
        get_trading_halt_state,
        set_trading_halt,
    )

    assert get_trading_halt_state()["halted"] is False

    halted = set_trading_halt(
        "증권사 장애로 긴급 청산",
        source="test.operator",
        mode="live",
        detail={"ticket": "INC-42"},
    )
    assert halted["halted"] is True
    assert halted["reason"] == "증권사 장애로 긴급 청산"
    assert halted["source"] == "test.operator"

    with pytest.raises(ValueError, match="confirmed=True"):
        clear_trading_halt("확인 없는 해제")
    assert get_trading_halt_state()["halted"] is True

    cleared = clear_trading_halt(
        "잔고와 주문 상태 대조 완료",
        source="test.operator",
        mode="live",
        confirmed=True,
        expected_active_event_id=halted["event_id"],
    )
    assert cleared["halted"] is False
    assert get_trading_halt_state()["event_id"] == cleared["event_id"]

    session = get_session()
    try:
        events = (
            session.query(OperationEvent)
            .filter(OperationEvent.strategy == TRADING_HALT_STRATEGY)
            .order_by(OperationEvent.id.asc())
            .all()
        )
        assert [event.event_type for event in events] == [
            TRADING_HALT_SET,
            TRADING_HALT_CLEARED,
        ]
        assert [event.severity for event in events] == ["critical", "warning"]
        set_detail = json.loads(events[0].detail)
        clear_detail = json.loads(events[1].detail)
        assert set_detail == {
            "ticket": "INC-42",
            "reason": "증권사 장애로 긴급 청산",
            "source": "test.operator",
            "global": True,
        }
        assert clear_detail["confirmed"] is True
        assert clear_detail["reason"] == "잔고와 주문 상태 대조 완료"
    finally:
        session.close()


def test_active_halt_blocks_both_public_buy_paths_and_records_audit_events():
    from core.order_executor import OrderExecutor
    from database.models import OperationEvent, get_session
    from database.repositories import set_trading_halt

    halt = set_trading_halt(
        "긴급 청산 진행 중",
        source="test.operator",
        mode="live",
    )
    executor = OrderExecutor(account_key="halt_buy_test")

    sized = executor.execute_buy(
        symbol="005930",
        price=60_000,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="HALT 회귀 테스트",
        strategy="scoring",
    )
    fixed = executor.execute_buy_quantity(
        symbol="000660",
        price=120_000,
        quantity=1,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="HALT 회귀 테스트",
        strategy="target_weight_rotation",
    )

    for result in (sized, fixed):
        assert result["success"] is False
        assert result["trading_halt_blocked"] is True
        assert result["trading_halt_check_failed"] is False
        assert result["trading_halt_state"]["event_id"] == halt["event_id"]
        assert "긴급 청산 진행 중" in result["reason"]

    session = get_session()
    try:
        blocked = session.query(OperationEvent).filter(
            OperationEvent.event_type == "TRADING_HALT_BUY_BLOCKED"
        ).all()
        assert len(blocked) == 2
        assert {event.symbol for event in blocked} == {"005930", "000660"}
        assert all(event.severity == "critical" for event in blocked)
    finally:
        session.close()


def test_buy_paths_fail_closed_when_halt_state_lookup_fails(monkeypatch):
    import core.order_executor as executor_module
    from database.models import OperationEvent, get_session

    executor = executor_module.OrderExecutor(account_key="halt_lookup_failure_test")

    def fail_lookup():
        raise RuntimeError("HALT DB unavailable")

    monkeypatch.setattr(executor_module, "get_trading_halt_state", fail_lookup)

    sized = executor.execute_buy(
        symbol="005930",
        price=60_000,
        capital=10_000_000,
        available_cash=10_000_000,
        strategy="scoring",
    )
    fixed = executor.execute_buy_quantity(
        symbol="000660",
        price=120_000,
        quantity=1,
        capital=10_000_000,
        available_cash=10_000_000,
        strategy="target_weight_rotation",
    )

    for result in (sized, fixed):
        assert result["success"] is False
        assert result["trading_halt_blocked"] is True
        assert result["trading_halt_check_failed"] is True
        assert "fail-closed" in result["reason"]

    session = get_session()
    try:
        failures = session.query(OperationEvent).filter(
            OperationEvent.event_type == "TRADING_HALT_CHECK_FAILED"
        ).all()
        assert len(failures) == 2
        assert all(event.severity == "critical" for event in failures)
    finally:
        session.close()


def test_sell_bypasses_halt_lookup_and_remains_available(monkeypatch):
    import core.order_executor as executor_module
    from database.repositories import get_position, save_position, set_trading_halt

    set_trading_halt(
        "청산 전용 상태",
        source="test.operator",
        mode="live",
    )
    executor = executor_module.OrderExecutor(account_key="halt_sell_test")
    executor.config.risk_params["position_limits"]["min_holding_days"] = 0
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=1,
        strategy="scoring",
        account_key="halt_sell_test",
    )

    def lookup_must_not_run():
        raise AssertionError("SELL은 HALT DB를 조회하면 안 됩니다")

    monkeypatch.setattr(executor_module, "get_trading_halt_state", lookup_must_not_run)
    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        quantity=1,
        reason="긴급 전량 청산",
        strategy="emergency_liquidate",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="halt_sell_test") is None


def test_clear_halt_cli_requires_confirmation_and_persists_operator_audit():
    from database.repositories import get_trading_halt_state, set_trading_halt
    from tools.clear_trading_halt import main as clear_halt_main

    original = set_trading_halt(
        "운영자 긴급 청산",
        source="main.run_emergency_liquidate",
        mode="live",
    )

    assert clear_halt_main(["--reason", "잔고 대조 완료"]) == 2
    assert get_trading_halt_state()["event_id"] == original["event_id"]
    assert get_trading_halt_state()["halted"] is True

    assert clear_halt_main([
        "--confirm",
        "--reason",
        "잔고·미체결 수동 대조 완료",
    ]) == 0
    cleared = get_trading_halt_state()
    assert cleared["halted"] is False
    assert cleared["source"] == "tools.clear_trading_halt"
    assert cleared["detail"]["confirmed"] is True
    assert cleared["detail"]["previous_event_id"] == original["event_id"]


def test_live_liquidation_aborts_before_broker_sync_if_halt_cannot_persist(monkeypatch):
    import database.repositories as repositories
    import main as main_module

    config = SimpleNamespace(trading={"mode": "live"})
    calls = []
    monkeypatch.setattr(main_module.Config, "get", lambda: config)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

    def fail_halt_write(*args, **kwargs):
        calls.append("halt")
        raise RuntimeError("HALT persistence failed")

    monkeypatch.setattr(repositories, "set_trading_halt", fail_halt_write)
    monkeypatch.setattr(
        main_module,
        "_sync_live_positions_before_liquidation",
        lambda cfg: calls.append("broker_sync"),
    )

    with pytest.raises(RuntimeError, match="HALT persistence failed"):
        main_module.run_emergency_liquidate(SimpleNamespace(confirm_live=True))

    assert calls == ["halt"]
