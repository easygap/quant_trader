import pandas as pd

from backtest.backtester import Backtester


class _MockConfig:
    risk_params = {
        "position_sizing": {
            "max_risk_per_trade": 0.01,  # 1% 룰
            "initial_capital": 1_000_000,
        },
        "stop_loss": {
            "type": "fixed",
            "fixed_rate": 0.10,  # 10% 손절
        },
        "take_profit": {
            "type": "fixed",
            "fixed_rate": 0.20,  # 20% 익절 (이 테스트에서는 도달하지 않도록)
            "partial_exit": False,
        },
        "trailing_stop": {
            "enabled": True,
            "type": "fixed",
            "fixed_rate": 0.05,  # 고점 대비 5% 하락 시 트레일링 청산
        },
        "diversification": {
            "max_position_ratio": 0.20,
        },
        "transaction_costs": {
            "commission_rate": 0.0,
            "tax_rate": 0.0,
            "slippage": 0.0,
            "slippage_ticks": 0,
        },
    }


def test_backtester_trailing_stop_triggers():
    """
    흐름:
    - BUY @ 100
    - 고점 갱신: 110
    - 고점 대비 5% 하락: 104 (110 * (1-0.05)=104.5 이하) -> TRAILING_STOP 발생
    """
    bt = Backtester(config=_MockConfig())

    dates = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame(
        {
            "close": [100.0, 110.0, 104.0],
            "signal": ["BUY", "HOLD", "HOLD"],
            "atr": [1.0, 1.0, 1.0],  # fixed trailing_stop 호환용
        },
        index=dates,
    )

    result = bt._simulate(df, initial_capital=1_000_000)
    actions = [t["action"] for t in result["trades"]]

    assert "TRAILING_STOP" in actions

    trailing = next(t for t in result["trades"] if t["action"] == "TRAILING_STOP")
    assert trailing["price"] == 104.0
    assert trailing["quantity"] == 1000
    assert trailing["pnl"] == 4000.0

