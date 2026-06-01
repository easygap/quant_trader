"""buy&hold 강건성 분석기(tools/buy_hold_robustness.py) 단위 테스트.

실데이터 fetch는 모킹하고, NAV/통계/구간 분해 로직만 결정론적으로 검증한다.
"""
from unittest.mock import patch

import pandas as pd
import pytest


def _fake_collector(panel: pd.DataFrame):
    class _DC:
        def fetch_stock(self, sym, start_date=None, end_date=None):
            if sym not in panel.columns:
                return None
            return pd.DataFrame({"date": panel.index, "close": panel[sym].values})
    return _DC()


def test_equal_weight_nav_growth():
    import tools.buy_hold_robustness as bhr

    dates = pd.date_range("2023-01-01", periods=100, freq="D")
    # 두 종목 모두 +50% 선형 상승 → NAV도 ~+50%
    a = pd.Series([100 + 50 * i / 99 for i in range(100)], index=dates)
    panel = pd.DataFrame({"AAA": a, "BBB": a.copy()})
    nav = bhr._equal_weight_nav(panel, capital=10_000_000)
    assert nav.iloc[0] <= 10_000_000  # 진입 수수료로 약간 작거나 같음
    ret = (nav.iloc[-1] / 10_000_000 - 1) * 100
    assert 45 <= ret <= 52


def test_stats_computes_mdd_negative():
    import tools.buy_hold_robustness as bhr

    dates = pd.date_range("2023-01-01", periods=100, freq="D")
    # 올랐다가 떨어지는 NAV → MDD 음수
    up = [10_000_000 * (1 + 0.5 * i / 49) for i in range(50)]
    down = [up[-1] * (1 - 0.3 * i / 49) for i in range(50)]
    nav = pd.Series(up + down, index=dates)
    total, sharpe, mdd = bhr._stats(nav)
    assert mdd < 0
    assert mdd <= -25  # 고점 대비 30% 하락 구간 포함


def test_analyze_splits_periods_and_rolling(monkeypatch):
    import tools.buy_hold_robustness as bhr

    # 2022~2025 합성: 2022 하락, 2023~2025 상승
    dates = pd.date_range("2022-01-01", "2025-12-31", freq="D")
    n = len(dates)
    # 2022 -20%, 이후 꾸준히 상승하는 단순 경로
    vals = []
    for i, d in enumerate(dates):
        if d.year == 2022:
            vals.append(100 - 20 * (d.dayofyear / 365))
        else:
            base = 80
            yrs_in = (d.year - 2023) + d.dayofyear / 365
            vals.append(base * (1 + 0.3 * yrs_in))
    s = pd.Series(vals, index=dates)
    panel = pd.DataFrame({sym: s.copy() for sym in bhr.SYMBOLS})

    monkeypatch.setattr("core.data_collector.DataCollector", lambda: _fake_collector(panel))
    r = bhr.analyze()

    assert r["symbols"] == len(bhr.SYMBOLS)
    # 2022 구간은 음수여야 함
    assert r["periods"]["2022 (약세장 스트레스)"][0] < 0
    # 롤링 1년 분포가 계산됨
    assert r["rolling_1y"]["count"] > 0
    assert "negative_pct" in r["rolling_1y"]


def test_analyze_no_data_raises(monkeypatch):
    import tools.buy_hold_robustness as bhr
    empty = pd.DataFrame()
    monkeypatch.setattr("core.data_collector.DataCollector", lambda: _fake_collector(empty))
    with pytest.raises(SystemExit):
        bhr.analyze()
