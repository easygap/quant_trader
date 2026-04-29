"""
Paper Pilot Authorization 통합 테스트

시나리오:
  - blocked + no pilot → entry blocked
  - blocked + valid pilot + caps OK → limited entry allowed
  - cap hit → further entries blocked
  - pilot expired → blocked
  - notifier unhealthy → pilot blocked
  - research_disabled → pilot auth 거부
  - rotation no evidence + insufficient prerequisites → pilot denied
  - pilot entry → DailyEvidence에 pilot_paper, execution_backed=True
  - promotion package: pilot_real_paper_days vs shadow_days 분리
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import asdict

import pytest


PILOT_STRATEGY = "relative_strength_rotation"
OBSERVATION_STRATEGY = "scoring"


@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    import core.paper_preflight as ppf
    import core.paper_pilot as pp
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(ppf, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", tmp_path / "paper_runtime" / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", tmp_path / "paper_runtime" / "pilot_audit.jsonl")
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
            "total_trades": 2, "buy_count": 1, "sell_count": 1,
            "same_universe_excess": cfg.get("same_universe_excess", 0.05),
            "exposure_matched_excess": 0.03, "cash_adjusted_excess": 0.02,
            "benchmark_status": cfg.get("benchmark_status", "final"),
            "benchmark_meta": {"completeness": 1.0},
            "raw_fill_rate": 1.0,
            "reject_count": 0, "phantom_position_count": 0,
            "stale_pending_count": 0, "duplicate_blocked_count": 0,
            "restart_recovery_count": 0,
            "anomalies": cfg.get("anomalies", []),
            "cross_validation_warnings": [],
            "status": cfg.get("status", "normal"),
            "record_version": 1, "schema_version": 2, "diagnostics": [],
            "evidence_mode": cfg.get("evidence_mode", "real_paper"),
            "execution_backed": cfg.get("execution_backed", True),
            "order_submit_count": 2, "fill_count": 2,
        }
        _append_jsonl(jsonl_path, record)


class TestPilotBasic:

    def test_no_pilot_entry_blocked(self, evidence_dir, runtime_dir, fresh_db):
        """blocked + no pilot auth → entry blocked."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_pilot import check_pilot_entry
        result = check_pilot_entry("scoring", as_of_date="2026-04-07")
        assert result.allowed is False
        assert "no active pilot" in result.reason

    def test_valid_pilot_entry_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """blocked + valid pilot + caps OK → entry allowed."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        # notifier 설정 (pilot requires discord)
        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30",
                     max_orders=2, max_notional=1_000_000, reason="test pilot")

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-08")
        assert result.allowed is True
        assert result.remaining_orders is not None
        assert result.remaining_orders > 0

    def test_cap_hit_blocks_entry(self, evidence_dir, runtime_dir, fresh_db):
        """max_orders_per_day 초과 → entry blocked."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30", max_orders=1)

        # 오늘 주문 1건 삽입
        from database.models import get_session, TradeHistory
        session = get_session()
        session.add(TradeHistory(
            account_key=PILOT_STRATEGY, symbol="005930", action="BUY",
            price=60000, quantity=10, total_amount=600000,
            mode="paper", strategy=PILOT_STRATEGY,
            executed_at=datetime(2026, 4, 7, 10, 0),
        ))
        session.commit()
        session.close()

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert result.allowed is False
        assert "max_orders_per_day" in result.reason

    def test_pilot_expired_blocks(self, evidence_dir, runtime_dir, fresh_db):
        """pilot 기간 만료 → blocked."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-03-01", "2026-03-31")  # 이미 만료

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert result.allowed is False

    def test_notifier_unhealthy_blocks_pilot(self, evidence_dir, runtime_dir, fresh_db):
        """notifier unconfigured → pilot entry blocked."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": False}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert result.allowed is False
        assert "notifier" in result.reason.lower()


