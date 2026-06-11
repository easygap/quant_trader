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
