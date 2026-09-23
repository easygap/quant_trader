"""연구 도구 회귀 테스트 (2026-09-23).

관찰 트랙 오버레이 결론은 운영 트랙과 같은 조건(보유 종목·재조정 규칙·현금 이자)으로
내야 한다. 하이닉스가 든 10종목·현금 3% 표로 9종목·무이자 트랙의 정책을 정했다.
"""

import pandas as pd
import pytest

from tools import risk_overlay_backtest as research


def test_basket_symbols_come_from_config_not_the_old_list():
    symbols = research.configured_basket_symbols()
    assert "000660" not in symbols
    assert len(symbols) == 9


def test_sleeve_is_rebalanced_when_a_name_drifts():
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    # A만 두 배가 되면 동일비중 슬리브에서 A 비중이 50%→67%로 벌어진다(>8%p) → 재조정
    panel = pd.DataFrame({"A": [1.0, 2.0, 2.0, 1.0], "B": [1.0, 1.0, 1.0, 1.0]}, index=idx)
    held = (panel / panel.iloc[0]).mean(axis=1)            # 첫날 비중을 들고만 간 경우
    rebal = research.rebalanced_ew_index(panel, cost_rate=0.0)
    # 재조정하면 A가 반토막 날 때 손실이 작다(보유만 하면 A 비중이 커진 채로 맞는다)
    assert rebal.iloc[-1] > held.iloc[-1]


def test_sharpe_uses_given_risk_free_rate():
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    daily = pd.Series(0.0005, index=idx)
    daily.iloc[::7] = -0.001
    twr = (1 + daily).cumprod()
    frame = pd.DataFrame({
        "twr": twr, "daily_return": daily, "drawdown": twr / twr.cummax() - 1,
        "total": twr * 1e6, "contributed": 1e6,
    })
    extra = {"turnover_value": 0.0, "avg_exposure": 0.6}
    s0 = research.metrics(frame, extra, rf_annual=0.0)["sharpe"]
    s3 = research.metrics(frame, extra)["sharpe"]
    assert s0 > s3


def test_capture_uses_daily_average_not_compounded_regime_return():
    """상승일만 몇 년치 복리로 이으면 지수 수익이 폭증해 포착률이 0 쪽으로 쏠린다."""
    from core.performance_lens import split_by_regime

    pairs = [(0.6, 1.0)] * 600 + [(-0.6, -1.0)] * 600
    r = split_by_regime(pairs)
    assert r["up"]["capture"] == pytest.approx(0.6, abs=0.01)
    assert r["down"]["capture"] == pytest.approx(0.6, abs=0.01)
