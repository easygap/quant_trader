"""tools/hold_policy_backtest.py — 규칙 조합 비교 모형이 의도대로 움직이는지 합성 가격으로 확인한다."""

import pandas as pd
import pytest

from tools.hold_policy_backtest import HoldPolicy, SimConfig, simulate_hold

DAYS = pd.bdate_range("2024-01-02", periods=160)


def _panel(market_path, laggard_path):
    """세 종목: 둘은 시장과 같이, 하나는 laggard_path로 움직인다."""
    return pd.DataFrame({
        "A": [100_000 * m for m in market_path],
        "B": [50_000 * m for m in market_path],
        "C": [80_000 * x for x in laggard_path],
    }, index=DAYS)


def _path(drop_at, depth):
    return [1.0 if i < drop_at else 1.0 - depth for i in range(len(DAYS))]


CFG = SimConfig(initial=3_000_000, min_trade=50_000, max_turnover=1.0)


def test_market_wide_drop_triggers_absolute_but_not_relative_stop():
    market = _path(40, 0.30)
    panel = _panel(market, market)
    bench = pd.Series([100.0 * m for m in market], index=DAYS)
    _, _, absolute = simulate_hold(panel, bench, HoldPolicy("abs", "", stop="absolute"), CFG)
    _, _, relative = simulate_hold(panel, bench, HoldPolicy("rel", "", stop="relative"), CFG)
    assert {e["symbol"] for e in absolute} == {"A", "B", "C"}
    assert relative == []


def test_idiosyncratic_drop_triggers_relative_stop_and_cooldown_blocks_rebuy():
    market = [1.0] * len(DAYS)
    panel = _panel(market, _path(40, 0.40))
    bench = pd.Series(100.0, index=DAYS)
    frame, _, events = simulate_hold(panel, bench, HoldPolicy("rel", "", stop="relative"), CFG)
    assert [e["symbol"] for e in events] == ["C"]
    # 60일 재매수 차단: 손절 다음 날부터 60일 동안 C를 다시 사지 않는다
    stop_day = events[0]["date"]
    after = frame.loc[stop_day:]
    assert (after["held_names"].iloc[1:40] == 2).all()


def test_account_lock_blocks_buys_only_while_in_drawdown():
    market = _path(30, 0.35)
    panel = _panel(market, market)
    bench = pd.Series([100.0 * m for m in market], index=DAYS)
    _, extra_lock, _ = simulate_hold(panel, bench, HoldPolicy("lock", "", account_lock=True), CFG)
    _, extra_free, _ = simulate_hold(panel, bench, HoldPolicy("free", ""), CFG)
    assert extra_lock["locked_days"] > 0
    assert extra_free["locked_days"] == 0


def test_empty_price_panel_raises_instead_of_returning_empty_result():
    with pytest.raises(Exception):
        simulate_hold(pd.DataFrame(), pd.Series(dtype=float), HoldPolicy("static", ""), CFG)
