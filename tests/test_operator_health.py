"""운영자 통합 헬스 요약(core.operator_health) 단위 테스트."""

from types import SimpleNamespace

from core.operator_health import (
    summarize_runtime_state,
    summarize_blockers,
    build_operator_health,
)


def _state(strategy, state, manual_freeze=False, anomalies=None):
    return SimpleNamespace(
        strategy=strategy,
        state=state,
        manual_freeze=manual_freeze,
        last_anomalies=anomalies or [],
    )


class TestSummarizeRuntimeState:
    def test_normal_is_ok(self):
        out = summarize_runtime_state(_state("scoring", "normal"))
        assert out["verdict"] == "OK"
        assert out["notes"] == []

    def test_frozen_is_blocked(self):
        out = summarize_runtime_state(_state("scoring", "frozen"))
        assert out["verdict"] == "BLOCKED"

    def test_degraded_is_attention(self):
        out = summarize_runtime_state(_state("scoring", "degraded"))
        assert out["verdict"] == "ATTENTION"

    def test_blocked_insufficient_evidence_is_attention(self):
        out = summarize_runtime_state(_state("rotation", "blocked_insufficient_evidence"))
        assert out["verdict"] == "ATTENTION"

    def test_manual_freeze_on_normal_is_attention(self):
        out = summarize_runtime_state(_state("scoring", "normal", manual_freeze=True))
        assert out["verdict"] == "ATTENTION"
        assert "manual_freeze" in out["notes"]

    def test_anomalies_on_normal_is_attention(self):
        out = summarize_runtime_state(_state("scoring", "normal", anomalies=[{"type": "x"}]))
        assert out["verdict"] == "ATTENTION"
        assert out["anomaly_count"] == 1


class TestSummarizeBlockers:
    def test_none_is_attention(self):
        out = summarize_blockers(None)
        assert out["verdict"] == "ATTENTION"
        assert out["freshness_stale"] is True

    def test_hard_blockers_is_blocked(self):
        out = summarize_blockers({"go_live": False, "hard_blockers": ["x", "y"]})
        assert out["verdict"] == "BLOCKED"
        assert out["hard_blocker_count"] == 2

    def test_stale_artifact_is_blocked(self):
        out = summarize_blockers({
            "go_live": False, "hard_blockers": [],
            "promotion_artifact_freshness": {"stale": True},
        })
        assert out["verdict"] == "BLOCKED"
        assert out["freshness_stale"] is True

    def test_clean_blockers_is_ok(self):
        out = summarize_blockers({
            "go_live": False, "hard_blockers": [],
            "live_candidates": [],
            "promotion_artifact_freshness": {"stale": False},
        })
        assert out["verdict"] == "OK"

    def test_inconsistent_go_live_is_attention(self):
        out = summarize_blockers({
            "go_live": False, "hard_blockers": [],
            "live_candidates": ["scoring"],
            "promotion_artifact_freshness": {"stale": False},
        })
        assert out["verdict"] == "ATTENTION"
        assert any("go_live" in n for n in out["notes"])


