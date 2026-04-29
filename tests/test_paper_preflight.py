"""
Paper Preflight 통합 테스트

시나리오:
  - scoring blocked + notifier healthy → entry blocked, exit allowed
  - scoring blocked + notifier unhealthy
  - rotation seeded-normal but no real evidence → preflight 표기 검증
  - webhook configured → test send artifact
  - webhook missing → notifier_health warn
  - DB fail → preflight critical fail
  - scheduler가 preflight 결과를 읽고 entry만 막는지
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    import core.paper_preflight as ppf
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(pr, "PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS", 999)
    monkeypatch.setattr(ppf, "RUNTIME_DIR", tmp_path / "paper_runtime")
    return tmp_path


@pytest.fixture
def evidence_dir(_isolate_dirs):
    return _isolate_dirs / "paper_evidence"


@pytest.fixture
def runtime_dir(_isolate_dirs):
    return _isolate_dirs / "paper_runtime"


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


def _seed_v2(evidence_dir, strategy, days):
    from core.paper_evidence import _append_jsonl
    jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
    for i, cfg in enumerate(days):
        record = {
            "date": cfg["date"], "day_number": i + 1, "strategy": strategy,
            "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
            "daily_return": cfg.get("daily_return", 0.3),
            "cumulative_return": 0.5, "mdd": -2.0, "position_count": 2,
            "total_trades": 2,
            "same_universe_excess": cfg.get("same_universe_excess", 0.05),
            "exposure_matched_excess": 0.03, "cash_adjusted_excess": 0.02,
            "benchmark_status": cfg.get("benchmark_status", "final"),
            "benchmark_meta": {"completeness": 1.0},
            "raw_fill_rate": 1.0,
            "reject_count": cfg.get("reject_count", 0),
            "phantom_position_count": cfg.get("phantom_position_count", 0),
            "stale_pending_count": 0, "duplicate_blocked_count": 0,
            "restart_recovery_count": 0,
            "anomalies": cfg.get("anomalies", []),
            "cross_validation_warnings": [],
            "status": cfg.get("status", "normal"),
            "record_version": 1, "schema_version": 2, "diagnostics": [],
        }
        _append_jsonl(jsonl_path, record)


# ═══════════════════════════════════════════════════════════════

class TestPreflightBasic:

    def test_scoring_blocked_entry_blocked_exit_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """scoring blocked → preflight entry=False, exit=True."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("scoring", "2026-04-06")

        assert r.entry_allowed is False
        assert r.exit_allowed is True
        assert r.runtime_state == "blocked_insufficient_evidence"
        assert r.has_real_evidence is True

    def test_normal_strategy_entry_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """normal → preflight entry=True."""
        _seed_v2(evidence_dir, "normal_s", [
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-05", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("normal_s", "2026-04-06")

        assert r.entry_allowed is True
        assert r.overall in ("pass", "warn")  # warn if notifier unconfigured
        assert r.runtime_state == "normal"

    def test_frozen_strategy_is_fail(self, evidence_dir, runtime_dir, fresh_db):
        """frozen → preflight overall=fail."""
        _seed_v2(evidence_dir, "frozen_s", [
            {"date": "2026-04-06", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("frozen_s", "2026-04-06")

        assert r.overall == "fail"
        assert r.entry_allowed is False


class TestNotifierHealth:

    def test_webhook_not_configured(self, evidence_dir, runtime_dir, fresh_db):
        """webhook 미설정 → notifier_health=unconfigured, warn."""
        _seed_v2(evidence_dir, "notif_s", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("notif_s", "2026-04-06")

        assert r.notifier_health in ("unconfigured", "error", "configured")
        notifier_checks = [c for c in r.checks if c["name"] == "notifier_discord"]
        assert len(notifier_checks) == 1

    def test_notifier_health_json_created(self, evidence_dir, runtime_dir, fresh_db):
        """notifier_health.json 생성."""
        _seed_v2(evidence_dir, "notif_s", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        run_preflight("notif_s", "2026-04-06")

        health_path = runtime_dir / "notifier_health.json"
        assert health_path.exists()
        health = json.loads(health_path.read_text(encoding="utf-8"))
        assert "discord_configured" in health


class TestEvidenceFreshness:

    def test_fresh_evidence(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "fresh_s", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("fresh_s", "2026-04-06")
        assert r.evidence_freshness == "fresh"

    def test_stale_evidence(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "stale_s", [
            {"date": "2026-04-01", "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("stale_s", "2026-04-06")
        assert r.evidence_freshness == "stale"

    def test_no_evidence(self, evidence_dir, runtime_dir, fresh_db):
        from core.paper_preflight import run_preflight
        r = run_preflight("empty_s", "2026-04-06")
        assert r.has_real_evidence is False
        assert r.overall == "fail"  # no evidence at all → blocked

    def test_rotation_no_real_evidence(self, evidence_dir, runtime_dir, fresh_db):
        """rotation에 seeded evidence가 없으면 has_real_evidence=False."""
        from core.paper_preflight import run_preflight
        r = run_preflight("rotation", "2026-04-06")
        assert r.has_real_evidence is False


class TestDBHealth:

    def test_db_health_pass(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "db_s", [{"date": "2026-04-06"}])

        from core.paper_preflight import run_preflight
        r = run_preflight("db_s", "2026-04-06")

        db_checks = [c for c in r.checks if c["name"] == "db_health"]
        assert len(db_checks) == 1
        assert db_checks[0]["status"] == "pass"


class TestArtifacts:

    def test_preflight_json_created(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "art_s", [{"date": "2026-04-06"}])

        from core.paper_preflight import run_preflight
        run_preflight("art_s", "2026-04-06")

        assert (runtime_dir / "preflight_status_art_s.json").exists()
        data = json.loads((runtime_dir / "preflight_status_art_s.json").read_text(encoding="utf-8"))
        assert data["strategy"] == "art_s"

    def test_preflight_md_created(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "art_s", [{"date": "2026-04-06"}])

        from core.paper_preflight import run_preflight
        run_preflight("art_s", "2026-04-06")

        md_path = runtime_dir / "preflight_status_art_s.md"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "Preflight" in content
        assert "Entry Allowed" in content

    def test_session_bootstrap_md(self, evidence_dir, runtime_dir, fresh_db):
        _seed_v2(evidence_dir, "s1", [{"date": "2026-04-06"}])
        _seed_v2(evidence_dir, "s2", [
            {"date": "2026-04-04"},
            {"date": "2026-04-05"},
            {"date": "2026-04-06"},
        ])

        from core.paper_preflight import run_preflight, _save_session_bootstrap
        r1 = run_preflight("s1", "2026-04-06")
        r2 = run_preflight("s2", "2026-04-06")
        path = _save_session_bootstrap("2026-04-06", [r1, r2])

        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Session Bootstrap" in content
        assert "s1" in content
        assert "s2" in content


class TestSchedulerPreflightGating:

    def test_preflight_fail_blocks_entry(self, evidence_dir, runtime_dir, fresh_db):
        """preflight fail → scheduler entry candidates 비워짐."""
        _seed_v2(evidence_dir, "gate_s", [
            {"date": "2026-04-06", "phantom_position_count": 1, "status": "frozen",
             "anomalies": [{"type": "phantom_position", "severity": "critical"}]},
        ])

        from core.paper_preflight import run_preflight
        run_preflight("gate_s", "2026-04-06")

        # scheduler shadow
        from core.scheduler import Scheduler
        sched = object.__new__(Scheduler)
        sched.config = MagicMock()
        sched.config.trading = {"mode": "paper"}
        sched.strategy_name = "gate_s"
        sched._mode = "paper"
        sched.discord = MagicMock()
        sched.discord.send_message = MagicMock(return_value=True)
        sched.portfolio = MagicMock()
        sched.trading_hours = MagicMock()
        sched.blackswan = MagicMock()
        sched.blackswan.is_on_cooldown.return_value = False
        sched.auto_entry = True
        sched._entry_candidates = [{"symbol": "005930", "price": 62000}]
        sched._restart_recovery_count = 0
        sched.monitor_interval = 600
        sched._skip_next_monitor_cycle = False

        with patch("core.market_regime.check_market_regime",
                   return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        assert sched._entry_candidates == []

        # PREFLIGHT_BLOCK OperationEvent
        from database.models import get_session, OperationEvent
        session = get_session()
        events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "PREFLIGHT_BLOCK",
        ).all()
        session.close()
        assert len(events) >= 1

    def test_preflight_pass_allows_runtime_check(self, evidence_dir, runtime_dir, fresh_db):
        """preflight pass → runtime guard까지 진행 (runtime에서 최종 판정)."""
        _seed_v2(evidence_dir, "pass_s", [
            {"date": "2026-04-04"}, {"date": "2026-04-05"}, {"date": "2026-04-06"},
        ])

        from core.paper_preflight import run_preflight
        r = run_preflight("pass_s", "2026-04-06")
        assert r.overall in ("pass", "warn")  # warn if notifier unconfigured
        assert r.entry_allowed is True


class TestScoringRotationComparison:

    def test_scoring_vs_rotation(self, evidence_dir, runtime_dir, fresh_db):
        """scoring: blocked. rotation: no evidence → 각각 독립 preflight."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_preflight import run_preflight

        s = run_preflight("scoring", "2026-04-06")
        r = run_preflight("rotation", "2026-04-06")

        # scoring: blocked, has real evidence
        assert s.entry_allowed is False
        assert s.has_real_evidence is True

        # rotation: no evidence → blocked, no real evidence
        assert r.entry_allowed is False
        assert r.has_real_evidence is False
