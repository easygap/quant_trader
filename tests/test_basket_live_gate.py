"""바스켓 전용 live readiness gate 회귀 테스트.

배경: 공통 live gate는 신호 전략의 canonical promotion 체계(canonical bundle·
live_candidate·벤치마크 양의 초과수익)를 요구한다. 바스켓(베타 전략)은 그 체계
밖이라 60영업일 paper가 완벽해도 영구 통과 불가였다 — basket_rebalance:* 승인
단위는 바스켓 전용 게이트(paper 운영 평가 PASS_CANDIDATE)로 분기해야 한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from core.live_readiness import check_basket_live_readiness, check_live_readiness_gate

_BASKETS = {
    "kr_diversified_hold": {
        "enabled": True,
        "holdings": {"005930": 0.5, "000660": 0.5},
    },
    "kr_disabled": {
        "enabled": False,
        "holdings": {"005930": 1.0},
    },
    "kr_bad_weights": {
        "enabled": True,
        "holdings": {"005930": 0.5, "000660": 0.4},  # 합 0.9
    },
}


def _eval_result(verdict, issues=None, progress=60):
    return {
        "verdict": verdict,
        "issues": issues or [],
        "progress_days": progress,
        "min_trading_days": 60,
    }


def _config(use_mock=False):
    """게이트 테스트용 config — 기본은 실계좌(use_mock=False, 평가 차단 적용)."""
    return SimpleNamespace(kis_api={"use_mock": use_mock})


class TestBasketLiveGate:
    def _run(self, strategy_name, verdict="PASS_CANDIDATE", eval_issues=None,
             progress=60, use_mock=False, baskets=None):
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=baskets or _BASKETS,
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            return_value=(_eval_result(verdict, eval_issues, progress), "label"),
        ) as collect:
            issues = check_basket_live_readiness(_config(use_mock), strategy_name)
        self._last_collect = collect
        return issues

    def test_pass_candidate_opens_gate(self):
        issues = self._run("basket_rebalance:kr_diversified_hold")
        assert issues == []

    def test_gate_evaluates_its_own_basket_record(self):
        """게이트는 반드시 '자기 바스켓'의 기록으로 평가한다 — 이름 없이 합산하면
        다른 바스켓의 60일 트랙레코드로 신규 바스켓이 승격되는 구멍."""
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_BASKETS,
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            return_value=(_eval_result("PASS_CANDIDATE"), "label"),
        ) as collect:
            check_basket_live_readiness(
                _config(), "basket_rebalance:kr_diversified_hold"
            )
        assert collect.call_args.kwargs["basket_name"] == "kr_diversified_hold"

    def test_wait_blocks_with_progress_reason(self):
        issues = self._run(
            "basket_rebalance:kr_diversified_hold", verdict="WAIT", progress=1
        )
        assert len(issues) == 1
        assert "WAIT" in issues[0] and "1/60" in issues[0]

    def test_fail_review_blocks_with_eval_issues(self):
        issues = self._run(
            "basket_rebalance:kr_diversified_hold",
            verdict="FAIL_REVIEW",
            eval_issues=["스냅샷 커버리지 80% < 95%"],
        )
        assert len(issues) == 1
        assert "커버리지" in issues[0]

    def test_missing_basket_name_blocks(self):
        issues = self._run("basket_rebalance")
        assert any("바스켓 이름" in i for i in issues)

    def test_unknown_basket_blocks(self):
        issues = self._run("basket_rebalance:nonexistent")
        assert any("baskets.yaml에 없습니다" in i for i in issues)

    def test_disabled_basket_blocks(self):
        issues = self._run("basket_rebalance:kr_disabled")
        assert any("enabled=false" in i for i in issues)

    def test_bad_weight_sum_blocks(self):
        issues = self._run("basket_rebalance:kr_bad_weights")
        assert any("비중 합" in i for i in issues)

    def test_evaluation_error_fails_closed(self):
        with patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_BASKETS,
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            side_effect=RuntimeError("db corrupted"),
        ):
            issues = check_basket_live_readiness(
                _config(), "basket_rebalance:kr_diversified_hold"
            )
        assert any("fail-closed" in i for i in issues)


class TestGateRouting:
    def test_signal_strategy_uses_canonical_gate(self):
        """비바스켓 전략명은 기존 canonical 게이트(validate_live_readiness)로 간다."""
        with patch(
            "core.live_gate.validate_live_readiness",
            return_value=["canonical issue"],
        ) as canonical:
            issues = check_live_readiness_gate(_config(), "scoring")
        canonical.assert_called_once()
        assert issues == ["canonical issue"]

    def test_basket_strategy_skips_canonical_gate(self):
        """basket_rebalance:*는 canonical 게이트를 호출하지 않는다."""
        with patch(
            "core.live_gate.validate_live_readiness",
            return_value=["should not be called"],
        ) as canonical, patch(
            "core.basket_rebalancer.BasketRebalancer._load_baskets_config",
            return_value=_BASKETS,
        ), patch(
            "core.basket_evaluation.collect_basket_paper_evaluation",
            return_value=(_eval_result("WAIT", progress=1), "label"),
        ):
            issues = check_live_readiness_gate(
                _config(), "basket_rebalance:kr_diversified_hold"
            )
        canonical.assert_not_called()
        assert len(issues) == 1 and "WAIT" in issues[0]


class TestTimelineLevers:
    """live 전환 타임라인 레버 — 모의서버 리허설 즉시 허용 + 승격 기간 운영자 설정."""

    def _run(self, **kw):
        return TestBasketLiveGate._run(TestBasketLiveGate(), **kw)

    def test_mock_server_allows_rehearsal_during_wait(self):
        """use_mock=true(KIS 모의서버, 실돈 아님)면 평가 WAIT여도 게이트 통과 —
        런북 Phase 1 리허설을 60일 평가와 병행 가능."""
        issues = self._run(
            strategy_name="basket_rebalance:kr_diversified_hold",
            verdict="WAIT", progress=1, use_mock=True,
        )
        assert issues == []

    def test_real_account_still_blocked_during_wait(self):
        """실계좌(use_mock=false)는 평가 통과 전 차단 유지(기존 보호 불변)."""
        issues = self._run(
            strategy_name="basket_rebalance:kr_diversified_hold",
            verdict="WAIT", progress=1, use_mock=False,
        )
        assert len(issues) == 1 and "WAIT" in issues[0]

    def test_mock_server_does_not_waive_config_issues(self):
        """모의서버 완화는 '평가 기간'에만 적용 — 비중합 오류 같은 설정 결함은 여전히 차단."""
        issues = self._run(
            strategy_name="basket_rebalance:kr_bad_weights",
            verdict="WAIT", use_mock=True,
        )
        assert any("비중 합" in i for i in issues)

    def test_promotion_min_days_from_basket_config(self):
        """baskets.yaml의 promotion.min_trading_days가 평가 기간으로 전달된다."""
        baskets = {
            "kr_diversified_hold": {
                "enabled": True,
                "holdings": {"005930": 1.0},
                "promotion": {"min_trading_days": 20},
            },
        }
        gate = TestBasketLiveGate()
        gate._run(
            strategy_name="basket_rebalance:kr_diversified_hold",
            baskets=baskets,
        )
        assert gate._last_collect.call_args.kwargs["min_days"] == 20
