"""리스크 오버레이(추세 필터·낙폭 제어·변동성 목표) 순수 함수와 상태 파일 테스트.

계약: 배수는 설계 비중을 대체하지 않고 곱한다. 히스테리시스는 상태로 이어지고,
데이터가 없으면 새 판단을 지어내지 않고 직전 상태를 유지하며 data_issues에 남긴다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math

import pytest

from core.risk_overlays import (
    DrawdownGuardConfig,
    OverlayConfig,
    OverlayDecision,
    TrendFilterConfig,
    VolatilityTargetConfig,
    applied_stock_fraction,
    compute_decision,
    describe_decision,
    drawdown_from_cumulative_returns,
    drawdown_guard_active,
    load_overlay_state,
    parse_overlay_config,
    save_overlay_state,
    trend_below_ma,
    volatility_scale,
)


def _closes(level_then_last, ma_days=200):
    """ma_days-1개는 100, 마지막 종가만 지정."""
    return [100.0] * (ma_days - 1) + [level_then_last]


class TestParse:
    def test_missing_block_is_all_off(self):
        cfg = parse_overlay_config({})
        assert cfg.any_enabled is False
        assert cfg.trend.ma_days == 200 and cfg.drawdown.trigger == -0.10

    def test_partial_block_and_clamping(self):
        cfg = parse_overlay_config({"overlays": {
            "trend_filter": {"enabled": True, "off_scale": 1.7, "band": "0.03"},
            "drawdown_guard": {"enabled": "yes", "trigger": 0.2},
            "volatility_target": {"enabled": True, "target": 0.15, "min_scale": -1},
        }})
        assert cfg.trend.enabled and cfg.trend.off_scale == 1.0 and cfg.trend.band == pytest.approx(0.03)
        assert cfg.drawdown.enabled and cfg.drawdown.trigger == 0.0  # 양수 trigger는 0으로 고정
        assert cfg.volatility.min_scale == 0.0
        assert cfg.any_enabled

    def test_garbage_block_is_ignored(self):
        assert parse_overlay_config({"overlays": "nope"}).any_enabled is False


class TestTrend:
    cfg = TrendFilterConfig(enabled=True, ma_days=200, band=0.02, off_scale=0.5)

    def test_insufficient_data_keeps_previous_state(self):
        assert trend_below_ma([100.0] * 50, self.cfg, True) == (True, None)
        assert trend_below_ma([], self.cfg, None) == (None, None)

    def test_first_run_initialises_without_band(self):
        below, rel = trend_below_ma(_closes(99.0), self.cfg, None)
        assert below is True and rel < 0

    def test_hysteresis_holds_inside_band(self):
        # 위 상태에서 -1%는 band(2%) 안 → 유지
        assert trend_below_ma(_closes(99.0), self.cfg, False)[0] is False
        # -3%면 아래로 전환
        assert trend_below_ma(_closes(96.0), self.cfg, False)[0] is True
        # 아래 상태에서 +1%는 band 안 → 유지
        assert trend_below_ma(_closes(101.0), self.cfg, True)[0] is True
        # +3%면 위로 복귀
        assert trend_below_ma(_closes(103.5), self.cfg, True)[0] is False


class TestDrawdown:
    cfg = DrawdownGuardConfig(enabled=True, trigger=-0.10, release=-0.05, scale=0.5)

    def test_drawdown_from_cumulative_returns_uses_initial_capital_as_first_peak(self):
        assert drawdown_from_cumulative_returns([-0.11, -5.0, -10.71]) == pytest.approx(-0.1071, abs=1e-4)
        assert drawdown_from_cumulative_returns([10.0, 21.0, 9.9]) == pytest.approx(1.099 / 1.21 - 1)
        assert drawdown_from_cumulative_returns([]) is None
        assert drawdown_from_cumulative_returns([None, "x"]) is None
        assert drawdown_from_cumulative_returns([0., None, 10.]) is None
        assert drawdown_from_cumulative_returns([0., "오류", 10.]) is None

    def test_trigger_and_release(self):
        assert drawdown_guard_active(-0.09, self.cfg, False) is False
        assert drawdown_guard_active(-0.10, self.cfg, False) is True
        assert drawdown_guard_active(-0.07, self.cfg, True) is True     # 아직 release 안
        assert drawdown_guard_active(-0.05, self.cfg, True) is False
        assert drawdown_guard_active(None, self.cfg, True) is True      # 데이터 없음 → 유지


class TestVolatility:
    cfg = VolatilityTargetConfig(enabled=True, target=0.20, lookback_days=20, min_scale=0.5, max_scale=1.0, step=0.1)

    def test_scale_quantised_and_capped(self):
        calm = [0.001, -0.001] * 10        # 연 ~2% → 배수 상한 1.0
        wild = [0.03, -0.03] * 10          # 연 ~48% → 0.2/0.48≈0.42 → 하한 0.5
        assert volatility_scale(calm, self.cfg)[0] == 1.0
        assert volatility_scale(wild, self.cfg)[0] == 0.5
        assert volatility_scale([0.01] * 5, self.cfg) == (None, None)

    def test_scale_rounds_to_step(self):
        mid = [0.018, -0.018] * 10         # 연 ~29% → 0.69 → 0.7
        scale, vol = volatility_scale(mid, self.cfg)
        assert scale == pytest.approx(0.7)
        assert 0.25 < vol < 0.35


class TestDecision:
    def test_all_off_is_identity(self):
        d = compute_decision(OverlayConfig())
        assert d.scale == 1.0 and d.reasons == [] and d.data_issues == []

    def test_trend_and_drawdown_multiply(self):
        cfg = parse_overlay_config({"overlays": {
            "trend_filter": {"enabled": True, "ma_days": 200},
            "drawdown_guard": {"enabled": True},
        }})
        d = compute_decision(cfg, index_closes=_closes(95.0), cumulative_returns_pct=[0, -12.0], prev_state=None)
        assert d.trend_below is True and d.drawdown_active is True
        assert d.scale == pytest.approx(0.25)
        assert len(d.reasons) == 2 and "200일선 아래" in d.reasons[0]

    def test_missing_data_keeps_previous_state_and_flags_issue(self):
        cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True}}})
        prev = {"trend_below": True}
        d = compute_decision(cfg, index_closes=[100.0] * 10, prev_state=prev)
        assert d.trend_below is True and d.scale == 0.5
        assert d.data_issues and "부족" in d.data_issues[0]

    def test_describe_is_korean_one_liner(self):
        cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True}}})
        d = compute_decision(cfg, index_closes=_closes(103.0), prev_state=None)
        assert describe_decision(d).startswith("기본 투자 비중 유지")
        assert "첫 실행 대기" in describe_decision(None)

    def test_minimum_combination_does_not_cut_same_risk_twice(self):
        cfg = parse_overlay_config({"overlays": {"combination": "minimum", "trend_filter": {"enabled": True}, "drawdown_guard": {"enabled": True}}})
        d = compute_decision(cfg, index_closes=_closes(95.), cumulative_returns_pct=[0., -12.])
        assert d.scale == .5

    def test_missing_data_cannot_increase_previous_exposure(self):
        cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True}}})
        d = compute_decision(cfg, index_closes=[], prev_state={"scale": .25})
        assert d.scale == .25
        assert d.data_issues

    def test_nonfinite_price_is_not_a_recovery_signal(self):
        cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True}}})
        d = compute_decision(cfg, index_closes=_closes(float('inf')), prev_state={"trend_below": True, "scale": .5})
        assert d.scale == .5
        assert d.data_issues


class TestDefensiveAllocation:
    def test_released_stock_weight_goes_to_defensive_asset(self):
        from core.risk_overlays import overlay_target_weights
        result = overlay_target_weights({"069500": .5, "357870": .5}, .95, .5, "357870")
        assert result == pytest.approx({"069500": .2375, "357870": .7125})
        assert sum(result.values()) == pytest.approx(.95)

    def test_stock_only_basket_keeps_cash_fallback(self):
        from core.risk_overlays import overlay_target_weights
        assert overlay_target_weights({"A": 1}, .6, .5) == {"A": .3}

    def test_unknown_defensive_asset_is_not_silently_accepted(self):
        from core.risk_overlays import overlay_target_weights
        with pytest.raises(ValueError):
            overlay_target_weights({"A": 1}, .6, .5, "missing")


class TestState:
    def test_paper_and_live_states_are_isolated(self, tmp_path):
        save_overlay_state("basket", OverlayDecision(scale=.5, trend_below=True), tmp_path, mode="paper")
        assert load_overlay_state("basket", tmp_path, mode="live") is None
        save_overlay_state("basket", OverlayDecision(scale=1., trend_below=False), tmp_path, mode="live")
        assert load_overlay_state("basket", tmp_path, mode="paper")["scale"] == .5
        assert load_overlay_state("basket", tmp_path, mode="live")["scale"] == 1.

    def test_legacy_state_is_only_used_for_paper_migration(self, tmp_path):
        save_overlay_state("basket", OverlayDecision(scale=.5), tmp_path)
        assert load_overlay_state("basket", tmp_path, mode="paper")["scale"] == .5
        assert load_overlay_state("basket", tmp_path, mode="live") is None

    @pytest.mark.parametrize("scale", [float("nan"), float("inf"), -1., 2., "broken"])
    def test_invalid_saved_scale_is_not_used(self, tmp_path, scale):
        import json
        (tmp_path / "basket.json").write_text(json.dumps({"scale": scale}), encoding="utf-8")
        assert load_overlay_state("basket", tmp_path) is None

    def test_roundtrip_and_applied_fraction(self, tmp_path):
        cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True}}})
        d = compute_decision(cfg, index_closes=_closes(90.0), prev_state=None)
        path = save_overlay_state("kr_pocket", d, tmp_path)
        assert path.parent == tmp_path and path.name == "kr_pocket.json"
        state = load_overlay_state("kr_pocket", tmp_path)
        assert state["scale"] == 0.5 and state["basket"] == "kr_pocket" and state["trend_below"] is True
        assert applied_stock_fraction(0.95, state) == pytest.approx(0.475)
        assert applied_stock_fraction(0.95, None) == pytest.approx(0.95)
        assert load_overlay_state("missing", tmp_path) is None

    def test_env_dir_is_respected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUANT_OVERLAY_STATE_DIR", str(tmp_path / "env"))
        cfg = parse_overlay_config({"overlays": {"drawdown_guard": {"enabled": True}}})
        d = compute_decision(cfg, cumulative_returns_pct=[0.0, -1.0])
        save_overlay_state("x", d)
        assert (tmp_path / "env" / "x.json").exists()
