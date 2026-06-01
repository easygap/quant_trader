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
        assert "전체 정상" in out["headline"]

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

    def test_hard_blocker_dominates_even_if_strategies_ok(self):
        states = [_state("scoring", "normal")]
        blockers = {"go_live": False, "hard_blockers": ["paper_evidence_insufficient"],
                    "live_candidates": [], "promotion_artifact_freshness": {"stale": False}}
        out = build_operator_health(states, blockers)
        assert out["verdict"] == "BLOCKED"

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
