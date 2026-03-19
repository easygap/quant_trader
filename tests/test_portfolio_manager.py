"""PortfolioManager 집계/동기화 테스트."""

import api.kis_api
import core.notifier
from core.portfolio_manager import PortfolioManager


class _MockPosition:
    def __init__(self, symbol: str, avg_price: float, quantity: int):
        self.symbol = symbol
        self.avg_price = avg_price
        self.quantity = quantity


class _MockConfig:
    trading = {"mode": "paper"}
    risk_params = {"position_sizing": {"initial_capital": 1000000}}
    database = {"sqlite_path": "data/quant_trader.db"}

    def get_account_no(self, key=""):
        return "00000000-00"


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