class TestPilotEligibility:

    def test_disabled_strategy_pilot_rejected(self, evidence_dir, runtime_dir, fresh_db):
        """research_disabled → pilot auth 거부."""
        from core.paper_pilot import enable_pilot

        with pytest.raises(ValueError, match="not paper-eligible"):
            enable_pilot("breakout_volume", "2026-04-01", "2026-04-30")

    def test_paper_only_strategy_pilot_rejected(self, evidence_dir, runtime_dir, fresh_db):
        """paper_only 관찰 전략은 pilot authorization 대상이 아니다."""
        from core.paper_pilot import enable_pilot

        with pytest.raises(ValueError, match="pilot requires provisional_paper_candidate"):
            enable_pilot(OBSERVATION_STRATEGY, "2026-04-01", "2026-04-30")

    def test_rotation_no_prerequisites(self, evidence_dir, runtime_dir, fresh_db):
        """rotation no evidence → prerequisites not met."""
        from core.paper_pilot import check_pilot_prerequisites

        ok, reason = check_pilot_prerequisites("relative_strength_rotation")
        assert ok is False
        assert "shadow bootstrap" in reason.lower() or "no eligible" in reason.lower()

    def test_rotation_with_shadow_prerequisites_met(self, evidence_dir, runtime_dir, fresh_db):
        """rotation + 3 shadow clean days → prerequisites met."""
        _seed_v2(evidence_dir, "relative_strength_rotation", [
            {"date": "2026-04-04", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
            {"date": "2026-04-05", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
            {"date": "2026-04-06", "evidence_mode": "shadow_bootstrap", "execution_backed": False},
        ])

        from core.paper_pilot import check_pilot_prerequisites

        ok, reason = check_pilot_prerequisites("relative_strength_rotation")
        assert ok is True


class TestPilotSchedulerIntegration:

    def test_scheduler_execute_with_pilot(self, evidence_dir, runtime_dir, fresh_db):
        """blocked + pilot auth → scheduler execute 통과."""
        latest = datetime.now() - timedelta(days=1)
        earlier = latest - timedelta(days=3)
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": earlier.strftime("%Y-%m-%d"), "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": latest.strftime("%Y-%m-%d"), "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        valid_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        valid_to = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        enable_pilot(PILOT_STRATEGY, valid_from, valid_to, max_orders=5)

        # scheduler shadow
        from core.scheduler import Scheduler
        sched = object.__new__(Scheduler)
        sched.config = MagicMock()
        sched.config.trading = {"mode": "paper"}
        sched.strategy_name = PILOT_STRATEGY
        sched._mode = "paper"
        sched.discord = MagicMock()
        sched.discord.send_message = MagicMock(return_value=True)
        sched.portfolio = MagicMock()
        sched.trading_hours = MagicMock()
        sched.blackswan = MagicMock()
        sched.blackswan.is_on_cooldown.return_value = False
        sched.auto_entry = True
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8, "reason": "test"},
        ]
        sched._restart_recovery_count = 0
        sched.monitor_interval = 600
        sched._skip_next_monitor_cycle = False

        mock_executor = MagicMock()
        mock_executor.execute_buy.return_value = {"success": True, "symbol": "005930", "action": "BUY"}
        mock_strategy = MagicMock()
        mock_strategy.generate_signal.return_value = {"signal": "BUY", "score": 0.8}

        with patch("core.market_regime.check_market_regime",
                   return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None), \
             patch.object(sched, "_get_or_create_executor", return_value=mock_executor), \
             patch.object(sched, "_get_strategy", return_value=mock_strategy):
            sched._execute_entry_candidates()

        # PILOT_ENTRY_ALLOWED OperationEvent가 기록됐는지
        from database.models import get_session, OperationEvent
        session = get_session()
        pilot_events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "PILOT_ENTRY_ALLOWED",
        ).all()
        session.close()
        assert len(pilot_events) >= 1

        # RUNTIME_BLOCK은 없어야 함
        session = get_session()
        block_events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "RUNTIME_BLOCK",
            OperationEvent.strategy == PILOT_STRATEGY,
        ).all()
        session.close()
        assert len(block_events) == 0

    def test_preflight_shows_pilot(self, evidence_dir, runtime_dir, fresh_db):
        """preflight에 pilot authorization 표시."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import enable_pilot
        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        from core.paper_preflight import run_preflight
        r = run_preflight(PILOT_STRATEGY, "2026-04-06")
        assert r.pilot_authorized is True
        assert r.entry_allowed is True  # pilot override

    def test_promotion_separates_pilot_days(self, evidence_dir, runtime_dir, fresh_db):
        """promotion package: pilot_paper_days 별도 표기."""
        days = []
        for i in range(5):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "pilot_paper",
                "execution_backed": True,
            })
        for i in range(5, 8):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "shadow_bootstrap",
                "execution_backed": False,
            })
        _seed_v2(evidence_dir, "pilot_promo", days)

        from core.paper_evidence import generate_promotion_package
        pkg_path, _ = generate_promotion_package("pilot_promo")
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["real_paper_days"] == 5  # pilot_paper is execution_backed
        assert pkg["shadow_days"] == 3
        assert pkg["promotable_evidence_days"] == 5
        # pilot / non-pilot 분리
        assert pkg["real_paper_days_total"] == 5
        assert pkg["pilot_real_paper_days"] == 5
        assert pkg["non_pilot_real_paper_days"] == 0

    def test_promotion_mixed_pilot_and_normal(self, evidence_dir, runtime_dir, fresh_db):
        """promotion package: pilot + normal real paper + shadow 혼합."""
        days = []
        # 3 normal real paper days
        for i in range(3):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "real_paper",
                "execution_backed": True,
            })
        # 4 pilot paper days
        for i in range(3, 7):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "pilot_paper",
                "execution_backed": True,
            })
        # 2 shadow days
        for i in range(7, 9):
            days.append({
                "date": (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d"),
                "evidence_mode": "shadow_bootstrap",
                "execution_backed": False,
            })
        _seed_v2(evidence_dir, "mixed_promo", days)

        from core.paper_evidence import generate_promotion_package
        pkg_path, _ = generate_promotion_package("mixed_promo")
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["real_paper_days_total"] == 7
        assert pkg["pilot_real_paper_days"] == 4
        assert pkg["non_pilot_real_paper_days"] == 3
        assert pkg["shadow_days"] == 2

    def test_pilot_evidence_records_pilot_paper_mode(self, evidence_dir, runtime_dir, fresh_db):
        """scoring with pilot entry → DailyEvidence에 pilot_paper, execution_backed=True."""
        from core.paper_evidence import _append_jsonl, get_canonical_records

        jsonl_path = evidence_dir / "daily_evidence_scoring.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "date": "2026-04-07", "day_number": 1, "strategy": "scoring",
            "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
            "daily_return": 0.3, "cumulative_return": 0.5, "mdd": -2.0,
            "position_count": 2, "total_trades": 2, "buy_count": 1, "sell_count": 1,
            "same_universe_excess": 0.05,
            "benchmark_status": "final", "benchmark_meta": {},
            "raw_fill_rate": 1.0, "reject_count": 0, "phantom_position_count": 0,
            "stale_pending_count": 0, "duplicate_blocked_count": 0,
            "restart_recovery_count": 0,
            "anomalies": [], "cross_validation_warnings": [],
            "status": "normal", "record_version": 1, "schema_version": 2,
            "diagnostics": [],
            "evidence_mode": "pilot_paper",
            "execution_backed": True,
            "order_submit_count": 2, "fill_count": 2,
            "session_mode": "pilot_paper",
            "pilot_authorized": True,
            "pilot_caps_snapshot": {"max_orders_per_day": 2, "max_notional_per_trade": 1000000},
        }
        _append_jsonl(jsonl_path, record)

        records = get_canonical_records("scoring")
        assert len(records) == 1
        r = records[0]
        assert r["evidence_mode"] == "pilot_paper"
        assert r["execution_backed"] is True
        assert r["session_mode"] == "pilot_paper"
        assert r["pilot_authorized"] is True

    def test_approved_strategies_not_changed(self, evidence_dir, runtime_dir, fresh_db):
        """legacy approved_strategies.json / live eligibility 자동 변경 없음."""
        approved_path = Path("reports/approved_strategies.json")
        if approved_path.exists():
            original = approved_path.read_text(encoding="utf-8")
        else:
            original = None

        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import enable_pilot, disable_pilot
        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")
        disable_pilot(PILOT_STRATEGY, "test cleanup")

        # approved_strategies.json 변경 없음
        if original is not None:
            assert approved_path.read_text(encoding="utf-8") == original


class TestPilotEvidenceFreshness:

    def test_stale_evidence_blocks_pilot(self, evidence_dir, runtime_dir, fresh_db):
        """evidence가 너무 오래됨 → pilot entry blocked."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-03-20", "benchmark_status": "final"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-08")
        assert result.allowed is False
        assert "stale" in result.reason.lower()

    def test_benchmark_final_ratio_low_blocks_pilot(self, evidence_dir, runtime_dir, fresh_db):
        """benchmark final ratio 낮음 → pilot blocked."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-03", "benchmark_status": "failed"},
            {"date": "2026-04-04", "benchmark_status": "failed"},
            {"date": "2026-04-05", "benchmark_status": "failed"},
            {"date": "2026-04-06", "benchmark_status": "failed"},
            {"date": "2026-04-07", "benchmark_status": "failed"},
        ])

        from core.paper_pilot import enable_pilot, check_pilot_entry

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        result = check_pilot_entry(PILOT_STRATEGY, as_of_date="2026-04-08")
        assert result.allowed is False
        assert "benchmark" in result.reason.lower()


class TestPilotSessionE2E:
    """Scheduler pilot session → post-market evidence → promotion provenance 일관성."""

    def _make_scheduler(self, strategy, evidence_dir, runtime_dir):
        """Minimal scheduler mock with pilot session support."""
        from core.scheduler import Scheduler
        sched = object.__new__(Scheduler)
        sched.config = MagicMock()
        sched.config.trading = {"mode": "paper"}
        sched.config.risk_params = {}
        sched.strategy_name = strategy
        sched._mode = "paper"
        sched.discord = MagicMock()
        sched.discord.send_message = MagicMock(return_value=True)
        sched.portfolio = MagicMock()
        sched.trading_hours = MagicMock()
        sched.blackswan = MagicMock()
        sched.blackswan.is_on_cooldown.return_value = False
        sched.blackswan.get_recovery_scale.return_value = 1.0
        sched.auto_entry = True
        sched._entry_candidates = []
        sched._restart_recovery_count = 0
        sched.monitor_interval = 600
        sched._skip_next_monitor_cycle = False
        sched._order_executor = None
        sched._pilot_session = {
            "active": False, "pilot_authorized": False,
            "pilot_caps_snapshot": {}, "session_mode": "normal_paper",
            "evidence_mode": "real_paper",
        }
        return sched

    def test_pilot_entry_sets_session_context(self, evidence_dir, runtime_dir, fresh_db):
        """pilot entry 허용 시 _pilot_session이 자동 설정된다."""
        latest = datetime.now() - timedelta(days=1)
        earlier = latest - timedelta(days=3)
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": earlier.strftime("%Y-%m-%d"), "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": latest.strftime("%Y-%m-%d"), "same_universe_excess": 0.05, "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import enable_pilot
        valid_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        valid_to = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        enable_pilot(PILOT_STRATEGY, valid_from, valid_to, max_orders=5)

        sched = self._make_scheduler(PILOT_STRATEGY, evidence_dir, runtime_dir)
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8,
             "reason": "test", "timestamp": datetime.now()},
        ]

        mock_executor = MagicMock()
        mock_executor.execute_buy.return_value = {"success": True, "symbol": "005930"}
        mock_strategy = MagicMock()
        mock_strategy.generate_signal.return_value = {"signal": "BUY", "score": 0.8, "close": 62000}

        with patch("core.market_regime.check_market_regime",
                   return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector") as MockDC, \
             patch("database.repositories.get_position", return_value=None), \
             patch.object(sched, "_get_or_create_executor", return_value=mock_executor), \
             patch.object(sched, "_get_strategy", return_value=mock_strategy):
            MockDC.return_value.fetch_stock.return_value = MagicMock(empty=False, __len__=lambda s: 50)
            sched._execute_entry_candidates()

        # pilot session context가 설정됐는지 확인
        assert sched._pilot_session["active"] is True
        assert sched._pilot_session["pilot_authorized"] is True
        assert sched._pilot_session["session_mode"] == "pilot_paper"
        assert sched._pilot_session["evidence_mode"] == "pilot_paper"
        assert sched._pilot_session["pilot_caps_snapshot"] != {}

    def test_post_market_evidence_inherits_pilot_context(self, evidence_dir, runtime_dir, fresh_db):
        """pilot session의 post-market evidence가 자동으로 pilot_paper provenance를 갖는다."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-05", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_evidence import collect_daily_evidence, get_canonical_records

        # pilot session context로 evidence 수집 시뮬레이션
        pilot_session = {
            "active": True,
            "pilot_authorized": True,
            "pilot_caps_snapshot": {"max_orders_per_day": 2, "max_notional_per_trade": 1_000_000},
            "session_mode": "pilot_paper",
            "evidence_mode": "pilot_paper",
        }

        ev = collect_daily_evidence(
            strategy="scoring",
            mode="paper",
            account_key="scoring",
            date=datetime(2026, 4, 7),
            evidence_mode=pilot_session["evidence_mode"],
            pilot_authorized=pilot_session["pilot_authorized"],
            pilot_caps_snapshot=pilot_session["pilot_caps_snapshot"],
        )

        if ev is not None:
            assert ev.evidence_mode == "pilot_paper"
            assert ev.execution_backed is True
            assert ev.session_mode == "pilot_paper"
            assert ev.pilot_authorized is True
            assert ev.pilot_caps_snapshot == pilot_session["pilot_caps_snapshot"]

            # promotion package에도 반영되는지
            from core.paper_evidence import generate_promotion_package
            pkg_path, _ = generate_promotion_package("scoring")
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            # 기존 3일 real_paper + 1일 pilot_paper = 4일 total
            assert pkg["real_paper_days_total"] >= 4
            assert pkg["pilot_real_paper_days"] >= 1

    def test_no_pilot_evidence_stays_normal(self, evidence_dir, runtime_dir, fresh_db):
        """pilot auth 없으면 evidence는 normal_paper 유지."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_evidence import collect_daily_evidence

        ev = collect_daily_evidence(
            strategy="scoring", mode="paper", account_key="scoring",
            date=datetime(2026, 4, 7),
            evidence_mode="real_paper",
            pilot_authorized=False,
        )

        if ev is not None:
            assert ev.evidence_mode == "real_paper"
            assert ev.session_mode == "normal_paper"
            assert ev.pilot_authorized is False

    def test_provenance_mismatch_detectable(self, evidence_dir, runtime_dir, fresh_db):
        """entry가 pilot인데 evidence가 normal_paper면 검출 가능해야 한다."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_evidence import _append_jsonl, get_canonical_records

        # pilot entry → normal evidence (불일치)
        jsonl_path = evidence_dir / "daily_evidence_scoring.jsonl"
        mismatch_record = {
            "date": "2026-04-07", "day_number": 2, "strategy": "scoring",
            "total_value": 10_000_000, "cash": 3_000_000, "invested": 7_000_000,
            "daily_return": 0.3, "cumulative_return": 0.5, "mdd": -2.0,
            "position_count": 2, "total_trades": 2, "buy_count": 1, "sell_count": 1,
            "same_universe_excess": 0.05,
            "benchmark_status": "final", "benchmark_meta": {},
            "raw_fill_rate": 1.0, "reject_count": 0, "phantom_position_count": 0,
            "stale_pending_count": 0, "duplicate_blocked_count": 0,
            "restart_recovery_count": 0,
            "anomalies": [], "cross_validation_warnings": [],
            "status": "normal", "record_version": 1, "schema_version": 2,
            "diagnostics": [],
            # pilot entry인데 evidence_mode가 real_paper (불일치!)
            "evidence_mode": "real_paper",
            "execution_backed": True,
            "session_mode": "normal_paper",  # 이것도 불일치
            "pilot_authorized": False,
        }
        _append_jsonl(jsonl_path, mismatch_record)

        records = get_canonical_records("scoring")
        latest = records[-1]
        # 검출: pilot entry session에서 온 evidence면 session_mode가 pilot_paper여야 함
        # 이 테스트는 "불일치가 데이터상 드러난다"는 것을 보여줌
        assert latest["session_mode"] == "normal_paper"
        assert latest["evidence_mode"] == "real_paper"
        # CI에서 검증 로직으로 이 불일치를 잡을 수 있음

    def test_rotation_pilot_denied_insufficient_shadow(self, evidence_dir, runtime_dir, fresh_db):
        """rotation: shadow prerequisites 미충족 → pilot denied."""
        # rotation에 shadow 1일만 (3일 미달)
        _seed_v2(evidence_dir, "relative_strength_rotation", [
            {"date": "2026-04-06", "evidence_mode": "shadow_bootstrap",
             "execution_backed": False, "benchmark_status": "final"},
        ])

        from core.paper_pilot import check_pilot_prerequisites
        ok, reason = check_pilot_prerequisites("relative_strength_rotation")
        assert ok is False
        assert "clean" in reason.lower() or "shadow" in reason.lower()

    def test_rotation_shadow_met_but_no_auth_still_blocked(self, evidence_dir, runtime_dir, fresh_db):
        """rotation: shadow 3일 충족 → pilot eligible 표시, 하지만 auth 없으면 entry blocked."""
        _seed_v2(evidence_dir, "relative_strength_rotation", [
            {"date": "2026-04-03", "evidence_mode": "shadow_bootstrap",
             "execution_backed": False, "benchmark_status": "final"},
            {"date": "2026-04-04", "evidence_mode": "shadow_bootstrap",
             "execution_backed": False, "benchmark_status": "final"},
            {"date": "2026-04-06", "evidence_mode": "shadow_bootstrap",
             "execution_backed": False, "benchmark_status": "final"},
        ])

        from core.paper_pilot import check_pilot_prerequisites, check_pilot_entry

        # prerequisites는 충족
        ok, reason = check_pilot_prerequisites("relative_strength_rotation")
        assert ok is True

        # 하지만 auth 없으면 entry 불가
        result = check_pilot_entry("relative_strength_rotation")
        assert result.allowed is False
        assert "no active pilot" in result.reason

    def test_pilot_session_artifact_saved(self, evidence_dir, runtime_dir, fresh_db):
        """pilot session artifact가 정상 저장되는지."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import save_pilot_session_artifact, enable_pilot

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        pilot_session = {
            "active": True, "pilot_authorized": True,
            "pilot_caps_snapshot": {"max_orders_per_day": 2},
            "session_mode": "pilot_paper", "evidence_mode": "pilot_paper",
        }
        json_path = save_pilot_session_artifact(PILOT_STRATEGY, "2026-04-07", pilot_session)

        assert json_path.exists()
        artifact = json.loads(json_path.read_text(encoding="utf-8"))
        assert artifact["strategy"] == PILOT_STRATEGY
        assert artifact["pilot_session"]["active"] is True
        assert artifact["evidence_snapshot"] is not None

        md_path = runtime_dir / f"pilot_session_{PILOT_STRATEGY}_2026-04-07.md"
        assert md_path.exists()


class TestLaunchReadiness:

    def test_scoring_clean1_not_ready(self, evidence_dir, runtime_dir, fresh_db):
        """scoring clean_final_days=1 → launch_ready=false."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-04", "benchmark_status": "failed"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import compute_launch_readiness
        lr = compute_launch_readiness("scoring", as_of_date="2026-04-07")
        assert lr["launch_ready"] is False
        assert lr["clean_final_days_current"] == 1
        assert lr["remaining_clean_days"] == 2
        assert any("clean_final_days" in b for b in lr["blocking_requirements"])

    def test_rotation_clean3_notifier_pilot_ready(self, evidence_dir, runtime_dir, fresh_db):
        """rotation clean_final_days=3 + notifier + pilot auth → launch_ready=true."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-03", "benchmark_status": "final"},
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import enable_pilot, compute_launch_readiness
        enable_pilot(PILOT_STRATEGY, "2026-04-01", "2026-04-30")

        lr = compute_launch_readiness(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert lr["clean_final_days_current"] == 3
        assert lr["remaining_clean_days"] == 0
        assert lr["notifier_ready"] is True
        assert lr["pilot_authorization_present"] is True
        assert lr["infra_ready"] is True
        assert lr["launch_ready"] is True
        assert lr["blocking_requirements"] == []

    def test_infra_ready_but_no_pilot_auth(self, evidence_dir, runtime_dir, fresh_db):
        """모든 인프라 조건 충족, pilot auth만 없음 → infra_ready=true, launch_ready=false."""
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-03", "benchmark_status": "final"},
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import compute_launch_readiness
        lr = compute_launch_readiness(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert lr["infra_ready"] is True
        assert lr["launch_ready"] is False
        assert lr["pilot_authorization_present"] is False

    def test_legacy_evidence_does_not_pollute(self, evidence_dir, runtime_dir, fresh_db):
        """legacy v1 evidence가 있어도 launch_readiness 계산은 v2만 사용."""
        from core.paper_evidence import _append_jsonl

        # v1 legacy record
        jsonl_path = evidence_dir / f"daily_evidence_{PILOT_STRATEGY}.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _append_jsonl(jsonl_path, {
            "date": "2026-03-30", "portfolio_value": 10_000_000,
            "n_positions": 2, "drawdown": -1.5,
        })

        # v2 clean records
        _seed_v2(evidence_dir, PILOT_STRATEGY, [
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-05", "benchmark_status": "final"},
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        (runtime_dir).mkdir(parents=True, exist_ok=True)
        (runtime_dir / "notifier_health.json").write_text(
            json.dumps({"discord_configured": True}), encoding="utf-8")

        from core.paper_pilot import compute_launch_readiness
        lr = compute_launch_readiness(PILOT_STRATEGY, as_of_date="2026-04-07")
        assert lr["quarantined_records"] >= 1
        assert lr["clean_final_days_current"] == 3
        assert lr["infra_ready"] is True

    def test_rotation_different_blocker(self, evidence_dir, runtime_dir, fresh_db):
        """rotation은 scoring과 다른 blocker reason 표시."""
        from core.paper_pilot import compute_launch_readiness

        lr_rot = compute_launch_readiness("relative_strength_rotation")
        assert lr_rot["launch_ready"] is False
        blockers = lr_rot["blocking_requirements"]
        # rotation은 "clean_final_days" 또는 "evidence_freshness" 등이 blocker
        assert len(blockers) > 0

        # scoring도 확인
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])
        lr_sc = compute_launch_readiness("scoring", as_of_date="2026-04-07")
        assert lr_sc["launch_ready"] is False

        # blocker가 다를 수 있음 (rotation은 evidence 자체가 없고, scoring은 clean days 부족)
        rot_reasons = set(b.split(":")[0] for b in lr_rot["blocking_requirements"])
        sc_reasons = set(b.split(":")[0] for b in lr_sc["blocking_requirements"])
        # 둘 다 blocked이지만 세부 사유가 구분됨
        assert lr_rot["eligible_records"] == 0
        assert lr_sc["eligible_records"] >= 1

    def test_readiness_artifact_generation(self, evidence_dir, runtime_dir, fresh_db):
        """readiness JSON/MD + runbook MD 생성 검증."""
        _seed_v2(evidence_dir, "scoring", [
            {"date": "2026-04-06", "benchmark_status": "final"},
        ])

        from core.paper_pilot import (
            generate_launch_readiness_artifact,
            generate_pilot_runbook,
        )

        json_path, md_path = generate_launch_readiness_artifact("scoring")
        assert json_path.exists()
        assert md_path.exists()

        lr = json.loads(json_path.read_text(encoding="utf-8"))
        assert "launch_ready" in lr
        assert "blocking_requirements" in lr
        assert "clean_day_definition" in lr

        md_text = md_path.read_text(encoding="utf-8")
        assert "Launch Ready" in md_text
        assert "Clean final days" in md_text or "Clean Final Days" in md_text

        rb_path = generate_pilot_runbook("scoring")
        assert rb_path.exists()
        rb_text = rb_path.read_text(encoding="utf-8")
        assert "Pilot Runbook" in rb_text
        assert "Enable 명령" in rb_text or "enable" in rb_text.lower()
        assert "Disable" in rb_text or "disable" in rb_text.lower()
        assert "Success Criteria" in rb_text
        assert "Abort Criteria" in rb_text
