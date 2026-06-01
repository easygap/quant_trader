"""
Shadow Smoke Test — 실제 scheduler 경로 E2E 검증

unit test가 아니라 "실제 운영 경로에서 보였다"를 증명한다.

검증 대상:
  1. _execute_entry_candidates() → runtime block → notifier.send_message + _log_op(RUNTIME_BLOCK)
  2. _rescan_for_new_entries() → runtime block → _log_op(RUNTIME_BLOCK)
  3. 두 경로가 동일한 state/block reason으로 차단
  4. OperationEvent DB 기록
  5. runtime_decisions.jsonl 기록
  6. scoring + rotation 둘 다 점검
  7. no-position / open-position 케이스
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_dirs(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_runtime as pr
    import core.paper_preflight as pp
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pr, "RUNTIME_DIR", tmp_path / "paper_runtime")
    # scheduler 의 execute-path preflight 게이트는 paper_preflight.RUNTIME_DIR 를 읽는다.
    # 이걸 격리하지 않으면 실 운영 reports 디렉터리를 보거나 missing→PREFLIGHT_BLOCK 으로
    # runtime-block 경로 도달 전에 early return 한다.
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
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


def _seed_v2_evidence(evidence_dir, strategy, days):
    from core.paper_evidence import _append_jsonl
    jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
    for i, cfg in enumerate(days):
        record = {
            "date": cfg["date"], "day_number": i + 1, "strategy": strategy,
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
            "cross_validation_warnings": [],
            "status": cfg.get("status", "normal"),
            "record_version": 1, "schema_version": 2, "diagnostics": [],
        }
        _append_jsonl(jsonl_path, record)


def _recent_business_days(n):
    """오늘(포함)부터 거슬러 올라가며 최근 n개 평일 날짜 문자열을 오름차순으로 반환.

    evidence staleness 는 latest evidence date vs now 로 계산되므로,
    scheduler/내부 호출이 as_of=now 를 쓰는 테스트는 evidence 를 '오늘 기준'으로
    심어야 stale 처리되지 않는다. (literal 날짜에 의존하지 않아 시간 독립적.)
    """
    out = []
    d = datetime.now().date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def _write_passing_preflight(strategy):
    """scheduler 의 execute-path preflight 게이트를 통과시키기 위해
    overall='pass' 인 preflight_status_{strategy}.json 을 기록한다.

    이 게이트는 runtime-block 경로(discord/RUNTIME_BLOCK)보다 앞에 있으므로,
    blocked 전략의 runtime 차단을 증명하려면 먼저 preflight 를 통과시켜야 한다.
    (preflight 통과 != 거래 허용 — 그 뒤의 runtime state 가 entry 를 차단한다.)
    """
    from core.paper_preflight import PreflightResult, _save_preflight
    result = PreflightResult(
        strategy=strategy,
        date="2026-04-06",
        overall="pass",
        entry_allowed=True,
        exit_allowed=True,
        runtime_state="normal",
        allowed_actions=["run", "entry", "exit"],
        evaluated_at="2026-04-06T09:00:00",
    )
    _save_preflight(result)


def _build_mock_scheduler(strategy_name, mode="paper"):
    """실제 Scheduler 메서드를 테스트할 수 있도록 최소한의 mock 구성."""
    from core.scheduler import Scheduler

    mock_config = MagicMock()
    mock_config.trading = {"mode": mode, "strategy": strategy_name}
    mock_config.risk_params = {"position_limits": {"max_holding_days": 60}}
    mock_config.get_account_no.return_value = "mock_account"
    mock_config.get_account_key.return_value = strategy_name

    # discord (Notifier)
    mock_discord = MagicMock()
    mock_discord.send_message = MagicMock(return_value=True)
    mock_discord.send_trade_alert = MagicMock()

    # other deps
    mock_portfolio = MagicMock()
    mock_portfolio.get_portfolio_summary.return_value = {"total_value": 10_000_000}

    mock_trading_hours = MagicMock()
    mock_trading_hours.is_market_open.return_value = True

    mock_blackswan = MagicMock()
    mock_blackswan.is_on_cooldown.return_value = False

    # Build minimal scheduler using object.__new__ to skip __init__
    sched = object.__new__(Scheduler)
    sched.config = mock_config
    sched.strategy_name = strategy_name
    sched._mode = mode
    sched.discord = mock_discord
    sched.portfolio = mock_portfolio
    sched.trading_hours = mock_trading_hours
    sched.blackswan = mock_blackswan
    sched.auto_entry = True
    sched._entry_candidates = []
    sched._restart_recovery_count = 0
    sched.monitor_interval = 600
    sched._skip_next_monitor_cycle = False
    sched._last_monitor_time = None

    return sched, mock_discord


# ═══════════════════════════════════════════════════════════════
# 1. _execute_entry_candidates 실제 경로 검증
# ═══════════════════════════════════════════════════════════════

class TestExecuteEntryBlock:
    """blocked strategy에서 _execute_entry_candidates → notifier + _log_op + decisions."""

    def test_blocked_strategy_entry_candidates_cleared(self, evidence_dir, runtime_dir, fresh_db):
        """blocked strategy → entry candidates 비움."""
        _seed_v2_evidence(evidence_dir, "smoke_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        sched, mock_discord = _build_mock_scheduler("smoke_s")
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8, "reason": "test"},
        ]

        # market regime mock
        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # candidates가 비워져야 함
        assert sched._entry_candidates == []

    def test_blocked_strategy_discord_called(self, evidence_dir, runtime_dir, fresh_db):
        """blocked → discord.send_message(critical=True) 호출."""
        _seed_v2_evidence(evidence_dir, "smoke_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("smoke_s")  # preflight 게이트 통과 → runtime-block 경로 도달

        sched, mock_discord = _build_mock_scheduler("smoke_s")
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8, "reason": "test"},
        ]

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # discord.send_message가 critical=True로 호출됐는지
        mock_discord.send_message.assert_called()
        call_args = mock_discord.send_message.call_args
        msg_text = call_args[0][0] if call_args[0] else call_args[1].get("text", "")
        assert "BLOCKED_INSUFFICIENT_EVIDENCE" in msg_text.upper() or \
               "DEGRADED" in msg_text.upper() or \
               "FROZEN" in msg_text.upper()
        # critical=True
        assert call_args[1].get("critical") is True or \
               (len(call_args[0]) > 1 and call_args[0][1] is True) or \
               call_args.kwargs.get("critical") is True

    def test_blocked_strategy_operation_event_recorded(self, evidence_dir, runtime_dir, fresh_db):
        """blocked → OperationEvent(RUNTIME_BLOCK) DB 기록."""
        _seed_v2_evidence(evidence_dir, "smoke_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("smoke_s")  # preflight 게이트 통과 → runtime-block 경로 도달

        sched, _ = _build_mock_scheduler("smoke_s")
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8, "reason": "test"},
        ]

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # OperationEvent에 RUNTIME_BLOCK 기록 확인
        from database.models import get_session, OperationEvent
        session = get_session()
        events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "RUNTIME_BLOCK"
        ).all()
        session.close()

        assert len(events) >= 1
        ev = events[-1]
        assert ev.strategy == "smoke_s"
        assert ev.severity == "warning"
        detail = json.loads(ev.detail) if ev.detail else {}
        # 구조화된 payload 필드 검증
        assert "state" in detail
        assert "strategy" in detail
        assert "evidence_date" in detail
        assert "benchmark_final_ratio" in detail or "recent_final_ratio" in detail
        assert "allowed_actions" in detail
        assert "reasons" in detail

    def test_blocked_strategy_runtime_decisions_recorded(self, evidence_dir, runtime_dir, fresh_db):
        """blocked → runtime_decisions.jsonl에 evaluate 기록."""
        _seed_v2_evidence(evidence_dir, "smoke_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("smoke_s")  # preflight 게이트 통과 → runtime-block 경로 도달

        sched, _ = _build_mock_scheduler("smoke_s")
        sched._entry_candidates = [{"symbol": "005930", "price": 62000}]

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # runtime_decisions.jsonl
        decisions_path = runtime_dir / "runtime_decisions.jsonl"
        assert decisions_path.exists()
        lines = [l for l in decisions_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        assert len(lines) >= 1
        last = json.loads(lines[-1])
        assert last["strategy"] == "smoke_s"
        assert "state" in last

    def test_normal_strategy_candidates_not_cleared(self, evidence_dir, runtime_dir, fresh_db):
        """normal strategy → candidates 유지, 실행 시도."""
        # execute 의 runtime guard 는 as_of=now 를 쓰므로 fresh evidence 로 심어야 normal.
        n1, n2, n3 = _recent_business_days(3)
        _seed_v2_evidence(evidence_dir, "normal_s", [
            {"date": n1, "benchmark_status": "final"},
            {"date": n2, "benchmark_status": "final"},
            {"date": n3, "benchmark_status": "final"},
        ])
        _write_passing_preflight("normal_s")  # execute-path preflight 게이트 통과

        sched, mock_discord = _build_mock_scheduler("normal_s")
        sched._entry_candidates = [
            {"symbol": "005930", "price": 62000, "atr": 1200, "score": 0.8, "reason": "test"},
        ]

        # market regime OK, get_position None → 실행 시도
        mock_executor = MagicMock()
        mock_executor.execute_buy.return_value = {"success": True, "symbol": "005930", "action": "BUY"}
        mock_strategy = MagicMock()
        mock_strategy.generate_signal.return_value = {"signal": "BUY", "score": 0.8}

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None), \
             patch.object(sched, "_get_or_create_executor", return_value=mock_executor), \
             patch.object(sched, "_get_strategy", return_value=mock_strategy):
            sched._execute_entry_candidates()

        # discord에 RUNTIME_BLOCK 메시지가 아닌 것을 확인
        for c in mock_discord.send_message.call_args_list:
            msg = c[0][0] if c[0] else ""
            assert "BLOCKED" not in msg.upper()
            assert "FROZEN" not in msg.upper()


# ═══════════════════════════════════════════════════════════════
# 2. _rescan_for_new_entries 실제 경로 검증
# ═══════════════════════════════════════════════════════════════

class TestRescanBlock:
    """blocked strategy에서 _rescan_for_new_entries → 즉시 return + _log_op."""

    def test_blocked_rescan_returns_early(self, evidence_dir, runtime_dir, fresh_db):
        """blocked → rescan 즉시 return, DataCollector 호출 없음."""
        _seed_v2_evidence(evidence_dir, "rescan_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        sched, _ = _build_mock_scheduler("rescan_s")

        with patch("core.data_collector.DataCollector") as MockDC, \
             patch("core.market_regime.check_market_regime") as MockRegime:
            sched._rescan_for_new_entries()

        # DataCollector와 check_market_regime이 호출되지 않아야 함 (early return)
        MockDC.assert_not_called()
        MockRegime.assert_not_called()

    def test_blocked_rescan_logs_operation_event(self, evidence_dir, runtime_dir, fresh_db):
        """blocked rescan → OperationEvent(RUNTIME_BLOCK) 기록."""
        _seed_v2_evidence(evidence_dir, "rescan_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])

        sched, _ = _build_mock_scheduler("rescan_s")
        sched._rescan_for_new_entries()

        from database.models import get_session, OperationEvent
        session = get_session()
        events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "RUNTIME_BLOCK",
            OperationEvent.strategy == "rescan_s",
        ).all()
        session.close()

        assert len(events) >= 1
        assert "rescan blocked" in events[-1].message

    def test_normal_rescan_proceeds(self, evidence_dir, runtime_dir, fresh_db):
        """normal → rescan은 DataCollector까지 진행."""
        # rescan 은 내부에서 as_of=now 로 runtime state 를 평가한다.
        # normal 로 판정되려면 evidence 가 fresh 해야 하므로 오늘 기준 최근 3 평일로 심는다.
        d1, d2, d3 = _recent_business_days(3)
        _seed_v2_evidence(evidence_dir, "normal_r", [
            {"date": d1, "benchmark_status": "final"},
            {"date": d2, "benchmark_status": "final"},
            {"date": d3, "benchmark_status": "final"},
        ])

        sched, _ = _build_mock_scheduler("normal_r")

        with patch("core.data_collector.DataCollector") as MockDC, \
             patch("core.market_regime.check_market_regime", return_value={"allow_buys": True}), \
             patch("core.watchlist_manager.WatchlistManager") as MockWM:
            MockWM.return_value.resolve.return_value = ["005930"]
            mock_collector = MagicMock()
            mock_collector.fetch_stock.return_value = None
            MockDC.return_value = mock_collector
            sched._rescan_for_new_entries()

        # normal이므로 DataCollector가 호출됨
        MockDC.assert_called()


# ═══════════════════════════════════════════════════════════════
# 3. execute vs rescan 동일 block reason 검증
# ═══════════════════════════════════════════════════════════════

class TestExecuteRescanConsistency:
    """두 경로가 동일한 runtime state로 차단."""

    def test_same_block_state_for_both_paths(self, evidence_dir, runtime_dir, fresh_db):
        """execute와 rescan이 동일 state로 차단."""
        _seed_v2_evidence(evidence_dir, "consist_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("consist_s")  # execute-path preflight 게이트 통과

        sched, mock_discord = _build_mock_scheduler("consist_s")

        # execute path
        sched._entry_candidates = [{"symbol": "005930", "price": 62000}]
        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()
        assert sched._entry_candidates == []

        # rescan path
        with patch("core.data_collector.DataCollector") as MockDC, \
             patch("core.market_regime.check_market_regime") as MockRegime:
            sched._rescan_for_new_entries()
        MockDC.assert_not_called()  # early return

        # 두 경로 모두 같은 state로 차단됨
        from database.models import get_session, OperationEvent
        session = get_session()
        events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "RUNTIME_BLOCK",
            OperationEvent.strategy == "consist_s",
        ).all()
        session.close()

        assert len(events) >= 2  # execute + rescan
        states_in_events = set()
        for ev in events:
            detail = json.loads(ev.detail) if ev.detail else {}
            if "state" in detail:
                states_in_events.add(detail["state"])
            else:
                # rescan은 message에 state 포함
                if "blocked_insufficient_evidence" in ev.message:
                    states_in_events.add("blocked_insufficient_evidence")
        # 같은 state로 차단
        assert len(states_in_events) <= 1 or "blocked_insufficient_evidence" in states_in_events


# ═══════════════════════════════════════════════════════════════
# 4. scoring + rotation 양쪽 점검
# ═══════════════════════════════════════════════════════════════

class TestDualStrategySmoke:
    """scoring과 rotation이 각각 독립된 state를 가지고 일관적으로 동작."""

    def test_scoring_blocked_rotation_normal(self, evidence_dir, runtime_dir, fresh_db):
        """scoring: blocked, rotation: normal → entry 정책이 각각 독립."""
        # scoring: insufficient evidence
        _seed_v2_evidence(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        # rotation: normal (5일 clean final)
        _seed_v2_evidence(evidence_dir, "rotation", [
            {"date": "2026-04-01", "benchmark_status": "final"},
            {"date": "2026-04-02", "benchmark_status": "final"},
            {"date": "2026-04-03", "benchmark_status": "final"},
            {"date": "2026-04-04", "benchmark_status": "final"},
            {"date": "2026-04-05", "benchmark_status": "final"},
        ])

        from core.paper_runtime import get_paper_runtime_state

        # as_of_date 를 각 전략의 최신 evidence 날짜로 고정해 시간 독립적으로 만든다.
        # (고정하지 않으면 "now" 대비 evidence 가 stale → 강제로 blocked 처리되어
        #  rotation 의 "normal" 기대가 깨진다.)
        s_state = get_paper_runtime_state("scoring", as_of_date="2026-04-06")
        r_state = get_paper_runtime_state("rotation", as_of_date="2026-04-05")

        assert s_state.state == "blocked_insufficient_evidence"
        assert "entry" not in s_state.allowed_actions
        assert "exit" in s_state.allowed_actions

        assert r_state.state == "normal"
        assert "entry" in r_state.allowed_actions

    def test_both_strategies_execute_block(self, evidence_dir, runtime_dir, fresh_db):
        """scoring: blocked → execute 차단. rotation: normal → execute 통과."""
        # scoring: blocked (stale/insufficient evidence — blocked 기대이므로 날짜 무관)
        _seed_v2_evidence(evidence_dir, "scoring", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        # rotation: normal — execute 의 runtime guard 가 as_of=now 를 쓰므로
        # fresh evidence(오늘 기준 최근 3 평일)로 심어야 normal 로 판정되어 진입을 진행한다.
        r1, r2, r3 = _recent_business_days(3)
        _seed_v2_evidence(evidence_dir, "rotation", [
            {"date": r1, "benchmark_status": "final"},
            {"date": r2, "benchmark_status": "final"},
            {"date": r3, "benchmark_status": "final"},
        ])
        # 두 전략 모두 execute-path preflight 게이트를 통과시킨다.
        _write_passing_preflight("scoring")
        _write_passing_preflight("rotation")

        # scoring: blocked
        sched_s, discord_s = _build_mock_scheduler("scoring")
        sched_s._entry_candidates = [{"symbol": "005930", "price": 62000}]
        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched_s._execute_entry_candidates()
        assert sched_s._entry_candidates == []
        discord_s.send_message.assert_called()

        # rotation: normal → 실행 시도
        sched_r, discord_r = _build_mock_scheduler("rotation")
        sched_r._entry_candidates = [{"symbol": "000660", "price": 120000, "atr": 2000, "score": 0.9, "reason": "test"}]
        mock_executor = MagicMock()
        mock_executor.execute_buy.return_value = {"success": False}
        mock_strategy = MagicMock()
        mock_strategy.generate_signal.return_value = {"signal": "BUY", "score": 0.9}
        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None), \
             patch.object(sched_r, "_get_or_create_executor", return_value=mock_executor), \
             patch.object(sched_r, "_get_strategy", return_value=mock_strategy):
            sched_r._execute_entry_candidates()

        # rotation에서는 BLOCKED 메시지가 없어야 함
        for c in discord_r.send_message.call_args_list:
            msg = c[0][0] if c[0] else ""
            assert "BLOCKED" not in msg.upper()


# ═══════════════════════════════════════════════════════════════
# 5. no-position / open-position 케이스
# ═══════════════════════════════════════════════════════════════

class TestPositionAwareBlock:
    """open position 유무에 따른 exit 허용."""

    def test_no_position_blocked_entry_only(self, evidence_dir, runtime_dir, fresh_db):
        """position 없음 + blocked → entry X, exit 허용(no-op)."""
        _seed_v2_evidence(evidence_dir, "nopos_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
        ])

        from core.paper_runtime import get_paper_runtime_state
        state = get_paper_runtime_state("nopos_s")
        assert "entry" not in state.allowed_actions
        assert "exit" in state.allowed_actions  # 코드상 허용 (호출해도 no-op)

    def test_open_position_degraded_exit_allowed(self, evidence_dir, runtime_dir, fresh_db):
        """open position + degraded → entry X, exit O (실제 scheduler 경로)."""
        from database.models import get_session, Position

        # degraded state 는 evidence 가 fresh 해야 성립한다 (stale 이면 blocked 로 강등).
        # 내부 is_paper_trade_allowed / scheduler 가 as_of=now 를 쓰므로 오늘 기준으로 심는다.
        today = _recent_business_days(1)[0]
        _seed_v2_evidence(evidence_dir, "openpos_s", [
            {"date": today, "reject_count": 5, "status": "degraded",
             "anomalies": [{"type": "repeated_reject", "severity": "warning"}]},
        ])
        _write_passing_preflight("openpos_s")  # execute-path preflight 게이트 통과

        # open position 삽입
        session = get_session()
        session.add(Position(
            account_key="openpos_s", symbol="005930",
            avg_price=60000, quantity=10, total_invested=600000,
            strategy="scoring",
        ))
        session.commit()
        session.close()

        from core.paper_runtime import get_paper_runtime_state, is_paper_trade_allowed
        state = get_paper_runtime_state("openpos_s")
        assert state.state == "degraded"
        assert is_paper_trade_allowed("openpos_s", "entry") is False
        assert is_paper_trade_allowed("openpos_s", "exit") is True

        # execute에서 candidates 비워짐
        sched, mock_discord = _build_mock_scheduler("openpos_s")
        sched._entry_candidates = [{"symbol": "000660", "price": 120000}]
        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()
        assert sched._entry_candidates == []

        # 하지만 exit signal check는 허용 (scheduler의 _check_exit_signals는 runtime guard 없음)
        assert is_paper_trade_allowed("openpos_s", "exit") is True


# ═══════════════════════════════════════════════════════════════
# 6. Notifier payload 검증
# ═══════════════════════════════════════════════════════════════

class TestNotifierPayload:
    """Discord notifier에 전달되는 payload가 block reason 전체를 포함."""

    def test_discord_payload_contains_state_and_reason(self, evidence_dir, runtime_dir, fresh_db):
        """discord.send_message payload에 state, evidence_date, reasons 포함."""
        _seed_v2_evidence(evidence_dir, "notif_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("notif_s")  # preflight 게이트 통과 → runtime-block 경로 도달

        sched, mock_discord = _build_mock_scheduler("notif_s")
        sched._entry_candidates = [{"symbol": "005930", "price": 62000}]

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # discord payload 캡처
        assert mock_discord.send_message.call_count >= 1
        call_args = mock_discord.send_message.call_args
        payload_text = call_args[0][0]

        # 필수 필드 확인
        assert "state=" in payload_text
        assert "strategy=" in payload_text
        assert "evidence_date=" in payload_text
        assert "benchmark_final_ratio=" in payload_text
        assert "allowed_actions=" in payload_text


# ═══════════════════════════════════════════════════════════════
# 7. Operator Observability 패키지 (1회 smoke run 기준)
# ═══════════════════════════════════════════════════════════════

class TestObservabilityPackage:
    """1회 shadow smoke run으로 생성되는 모든 artifact 검증."""

    def test_full_observability_package(self, evidence_dir, runtime_dir, fresh_db):
        """execute block 1회 → 5개 artifact 모두 생성."""
        _seed_v2_evidence(evidence_dir, "obs_s", [
            {"date": "2026-04-03", "same_universe_excess": None, "benchmark_status": "failed"},
            {"date": "2026-04-06", "same_universe_excess": -1.75, "benchmark_status": "final"},
        ])
        _write_passing_preflight("obs_s")  # preflight 게이트 통과 → runtime-block 경로 도달

        sched, _ = _build_mock_scheduler("obs_s")
        sched._entry_candidates = [{"symbol": "005930", "price": 62000}]

        with patch("core.market_regime.check_market_regime", return_value={"allow_buys": True, "position_scale": 1.0}), \
             patch("core.data_collector.DataCollector"), \
             patch("database.repositories.get_position", return_value=None):
            sched._execute_entry_candidates()

        # 1. runtime_status_{strategy}.json
        status_path = runtime_dir / "runtime_status_obs_s.json"
        assert status_path.exists()
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] in ("blocked_insufficient_evidence", "degraded")

        # 2. runtime_decisions.jsonl
        decisions_path = runtime_dir / "runtime_decisions.jsonl"
        assert decisions_path.exists()
        lines = [l for l in decisions_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        assert any(json.loads(l)["strategy"] == "obs_s" for l in lines)

        # 3. OperationEvent (RUNTIME_BLOCK)
        from database.models import get_session, OperationEvent
        session = get_session()
        events = session.query(OperationEvent).filter(
            OperationEvent.event_type == "RUNTIME_BLOCK",
            OperationEvent.strategy == "obs_s",
        ).all()
        session.close()
        assert len(events) >= 1

        # 4. audit markdown
        from core.paper_runtime import generate_runtime_audit
        audit_path = generate_runtime_audit("obs_s")
        assert audit_path is not None
        assert audit_path.exists()
        audit_content = audit_path.read_text(encoding="utf-8")
        assert "obs_s" in audit_content

        # 5. notifier payload sample (mock에서 캡처 가능 — 실제 Discord 미발송)
        # → test_discord_payload_contains_state_and_reason에서 별도 검증
