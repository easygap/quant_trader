"""성과 표뿐 아니라 매일의 투자 판단에 적립 중립 TWR을 쓰는지 검증한다."""

import numpy as np
import pandas as pd
import pytest

from tools.risk_overlay_backtest import Policy, simulate


def prices(values, dates=None):
    return pd.Series(
        values,
        index=pd.to_datetime(dates)
        if dates
        else pd.bdate_range("2020-01-01", periods=len(values)),
    )


def test_deposit_does_not_erase_drawdown_before_next_decision():
    # 고점 100 → 80으로 하락한 다음 날 100만원 입금. 입금이 손실을 지워서는 안 된다.
    series = prices(
        [100.0, 80.0, 80.0, 80.0],
        ["2020-01-29", "2020-01-30", "2020-02-03", "2020-02-04"],
    )
    frame, _ = simulate(
        series,
        Policy("dd", "낙폭", dd=True),
        target_stock=1,
        initial=100_000,
        monthly=1_000_000,
        rf_annual=0,
        commission=0,
        slippage=0,
    )
    assert frame.iloc[2].scale == 0.5
    assert frame.iloc[3].scale == 0.5
    assert frame.iloc[3].signal_drawdown == pytest.approx(-0.2)
    np.testing.assert_allclose(frame.twr, (1 + frame.daily_return).cumprod())


def test_flat_market_with_deposits_has_zero_return():
    frame, _ = simulate(
        prices([100.0] * 70),
        Policy("static", "고정"),
        rf_annual=0,
        commission=0,
        slippage=0,
    )
    assert frame.iloc[-1].contributed > 300_000
    np.testing.assert_allclose(frame.twr, 1.0)


def test_fees_count_from_first_trade_and_no_cash_borrowing():
    frame, _ = simulate(
        prices([100.0] * 4),
        Policy("all", "전액"),
        target_stock=1,
        monthly=0,
        rf_annual=0,
        commission=0.01,
        slippage=0.02,
    )
    assert (frame.cash >= -1e-8).all()
    assert frame.iloc[0].daily_return < -0.02
    assert frame.iloc[-1].twr < 1


def test_full_exit_never_creates_short_position():
    series = prices([100.0] * 21 + [50.0] * 5)
    frame, _ = simulate(
        series,
        Policy("exit", "청산", trend=True, trend_ma_days=20, trend_off_scale=0),
        target_stock=1,
        monthly=0,
        rf_annual=0,
        slippage=0.03,
    )
    assert (frame.shares >= 0).all()
    assert frame.iloc[-1].shares == 0


def test_changing_future_prices_cannot_change_past_orders():
    series = prices([100.0] * 25 + [80.0] * 10 + [105.0] * 10)
    changed = series.copy()
    changed.iloc[35:] *= 3
    policy = Policy(
        "both",
        "추세와 낙폭",
        trend=True,
        trend_ma_days=20,
        dd=True,
        combination="minimum",
    )
    a, _ = simulate(series, policy)
    b, _ = simulate(changed, policy)
    pd.testing.assert_frame_equal(a.iloc[:35], b.iloc[:35])


@pytest.mark.parametrize(
    "values", [[100.0, float("nan")], [100.0, float("inf")], [100.0, 0.0]]
)
def test_invalid_prices_are_rejected(values):
    with pytest.raises(ValueError):
        simulate(prices(values), Policy("test", "검증"))


def test_integer_etf_execution_respects_shares_cash_and_future_boundary():
    from tools.risk_review import integer_etf_simulation

    idx = pd.bdate_range("2020-01-01", periods=280)
    panel = pd.DataFrame(
        {
            "KS200": [100.0] * 220 + [65.0] * 60,
            "069500": [12000.0] * 220 + [8000.0] * 60,
            "357870": [5000.0] * 280,
        },
        index=idx,
    )
    policy = Policy("min", "방어", trend=True, dd=True, combination="minimum")
    f, _ = integer_etf_simulation(panel, policy)
    changed = panel.copy()
    changed.iloc[245:] = changed.iloc[245:] * 3
    g, _ = integer_etf_simulation(changed, policy)
    assert (f.cash >= 0).all()
    assert (f.qty_069500 >= 0).all() and (f.qty_357870 >= 0).all()
    assert f.scale.min() == 0.5
    pd.testing.assert_frame_equal(f.iloc[:45], g.iloc[:45])
    np.testing.assert_allclose(f.twr, (1 + f.daily_return).cumprod())
