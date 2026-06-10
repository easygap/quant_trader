"""바스켓 buy&hold 트랙레코드 생성기 단위 테스트.

build_track_record는 실데이터 fetch에 의존하므로, DataCollector와 basket config를
모킹해 계산 로직(주식 슬리브 배분→보유, 수익률/CAGR/MDD, 월별 평가)만 검증한다.

핵심 회귀: target_stock_weight(주식/현금 정적 배분)·min_cash_ratio를 무시하고
holdings 비중을 자본 100%에 적용하던 버그 — '주식50/현금50' 바스켓이 주식 100%로
시뮬레이션돼 수익·MDD가 과대 보고됐다. 트랙레코드는 운영(_stock_fraction)과
동일한 배분이어야 정직하다.
"""
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


def _build(baskets, panel, basket="t", **kw):
    import tools.basket_track_record as btr

    with patch(
        "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
        return_value=baskets,
    ), patch("core.basket_rebalancer.PortfolioManager"), patch(
        "core.basket_rebalancer.DataCollector"
    ), patch("core.data_collector.DataCollector", return_value=_fake_collector(panel)):
        return btr.build_track_record(basket, "2024-01-01", "2024-12-31", **kw)


def _panel_double_and_flat(n=300):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    # AAA: +100% (100→200), BBB: 보합(100→100)
    A = pd.Series([100 + (100 * i / (n - 1)) for i in range(n)], index=dates)
    B = pd.Series([100.0] * n, index=dates)
    return pd.DataFrame({"AAA": A, "BBB": B})


def test_track_record_two_names_equal_weight_with_default_cash_floor():
    """동일비중 2종목(+100%/보합) → 주식수익 +50%에 주식비중 80%(min_cash_ratio 0.2) 반영 ≈ +40%."""
    baskets = {"t": {"name": "테스트", "holdings": {"AAA": 0.5, "BBB": 0.5}}}
    tr = _build(baskets, _panel_double_and_flat(), capital=10_000_000)

    assert tr["stock_fraction"] == pytest.approx(0.8)
    assert 36 <= tr["total_return_pct"] <= 42
    assert tr["holdings"] == {"AAA": 0.5, "BBB": 0.5}
    assert tr["entry_commission"] > 0          # 진입 수수료 반영
    assert tr["final_value"] > tr["capital"]   # 수익
    assert len(tr["monthly"]) >= 10            # 월별 행
    rets = [r for _, _, r in tr["monthly"]]
    assert rets[-1] > rets[0]


def test_target_stock_weight_limits_equity_exposure():
    """주식50/현금50 바스켓: 가격 2배 종목 100% 보유라도 수익은 약 +50%×0.5=+25%대.

    (버그 시절엔 주식 100% 시뮬레이션 → +100%로 과대 보고)
    """
    n = 300
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    panel = pd.DataFrame(
        {"AAA": pd.Series([100 + (100 * i / (n - 1)) for i in range(n)], index=dates)}
    )
    baskets = {"t": {"holdings": {"AAA": 1.0}, "target_stock_weight": 0.5}}
    tr = _build(baskets, panel, capital=10_000_000)

    assert tr["stock_fraction"] == pytest.approx(0.5)
    assert 45 <= tr["total_return_pct"] <= 52


def test_track_record_missing_price_raises():
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    panel = pd.DataFrame({"AAA": pd.Series([100.0] * 50, index=dates)})
    baskets = {"t": {"holdings": {"AAA": 0.5, "BBB": 0.5}}}  # BBB 가격 없음
    with pytest.raises(SystemExit):
        _build(baskets, panel)


def test_render_markdown_has_key_sections():
    import tools.basket_track_record as btr
    tr = {
        "display_name": "테스트", "start": "2023-01-01", "end": "2025-12-31",
        "capital": 10_000_000, "final_value": 23_000_000, "total_return_pct": 130.0,
        "cagr_pct": 32.0, "sharpe": 1.3, "mdd_pct": -23.0, "entry_commission": 1300,
        "holdings": {"005930": 0.5, "000660": 0.5}, "stock_fraction": 0.5,
        "monthly": [("2023-01", 11_000_000, 10.0)], "years": 3.0,
    }
    md = btr.render_markdown(tr)
    assert "## 요약" in md
    assert "## 월별 평가액" in md
    assert "+130.00%" in md
    assert "005930" in md
    assert "주식 비중 | 50%" in md  # 현금 배분이 리포트에 명시
