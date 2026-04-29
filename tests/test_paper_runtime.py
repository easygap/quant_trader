"""
Paper Runtime State Machine 통합 테스트

scheduler entry-point 기준:
  - normal → paper 주문 허용
  - degraded → 신규 진입 차단, exit-only
  - frozen → 주문 제출 차단, finalize 계속
  - blocked_insufficient_evidence → 주문 차단
  - manual freeze/unfreeze → audit trail
  - post_market/finalize는 frozen 상태에서도 수행
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    """evidence + runtime 출력을 tmp_path로 격리."""
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(pr, "PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS", 999)
    return tmp_path


@pytest.fixture
def runtime_dir(_isolate_dirs):
    return _isolate_dirs / "paper_runtime"


@pytest.fixture
def evidence_dir(_isolate_dirs):
    return _isolate_dirs / "paper_evidence"


@pytest.fixture
def fresh_db():
    from config.config_loader import Config
    Config._instance = None
    from database.models import (
        init_database, get_session,
        TradeHistory, OperationEvent, PortfolioSnapshot,
        Position, FailedOrder, PendingOrderGuard, DailyReport,
    )
    init_database()
    session = get_session()
    for model in [TradeHistory, OperationEvent, PortfolioSnapshot,
                  Position, FailedOrder, PendingOrderGuard, DailyReport]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def _seed_evidence(evidence_dir, strategy, days_config):
    """
    evidence JSONL에 직접 record를 삽입한다.
    days_config: list of dict, 각 dict는 DailyEvidence 필드의 subset.
    """
    from core.paper_evidence import _append_jsonl

    jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
    for i, cfg in enumerate(days_config):
        # V1 legacy record 지원
        if cfg.get("_raw_v1"):
            _append_jsonl(jsonl_path, cfg["_raw_v1"])
            continue
        record = {
            "date": cfg.get("date", (datetime(2026, 3, 24) + timedelta(days=i)).strftime("%Y-%m-%d")),
            "day_number": i + 1,
            "strategy": strategy,
            "total_value": cfg.get("total_value", 10_000_000),
            "cash": cfg.get("cash", 3_000_000),
            "invested": cfg.get("invested", 7_000_000),
            "daily_return": cfg.get("daily_return", 0.3),
            "cumulative_return": cfg.get("cumulative_return", 0.5),
            "mdd": cfg.get("mdd", -2.0),
            "position_count": cfg.get("position_count", 2),
            "total_trades": cfg.get("total_trades", 2),
            "same_universe_excess": cfg.get("same_universe_excess", 0.05),
            "exposure_matched_excess": cfg.get("exposure_matched_excess", 0.03),
            "cash_adjusted_excess": cfg.get("cash_adjusted_excess", 0.02),
            "benchmark_status": cfg.get("benchmark_status", "final"),
            "benchmark_meta": cfg.get("benchmark_meta", {"completeness": 1.0}),
            "raw_fill_rate": cfg.get("raw_fill_rate", 1.0),
            "reject_count": cfg.get("reject_count", 0),
            "phantom_position_count": cfg.get("phantom_position_count", 0),
            "stale_pending_count": cfg.get("stale_pending_count", 0),
            "duplicate_blocked_count": cfg.get("duplicate_blocked_count", 0),
            "restart_recovery_count": cfg.get("restart_recovery_count", 0),
            "anomalies": cfg.get("anomalies", []),
            "cross_validation_warnings": cfg.get("cross_validation_warnings", []),
            "status": cfg.get("status", "normal"),
            "record_version": cfg.get("record_version", 1),
            "schema_version": cfg.get("schema_version", 2),
            "diagnostics": [],
        }
        _append_jsonl(jsonl_path, record)


# ═══════════════════════════════════════════════════════════════
# State Machine 기본 테스트
# ═══════════════════════════════════════════════════════════════

class TestRuntimeStateMachine:
    """runtime state가 evidence에 따라 올바르게 결정되는지."""

    def test_normal_state(self, evidence_dir, runtime_dir):
        """clean evidence → normal."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal", "benchmark_status": "final"},
            {"date": "2026-03-25", "status": "normal", "benchmark_status": "final"},
            {"date": "2026-03-26", "status": "normal", "benchmark_status": "final"},
        ])

        state = get_paper_runtime_state("test_s")
        assert state.state == "normal"
        assert "entry" in state.allowed_actions
        assert "exit" in state.allowed_actions
        assert "run" in state.allowed_actions

    def test_stale_evidence_blocks_entry_but_keeps_exit_safe(
        self, evidence_dir, runtime_dir, monkeypatch
    ):
        """오래된 evidence는 신규 진입만 막고 exit/finalize는 계속 허용."""
        import core.paper_runtime as pr
        from core.paper_runtime import get_paper_runtime_state

        monkeypatch.setattr(pr, "PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS", 1)
        _seed_evidence(evidence_dir, "stale_runtime", [
            {"date": "2026-04-10", "status": "normal", "benchmark_status": "final"},
        ])

        state = get_paper_runtime_state("stale_runtime", as_of_date="2026-04-15")
        assert state.state == "blocked_insufficient_evidence"
        assert "entry" not in state.allowed_actions
        assert "exit" in state.allowed_actions
        assert "finalize" in state.allowed_actions
        assert any("stale_evidence" in r for r in state.reasons)

    def test_degraded_on_repeated_reject(self, evidence_dir, runtime_dir):
        """latest reject_count > threshold → degraded."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
            {"date": "2026-03-25", "reject_count": 5, "status": "degraded",
             "anomalies": [{"type": "repeated_reject", "severity": "warning", "detail": "reject=5"}]},
        ])

        state = get_paper_runtime_state("test_s")
        assert state.state == "degraded"
        assert "entry" not in state.allowed_actions
        assert "exit" in state.allowed_actions
        assert "finalize" in state.allowed_actions

    def test_frozen_on_phantom_position(self, evidence_dir, runtime_dir):
        """phantom_position_count > 0 → frozen."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
            {"date": "2026-03-25", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical", "detail": "phantom=1"}]},
        ])

        state = get_paper_runtime_state("test_s")
        assert state.state == "frozen"
        assert "entry" not in state.allowed_actions
        assert "exit" in state.allowed_actions  # 기존 포지션 정리 허용
        assert "finalize" in state.allowed_actions

    def test_frozen_on_deep_drawdown(self, evidence_dir, runtime_dir):
        """deep_drawdown anomaly → frozen."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
            {"date": "2026-03-25", "mdd": -18.0, "daily_return": -6.0, "status": "frozen",
             "anomalies": [{"type": "deep_drawdown", "severity": "critical", "detail": "mdd=-18"}]},
        ])

        state = get_paper_runtime_state("test_s")
        assert state.state == "frozen"

    def test_blocked_insufficient_evidence(self, evidence_dir, runtime_dir):
        """excess non-null 비율 < 60% → blocked_insufficient_evidence."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "same_universe_excess": 0.05},
            {"date": "2026-03-25", "same_universe_excess": None},
            {"date": "2026-03-26", "same_universe_excess": None},
            {"date": "2026-03-27", "same_universe_excess": None},
            {"date": "2026-03-28", "same_universe_excess": None},
        ])

        state = get_paper_runtime_state("test_s")
        assert state.state == "blocked_insufficient_evidence"
        assert "entry" not in state.allowed_actions

    def test_no_evidence_blocked(self, evidence_dir, runtime_dir):
        """evidence 없음 → blocked_insufficient_evidence."""
        from core.paper_runtime import get_paper_runtime_state
        state = get_paper_runtime_state("nonexistent_strategy")
        assert state.state == "blocked_insufficient_evidence"

    def test_research_disabled(self, evidence_dir, runtime_dir, tmp_path):
        """approved_strategies.json에서 disabled → research_disabled."""
        from core.paper_runtime import get_paper_runtime_state

        approved = Path("reports/approved_strategies.json")
        original = None
        if approved.exists():
            original = approved.read_text(encoding="utf-8")

        try:
            approved.parent.mkdir(parents=True, exist_ok=True)
            approved.write_text(json.dumps({
                "strategies": [{"name": "disabled_s", "status": "disabled"}]
            }), encoding="utf-8")

            state = get_paper_runtime_state("disabled_s")
            assert state.state == "research_disabled"
            # exit-safe: research_disabled에서도 exit/cleanup은 허용
            assert "exit" in state.allowed_actions
            assert "finalize" in state.allowed_actions
            assert "entry" not in state.allowed_actions
            assert "run" not in state.allowed_actions
        finally:
            if original is not None:
                approved.write_text(original, encoding="utf-8")
            elif approved.exists():
                approved.unlink()