class TestBuildOperatorHealth:
    def test_all_ok(self):
        states = [_state("scoring", "normal"), _state("rotation", "normal")]
        blockers = {"go_live": False, "hard_blockers": [], "live_candidates": [],
                    "promotion_artifact_freshness": {"stale": False}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "OK"
        assert out["strategy_count"] == 2
        assert out["attention_items"] == []
        assert "운영 정상" in out["headline"]

    def test_one_frozen_makes_blocked(self):
        states = [_state("scoring", "normal"), _state("rotation", "frozen")]
        blockers = {"go_live": False, "hard_blockers": [], "live_candidates": [],
                    "promotion_artifact_freshness": {"stale": False}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "BLOCKED"
        assert any("rotation" in item for item in out["attention_items"])

    def test_degraded_strategy_makes_attention(self):
        states = [_state("scoring", "degraded")]
        blockers = {"go_live": False, "hard_blockers": [], "live_candidates": [],
                    "promotion_artifact_freshness": {"stale": False}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "ATTENTION"

    def test_hard_blocker_is_gate_status_not_operational_failure(self):
        """승격 게이트 NO-GO(hard_blocker)는 '진행 단계'지 장애가 아니다 — 알파 없음이
        정착 결론인 체제에서 상시 NO-GO를 전체 BLOCKED로 합산하면 운영자가 매일
        빨강을 보다가 진짜 장애를 못 알아본다(알람 피로). 전체 verdict는 운영
        건강 기준, 게이트 상태는 헤드라인 라벨로 보고한다."""
        states = [_state("scoring", "normal")]
        blockers = {"go_live": False, "hard_blockers": ["paper_evidence_insufficient"],
                    "live_candidates": [], "promotion_artifact_freshness": {"stale": False}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "OK"
        assert "NO-GO" in out["headline"]
        assert out["blockers"]["hard_blocker_count"] == 1  # 게이트 차원 정보는 보존

    def test_stale_artifact_still_degrades_overall_verdict(self):
        """게이트 차원이라도 '장애성' 신호(artifact stale)는 전체 verdict에 반영."""
        states = [_state("scoring", "normal")]
        blockers = {"go_live": False, "hard_blockers": [],
                    "live_candidates": [], "promotion_artifact_freshness": {"stale": True}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "ATTENTION"
        assert any("artifact_stale" in i for i in out["attention_items"])

    def test_empty_strategies_with_clean_blockers(self):
        out = build_operator_health([], {
            "go_live": False, "hard_blockers": [], "live_candidates": [],
            "promotion_artifact_freshness": {"stale": False},
        })
        assert out["verdict"] == "OK"
        assert out["strategy_count"] == 0

    def test_missing_blockers_is_attention(self):
        out = build_operator_health([_state("scoring", "normal")], None)
        assert out["verdict"] == "ATTENTION"


class TestSummarizeBasketOperation:
    """바스켓 paper 운영(트랙레코드) 헬스 — 일일 사이클 끊김 감지."""

    def _summ(self, **kw):
        from datetime import date
        from core.operator_health import summarize_basket_operation
        defaults = dict(
            enabled_baskets=["kr_diversified_hold"],
            last_snapshot_date=date(2026, 6, 10),
            position_count=6,
            today=date(2026, 6, 10),
        )
        defaults.update(kw)
        return summarize_basket_operation(**defaults)

    def test_fresh_snapshot_is_ok(self):
        out = self._summ()
        assert out["verdict"] == "OK"
        assert out["stale_days"] == 0

    def test_no_enabled_baskets_is_ok_with_note(self):
        out = self._summ(enabled_baskets=[])
        assert out["verdict"] == "OK"
        assert any("운영 안 함" in n for n in out["notes"])

    def test_enabled_but_no_snapshot_is_attention(self):
        out = self._summ(last_snapshot_date=None)
        assert out["verdict"] == "ATTENTION"
        assert any("스냅샷 없음" in n for n in out["notes"])

    def test_stale_snapshot_is_attention(self):
        from datetime import date
        out = self._summ(last_snapshot_date=date(2026, 6, 1), today=date(2026, 6, 10))
        assert out["verdict"] == "ATTENTION"
        assert out["stale_days"] == 9
        assert any("끊김" in n for n in out["notes"])

    def test_weekend_gap_within_threshold_is_ok(self):
        """금요일 스냅샷 → 화요일 점검(4일)은 주말+월 휴장 커버로 OK."""
        from datetime import date
        out = self._summ(last_snapshot_date=date(2026, 6, 5), today=date(2026, 6, 9))
        assert out["verdict"] == "OK"
        assert out["stale_days"] == 4

    def test_datetime_inputs_are_normalized(self):
        from datetime import datetime, date
        out = self._summ(
            last_snapshot_date=datetime(2026, 6, 10, 0, 0),
            today=date(2026, 6, 10),
        )
        assert out["verdict"] == "OK"

    def test_underdeployment_escalates_to_attention(self):
        # 신선한 스냅샷이라도 배치율 미달(59% vs 설계 80%, -21%p > 5%p)이면 ATTENTION
        out = self._summ(deployment_ratio=0.59, design_fraction=0.80)
        assert out["verdict"] == "ATTENTION"
        assert any("배치율" in n for n in out["notes"])
        assert out["deployment_ratio"] == 0.59

    def test_deployment_within_tolerance_stays_ok(self):
        out = self._summ(deployment_ratio=0.77, design_fraction=0.80)  # -3%p ≤ 5%p
        assert out["verdict"] == "OK"

    def test_deployment_none_is_ok(self):
        out = self._summ(deployment_ratio=None, design_fraction=None)
        assert out["verdict"] == "OK"

    def test_per_basket_tolerance_relaxes_attention(self):
        # 관찰용 강등 바스켓: 허용 오차 완화 시 큰 미달도 OK (경보 피로 방지)
        out = self._summ(
            deployment_ratio=0.59, design_fraction=0.80, deployment_tolerance=1.0,
        )
        assert out["verdict"] == "OK"
        # 소액 구조적 절사(10%p 허용): -7.3%p는 OK, -12%p는 ATTENTION
        ok = self._summ(deployment_ratio=0.427, design_fraction=0.50, deployment_tolerance=0.10)
        assert ok["verdict"] == "OK"
        bad = self._summ(deployment_ratio=0.38, design_fraction=0.50, deployment_tolerance=0.10)
        assert bad["verdict"] == "ATTENTION"


class TestSummarizeDeployment:
    """집계 배치율 미달 판정(순수 함수)."""

    def _f(self, ratio, design, **kw):
        from core.operator_health import summarize_deployment
        return summarize_deployment(ratio, design, **kw)

    def test_shortfall_beyond_tolerance_is_attention(self):
        out = self._f(0.61, 0.80)
        assert out["verdict"] == "ATTENTION"
        assert "61%" in out["note"] and "80%" in out["note"]

    def test_within_tolerance_ok(self):
        assert self._f(0.76, 0.80)["verdict"] == "OK"

    def test_overdeployment_is_ok(self):
        # 초과 배치는 리밸런서가 자연 교정 — 감시 대상 아님
        assert self._f(0.90, 0.80)["verdict"] == "OK"

    def test_none_inputs_ok(self):
        assert self._f(None, 0.80)["verdict"] == "OK"
        assert self._f(0.6, None)["verdict"] == "OK"

    def test_custom_tolerance(self):
        assert self._f(0.75, 0.80, tolerance=0.10)["verdict"] == "OK"   # -5%p ≤ 10%p
        assert self._f(0.68, 0.80, tolerance=0.10)["verdict"] == "ATTENTION"  # -12%p

    def test_exact_boundary_is_ok_strict_comparison(self):
        # shortfall == tolerance 이면 OK(엄격 '>' 고정). tolerance=0으로 부동소수 오차 없이
        # 경계를 못박는다 — '>'를 '>='로 바꾸면 완벽 배치도 ATTENTION이 되어 이 테스트가 잡는다.
        assert self._f(0.80, 0.80, tolerance=0.0)["verdict"] == "OK"
        assert self._f(0.79, 0.80, tolerance=0.0)["verdict"] == "ATTENTION"


class TestBuildOperatorHealthWithBasket:
    def test_basket_attention_escalates_overall_verdict(self):
        from datetime import date
        health = build_operator_health(
            [_state("scoring", "normal")],
            {"go_live": False, "hard_blockers": [], "live_candidates": []},
            basket_operation={
                "enabled_baskets": ["kr_diversified_hold"],
                "last_snapshot_date": None,
                "position_count": 0,
                "today": date(2026, 6, 10),
            },
        )
        assert health["verdict"] == "ATTENTION"
        assert health["basket"]["verdict"] == "ATTENTION"
        assert any(item.startswith("basket:") for item in health["attention_items"])

    def test_without_basket_input_section_is_none(self):
        """하위 호환: basket_operation 미전달 시 기존 동작 그대로."""
        health = build_operator_health(
            [_state("scoring", "normal")],
            {"go_live": False, "hard_blockers": [], "live_candidates": []},
        )
        assert health["basket"] is None
        assert health["verdict"] == "OK"
