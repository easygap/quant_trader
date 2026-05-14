"""PortfolioManager 집계/동기화 테스트."""

import api.kis_api
import core.notifier
import pytest
from core.portfolio_manager import PortfolioManager


class _MockPosition:
    def __init__(self, symbol: str, avg_price: float, quantity: int):
        self.symbol = symbol
        self.avg_price = avg_price
        self.quantity = quantity


class _MockConfig:
    trading = {"mode": "paper"}
    risk_params = {
        "position_sizing": {"initial_capital": 1000000},
        "stop_loss": {"type": "fixed", "fixed_rate": 0.03},
        "take_profit": {"type": "fixed", "fixed_rate": 0.08, "partial_exit": True},
        "trailing_stop": {"enabled": True, "type": "fixed", "fixed_rate": 0.05},
    }
    database = {"sqlite_path": "data/quant_trader.db"}

    def get_account_no(self, key=""):
        return "00000000-00"


class _LiveMockConfig(_MockConfig):
    trading = {"mode": "live"}


@pytest.fixture
def fresh_db():
    from database.models import (
        DailyReport,
        FailedOrder,
        OperationEvent,
        PendingOrderGuard,
        PortfolioSnapshot,
        Position,
        TradeHistory,
        get_session,
        init_database,
    )

    init_database()
    session = get_session()
    for model in [
        TradeHistory,
        OperationEvent,
        PortfolioSnapshot,
        Position,
        FailedOrder,
        PendingOrderGuard,
        DailyReport,
    ]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def test_portfolio_summary_uses_trade_cash_flow(monkeypatch):
    monkeypatch.setattr(
        "core.portfolio_manager.get_all_positions",
        lambda account_key=None: [_MockPosition("005930", 50000, 10)],
    )
    monkeypatch.setattr(
        "core.portfolio_manager.get_trade_cash_summary",
        lambda mode=None, account_key=None: {
            "cash_delta": -510000,
            "buy_count": 1,
            "sell_count": 0,
            "total_trades": 1,
            "commission": 0,
            "tax": 0,
            "slippage": 0,
        },
    )

    pm = PortfolioManager(_MockConfig())
    summary = pm.get_portfolio_summary({"005930": 55000})

    assert summary["cash"] == 490000
    assert summary["invested"] == 500000
    assert summary["current_value"] == 550000
    assert summary["realized_pnl"] == -10000
    assert summary["unrealized_pnl"] == 50000


def test_live_portfolio_summary_marks_broker_balance_fallback(monkeypatch):
    """live 잔고 조회 실패 시 DB fallback을 표시하고 주문 sizing용 자본 조회는 차단한다."""
    from core.portfolio_manager import LiveBrokerBalanceUnavailable

    monkeypatch.setattr(
        api.kis_api.KISApi,
        "get_balance",
        lambda self: (_ for _ in ()).throw(RuntimeError("kis down")),
    )
    monkeypatch.setattr("core.portfolio_manager.get_all_positions", lambda account_key=None: [])
    monkeypatch.setattr(
        "core.portfolio_manager.get_trade_cash_summary",
        lambda mode=None, account_key=None: {
            "cash_delta": 0,
            "buy_count": 0,
            "sell_count": 0,
            "total_trades": 0,
            "commission": 0,
            "tax": 0,
            "slippage": 0,
        },
    )

    pm = PortfolioManager(_LiveMockConfig())
    summary = pm.get_portfolio_summary()

    assert summary["broker_balance_ok"] is False
    assert summary["broker_balance_source"] == "db_fallback"
    assert "kis down" in summary["broker_balance_error"]

    with pytest.raises(LiveBrokerBalanceUnavailable, match="kis down"):
        pm.get_current_capital()

    with pytest.raises(LiveBrokerBalanceUnavailable, match="kis down"):
        pm.get_available_cash()