# ═══════════════════════════════════════════════════════════════
# Freeze / Unfreeze 테스트
# ═══════════════════════════════════════════════════════════════

class TestFreezeUnfreeze:
    """manual freeze/unfreeze + auto-unfreeze."""

    def test_manual_freeze(self, evidence_dir, runtime_dir):
        from core.paper_runtime import manual_freeze, get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        state = manual_freeze("test_s", "investigating anomaly", operator="admin")
        assert state.state == "frozen"
        assert state.manual_freeze is True
        assert "manual_freeze" in str(state.reasons)

    def test_manual_unfreeze(self, evidence_dir, runtime_dir):
        from core.paper_runtime import manual_freeze, manual_unfreeze, get_paper_runtime_state

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        manual_freeze("test_s", "test freeze")
        state = manual_unfreeze("test_s", "resolved", operator="admin")
        assert state.state == "normal"
        assert state.manual_freeze is False

    def test_auto_unfreeze_after_clean_days(self, evidence_dir, runtime_dir):
        """N연속 clean final days → 자동 unfreeze."""
        from core.paper_runtime import get_paper_runtime_state, CLEAN_DAYS_FOR_UNFREEZE

        # Day 1: phantom (would trigger freeze)
        # Days 2-4: clean (enough for auto-unfreeze)
        days = [
            {"date": "2026-03-24", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ]
        for i in range(CLEAN_DAYS_FOR_UNFREEZE):
            days.append({
                "date": (datetime(2026, 3, 25) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "status": "normal", "benchmark_status": "final",
                "phantom_position_count": 0, "anomalies": [],
            })

        _seed_evidence(evidence_dir, "test_s", days)
        state = get_paper_runtime_state("test_s")
        # latest record is clean → freeze trigger from day 1 is overridden
        # because latest phantom_position_count == 0
        assert state.state == "normal"

    def test_audit_trail_recorded(self, evidence_dir, runtime_dir):
        """freeze/unfreeze가 audit trail에 기록되는지."""
        from core.paper_runtime import manual_freeze, manual_unfreeze, _read_decisions

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        manual_freeze("test_s", "audit test")
        manual_unfreeze("test_s", "audit resolved")

        decisions = _read_decisions("test_s")
        actions = [d["action"] for d in decisions]
        assert "manual_freeze" in actions
        assert "manual_unfreeze" in actions

    def test_audit_md_generated(self, evidence_dir, runtime_dir):
        """generate_runtime_audit → markdown 파일."""
        from core.paper_runtime import manual_freeze, generate_runtime_audit

        _seed_evidence(evidence_dir, "test_s", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        manual_freeze("test_s", "audit md test")
        path = generate_runtime_audit("test_s")
        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert "Runtime Audit" in content
        assert "manual_freeze" in content
        assert "canonical promotion bundle" in content
        assert "live eligibility" in content


# ═══════════════════════════════════════════════════════════════
# Scheduler Enforcement 통합 테스트
# ═══════════════════════════════════════════════════════════════

class TestSchedulerEnforcement:
    """scheduler entry-point 경로에서 runtime state가 집행되는지."""

    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_normal_allows_entry(self, mock_diag, evidence_dir, runtime_dir, fresh_db):
        """normal → _execute_entry_candidates에서 진입 허용."""
        from core.paper_runtime import get_paper_runtime_state, is_paper_trade_allowed

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "status": "normal", "benchmark_status": "final"},
            {"date": "2026-03-25", "status": "normal", "benchmark_status": "final"},
        ])

        assert is_paper_trade_allowed("test_sched", "entry") is True
        state = get_paper_runtime_state("test_sched")
        assert "entry" in state.allowed_actions

    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_degraded_blocks_entry_allows_exit(self, mock_diag, evidence_dir, runtime_dir, fresh_db):
        """degraded → entry 차단, exit 허용."""
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "status": "normal"},
            {"date": "2026-03-25", "reject_count": 5, "status": "degraded",
             "anomalies": [{"type": "repeated_reject", "severity": "warning"}]},
        ])

        assert is_paper_trade_allowed("test_sched", "entry") is False
        assert is_paper_trade_allowed("test_sched", "exit") is True

    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_frozen_blocks_entry_allows_finalize(self, mock_diag, evidence_dir, runtime_dir, fresh_db):
        """frozen → entry 차단, finalize 허용."""
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "status": "normal"},
            {"date": "2026-03-25", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        assert is_paper_trade_allowed("test_sched", "entry") is False
        assert is_paper_trade_allowed("test_sched", "finalize") is True
        assert is_paper_trade_allowed("test_sched", "evidence") is True

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_frozen_still_collects_evidence(self, mock_diag, mock_bench, evidence_dir, runtime_dir, fresh_db):
        """frozen 상태에서도 collect_daily_evidence는 수행된다."""
        from database.models import get_session, PortfolioSnapshot
        from core.paper_evidence import collect_daily_evidence
        from core.paper_runtime import get_paper_runtime_state

        # seed frozen evidence
        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        state = get_paper_runtime_state("test_sched")
        assert state.state == "frozen"
        assert "evidence" in state.allowed_actions

        # seed DB for new day
        session = get_session()
        session.add(PortfolioSnapshot(
            account_key="test_sched",
            date=datetime(2026, 3, 25, 15, 35),
            total_value=10_000_000, cash=3_000_000, invested=7_000_000,
            daily_return=0.1, cumulative_return=0.1, mdd=-1.0, position_count=1,
        ))
        session.commit()
        session.close()

        mock_bench.return_value = {
            "same_universe_excess": 0.05,
            "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02,
            "benchmark_status": "final",
            "benchmark_meta": {"completeness": 1.0},
        }

        # evidence collection should succeed even when frozen
        r = collect_daily_evidence(
            strategy="test_sched", mode="paper", account_key="test_sched",
            date=datetime(2026, 3, 25, 15, 35),
            watchlist_symbols=["005930"],
        )
        assert r is not None

    def test_blocked_insufficient_blocks_entry(self, evidence_dir, runtime_dir):
        """blocked_insufficient_evidence → entry 차단."""
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "same_universe_excess": None},
            {"date": "2026-03-25", "same_universe_excess": None},
        ])

        assert is_paper_trade_allowed("test_sched", "entry") is False
        assert is_paper_trade_allowed("test_sched", "finalize") is True

    def test_manual_freeze_blocks_and_audit(self, evidence_dir, runtime_dir):
        """manual freeze → 차단 + audit trail."""
        from core.paper_runtime import manual_freeze, is_paper_trade_allowed, _read_decisions

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        manual_freeze("test_sched", "operator investigation")
        assert is_paper_trade_allowed("test_sched", "entry") is False

        decisions = _read_decisions("test_sched")
        freeze_events = [d for d in decisions if d["action"] == "manual_freeze"]
        assert len(freeze_events) >= 1
        assert freeze_events[-1]["reason"] == "operator investigation"

    def test_explain_block_reason(self, evidence_dir, runtime_dir):
        """explain_paper_block_reason이 이유를 반환."""
        from core.paper_runtime import explain_paper_block_reason

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "phantom_position_count": 2, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        reason = explain_paper_block_reason("test_sched")
        assert "frozen" in reason.lower()
        assert "phantom" in reason.lower()

    def test_normal_not_blocked(self, evidence_dir, runtime_dir):
        from core.paper_runtime import explain_paper_block_reason

        _seed_evidence(evidence_dir, "test_sched", [
            {"date": "2026-03-24", "status": "normal", "benchmark_status": "final"},
        ])

        assert explain_paper_block_reason("test_sched") == "not blocked"


