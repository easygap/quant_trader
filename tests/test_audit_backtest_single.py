"""단일 종목 Backtester 체결 순서·보유 규칙 감사 회귀 테스트.

- next_open에서 시가에 산 주식을 같은 시가에 갭다운으로 되팔지 않는다.
- 최소 보유 기간에는 실전처럼 손실 방어 청산(손절·트레일링·갭다운·블랙스완)만 허용한다.
"""

import numpy as np
import pandas as pd
import pytest


class _GuardConfig:
    settings = {}
    strategies = {}

    def __init__(
        self,
        *,
        gap_enabled=True,
        min_holding_days=0,
        max_holding_days=0,
        partial_exit=False,
        take_profit=0.50,
        stop_loss=0.03,
        trailing=False,
    ):
        self._trading = {"skip_earnings_days": 0}
        self._risk_params = {
            "transaction_costs": {
                "commission_rate": 0.0,
                "tax_rate": 0.0,
                "slippage": 0.0,
                "slippage_ticks": 0,
                "dynamic_slippage": {"enabled": False},
            },
            "stop_loss": {"type": "fixed", "fixed_rate": stop_loss},
            "take_profit": {
                "fixed_rate": take_profit,
                "partial_exit": partial_exit,
                "partial_ratio": 0.5,
                "partial_target": 0.04,
            },
            "trailing_stop": {"enabled": trailing, "type": "fixed", "fixed_rate": 0.05},
            "position_sizing": {"max_risk_per_trade": 0.01, "initial_capital": 100_000},
            "diversification": {"max_position_ratio": 0.20, "max_investment_ratio": 0.70},
            "position_limits": {
                "min_holding_days": min_holding_days,
                "max_holding_days": max_holding_days,
                "max_monthly_roundtrips": 0,
            },
            "liquidity_filter": {"backtest_max_participation_rate": 1.0},
            "gap_risk": {
                "enabled": gap_enabled,
                "gap_down_threshold": -0.03,
                "gap_up_entry_block": 0.05,
            },
            "blackswan": {"enabled": False},
            "backtest_regime_filter": {"enabled": False},
        }

    @property
    def risk_params(self):
        return self._risk_params

    @property
    def trading(self):
        return self._trading


def _frame(close, *, open_=None, signals=None, start="2024-01-01"):
    dates = pd.bdate_range(start, periods=len(close))
    close = np.asarray(close, dtype=float)
    open_ = close if open_ is None else np.asarray(open_, dtype=float)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": [1_000_000] * len(close),
            "signal": signals or ["HOLD"] * len(close),
        },
        index=dates,
    )
    df["_avg_daily_volume"] = df["volume"]
    return df


# ─── 갭다운 청산은 전일부터 보유한 포지션에만 ──────────────────────


def test_pending_buy_on_gap_down_open_is_not_sold_at_the_same_open():
    from backtest.backtester import Backtester

    bt = Backtester(_GuardConfig(gap_enabled=True))
    df = _frame(
        [100.0, 96.0, 96.0],
        open_=[100.0, 96.0, 96.0],  # 1일차 시가 -4% 갭다운 (임계 -3%)
        signals=["BUY", "HOLD", "HOLD"],
    )

    result = bt._simulate(df, initial_capital=100_000.0)

    assert [t["action"] for t in result["trades"]] == ["BUY"]
    buy = result["trades"][0]
    assert buy["date"] == df.index[1]
    assert buy["price"] == pytest.approx(96.0)
    assert result["gap_down_exits"] == 0
    # 매수일 장 마감 시점에도 포지션이 남아 있어야 한다.
    day1 = result["equity_curve"].iloc[1]
    assert day1["position_value"] == pytest.approx(buy["quantity"] * 96.0)


def test_gap_down_still_exits_a_position_carried_overnight():
    from backtest.backtester import Backtester

    bt = Backtester(_GuardConfig(gap_enabled=True))
    df = _frame(
        [100.0, 100.0, 96.0],
        open_=[100.0, 100.0, 96.0],
        signals=["BUY", "HOLD", "HOLD"],
    )

    result = bt._simulate(df, initial_capital=100_000.0)

    assert [t["action"] for t in result["trades"]] == ["BUY", "GAP_DOWN"]
    gap = result["trades"][1]
    assert gap["date"] == df.index[2]
    assert gap["price"] == pytest.approx(96.0)
    assert result["gap_down_exits"] == 1