def test_sync_with_broker_uses_core_notifier(monkeypatch):
    messages = []

    class _FakeNotifier:
        def send_message(self, text, critical=False):
            messages.append((text, critical))

    monkeypatch.setattr(
        api.kis_api.KISApi,
        "get_balance",
        lambda self: {
            "positions": [{"symbol": "005930", "quantity": 12}],
            "total_value": 1000000,
            "cash": 400000,
        },
    )
    monkeypatch.setattr(
        "core.portfolio_manager.get_all_positions",
        lambda account_key=None: [_MockPosition("005930", 50000, 10)],
    )
    monkeypatch.setattr(core.notifier, "Notifier", _FakeNotifier)

    pm = PortfolioManager(_MockConfig())
    result = pm.sync_with_broker()

    assert result["ok"] is False
    assert result["mismatches"]
    assert messages
    assert "포지션 동기화 불일치" in messages[0][0]


def test_sync_with_broker_skips_auto_correct_when_kis_positions_empty(monkeypatch):
    """KIS 보유 목록이 빈 응답이면 DB 포지션 전체 삭제 자동보정을 보류한다."""
    messages = []
    auto_correct_calls = []

    class _FakeNotifier:
        def send_message(self, text, critical=False):
            messages.append((text, critical))

    def _record_auto_correct(self, mismatches):
        auto_correct_calls.append(mismatches)
        return [{"symbol": "005930", "action": "deleted"}]

    monkeypatch.setattr(
        api.kis_api.KISApi,
        "get_balance",
        lambda self: {
            "positions": [],
            "total_value": 0,
            "cash": 0,
        },
    )
    monkeypatch.setattr(
        "core.portfolio_manager.get_all_positions",
        lambda account_key=None: [_MockPosition("005930", 50000, 10)],
    )
    monkeypatch.setattr(core.notifier, "Notifier", _FakeNotifier)
    monkeypatch.setattr(PortfolioManager, "_auto_correct_positions", _record_auto_correct)

    pm = PortfolioManager(_MockConfig())
    result = pm.sync_with_broker(auto_correct=True)

    assert result["ok"] is False
    assert result["corrected"] == []
    assert result["auto_correct_skipped_reason"] == "empty_broker_positions"
    assert result["mismatches"][0]["type"] == "db_only"
    assert "자동 보정 보류" in result["message"]
    assert auto_correct_calls == []
    assert messages


def test_auto_correct_adds_kis_only_position_with_risk_targets(fresh_db):
    from database.repositories import get_position

    pm = PortfolioManager(_MockConfig(), account_key="sync_recover_test")

    corrected = pm._auto_correct_positions([
        {
            "symbol": "005930",
            "type": "kis_only",
            "kis_qty": 3,
            "kis_avg_price": 60_000,
            "kis_current_price": 61_000,
        }
    ])

    position = get_position("005930", account_key="sync_recover_test")

    assert corrected == [{
        "symbol": "005930",
        "action": "added",
        "qty": 3,
        "risk_targets_recovered": True,
    }]
    assert position.quantity == 3
    assert position.avg_price == 60_000
    assert position.stop_loss_price == 58_200
    assert position.take_profit_price == 64_800
    assert position.trailing_stop_price == 57_000
    assert position.strategy == "broker_sync_recovered"


def test_auto_correct_qty_mismatch_sets_broker_quantity_without_adding(fresh_db):
    from database.repositories import get_position, save_position

    save_position(
        symbol="005930",
        avg_price=50_000,
        quantity=2,
        stop_loss_price=48_500,
        take_profit_price=54_000,
        trailing_stop_price=47_500,
        strategy="scoring",
        account_key="sync_qty_test",
    )
    pm = PortfolioManager(_MockConfig(), account_key="sync_qty_test")

    corrected = pm._auto_correct_positions([
        {
            "symbol": "005930",
            "type": "qty_mismatch",
            "kis_qty": 5,
            "kis_avg_price": 60_000,
            "kis_current_price": 61_000,
            "db_qty": 2,
        }
    ])

    position = get_position("005930", account_key="sync_qty_test")

    assert corrected == [{
        "symbol": "005930",
        "action": "qty_updated",
        "old_qty": 2,
        "new_qty": 5,
        "risk_targets_recovered": True,
    }]
    assert position.quantity == 5
    assert position.avg_price == 60_000
    assert position.total_invested == 300_000
    assert position.stop_loss_price == 58_200
    assert position.take_profit_price == 64_800
    assert position.trailing_stop_price == 57_000
