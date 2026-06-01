"""주식+현금 정적 배분 분석기(tools/static_allocation_analysis.py) 단위 테스트."""
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


def test_blended_nav_reduces_drawdown():
    """현금 비중이 높을수록 MDD(절대값)가 작아져야 한다."""
    import tools.static_allocation_analysis as saa

    dates = pd.date_range("2022-01-01", periods=400, freq="D")
    # 올랐다가 크게 떨어지는 주식 NAV
    up = [10_000_000 * (1 + 0.4 * i / 199) for i in range(200)]
    down = [up[-1] * (1 - 0.4 * i / 199) for i in range(200)]
    stock_nav = pd.Series(up + down, index=dates)

    nav100 = stock_nav
    nav50 = saa._blended_nav(stock_nav, 0.5, 10_000_000)
    nav40 = saa._blended_nav(stock_nav, 0.4, 10_000_000)

    m100 = saa._metrics(nav100, 10_000_000)
    m50 = saa._metrics(nav50, 10_000_000)
    m40 = saa._metrics(nav40, 10_000_000)

    # 현금 많을수록 낙폭 작아짐(절대값 감소 = mdd 값이 더 큼/0에 가까움)
    assert m50["mdd"] > m100["mdd"]
    assert m40["mdd"] > m50["mdd"]


def test_metrics_counts_negative_years():
    import tools.static_allocation_analysis as saa
    # 2년: 1년차 상승, 2년차 하락
    dates = pd.date_range("2022-01-01", "2023-12-31", freq="D")
    vals = []
    for d in dates:
        if d.year == 2022:
            vals.append(10_000_000 * (1 + 0.2 * d.dayofyear / 365))
        else:
            peak = 12_000_000
            vals.append(peak * (1 - 0.15 * d.dayofyear / 365))
    nav = pd.Series(vals, index=dates)
    m = saa._metrics(nav, 10_000_000)
    assert m["negative_years"] >= 1
    assert m["worst_year"] < 0


def test_analyze_allocations_sorted_by_risk(monkeypatch):
    import tools.static_allocation_analysis as saa

    dates = pd.date_range("2021-12-01", "2025-12-31", freq="D")
    # 변동성 있는 상승 경로
    import math
    vals = [100 * (1 + 0.15 * (i / 365)) * (1 + 0.1 * math.sin(i / 30)) for i in range(len(dates))]
    s = pd.Series(vals, index=dates)
    panel = pd.DataFrame({sym: s.copy() for sym in saa.SYMBOLS})

    monkeypatch.setattr("core.data_collector.DataCollector", lambda: _fake_collector(panel))
    r = saa.analyze()

    allocs = r["allocations"]
    assert 1.0 in allocs and 0.4 in allocs
    # 주식 100%의 MDD 절대값이 40%보다 크거나 같아야(현금 혼합이 낙폭 완화)
    assert allocs[1.0]["mdd"] <= allocs[0.4]["mdd"]


def test_analyze_insufficient_data_raises(monkeypatch):
    import tools.static_allocation_analysis as saa
    monkeypatch.setattr("core.data_collector.DataCollector", lambda: _fake_collector(pd.DataFrame()))
    with pytest.raises(SystemExit):
        saa.analyze()