# ─── 최소 보유 기간: 실전처럼 손실 방어 청산만 허용 ───────────────────


def _actions_and_dates(result):
    return [(t["action"], t["date"].strftime("%Y-%m-%d")) for t in result["trades"]]


def test_min_hold_blocks_take_profit_and_partial_until_day_five():
    """매수 다음날 +9%여도 5일 미만이면 부분·전량 익절하지 않는다 (실전 order_executor와 동일)."""
    from backtest.backtester import Backtester

    bt = Backtester(
        _GuardConfig(gap_enabled=False, min_holding_days=5, partial_exit=True, take_profit=0.08)
    )
    # 2024-01-01(월) 신호 → 01-02 시가 매수. 01-03~01-05는 보유 1~3일, 01-08은 6일.
    df = _frame(
        [100.0, 100.0, 109.0, 109.0, 109.0, 109.0, 109.0],
        signals=["BUY"] + ["HOLD"] * 6,
    )

    result = bt._simulate(df, initial_capital=100_000.0)

    assert _actions_and_dates(result) == [
        ("BUY", "2024-01-02"),
        ("TAKE_PROFIT_PARTIAL", "2024-01-08"),
        ("TAKE_PROFIT", "2024-01-09"),
    ]


def test_min_hold_blocks_max_hold_shorter_than_min_hold():
    from backtest.backtester import Backtester

    bt = Backtester(_GuardConfig(gap_enabled=False, min_holding_days=5, max_holding_days=3))
    df = _frame([100.0] * 8, signals=["BUY"] + ["HOLD"] * 7)

    result = bt._simulate(df, initial_capital=100_000.0)

    # 01-05(보유 3일)는 최소 보유 기간 안이라 만료 매도가 막히고 01-08(6일)에 청산된다.
    assert _actions_and_dates(result) == [("BUY", "2024-01-02"), ("MAX_HOLD", "2024-01-08")]


def test_min_hold_still_allows_stop_loss():
    from backtest.backtester import Backtester

    bt = Backtester(_GuardConfig(gap_enabled=False, min_holding_days=5))
    df = _frame(
        [100.0, 100.0, 96.0],
        open_=[100.0, 100.0, 100.0],
        signals=["BUY", "HOLD", "HOLD"],
    )

    result = bt._simulate(df, initial_capital=100_000.0)

    assert _actions_and_dates(result) == [("BUY", "2024-01-02"), ("STOP_LOSS", "2024-01-03")]


def test_min_hold_still_allows_trailing_stop():
    from backtest.backtester import Backtester

    bt = Backtester(_GuardConfig(gap_enabled=False, min_holding_days=5, trailing=True))
    # 고점 104 대비 5% 하락선 98.8 이탈(98.5), 손절선 97은 미도달
    df = _frame(
        [100.0, 100.0, 104.0, 98.5],
        open_=[100.0, 100.0, 104.0, 104.0],
        signals=["BUY", "HOLD", "HOLD", "HOLD"],
    )

    result = bt._simulate(df, initial_capital=100_000.0)

    assert _actions_and_dates(result) == [("BUY", "2024-01-02"), ("TRAILING_STOP", "2024-01-04")]


def test_legacy_same_close_keeps_its_original_take_profit_rule():
    """legacy_same_close는 과거 결과 재현 경로라 최소 보유 기간 익절 차단을 적용하지 않는다."""
    from backtest.backtester import Backtester

    bt = Backtester(
        _GuardConfig(gap_enabled=False, min_holding_days=5, partial_exit=True, take_profit=0.08)
    )
    df = _frame(
        [100.0, 100.0, 109.0, 109.0, 109.0, 109.0, 109.0],
        signals=["BUY"] + ["HOLD"] * 6,
    )

    result = bt._simulate(df, initial_capital=100_000.0, execution_model="legacy_same_close")

    assert _actions_and_dates(result) == [
        ("BUY", "2024-01-01"),
        ("TAKE_PROFIT_PARTIAL", "2024-01-03"),
        ("TAKE_PROFIT", "2024-01-04"),
    ]
