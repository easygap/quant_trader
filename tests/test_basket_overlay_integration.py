"""BasketRebalancer × 리스크 오버레이 통합 — 배수가 목표 비중·배치 진단·상태 파일에 반영되는가.

계약
  - 오버레이가 꺼진 바스켓은 기존 동작 그대로(배수 1.0, overlay None).
  - 추세 필터 발동 시 _stock_fraction = 설계 × off_scale, 배치 진단의 design_fraction도 같은 값.
  - 지수 데이터가 부족하면 직전 상태를 유지하고 data_issues를 남긴다.
  - 판단은 상태 파일(QUANT_OVERLAY_STATE_DIR)에 저장돼 다음 실행의 히스테리시스 입력이 된다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.basket_rebalancer import BasketRebalancer
from core.risk_overlays import load_overlay_state, save_overlay_state, OverlayDecision


class _Collector:
    """실제 시그니처(symbol, start_date, end_date)만 받는 지수 fake."""

    def __init__(self, closes):
        self.closes = closes
        self.calls = []

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        self.calls.append((symbol, start_date, end_date))
        if self.closes is None:
            return pd.DataFrame()
        dates = pd.date_range(end="2020-01-01", periods=len(self.closes), freq="B")
        return pd.DataFrame({"date": dates, "close": self.closes})


def _basket(overlays=None, target=0.6):
    cfg = {
        "name": "t", "enabled": True, "initial_capital": 1_000_000,
        "target_stock_weight": target, "min_cash_ratio": 0.05,
        "holdings": {"005930": 1.0},
        "rebalance": {"trigger": "drift", "drift_threshold": 0.08, "min_trade_amount": 50_000},
    }
    if overlays is not None:
        cfg["overlays"] = overlays
    return cfg


def _make(basket_cfg, closes, cumulative=None):
    with patch.object(BasketRebalancer, "_load_baskets_config", return_value={"t": basket_cfg}):
        rb = BasketRebalancer(basket_name="t")
    rb.data_collector = _Collector(closes)
    frame = pd.DataFrame({"cumulative_return": cumulative}) if cumulative is not None else pd.DataFrame()
    rb._nav_frame = frame
    return rb


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_OVERLAY_STATE_DIR", str(tmp_path))
    yield tmp_path


def _patch_snapshots(rb):
    return patch("database.repositories.get_portfolio_snapshots", return_value=rb._nav_frame)


class TestOverlayOff:
    def test_no_overlay_block_keeps_design(self):
        rb = _make(_basket(), closes=[100.0] * 300)
        assert rb.overlay_decision() is None
        assert rb._stock_fraction() == pytest.approx(0.6)
        assert rb.data_collector.calls == []  # 지수 조회조차 하지 않는다


class TestTrendFilter:
    overlays = {"trend_filter": {"enabled": True, "index_symbol": "KS200", "ma_days": 200, "band": 0.02, "off_scale": 0.5}}

    def test_below_ma_halves_target_and_saves_state(self, _isolated_state):
        closes = [100.0] * 199 + [90.0]           # 마지막 종가가 200일선 -10%
        rb = _make(_basket(self.overlays), closes)
        with _patch_snapshots(rb):
            decision = rb.overlay_decision()
        assert decision.trend_below is True and decision.scale == pytest.approx(0.5)
        assert rb._stock_fraction() == pytest.approx(0.3)
        assert rb.data_collector.calls[0][0] == "KS200"
        state = load_overlay_state("t")
        assert state["scale"] == pytest.approx(0.5) and state["trend_below"] is True
        # 같은 인스턴스에서는 한 번만 계산한다(plan→execute 사이클 내 동일 값)
        assert rb.overlay_decision() is decision and len(rb.data_collector.calls) == 1

    def test_above_ma_keeps_design(self):
        closes = [100.0] * 199 + [105.0]
        rb = _make(_basket(self.overlays), closes)
        with _patch_snapshots(rb):
            assert rb._stock_fraction() == pytest.approx(0.6)
        assert rb.overlay_decision().reasons == []

    def test_hysteresis_uses_previous_state_file(self):
        # 직전 실행이 '아래' 상태를 남겼고, 오늘은 선 +1% (band 2% 안) → 아래 유지
        save_overlay_state("t", OverlayDecision(scale=0.5, trend_below=True))
        closes = [100.0] * 199 + [101.0]
        rb = _make(_basket(self.overlays), closes)
        with _patch_snapshots(rb):
            assert rb._stock_fraction() == pytest.approx(0.3)

    def test_missing_index_data_keeps_previous_state_and_flags(self):
        save_overlay_state("t", OverlayDecision(scale=0.5, trend_below=True))
        rb = _make(_basket(self.overlays), closes=None)
        with _patch_snapshots(rb):
            decision = rb.overlay_decision()
        assert decision.scale == pytest.approx(0.5)
        assert decision.data_issues and "부족" in decision.data_issues[0]

    def test_today_bar_is_excluded(self):
        """오늘 날짜 봉은 전일까지의 정보가 아니므로 제외한다."""
        from datetime import datetime
        closes = [100.0] * 200 + [50.0]
        rb = _make(_basket(self.overlays), closes)
        today = datetime.now().strftime("%Y-%m-%d")
        dates = list(pd.date_range(end=today, periods=len(closes), freq="D"))
        rb.data_collector.fetch_korean_stock = lambda symbol, start_date=None, end_date=None: pd.DataFrame({"date": dates, "close": closes})
        got = rb._fetch_index_closes("KS200", 200)
        assert got[-1] == 100.0 and len(got) == 200


class TestDrawdownGuard:
    overlays = {"drawdown_guard": {"enabled": True, "trigger": -0.10, "release": -0.05, "scale": 0.5}}

    def test_deep_drawdown_halves_and_diagnose_reports_both_fractions(self):
        rb = _make(_basket(self.overlays), closes=None, cumulative=[0.0, 3.0, -8.0, -12.0])
        rb._fetch_current_prices = MagicMock(return_value={"005930": 70_000.0})
        rb.portfolio_mgr.get_portfolio_summary = MagicMock(return_value={"total_value": 1_000_000.0, "cash": 400_000.0})
        rb.get_current_weights = MagicMock(return_value={"005930": 0.6})
        with _patch_snapshots(rb):
            diag = rb.diagnose_deployment()
        assert diag["base_stock_fraction"] == pytest.approx(0.6)
        assert diag["design_fraction"] == pytest.approx(0.3)
        assert diag["overlay"]["drawdown_active"] is True
        assert diag["overlay"]["drawdown"] == pytest.approx((1 - 0.12) / 1.03 - 1, abs=1e-4)

    def test_recovery_releases(self):
        save_overlay_state("t", OverlayDecision(scale=0.5, drawdown_active=True))
        rb = _make(_basket(self.overlays), closes=None, cumulative=[0.0, 10.0, 5.0, 6.0])
        with _patch_snapshots(rb):
            assert rb._stock_fraction() == pytest.approx(0.6)   # 낙폭 -3.6% → -5% 안 → 해제
