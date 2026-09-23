"""PortfolioBacktester 감사 회귀 테스트.

next_open 체결 순서: (1) 시가 청산(갭다운 → 예약 SELL) → (2) 시가 매수 → (3) 종가 청산.
종가 사건이 먼저 도착한 시가 주문을 밀어내거나, 종가에 판 종목을 같은 날 시가에 다시 사는
시간 역행이 없어야 한다.
"""

import numpy as np
import pandas as pd
import pytest


class _PortfolioConfig:
    settings = {}
    strategies = {}

    def __init__(self, *, gap_enabled=False, take_profit=0.08, max_holding_days=0):
        self._trading = {"skip_earnings_days": 0}
        self._risk_params = {
            "transaction_costs": {
                "commission_rate": 0.0,
                "tax_rate": 0.0,
                "slippage": 0.0,
                "slippage_ticks": 0,
                "dynamic_slippage": {"enabled": False},
            },
            "stop_loss": {"type": "fixed", "fixed_rate": 0.03},
            "take_profit": {"fixed_rate": take_profit},
            "trailing_stop": {"enabled": False},
            "position_sizing": {"max_risk_per_trade": 0.01, "initial_capital": 100_000},
            "diversification": {
                "max_positions": 10,
                "max_position_ratio": 0.20,
                "max_investment_ratio": 0.95,
                "min_cash_ratio": 0.0,
            },
            "position_limits": {"max_holding_days": max_holding_days},
            "backtest_regime_filter": {"enabled": False},
            "gap_risk": {
                "enabled": gap_enabled,
                "gap_down_threshold": -0.03,
                "gap_up_entry_block": 0.05,
            },
            "blackswan": {"enabled": False},
        }

    @property
    def risk_params(self):
        return self._risk_params

    @property
    def trading(self):
        return self._trading


def _signal_df(close, *, open_=None, signals=None, start="2024-01-01", dates=None):
    dates = pd.bdate_range(start, periods=len(close)) if dates is None else pd.DatetimeIndex(dates)
    close = np.asarray(close, dtype=float)
    open_ = close if open_ is None else np.asarray(open_, dtype=float)
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": [1_000_000] * len(close),
            "signal": signals or ["HOLD"] * len(close),
            "strategy_score": [3.0] * len(close),
            "atr": close * 0.02,
        },
        index=dates,
    )


def _run(signals: dict, config, **kwargs):
    from backtest.portfolio_backtester import PortfolioBacktester

    all_dates = sorted(set().union(*(df.index for df in signals.values())))
    return PortfolioBacktester(config)._simulate_portfolio(
        symbols=list(signals.keys()),
        signals=signals,
        data={},
        all_dates=all_dates,
        initial_capital=100_000.0,
        **kwargs,
    )


def _actions(result):
    return [
        (t["symbol"], t["action"], t["date"].strftime("%Y-%m-%d"), t["price"])
        for t in result["trades"]
    ]


# ─── (1) 시가 주문이 같은 날 종가 사건보다 먼저 ────────────────────


def test_pending_sell_fills_at_open_before_close_take_profit():
    """전일 SELL 신호는 시가에 체결된다. 그날 종가가 익절선을 넘었어도 TAKE_PROFIT로 바뀌지 않는다."""
    df = _signal_df(
        [100.0, 100.0, 109.0],
        open_=[100.0, 100.0, 100.0],
        signals=["BUY", "SELL", "HOLD"],
    )

    result = _run({"AAA": df}, _PortfolioConfig(take_profit=0.08))

    assert _actions(result) == [
        ("AAA", "BUY", "2024-01-02", 100.0),
        ("AAA", "SELL", "2024-01-03", 100.0),
    ]
    assert result["trades"][1]["signal_date"] == df.index[1]
    assert result["exit_reason_counts"] == {"SELL": 1}


def test_gap_down_at_open_takes_precedence_over_pending_sell():
    df = _signal_df(
        [100.0, 100.0, 96.0],
        open_=[100.0, 100.0, 96.0],
        signals=["BUY", "SELL", "HOLD"],
    )

    result = _run({"AAA": df}, _PortfolioConfig(gap_enabled=True))

    assert _actions(result) == [
        ("AAA", "BUY", "2024-01-02", 100.0),
        ("AAA", "GAP_DOWN", "2024-01-03", 96.0),
    ]
    assert result["exit_reason_counts"] == {"GAP_DOWN": 1}
    assert result["executed_sell_count"] == 1
    assert result["gap_down_exits"] == 1


def test_no_rebuy_at_open_of_the_day_a_position_is_stopped_at_the_close():
    """보유 중 BUY 신호가 이어져도, 그날 종가 손절 뒤 같은 날 시가 매수는 생기지 않는다."""
    df = _signal_df(
        [100.0, 100.0, 96.0],
        open_=[100.0, 100.0, 100.0],
        signals=["BUY", "BUY", "HOLD"],
    )

    result = _run({"AAA": df}, _PortfolioConfig())

    assert _actions(result) == [
        ("AAA", "BUY", "2024-01-02", 100.0),
        ("AAA", "STOP_LOSS", "2024-01-03", 96.0),
    ]


def test_position_bought_at_open_is_still_subject_to_close_stop():
    """시가에 산 종목도 그날 종가 손절선 아래면 종가에 손절된다 (시간 순서상 정상)."""
    df = _signal_df([100.0, 96.0], open_=[100.0, 100.0], signals=["BUY", "HOLD"])

    result = _run({"AAA": df}, _PortfolioConfig())

    assert _actions(result) == [
        ("AAA", "BUY", "2024-01-02", 100.0),
        ("AAA", "STOP_LOSS", "2024-01-02", 96.0),
    ]


