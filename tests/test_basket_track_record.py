"""바스켓 buy&hold 트랙레코드 생성기 단위 테스트.

build_track_record는 실데이터 fetch에 의존하므로, DataCollector와 basket config를
모킹해 계산 로직(동일비중 매수→보유, 수익률/CAGR/MDD, 월별 평가)만 검증한다.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest


def _fake_collector(panel: pd.DataFrame):
    """panel(컬럼=종목, 인덱스=날짜)을 종목별 fetch_stock 응답으로 변환하는 mock."""
    class _DC:
        def fetch_stock(self, sym, start_date=None, end_date=None):
            if sym not in panel.columns:
                return None
            return pd.DataFrame({"date": panel.index, "close": panel[sym].values})
    return _DC()


def test_track_record_two_names_equal_weight():
    import tools.basket_track_record as btr

    dates = pd.date_range("2024-01-01", periods=300, freq="D")
    # A: +100% (100→200), B: 보합(100→100). 동일비중이면 대략 +50%.
    A = pd.Series([100 + (100 * i / 299) for i in range(300)], index=dates)
    B = pd.Series([100.0] * 300, index=dates)
    panel = pd.DataFrame({"AAA": A, "BBB": B})

    baskets = {"t": {"name": "테스트", "holdings": {"AAA": 0.5, "BBB": 0.5}}}
    with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config", return_value=baskets), \
         patch("core.data_collector.DataCollector", return_value=_fake_collector(panel)):
        tr = btr.build_track_record("t", "2024-01-01", "2024-12-31", capital=10_000_000)

    # A가 2배, B 보합 → 동일비중 약 +50% 근처(정수 수량/수수료로 약간 차이)
    assert 45 <= tr["total_return_pct"] <= 52
    assert tr["holdings"] == {"AAA": 0.5, "BBB": 0.5}
    assert tr["entry_commission"] > 0          # 진입 수수료 반영
    assert tr["final_value"] > tr["capital"]   # 수익
    assert len(tr["monthly"]) >= 10            # 월별 행
    # 월별 누적수익률은 단조증가에 가까움(A 선형상승)
    rets = [r for _, _, r in tr["monthly"]]
    assert rets[-1] > rets[0]


def test_track_record_missing_price_raises():
    import tools.basket_track_record as btr

    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    panel = pd.DataFrame({"AAA": pd.Series([100.0] * 50, index=dates)})
    baskets = {"t": {"holdings": {"AAA": 0.5, "BBB": 0.5}}}  # BBB 가격 없음
    with patch("core.basket_rebalancer.BasketRebalancer._load_baskets_config", return_value=baskets), \
         patch("core.data_collector.DataCollector", return_value=_fake_collector(panel)):
        with pytest.raises(SystemExit):
            btr.build_track_record("t", "2024-01-01", "2024-02-29")


def test_render_markdown_has_key_sections():
    import tools.basket_track_record as btr
    tr = {
        "display_name": "테스트", "start": "2023-01-01", "end": "2025-12-31",
        "capital": 10_000_000, "final_value": 23_000_000, "total_return_pct": 130.0,
        "cagr_pct": 32.0, "sharpe": 1.3, "mdd_pct": -23.0, "entry_commission": 1300,
        "holdings": {"005930": 0.5, "000660": 0.5},
        "monthly": [("2023-01", 11_000_000, 10.0)], "years": 3.0,
    }
    md = btr.render_markdown(tr)
    assert "## 요약" in md
    assert "## 월별 평가액" in md
    assert "+130.00%" in md
    assert "005930" in md