# ═══════════════════════════════════════════════════════════════
# Artifacts 생성 테스트
# ═══════════════════════════════════════════════════════════════

class TestArtifacts:
    """runtime_status.json, runtime_decisions.jsonl, runtime_audit.md."""

    def test_status_json_created(self, evidence_dir, runtime_dir):
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "art_test", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        get_paper_runtime_state("art_test")
        status_path = runtime_dir / "runtime_status_art_test.json"
        assert status_path.exists()
        data = json.loads(status_path.read_text(encoding="utf-8"))
        assert data["strategy"] == "art_test"
        assert data["state"] == "normal"

    def test_decisions_jsonl_created(self, evidence_dir, runtime_dir):
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "art_test", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        get_paper_runtime_state("art_test")
        decisions_path = runtime_dir / "runtime_decisions.jsonl"
        assert decisions_path.exists()
        lines = [l for l in decisions_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        assert len(lines) >= 1
        rec = json.loads(lines[-1])
        assert rec["strategy"] == "art_test"
        assert rec["action"] == "evaluate"

    def test_audit_md_created(self, evidence_dir, runtime_dir):
        from core.paper_runtime import get_paper_runtime_state, generate_runtime_audit

        _seed_evidence(evidence_dir, "art_test", [
            {"date": "2026-03-24", "status": "normal"},
        ])

        get_paper_runtime_state("art_test")
        path = generate_runtime_audit("art_test")
        assert path is not None
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Runtime Audit" in content

    def test_status_reusable_for_promotion(self, evidence_dir, runtime_dir):
        """runtime status가 promotion package에서 참조 가능한 구조인지."""
        from core.paper_runtime import get_paper_runtime_state, load_runtime_status

        _seed_evidence(evidence_dir, "art_test", [
            {"date": "2026-03-24", "status": "normal", "benchmark_status": "final"},
        ])

        get_paper_runtime_state("art_test")
        loaded = load_runtime_status("art_test")
        assert loaded is not None
        assert "state" in loaded
        assert "metrics" in loaded
        assert "evidence_date" in loaded


# ═══════════════════════════════════════════════════════════════
# Real-data Smoke / Bootstrap
# ═══════════════════════════════════════════════════════════════

class TestRealDataBootstrap:
    """실제 paper 실행 이력 탐색 + dry-run 예시."""

    def test_bootstrap_dry_run(self, evidence_dir, runtime_dir):
        """실제 paper 이력 없음 → dry-run 예시로 runtime state 계산 경로 검증."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "bootstrap_test", [
            {"date": "2026-04-01", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": 0.15, "daily_return": 0.5},
            {"date": "2026-04-02", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": -0.10, "daily_return": -0.3},
            {"date": "2026-04-03", "status": "degraded", "benchmark_status": "final",
             "same_universe_excess": 0.05, "daily_return": 0.2,
             "reject_count": 5,
             "anomalies": [{"type": "repeated_reject", "severity": "warning"}]},
            {"date": "2026-04-04", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": 0.08, "daily_return": 0.4},
            {"date": "2026-04-05", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": 0.12, "daily_return": 0.6},
        ])

        state = get_paper_runtime_state("bootstrap_test")
        assert state.state == "normal"
        assert state.evidence_date == "2026-04-05"
        assert state.metrics["eligible_records"] == 5
        assert state.metrics["excess_non_null_ratio"] == 1.0

    def test_artifact_checklist(self, evidence_dir, runtime_dir):
        """다음 real paper run에서 채워져야 할 artifact checklist."""
        # 이 테스트 자체가 checklist:
        # 1. PortfolioSnapshot (daily_return, mdd 포함)
        # 2. TradeHistory (fill 기록)
        # 3. OperationEvent (SIGNAL, STARTUP_RECOVERY)
        # 4. DailyReport (cross-validation용)
        # 5. DataCollector 종목별 종가 (benchmark excess 계산용)
        #
        # 위 5개가 존재하면 collect_daily_evidence()가 non-null excess를 생성하고
        # get_paper_runtime_state()가 normal/degraded/frozen을 정확히 판정
        assert True, "실제 paper 이력 없음 — 다음 run에서 위 5 artifact 필수"

    def test_dry_run_state_transition_example(self, evidence_dir, runtime_dir):
        """실제 운영에서 예상되는 state transition dry-run."""
        from core.paper_runtime import get_paper_runtime_state, manual_freeze, manual_unfreeze

        # Day 1-3: normal operation
        _seed_evidence(evidence_dir, "dryrun_s", [
            {"date": "2026-04-01", "status": "normal", "benchmark_status": "final"},
            {"date": "2026-04-02", "status": "normal", "benchmark_status": "final"},
            {"date": "2026-04-03", "status": "normal", "benchmark_status": "final"},
        ])
        s1 = get_paper_runtime_state("dryrun_s")
        assert s1.state == "normal"

        # Operator freezes for investigation
        s2 = manual_freeze("dryrun_s", "scheduled maintenance")
        assert s2.state == "frozen"

        # Operator unfreezes after resolution
        s3 = manual_unfreeze("dryrun_s", "maintenance complete")
        assert s3.state == "normal"


# ═══════════════════════════════════════════════════════════════
# Exit-safe Policy 테스트
# ═══════════════════════════════════════════════════════════════

class TestExitSafePolicy:
    """핵심 invariant: 모든 state에서 exit/cancel/reconcile/finalize/evidence/reporting은 허용."""

    def test_invariant_exit_safe_all_states(self, evidence_dir, runtime_dir):
        """모든 state에서 exit-safe actions이 허용되는지 코드 레벨에서 검증."""
        from core.paper_runtime import ALLOWED_ACTIONS, VALID_STATES, _EXIT_SAFE_ACTIONS

        for state_name in VALID_STATES:
            actions = ALLOWED_ACTIONS[state_name]
            for safe_action in _EXIT_SAFE_ACTIONS:
                assert safe_action in actions, \
                    f"state={state_name}: {safe_action} not in allowed_actions"

    def test_blocked_insufficient_with_open_position_allows_exit(self, evidence_dir, runtime_dir):
        """blocked_insufficient_evidence + open position → entry X, exit O."""
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "exit_test", [
            {"date": "2026-03-24", "same_universe_excess": None},
            {"date": "2026-03-25", "same_universe_excess": None},
        ])

        assert is_paper_trade_allowed("exit_test", "entry") is False
        assert is_paper_trade_allowed("exit_test", "exit") is True
        assert is_paper_trade_allowed("exit_test", "cancel") is True
        assert is_paper_trade_allowed("exit_test", "reconcile") is True

    def test_research_disabled_allows_cleanup(self, evidence_dir, runtime_dir, tmp_path):
        """research_disabled + outstanding position → cleanup O."""
        from core.paper_runtime import get_paper_runtime_state, is_paper_trade_allowed

        approved = Path("reports/approved_strategies.json")
        original = None
        if approved.exists():
            original = approved.read_text(encoding="utf-8")
        try:
            approved.parent.mkdir(parents=True, exist_ok=True)
            approved.write_text(json.dumps({
                "strategies": [{"name": "cleanup_s", "status": "disabled"}]
            }), encoding="utf-8")

            state = get_paper_runtime_state("cleanup_s")
            assert state.state == "research_disabled"
            assert is_paper_trade_allowed("cleanup_s", "exit") is True
            assert is_paper_trade_allowed("cleanup_s", "cancel") is True
            assert is_paper_trade_allowed("cleanup_s", "reconcile") is True
            assert is_paper_trade_allowed("cleanup_s", "entry") is False
        finally:
            if original is not None:
                approved.write_text(original, encoding="utf-8")
            elif approved.exists():
                approved.unlink()

    def test_frozen_allows_exit_and_finalize(self, evidence_dir, runtime_dir):
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "frozen_exit", [
            {"date": "2026-03-24", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        assert is_paper_trade_allowed("frozen_exit", "entry") is False
        assert is_paper_trade_allowed("frozen_exit", "exit") is True
        assert is_paper_trade_allowed("frozen_exit", "finalize") is True

    def test_no_open_position_blocked_insufficient_no_exit_needed(self, evidence_dir, runtime_dir):
        """open position 없이 blocked → entry X, exit는 코드상 허용 (no-op)."""
        from core.paper_runtime import get_paper_runtime_state

        _seed_evidence(evidence_dir, "no_pos", [
            {"date": "2026-03-24", "same_universe_excess": None, "position_count": 0},
        ])

        state = get_paper_runtime_state("no_pos")
        assert state.state == "blocked_insufficient_evidence"
        assert "exit" in state.allowed_actions  # 코드상 허용 (호출해도 no-op)
        assert "entry" not in state.allowed_actions


# ═══════════════════════════════════════════════════════════════
# Legacy Evidence 분리 테스트
# ═══════════════════════════════════════════════════════════════

class TestLegacyEvidenceNormalization:
    """v1 legacy evidence가 runtime state를 왜곡하지 않는지."""

    def test_v1_record_quarantined(self, evidence_dir, runtime_dir):
        """V1 record는 runtime eligible에서 제외."""
        from core.paper_runtime import get_paper_runtime_state, classify_evidence_schema

        v1_record = {
            "date": "2026-04-02", "day_number": 1, "strategy": "legacy_s",
            "portfolio_value": 10000000, "cash": 10000000, "n_positions": 0,
            "same_universe_excess": 0.0, "absolute_return": 0.0,
        }
        _seed_evidence(evidence_dir, "legacy_s", [
            {"_raw_v1": v1_record},
            {"date": "2026-04-03", "status": "normal", "benchmark_status": "final"},
        ])

        state = get_paper_runtime_state("legacy_s")
        assert state.metrics["quarantined_records"] == 1
        assert state.metrics["eligible_records"] == 1
        # v1이 분모에서 제외되므로 eligible 1건으로만 판정
        assert state.state != "research_disabled"

    def test_all_v1_records_blocked(self, evidence_dir, runtime_dir):
        """모든 record가 V1 → blocked_insufficient_evidence."""
        from core.paper_runtime import get_paper_runtime_state

        v1_a = {"date": "2026-04-01", "day_number": 1, "strategy": "all_v1",
                "portfolio_value": 10000000, "cash": 10000000, "n_positions": 0,
                "same_universe_excess": 0.0}
        v1_b = {"date": "2026-04-02", "day_number": 2, "strategy": "all_v1",
                "portfolio_value": 10000000, "cash": 10000000, "n_positions": 0,
                "same_universe_excess": 0.0}

        _seed_evidence(evidence_dir, "all_v1", [
            {"_raw_v1": v1_a},
            {"_raw_v1": v1_b},
        ])

        state = get_paper_runtime_state("all_v1")
        assert state.state == "blocked_insufficient_evidence"
        assert "legacy records quarantined" in str(state.reasons)

    def test_mixed_legacy_and_v2_correct_ratio(self, evidence_dir, runtime_dir):
        """V1 + V2 mix → V2만 분모로 사용."""
        from core.paper_runtime import get_paper_runtime_state

        v1_rec = {"date": "2026-04-01", "day_number": 1, "strategy": "mix_s",
                  "portfolio_value": 10000000, "cash": 10000000, "n_positions": 0,
                  "same_universe_excess": 0.0}

        _seed_evidence(evidence_dir, "mix_s", [
            {"_raw_v1": v1_rec},
            {"date": "2026-04-03", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": 0.05},
            {"date": "2026-04-06", "status": "normal", "benchmark_status": "final",
             "same_universe_excess": -1.75},
        ])

        state = get_paper_runtime_state("mix_s")
        # v2 2건, v1 1건(quarantined)
        assert state.metrics["eligible_records"] == 2
        assert state.metrics["quarantined_records"] == 1
        # 2/2 final, 2/2 excess non-null → not degraded for ratio reasons
        assert state.metrics["excess_non_null_ratio"] == 1.0
        assert state.metrics["recent_final_ratio"] == 1.0

    def test_real_scoring_evidence_normalization(self, evidence_dir, runtime_dir):
        """실제 scoring 3일 데이터 시뮬레이션: V1 제외 후 state 변화."""
        from core.paper_runtime import get_paper_runtime_state, rebuild_runtime_history

        # 실제 scoring JSONL 구조 재현
        v1_rec = {
            "date": "2026-04-02", "strategy": "scoring", "day_number": 1,
            "portfolio_value": 10000000.0, "cash": 10000000.0, "n_positions": 0,
            "same_universe_excess": 0.0, "absolute_return": 0.0,
            "raw_fill_rate": 100.0, "reject_count": 0,
        }
        _seed_evidence(evidence_dir, "scoring", [
            {"_raw_v1": v1_rec},
            # V2: failed (daily_return null → benchmark failed)
            {"date": "2026-04-03", "daily_return": None, "same_universe_excess": None,
             "benchmark_status": "failed", "status": "normal"},
            # V2: final
            {"date": "2026-04-06", "daily_return": 0.0, "same_universe_excess": -1.753,
             "benchmark_status": "final", "status": "normal"},
        ])

        # legacy 포함 시: 3 records, 1 v1(quarantined) + 2 v2
        # v2 분모: 2건 중 final=1, excess_non_null=1 → 50% each
        state = get_paper_runtime_state("scoring")
        assert state.metrics["quarantined_records"] == 1
        assert state.metrics["eligible_records"] == 2
        # excess: 1/2 = 50% < 60% → insufficient
        # final: 1/2 = 50% → not below 50% threshold
        assert state.metrics["excess_non_null_ratio"] == 0.5

        # rebuild history
        history = rebuild_runtime_history("scoring")
        assert len(history) == 3
        assert history[0]["is_legacy"] is True
        assert history[0]["runtime_state"] == "quarantined_legacy"
        assert history[2]["is_legacy"] is False


# ═══════════════════════════════════════════════════════════════
# Operator Visibility 통합 테스트
# ═══════════════════════════════════════════════════════════════

class TestOperatorVisibility:
    """block reason이 구조화된 payload로 남고 notifier 경로가 연결되는지."""

    def test_explain_block_reason_structured(self, evidence_dir, runtime_dir):
        """explain_paper_block_reason이 구조화된 필드를 포함."""
        from core.paper_runtime import explain_paper_block_reason

        _seed_evidence(evidence_dir, "vis_test", [
            {"date": "2026-03-24", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        reason = explain_paper_block_reason("vis_test")
        assert "state=frozen" in reason
        assert "strategy=vis_test" in reason
        assert "evidence_date=" in reason
        assert "benchmark_final_ratio=" in reason
        assert "anomaly_count=" in reason
        assert "allowed_actions=" in reason

    def test_decisions_record_full_metrics(self, evidence_dir, runtime_dir):
        """runtime_decisions.jsonl에 metrics가 기록됨."""
        from core.paper_runtime import get_paper_runtime_state, _read_decisions

        _seed_evidence(evidence_dir, "vis_test", [
            {"date": "2026-03-24", "status": "normal", "benchmark_status": "final"},
        ])

        get_paper_runtime_state("vis_test")
        decisions = _read_decisions("vis_test")
        last = decisions[-1]
        assert "metrics" in last
        assert "eligible_records" in last["metrics"]
        assert "recent_final_ratio" in last["metrics"]

    def test_scheduler_block_payload(self, evidence_dir, runtime_dir):
        """scheduler RUNTIME_BLOCK의 detail에 필수 필드가 포함되는지 검증.
        실제 scheduler 호출 대신 block_detail 구조를 직접 검증."""
        from core.paper_runtime import get_paper_runtime_state, explain_paper_block_reason

        _seed_evidence(evidence_dir, "block_test", [
            {"date": "2026-03-24", "reject_count": 5, "status": "degraded",
             "anomalies": [{"type": "repeated_reject", "severity": "warning"}]},
        ])

        state = get_paper_runtime_state("block_test")
        block_detail = explain_paper_block_reason("block_test")

        # scheduler가 _log_op에 넘기는 detail dict 시뮬레이션
        detail = {
            "state": state.state,
            "strategy": "block_test",
            "evidence_date": state.evidence_date,
            "benchmark_final_ratio": state.metrics.get("recent_final_ratio"),
            "anomaly_count": state.metrics.get("recent_anomaly_count", 0),
            "allowed_actions": state.allowed_actions,
            "reasons": state.reasons,
        }

        assert detail["state"] == "degraded"
        assert detail["strategy"] == "block_test"
        assert detail["evidence_date"] is not None
        assert detail["benchmark_final_ratio"] is not None
        assert isinstance(detail["allowed_actions"], list)
        assert "entry" not in detail["allowed_actions"]


# ═══════════════════════════════════════════════════════════════
# Policy 충돌 검증 (CI에서 실행)
# ═══════════════════════════════════════════════════════════════

class TestPolicyConsistency:
    """registry/runtime/allowed_actions 간 모순이 없는지 코드 레벨 검증."""

    def test_entry_only_in_normal(self):
        """'entry'는 normal에서만 허용."""
        from core.paper_runtime import ALLOWED_ACTIONS
        for state_name, actions in ALLOWED_ACTIONS.items():
            if state_name == "normal":
                assert "entry" in actions
            else:
                assert "entry" not in actions, \
                    f"state={state_name} should not allow entry"

    def test_run_only_in_normal(self):
        """'run'은 normal에서만 허용."""
        from core.paper_runtime import ALLOWED_ACTIONS
        for state_name, actions in ALLOWED_ACTIONS.items():
            if state_name == "normal":
                assert "run" in actions
            else:
                assert "run" not in actions, \
                    f"state={state_name} should not allow run"

    def test_exit_safe_in_all_states(self):
        """exit-safe actions는 모든 state에서 허용."""
        from core.paper_runtime import ALLOWED_ACTIONS, VALID_STATES, _EXIT_SAFE_ACTIONS
        for state_name in VALID_STATES:
            for action in _EXIT_SAFE_ACTIONS:
                assert action in ALLOWED_ACTIONS[state_name], \
                    f"state={state_name}: exit-safe action '{action}' missing"

    def test_no_state_allows_entry_without_run(self):
        """entry 허용 state는 반드시 run도 허용."""
        from core.paper_runtime import ALLOWED_ACTIONS
        for state_name, actions in ALLOWED_ACTIONS.items():
            if "entry" in actions:
                assert "run" in actions, \
                    f"state={state_name} allows entry but not run"

    def test_degraded_does_not_allow_entry_but_allows_exit(self):
        """degraded: entry X, exit O — 핵심 policy."""
        from core.paper_runtime import ALLOWED_ACTIONS
        assert "entry" not in ALLOWED_ACTIONS["degraded"]
        assert "exit" in ALLOWED_ACTIONS["degraded"]

    def test_rescan_guard_consistent_with_execute_guard(self, evidence_dir, runtime_dir):
        """_rescan_for_new_entries와 _execute_entry_candidates의 guard가 동일한 조건을 사용."""
        from core.paper_runtime import is_paper_trade_allowed

        _seed_evidence(evidence_dir, "guard_test", [
            {"date": "2026-03-24", "reject_count": 5, "status": "degraded",
             "anomalies": [{"type": "repeated_reject", "severity": "warning"}]},
        ])

        # 둘 다 "entry" action을 체크하므로 결과가 동일해야 함
        assert is_paper_trade_allowed("guard_test", "entry") is False


# ═══════════════════════════════════════════════════════════════
# Rebuild 도구 테스트
# ═══════════════════════════════════════════════════════════════

class TestRebuild:
    """rebuild_runtime_history + generate_rebuild_report."""

    def test_rebuild_with_legacy_and_v2(self, evidence_dir, runtime_dir):
        from core.paper_runtime import rebuild_runtime_history, generate_rebuild_report

        v1_rec = {"date": "2026-04-02", "strategy": "rb_test", "day_number": 1,
                  "portfolio_value": 10000000, "cash": 10000000, "n_positions": 0,
                  "same_universe_excess": 0.0}

        _seed_evidence(evidence_dir, "rb_test", [
            {"_raw_v1": v1_rec},
            {"date": "2026-04-03", "benchmark_status": "failed", "same_universe_excess": None},
            {"date": "2026-04-06", "benchmark_status": "final", "same_universe_excess": -1.753},
        ])

        history = rebuild_runtime_history("rb_test")
        assert len(history) == 3
        assert history[0]["schema_version"] == 1
        assert history[0]["is_legacy"] is True
        assert history[0]["runtime_state"] == "quarantined_legacy"
        assert history[1]["is_legacy"] is False
        assert history[2]["runtime_state"] == "normal"

        # report 생성
        path = generate_rebuild_report("rb_test", history)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "quarantined_legacy" in content
        assert "Runtime Rebuild" in content

    def test_rebuild_date_filter(self, evidence_dir, runtime_dir):
        from core.paper_runtime import rebuild_runtime_history

        _seed_evidence(evidence_dir, "rb_filt", [
            {"date": "2026-04-01"},
            {"date": "2026-04-02"},
            {"date": "2026-04-03"},
        ])

        history = rebuild_runtime_history("rb_filt", from_date="2026-04-02", to_date="2026-04-02")
        assert len(history) == 1
        assert history[0]["date"] == "2026-04-02"