def test_pending_buy_on_gap_down_open_is_not_sold_at_the_same_open():
    df = _signal_df([100.0, 96.0, 96.0], open_=[100.0, 96.0, 96.0], signals=["BUY", "HOLD", "HOLD"])

    result = _run({"AAA": df}, _PortfolioConfig(gap_enabled=True))

    assert _actions(result) == [("AAA", "BUY", "2024-01-02", 96.0)]
    assert result["gap_down_exits"] == 0


def test_symbol_sold_at_open_is_not_rebought_at_the_same_open():
    """시가에 예약 SELL로 판 종목은 같은 날 시가 BUY 후보에서 빠진다."""
    df = _signal_df(
        [100.0, 100.0, 100.0],
        open_=[100.0, 100.0, 100.0],
        signals=["BUY", "SELL", "HOLD"],
    )
    # 두 번째 종목이 1/2 BUY 신호를 내 1/3에도 매수 후보 평가가 일어나게 한다.
    other = _signal_df([100.0] * 3, signals=["HOLD", "BUY", "HOLD"])

    result = _run({"AAA": df, "BBB": other}, _PortfolioConfig())

    aaa = [a for a in _actions(result) if a[0] == "AAA"]
    assert aaa == [("AAA", "BUY", "2024-01-02", 100.0), ("AAA", "SELL", "2024-01-03", 100.0)]


# ─── (2) 시가 주문 수량은 시가 시점 평가로 ─────────────────────────


def test_open_orders_are_sized_with_open_marks_not_same_day_close():
    """AAA가 그날 종가 +50%여도, 시가에 체결되는 BBB 수량은 시가 평가 자산으로 정한다."""
    aaa = _signal_df(
        [100.0, 100.0, 150.0, 150.0],
        open_=[100.0, 100.0, 100.0, 150.0],
        signals=["BUY", "HOLD", "HOLD", "HOLD"],
    )
    bbb = _signal_df([100.0] * 4, signals=["HOLD", "BUY", "HOLD", "HOLD"])

    result = _run({"AAA": aaa, "BBB": bbb}, _PortfolioConfig(take_profit=0.90))

    buys = {t["symbol"]: t for t in result["trades"] if t["action"] == "BUY"}
    assert buys["AAA"]["quantity"] == 200  # 100,000 × 20% / 100
    # 시가 평가 자산 = 현금 80,000 + AAA 200주 × 시가 100 = 100,000 → 200주
    # (당일 종가 150으로 평가하면 110,000 → 220주가 된다)
    assert buys["BBB"]["date"] == pd.Timestamp("2024-01-03")
    assert buys["BBB"]["quantity"] == 200


# ─── 당일 행이 없는 보유 종목 평가와 데이터 종료 청산 ───────────────


def _equity_by_date(result):
    eq = result["equity_curve"]
    return dict(zip(eq["date"].dt.strftime("%Y-%m-%d"), eq["value"]))


def test_symbol_whose_data_ends_is_valued_at_last_close_then_force_exited():
    """BBB 데이터가 끝난 다음 날 평균단가(100)로 되돌아가는 가짜 수익 없이 마지막 종가(98)로 청산."""
    from backtest.backtester import PNL_EXIT_ACTIONS
    from backtest.portfolio_backtester import PortfolioBacktester

    aaa = _signal_df([100.0] * 10)  # 달력을 이어 가는 종목 (거래 없음)
    bbb = _signal_df([100.0, 100.0, 99.0, 99.0, 98.0, 98.0], signals=["BUY"] + ["HOLD"] * 5)

    result = _run({"AAA": aaa, "BBB": bbb}, _PortfolioConfig())

    assert _actions(result) == [
        ("BBB", "BUY", "2024-01-02", 100.0),
        ("BBB", "DATA_END", "2024-01-09", 98.0),
    ]
    assert result["data_end_exits"] == 1
    assert result["exit_reason_counts"] == {"DATA_END": 1}

    equity = _equity_by_date(result)
    # 매수 200주, 현금 80,000. 마지막 종가 98 → 99,600에서 멈춰야 한다 (예전엔 100,000으로 복귀).
    assert equity["2024-01-08"] == pytest.approx(99_600.0)
    assert equity["2024-01-09"] == pytest.approx(99_600.0)
    assert equity["2024-01-12"] == pytest.approx(99_600.0)

    assert "DATA_END" in PNL_EXIT_ACTIONS
    metrics = PortfolioBacktester(_PortfolioConfig())._calculate_portfolio_metrics(
        result, initial_capital=100_000.0
    )
    assert metrics["total_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["data_end_exits"] == 1


def test_mid_series_missing_row_is_valued_at_last_close_without_exit():
    """거래정지처럼 중간에 행만 빠진 날은 청산하지 않고 마지막 종가로 평가한다."""
    aaa = _signal_df([100.0] * 8)
    dates = pd.bdate_range("2024-01-01", periods=8)
    bbb_dates = dates.delete(4)  # 2024-01-05 결측
    bbb = _signal_df(
        [100.0, 100.0, 99.0, 98.0, 98.0, 98.0, 98.0],
        signals=["BUY"] + ["HOLD"] * 6,
        dates=bbb_dates,
    )

    result = _run({"AAA": aaa, "BBB": bbb}, _PortfolioConfig())

    assert _actions(result) == [("BBB", "BUY", "2024-01-02", 100.0)]
    assert result["data_end_exits"] == 0
    equity = _equity_by_date(result)
    assert equity["2024-01-04"] == pytest.approx(99_600.0)
    assert equity["2024-01-05"] == pytest.approx(99_600.0)  # 예전엔 평균단가로 100,000
