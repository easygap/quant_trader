"""
승격 규칙 엔진 테스트 + registry 정합성 CI 검증

검증:
- 규칙 엔진이 metrics 기반으로 올바르게 판정하는지
- STRATEGY_STATUS가 promotion engine 결과와 일치하는지 (불일치 시 CI 실패)
- 불가능한 조합(negative return + paper_only)이 발생하지 않는지
- experiment_note가 상태에 영향을 주지 않는지
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.promotion_engine import (
    StrategyMetrics, promote, PromotionResult,
    get_all_promotions, load_promotion_artifact,
    _check_paper_only, _check_provisional_candidate, _check_live_candidate,
)

# 테스트용 inline metrics fixture (코드 상수가 아닌 테스트 데이터)
_TEST_METRICS = {
    "relative_strength_rotation": StrategyMetrics(
        name="relative_strength_rotation",
        total_return=18.09, profit_factor=1.62, mdd=-5.66,
        wf_positive_rate=1.0, wf_sharpe_positive_rate=0.833,
        wf_windows=6, wf_total_trades=126, sharpe=0.49,
    ),
    "scoring": StrategyMetrics(
        name="scoring",
        total_return=11.22, profit_factor=1.07, mdd=-14.55,
        wf_positive_rate=0.833, wf_sharpe_positive_rate=0.5,
        wf_windows=6, wf_total_trades=1155, sharpe=-0.02,
    ),
    "breakout_volume": StrategyMetrics(
        name="breakout_volume",
        total_return=-13.31, profit_factor=0.79, mdd=-16.62,
        wf_positive_rate=0.0, wf_sharpe_positive_rate=0.0,
        wf_windows=6, wf_total_trades=618, sharpe=-1.17,
    ),
    "mean_reversion": StrategyMetrics(
        name="mean_reversion",
        total_return=-8.36, profit_factor=0.85, mdd=-12.51,
        wf_positive_rate=0.333, wf_sharpe_positive_rate=0.0,
        wf_windows=6, wf_total_trades=328, sharpe=-0.94,
    ),
    "trend_following": StrategyMetrics(
        name="trend_following",
        total_return=-6.94, profit_factor=0.67, mdd=-9.38,
        wf_positive_rate=0.167, wf_sharpe_positive_rate=0.0,
        wf_windows=6, wf_total_trades=106, sharpe=-1.30,
    ),
    "ensemble": StrategyMetrics(
        name="ensemble",
        total_return=0, profit_factor=0, mdd=0,
        wf_positive_rate=0, wf_sharpe_positive_rate=0,
        wf_windows=0, wf_total_trades=0, sharpe=0,
    ),
}


# ── 1. 규칙 엔진 단위 테스트 ──

class TestPromotionRules:
    def test_negative_return_is_research_only(self):
        m = StrategyMetrics("test", total_return=-5, profit_factor=0.8, mdd=-10,
                            wf_positive_rate=0, wf_sharpe_positive_rate=0,
                            wf_windows=6, wf_total_trades=100, sharpe=-1)
        r = promote(m)
        assert r.status == "research_only"

    def test_pf_below_1_is_research_only(self):
        m = StrategyMetrics("test", total_return=5, profit_factor=0.9, mdd=-5,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.5,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status == "research_only"

    def test_wf_positive_below_50_is_research_only(self):
        m = StrategyMetrics("test", total_return=5, profit_factor=1.2, mdd=-5,
                            wf_positive_rate=0.3, wf_sharpe_positive_rate=0.1,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status == "research_only"

    def test_paper_only_minimum(self):
        m = StrategyMetrics("test", total_return=1, profit_factor=1.01, mdd=-5,
                            wf_positive_rate=0.5, wf_sharpe_positive_rate=0.3,
                            wf_windows=6, wf_total_trades=100, sharpe=0.1)
        r = promote(m)
        assert r.status == "paper_only"

    def test_provisional_candidate(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.6,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status == "provisional_paper_candidate"

    def test_provisional_requires_sharpe_floor(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.8,
                            wf_windows=6, wf_total_trades=100, sharpe=0.1)
        r = promote(m)
        assert r.status == "paper_only"

    def test_provisional_requires_profit_factor_floor(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.05, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.8,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status == "paper_only"

    def test_provisional_requires_ev_when_present(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.8,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5,
                            ev_per_trade=-1)
        r = promote(m)
        assert r.status == "paper_only"

    def test_provisional_blocks_extreme_turnover_when_present(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.8,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5,
                            turnover_per_year=1200)
        r = promote(m)
        assert r.status == "paper_only"

    def test_live_candidate_requires_paper(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.6,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5,
                            paper_days=60, paper_sharpe=0.5, paper_excess=1.0,
                            paper_evidence_recommendation="ELIGIBLE",
                            paper_benchmark_final_ratio=0.9,
                            paper_sell_count=6,
                            paper_win_rate=50.0,
                            paper_frozen_days=0,
                            paper_cumulative_return=2.0)
        r = promote(m)
        assert r.status == "live_candidate"
        assert "live" in r.allowed_modes

    def test_live_candidate_fails_without_paper(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.6,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status != "live_candidate"

    def test_live_candidate_requires_eligible_paper_package(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.6,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5,
                            paper_days=60, paper_sharpe=0.5, paper_excess=1.0,
                            paper_evidence_recommendation="BLOCKED",
                            paper_benchmark_final_ratio=0.9,
                            paper_sell_count=6,
                            paper_win_rate=50.0,
                            paper_frozen_days=0,
                            paper_cumulative_return=2.0)
        r = promote(m)
        assert r.status != "live_candidate"
        ok, reason = _check_live_candidate(m)
        assert ok is False
        assert "paper evidence recommendation" in reason

    def test_experiment_note_does_not_affect_status(self):
        m = StrategyMetrics("test", total_return=-5, profit_factor=0.8, mdd=-10,
                            wf_positive_rate=0, wf_sharpe_positive_rate=0,
                            wf_windows=6, wf_total_trades=100, sharpe=-1)
        r1 = promote(m, experiment_note="")
        r2 = promote(m, experiment_note="현재 Paper 실험 가동 중")
        assert r1.status == r2.status == "research_only"

    def test_insufficient_wf_windows_blocks_candidate(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-8,
                            wf_positive_rate=1.0, wf_sharpe_positive_rate=1.0,
                            wf_windows=2, wf_total_trades=20, sharpe=0.5)
        r = promote(m)
        # wf_windows < 3 또는 wf_total_trades < 30 → provisional 미달
        assert r.status in ("paper_only", "research_only")

    def test_mdd_too_deep_blocks_candidate(self):
        m = StrategyMetrics("test", total_return=10, profit_factor=1.5, mdd=-25,
                            wf_positive_rate=0.8, wf_sharpe_positive_rate=0.6,
                            wf_windows=6, wf_total_trades=100, sharpe=0.5)
        r = promote(m)
        assert r.status == "paper_only"  # MDD -25% < -20%


# ── 2. debiased 결과 자동 판정 ──

class TestDebiasedPromotions:
    def test_rotation_is_provisional(self):
        r = promote(_TEST_METRICS["relative_strength_rotation"])
        assert r.status == "provisional_paper_candidate"

    def test_scoring_is_paper_only(self):
        """scoring: 절대수익은 양수지만 Sharpe/PF/WF 안정성 미달 → paper_only."""
        r = promote(_TEST_METRICS["scoring"])
        assert r.status == "paper_only"

    def test_breakout_volume_is_research_only(self):
        """BV는 return<0, PF<1 → research_only. paper_only가 되면 안 됨."""
        r = promote(_TEST_METRICS["breakout_volume"])
        assert r.status == "research_only", \
            f"breakout_volume이 {r.status}로 판정됨 — research_only여야 함 (ret<0, PF<1)"

    def test_mean_reversion_is_research_only(self):
        r = promote(_TEST_METRICS["mean_reversion"])
        assert r.status == "research_only"

    def test_trend_following_is_research_only(self):
        r = promote(_TEST_METRICS["trend_following"])
        assert r.status == "research_only"

    def test_ensemble_is_research_only(self):
        r = promote(_TEST_METRICS["ensemble"])
        assert r.status == "research_only"

    def test_no_live_candidate_exists(self):
        """현재 어떤 전략도 live_candidate가 아니어야 함."""
        for name, m in _TEST_METRICS.items():
            r = promote(m)
            assert r.status != "live_candidate", f"{name}이 live_candidate로 판정됨"


# ── 3. Registry 정합성 CI 검증 (핵심) ──

class TestRegistryConsistency:
    """STRATEGY_STATUS가 promotion engine 결과와 일치하는지 검증.
    이 테스트가 실패하면 수동 상태 기입이 자동 판정과 모순된 것."""

    def test_all_statuses_match_engine(self):
        """모든 전략의 registry 상태가 promotion engine 판정과 일치."""
        from strategies import STRATEGY_STATUS

        # test fixture metrics로 엔진 판정
        status_map = {
            "disabled": "research_only",
            "experimental": "research_only",
            "paper_only": "paper_only",
            "paper_candidate": "provisional_paper_candidate",
            "provisional_paper_candidate": "provisional_paper_candidate",
            "live_candidate": "live_candidate",
        }

        mismatches = []
        for name, m in _TEST_METRICS.items():
            if name not in STRATEGY_STATUS:
                continue
            eng_result = promote(m)
            reg_status = STRATEGY_STATUS[name]["status"]
            normalized_reg = status_map.get(reg_status, reg_status)

            if normalized_reg != eng_result.status:
                mismatches.append(
                    f"{name}: registry={reg_status}(→{normalized_reg}), "
                    f"engine={eng_result.status}, reason={eng_result.reason}"
                )

        assert not mismatches, (
            "Registry-Engine 불일치 발견:\n" + "\n".join(f"  {m}" for m in mismatches)
        )

    def test_no_impossible_combination(self):
        """return<0 또는 PF<1인 전략이 paper 허용 상태이면 안 됨."""
        from strategies import STRATEGY_STATUS

        for name, st in STRATEGY_STATUS.items():
            if st["status"] in ("paper_only", "paper_candidate", "provisional_paper_candidate"):
                m = _TEST_METRICS.get(name)
                if m is None:
                    continue
                assert m.total_return > 0, \
                    f"{name}: return {m.total_return}% ≤ 0인데 status={st['status']}"
                assert m.profit_factor >= 1.0, \
                    f"{name}: PF {m.profit_factor} < 1.0인데 status={st['status']}"

    def test_allowed_modes_consistent(self):
        """status별 allowed_modes가 올바른지."""
        from strategies import STRATEGY_STATUS

        for name, st in STRATEGY_STATUS.items():
            status = st["status"]
            modes = st["allowed_modes"]
            if status in ("disabled", "research_only"):
                assert modes == ["backtest"], \
                    f"{name}: {status}인데 allowed_modes={modes}"
            elif status in ("paper_only", "paper_candidate", "provisional_paper_candidate"):
                assert "paper" in modes, \
                    f"{name}: {status}인데 paper 미허용"
                assert "live" not in modes, \
                    f"{name}: {status}인데 live 허용"


# ── 4. is_strategy_allowed 실행 허용 범위 ──

class TestAllowedScope:
    def test_research_only_blocks_paper(self):
        from strategies import is_strategy_allowed
        # BV는 registry에서 research_only로 바뀌어야 함
        # 현재 registry가 engine과 일치하면 BV는 paper 차단됨
        from strategies import STRATEGY_STATUS
        for name, st in STRATEGY_STATUS.items():
            if st["status"] in ("disabled", "research_only"):
                allowed, _ = is_strategy_allowed(name, "paper")
                assert not allowed, f"{name}({st['status']})이 paper에서 허용됨"

    def test_all_block_live(self):
        from strategies import is_strategy_allowed, STRATEGY_STATUS
        for name in STRATEGY_STATUS:
            allowed, _ = is_strategy_allowed(name, "live")
            assert not allowed, f"{name}이 live에서 허용됨"

    def test_paper_only_allows_paper(self):
        from strategies import is_strategy_allowed
        allowed, _ = is_strategy_allowed("scoring", "paper")
        assert allowed, "scoring(paper_only)이 paper에서 차단됨"

    def test_provisional_allows_paper(self):
        from strategies import is_strategy_allowed
        allowed, _ = is_strategy_allowed("relative_strength_rotation", "paper")
        assert allowed, "rotation(provisional)이 paper에서 차단됨"


# ── 5. Artifact 기반 로드 ──

class TestArtifactLoading:
    def test_load_missing_artifact_returns_none(self):
        result = load_promotion_artifact("/nonexistent/path")
        assert result is None

    def test_load_valid_artifact(self):
        """artifact가 있으면 promotion 결과를 반환."""
        import tempfile, json
        from pathlib import Path
        from core.live_gate import LIVE_GATE_ARTIFACT_TYPE, LIVE_GATE_SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "metrics_summary.json").write_text("{}", encoding="utf-8")
            (base / "walk_forward_summary.json").write_text("{}", encoding="utf-8")
            (base / "benchmark_comparison.json").write_text(
                json.dumps({
                    "strategy_excess_return_pct": {"scoring": 1.0},
                    "strategy_excess_sharpe": {"scoring": 0.2},
                }),
                encoding="utf-8",
            )
            (base / "run_metadata.json").write_text(
                json.dumps({
                    "schema_version": LIVE_GATE_SCHEMA_VERSION,
                    "artifact_type": LIVE_GATE_ARTIFACT_TYPE,
                    "commit_hash": "abc",
                }),
                encoding="utf-8",
            )
            (base / "promotion_result.json").write_text(
                json.dumps({"scoring": {"status": "paper_only", "allowed_modes": ["backtest", "paper"], "reason": "test"}}),
                encoding="utf-8",
            )
            result = load_promotion_artifact(str(base))
            assert result is not None
            assert "scoring" in result
            assert result["scoring"]["status"] == "paper_only"

    def test_artifact_schema_mismatch_returns_none(self):
        """schema가 다르면 None 반환."""
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "metrics_summary.json").write_text("{}", encoding="utf-8")
            (base / "walk_forward_summary.json").write_text("{}", encoding="utf-8")
            (base / "benchmark_comparison.json").write_text(
                json.dumps({
                    "strategy_excess_return_pct": {"scoring": 1.0},
                    "strategy_excess_sharpe": {"scoring": 0.2},
                }),
                encoding="utf-8",
            )
            (base / "run_metadata.json").write_text("{}", encoding="utf-8")
            # schema 오류: status 키 없음
            (base / "promotion_result.json").write_text(
                json.dumps({"scoring": {"wrong_key": "value"}}),
                encoding="utf-8",
            )
            result = load_promotion_artifact(str(base))
            assert result is None
