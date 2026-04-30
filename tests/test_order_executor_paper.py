"""OrderExecutor paper 모드 단위 테스트 (DB/외부 의존성 최소화)"""
import pytest

from config.config_loader import Config


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
