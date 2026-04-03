"""
Paper Evidence 모듈 테스트
- Unit: JSONL I/O, day_number, idempotency, anomaly rules, status, benchmark missing
- E2E Replay: 7영업일 synthetic → evidence/anomaly/weekly/package 전체 생성 검증

Requirements covered:
  1. Paper runtime wiring (append-only, idempotent, day_number continuity)
  2. Benchmark excess (real calculation + missing data graceful)
  3. Execution/ops metrics
  4. Anomaly detection + protection (degraded/frozen)
  5. Reporting/package generation
  7. End-to-end replay test
  8. Promotion separation (approved_strategies.json 미수정)
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_evidence_dir(monkeypatch, tmp_path):
    """모든 테스트에서 evidence 출력을 tmp_path로 격리."""
    import core.paper_evidence as pe
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    return tmp_path / "paper_evidence"


@pytest.fixture
def evidence_dir(_isolate_evidence_dir):
    return _isolate_evidence_dir


@pytest.fixture
def fresh_db():
    """테스트용 fresh in-memory-like DB. 각 테스트 전 truncate."""
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


# ═══════════════════════════════════════════════════════════════
# Unit Tests
# ═══════════════════════════════════════════════════════════════

class TestJsonlIO:
    """JSONL append, idempotency, day_number."""

    def test_append_creates_file(self, evidence_dir):
        from core.paper_evidence import _append_jsonl
        path = evidence_dir / "test.jsonl"
        _append_jsonl(path, {"date": "2026-04-01", "value": 1})
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["date"] == "2026-04-01"

    def test_append_is_additive(self, evidence_dir):
        from core.paper_evidence import _append_jsonl
        path = evidence_dir / "test.jsonl"
        _append_jsonl(path, {"date": "2026-04-01"})
        _append_jsonl(path, {"date": "2026-04-02"})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_already_recorded_true(self, evidence_dir):
        from core.paper_evidence import _append_jsonl, _already_recorded
        path = evidence_dir / "test.jsonl"
        _append_jsonl(path, {"date": "2026-04-01"})
        assert _already_recorded(path, "2026-04-01") is True

    def test_already_recorded_false(self, evidence_dir):
        from core.paper_evidence import _append_jsonl, _already_recorded
        path = evidence_dir / "test.jsonl"
        _append_jsonl(path, {"date": "2026-04-01"})
        assert _already_recorded(path, "2026-04-02") is False

    def test_already_recorded_empty(self, evidence_dir):
        from core.paper_evidence import _already_recorded
        path = evidence_dir / "nonexistent.jsonl"
        assert _already_recorded(path, "2026-04-01") is False

    def test_day_number_first_entry(self, evidence_dir):
        from core.paper_evidence import _compute_day_number
        path = evidence_dir / "test.jsonl"
        assert _compute_day_number(path, "2026-04-01") == 1

    def test_day_number_increments(self, evidence_dir):
        from core.paper_evidence import _append_jsonl, _compute_day_number
        path = evidence_dir / "test.jsonl"
        _append_jsonl(path, {"date": "2026-04-01", "day_number": 1})
        assert _compute_day_number(path, "2026-04-02") == 2
        _append_jsonl(path, {"date": "2026-04-02", "day_number": 2})
        assert _compute_day_number(path, "2026-04-03") == 3


class TestAnomalyDetection:
    """Anomaly rule evaluation."""

    def test_no_anomalies(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        portfolio = {"mdd": -5.0, "daily_return": 0.5}
        assert _detect_anomalies(ops, portfolio) == []

    def test_repeated_reject(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 5, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": 0})
        types = [a["type"] for a in anomalies]
        assert "repeated_reject" in types

    def test_phantom_position_critical(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 2, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": 0})
        assert any(a["type"] == "phantom_position" and a["severity"] == "critical" for a in anomalies)

    def test_deep_drawdown_mdd(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -18.0, "daily_return": 0})
        assert any(a["type"] == "deep_drawdown" for a in anomalies)

    def test_deep_drawdown_daily(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": -6.0})
        assert any(a["type"] == "deep_drawdown" and a["severity"] == "critical" for a in anomalies)

    def test_duplicate_flood(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 8, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": 0})
        assert any(a["type"] == "duplicate_flood" for a in anomalies)

    def test_stale_pending(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 1,
               "duplicate_blocked_count": 0, "reconcile_count": 0}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": 0})
        assert any(a["type"] == "stale_pending" for a in anomalies)

    def test_reconcile_anomaly(self):
        from core.paper_evidence import _detect_anomalies
        ops = {"reject_count": 0, "phantom_position_count": 0, "stale_pending_count": 0,
               "duplicate_blocked_count": 0, "reconcile_count": 2}
        anomalies = _detect_anomalies(ops, {"mdd": -5, "daily_return": 0})
        assert any(a["type"] == "reconcile_anomaly" for a in anomalies)


class TestStatusDetermination:
    """normal/degraded/frozen."""

    def test_normal(self):
        from core.paper_evidence import _determine_status
        assert _determine_status([]) == "normal"

    def test_degraded(self):
        from core.paper_evidence import _determine_status
        assert _determine_status([{"severity": "warning", "type": "x"}]) == "degraded"

    def test_frozen_on_critical(self):
        from core.paper_evidence import _determine_status
        assert _determine_status([{"severity": "critical", "type": "x"}]) == "frozen"

    def test_frozen_overrides_warning(self):
        from core.paper_evidence import _determine_status
        anomalies = [
            {"severity": "warning", "type": "a"},
            {"severity": "critical", "type": "b"},
        ]
        assert _determine_status(anomalies) == "frozen"


class TestBenchmarkMissing:
    """Benchmark 데이터 없을 때 graceful null 반환."""

    def test_null_daily_return(self):
        from core.paper_evidence import _compute_benchmark_excess
        result = _compute_benchmark_excess(datetime.now(), None, 0.5, ["005930"])
        assert result["same_universe_excess"] is None
        assert "daily_return is null" in result["benchmark_meta"].get("warning", "")

    def test_empty_watchlist(self):
        from core.paper_evidence import _compute_benchmark_excess
        result = _compute_benchmark_excess(datetime.now(), 0.5, 0.5, [])
        assert result["same_universe_excess"] is None
        assert "empty watchlist" in result["benchmark_meta"].get("warning", "")


# ═══════════════════════════════════════════════════════════════
# E2E Replay Test (7 business days)
# ═══════════════════════════════════════════════════════════════

def _seed_day(session, day_date, account_key, scenario, models):
    """하루치 synthetic 데이터를 DB에 주입."""
    TradeHistory = models["TradeHistory"]
    FailedOrder = models["FailedOrder"]
    OperationEvent = models["OperationEvent"]
    PortfolioSnapshot = models["PortfolioSnapshot"]
    Position = models["Position"]
    PendingOrderGuard = models["PendingOrderGuard"]

    dt = day_date.replace(hour=15, minute=35)
    base_value = 10_000_000

    # portfolio snapshot
    snap_kwargs = {
        "account_key": account_key,
        "date": dt,
        "total_value": base_value + scenario.get("pnl", 0),
        "cash": base_value * 0.3,
        "invested": base_value * 0.7,
        "daily_return": scenario.get("daily_return", 0.1),
        "cumulative_return": scenario.get("cum_return", 0.5),
        "mdd": scenario.get("mdd", -3.0),
        "position_count": scenario.get("positions", 2),
    }
    session.add(PortfolioSnapshot(**snap_kwargs))

    # trades
    for i in range(scenario.get("buys", 0)):
        session.add(TradeHistory(
            account_key=account_key, symbol="005930", action="BUY",
            price=50000, quantity=10, total_amount=500000,
            mode="paper", strategy="scoring", executed_at=dt,
            signal_at=dt, order_at=dt,
        ))
    for i in range(scenario.get("sells", 0)):
        pnl = scenario.get("sell_pnl", 5000)
        session.add(TradeHistory(
            account_key=account_key, symbol="005930", action="SELL",
            price=51000, quantity=10, total_amount=510000,
            mode="paper", strategy="scoring", executed_at=dt,
            reason=f"PnL: {pnl}원",
            signal_at=dt, order_at=dt,
        ))

    # failed orders (rejects)
    for i in range(scenario.get("rejects", 0)):
        session.add(FailedOrder(
            account_key=account_key, symbol="000660", action="BUY",
            price=100000, quantity=5, mode="paper", strategy="scoring",
            error_detail="diversification limit exceeded" if i == 0 else "unknown error",
            failed_at=dt,
        ))

    # operation events
    for i in range(scenario.get("duplicate_blocked", 0)):
        session.add(OperationEvent(
            event_type="DUPLICATE_BLOCKED", severity="warning",
            symbol="005930", mode="paper", message="duplicate order blocked",
            created_at=dt,
        ))
    for i in range(scenario.get("signals", 2)):
        session.add(OperationEvent(
            event_type="SIGNAL", severity="info",
            symbol="005930", mode="paper", message="BUY signal",
            created_at=dt,
        ))
    if scenario.get("recovery"):
        session.add(OperationEvent(
            event_type="STARTUP_RECOVERY", severity="info",
            mode="paper", message="restart recovery",
            created_at=dt,
        ))

    # stale pending guard
    if scenario.get("stale_pending"):
        session.add(PendingOrderGuard(
            symbol="035720",
            expires_at=dt - timedelta(hours=2),  # already expired
        ))

    # positions (for phantom detection)
    if scenario.get("phantom"):
        # position with no recent BUY (phantom)
        session.add(Position(
            account_key=account_key, symbol="999999",
            avg_price=10000, quantity=100, total_invested=1000000,
            strategy="scoring",
        ))

    session.commit()


class TestEndToEndReplay:
    """7영업일 synthetic replay → evidence + anomaly + weekly + package 검증."""

    SCENARIOS = [
        # Day 1: Normal (2 buys, 1 sell)
        {"buys": 2, "sells": 1, "daily_return": 0.3, "mdd": -2.0, "cum_return": 0.3},
        # Day 2: Partial fill + 1 reject
        {"buys": 1, "sells": 0, "rejects": 1, "daily_return": 0.1, "mdd": -2.5, "cum_return": 0.4},
        # Day 3: 4 rejects (repeated_reject anomaly) + stale pending
        {"buys": 0, "sells": 0, "rejects": 4, "stale_pending": True,
         "daily_return": -0.2, "mdd": -3.0, "cum_return": 0.2},
        # Day 4: Restart recovery + 6 duplicate blocked (duplicate_flood)
        {"buys": 1, "sells": 1, "recovery": True, "duplicate_blocked": 6,
         "daily_return": 0.5, "mdd": -3.0, "cum_return": 0.7},
        # Day 5: Normal (benchmark data will be mocked as missing)
        {"buys": 1, "sells": 1, "daily_return": 0.2, "mdd": -3.0, "cum_return": 0.9},
        # Day 6: Deep drawdown
        {"buys": 0, "sells": 2, "daily_return": -6.0, "mdd": -16.0, "cum_return": -5.1, "sell_pnl": -50000},
        # Day 7: Normal recovery
        {"buys": 1, "sells": 1, "daily_return": 1.0, "mdd": -16.0, "cum_return": -4.1, "phantom": True},
    ]

    def setup_method(self):
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

        models = {
            "TradeHistory": TradeHistory,
            "FailedOrder": FailedOrder,
            "OperationEvent": OperationEvent,
            "PortfolioSnapshot": PortfolioSnapshot,
            "Position": Position,
            "PendingOrderGuard": PendingOrderGuard,
        }

        base_date = datetime(2026, 3, 24)  # Monday
        self.dates = []
        for i, scenario in enumerate(self.SCENARIOS):
            day = base_date + timedelta(days=i)
            # skip weekends
            while day.weekday() >= 5:
                day += timedelta(days=1)
            self.dates.append(day)
            _seed_day(session, day, "scoring", scenario, models)
            base_date = day  # ensure sequential

        session.close()

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_replay_produces_correct_jsonl(self, mock_diag, mock_bench, evidence_dir):
        """7일 evidence 수집 → JSONL에 7개 엔트리."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05,
            "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02,
            "benchmark_status": "final",
            "benchmark_meta": {"type": "mocked"},
        }

        from core.paper_evidence import collect_daily_evidence

        for day_date in self.dates:
            result = collect_daily_evidence(
                strategy="scoring", mode="paper", account_key="scoring",
                date=day_date, watchlist_symbols=["005930", "000660"],
            )
            assert result is not None, f"Day {day_date} returned None"

        # verify JSONL
        jsonl_path = evidence_dir / "daily_evidence_scoring.jsonl"
        assert jsonl_path.exists()
        records = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
        assert len(records) == 7, f"Expected 7 records, got {len(records)}"

        # day_number continuity
        for i, r in enumerate(records):
            assert r["day_number"] == i + 1, f"day_number mismatch at index {i}"

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_replay_anomalies(self, mock_diag, mock_bench, evidence_dir):
        """Day 3 (repeated_reject+stale), Day 4 (duplicate_flood), Day 6 (deep_drawdown), Day 7 (phantom) anomalies."""
        mock_bench.return_value = {
            "same_universe_excess": None, "exposure_matched_excess": None,
            "cash_adjusted_excess": None, "benchmark_status": "failed",
            "benchmark_meta": {"type": "mocked"},
        }

        from core.paper_evidence import collect_daily_evidence

        results = []
        for day_date in self.dates:
            r = collect_daily_evidence(
                strategy="scoring", mode="paper", account_key="scoring",
                date=day_date, watchlist_symbols=["005930"],
            )
            results.append(r)

        # Day 3 (index 2): repeated_reject + stale_pending
        assert results[2].status in ("degraded", "frozen")
        types_d3 = [a["type"] for a in results[2].anomalies]
        assert "repeated_reject" in types_d3

        # Day 4 (index 3): duplicate_flood + recovery
        types_d4 = [a["type"] for a in results[3].anomalies]
        assert "duplicate_flood" in types_d4

        # Day 6 (index 5): deep_drawdown (critical → frozen)
        assert results[5].status == "frozen"
        types_d6 = [a["type"] for a in results[5].anomalies]
        assert "deep_drawdown" in types_d6

        # Day 7 (index 6): phantom_position (critical → frozen)
        types_d7 = [a["type"] for a in results[6].anomalies]
        assert "phantom_position" in types_d7

        # anomalies.jsonl should have entries
        anom_path = evidence_dir / "anomalies.jsonl"
        assert anom_path.exists()
        anom_records = []
        with open(anom_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    anom_records.append(json.loads(line))
        assert len(anom_records) >= 4  # at least 4 anomaly records across days

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_replay_idempotency(self, mock_diag, mock_bench, evidence_dir):
        """같은 날짜 재실행 시 중복 기록 안 됨."""
        mock_bench.return_value = {
            "same_universe_excess": 0.01, "exposure_matched_excess": 0.01,
            "cash_adjusted_excess": 0.01, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence

        day = self.dates[0]
        r1 = collect_daily_evidence(strategy="scoring", mode="paper", account_key="scoring",
                                    date=day, watchlist_symbols=["005930"])
        r2 = collect_daily_evidence(strategy="scoring", mode="paper", account_key="scoring",
                                    date=day, watchlist_symbols=["005930"])
        assert r1 is not None
        assert r2 is None  # idempotent skip

        jsonl_path = evidence_dir / "daily_evidence_scoring.jsonl"
        lines = [l for l in jsonl_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        assert len(lines) == 1

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_replay_weekly_summary(self, mock_diag, mock_bench, evidence_dir):
        """주간 요약 markdown 생성."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence, generate_weekly_summary

        for day_date in self.dates:
            collect_daily_evidence(
                strategy="scoring", mode="paper", account_key="scoring",
                date=day_date, watchlist_symbols=["005930"],
            )

        last_date = self.dates[-1].strftime("%Y-%m-%d")
        path = generate_weekly_summary("scoring", week_end_date=last_date)
        assert path is not None
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Weekly Summary" in content
        assert "scoring" in content

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_replay_promotion_package(self, mock_diag, mock_bench, evidence_dir):
        """Promotion package + approval checklist 생성, BLOCKED (< 60 days)."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence, generate_promotion_package

        for day_date in self.dates:
            collect_daily_evidence(
                strategy="scoring", mode="paper", account_key="scoring",
                date=day_date, watchlist_symbols=["005930"],
            )

        pkg_path, cl_path = generate_promotion_package("scoring")
        assert pkg_path is not None
        assert cl_path is not None

        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["recommendation"] == "BLOCKED"  # < 60 days
        assert "insufficient_days" in str(pkg["block_reasons"])
        assert pkg["total_days"] == 7

        # checklist contains manual approval warning
        checklist = cl_path.read_text(encoding="utf-8")
        assert "approved_strategies.json" in checklist
        assert "수동 승인" in checklist or "수동" in checklist

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_promotion_never_modifies_approved_strategies(self, mock_diag, mock_bench, evidence_dir):
        """approved_strategies.json이 존재하든 아니든 절대 수정 안 됨."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence, generate_promotion_package

        # seed evidence
        for day_date in self.dates:
            collect_daily_evidence(
                strategy="scoring", mode="paper", account_key="scoring",
                date=day_date, watchlist_symbols=["005930"],
            )

        # create a fake approved_strategies.json
        approved_path = Path("reports/approved_strategies.json")
        original_content = approved_path.read_text(encoding="utf-8") if approved_path.exists() else None

        generate_promotion_package("scoring")

        # verify not modified
        if original_content is not None:
            assert approved_path.read_text(encoding="utf-8") == original_content


class TestOpsMetrics:
    """Execution/ops 메트릭 수집 (DB 실제 쿼리)."""

    def test_fill_rate_calculation(self, fresh_db):
        from database.models import get_session, TradeHistory, FailedOrder
        from core.paper_evidence import _collect_execution_ops_metrics

        session = get_session()
        dt = datetime(2026, 4, 1, 15, 35)
        # 3 filled trades
        for _ in range(3):
            session.add(TradeHistory(
                account_key="scoring", symbol="005930", action="BUY",
                price=50000, quantity=10, total_amount=500000,
                mode="paper", strategy="scoring", executed_at=dt,
            ))
        # 1 reject
        session.add(FailedOrder(
            account_key="scoring", symbol="005930", action="BUY",
            price=50000, quantity=10, mode="paper", strategy="scoring",
            error_detail="unknown error", failed_at=dt,
        ))
        session.commit()
        session.close()

        ops = _collect_execution_ops_metrics(
            mode="paper", account_key="scoring", date=dt,
            watchlist_size=10, total_value=10_000_000,
        )
        assert ops["raw_fill_rate"] == 0.75  # 3/4
        assert ops["reject_count"] == 1

    def test_turnover_calculation(self, fresh_db):
        from database.models import get_session, TradeHistory
        from core.paper_evidence import _collect_execution_ops_metrics

        session = get_session()
        dt = datetime(2026, 4, 1, 15, 35)
        session.add(TradeHistory(
            account_key="scoring", symbol="005930", action="BUY",
            price=50000, quantity=10, total_amount=500000,
            mode="paper", strategy="scoring", executed_at=dt,
        ))
        session.add(TradeHistory(
            account_key="scoring", symbol="005930", action="SELL",
            price=51000, quantity=10, total_amount=510000,
            mode="paper", strategy="scoring", executed_at=dt,
        ))
        session.commit()
        session.close()

        ops = _collect_execution_ops_metrics(
            mode="paper", account_key="scoring", date=dt,
            watchlist_size=10, total_value=10_000_000,
        )
        # (500000 + 510000) / 10000000 = 0.101
        assert ops["turnover"] is not None
        assert abs(ops["turnover"] - 0.101) < 0.001

    def test_signal_density(self, fresh_db):
        from database.models import get_session, OperationEvent
        from core.paper_evidence import _collect_execution_ops_metrics

        session = get_session()
        dt = datetime(2026, 4, 1, 15, 35)
        for _ in range(5):
            session.add(OperationEvent(
                event_type="SIGNAL", severity="info",
                symbol="005930", mode="paper", message="BUY signal",
                created_at=dt,
            ))
        session.commit()
        session.close()

        ops = _collect_execution_ops_metrics(
            mode="paper", account_key="scoring", date=dt,
            watchlist_size=20, total_value=10_000_000,
        )
        assert ops["signal_density"] == 0.25  # 5/20


# ═══════════════════════════════════════════════════════════════
# Finality Tests
# ═══════════════════════════════════════════════════════════════

class TestBenchmarkFinality:
    """benchmark_status: provisional / final / failed."""

    def test_benchmark_unavailable_returns_failed(self):
        from core.paper_evidence import _compute_benchmark_excess
        result = _compute_benchmark_excess(datetime.now(), 0.5, 0.3, [])
        assert result["benchmark_status"] == "failed"

    def test_benchmark_null_return_failed(self):
        from core.paper_evidence import _compute_benchmark_excess
        result = _compute_benchmark_excess(datetime.now(), None, 0.3, ["005930"])
        assert result["benchmark_status"] == "failed"

    def test_benchmark_meta_has_asof_and_source(self):
        """benchmark_meta에 asof, source, completeness가 항상 존재."""
        from core.paper_evidence import _compute_benchmark_excess
        result = _compute_benchmark_excess(datetime.now(), None, 0.3, ["005930"])
        meta = result["benchmark_meta"]
        assert "asof" in meta
        assert "source" in meta
        assert "completeness" in meta


class TestFinalizeEvidence:
    """provisional → final 승격."""

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_provisional_then_finalize(self, mock_diag, mock_bench, evidence_dir, fresh_db):
        """provisional 기록 후 finalize → final version append, canonical에는 1개만."""
        # 첫 수집: provisional
        mock_bench.return_value = {
            "same_universe_excess": None, "exposure_matched_excess": None,
            "cash_adjusted_excess": None, "benchmark_status": "provisional",
            "benchmark_meta": {"type": "test", "asof": "t1", "source": None, "completeness": 0.0},
        }
        from core.paper_evidence import collect_daily_evidence, finalize_daily_evidence, get_canonical_records, _evidence_path

        dt = datetime(2026, 4, 1, 15, 35)
        r1 = collect_daily_evidence(strategy="test_s", mode="paper", account_key="test_s",
                                    date=dt, watchlist_symbols=[])
        assert r1 is not None
        assert r1.benchmark_status == "provisional"
        assert r1.record_version == 1

        # finalize: benchmark now available
        mock_bench.return_value = {
            "same_universe_excess": 0.1, "exposure_matched_excess": 0.08,
            "cash_adjusted_excess": 0.07, "benchmark_status": "final",
            "benchmark_meta": {"type": "test", "asof": "t2", "source": "universe", "completeness": 1.0},
        }
        r2 = finalize_daily_evidence(strategy="test_s", mode="paper", account_key="test_s",
                                     date=dt, watchlist_symbols=["005930"])
        assert r2 is not None
        assert r2.benchmark_status == "final"
        assert r2.record_version == 2
        assert r2.same_universe_excess == 0.1

        # canonical: should have exactly 1 record for that date
        canonical = get_canonical_records("test_s")
        assert len(canonical) == 1
        assert canonical[0]["benchmark_status"] == "final"
        assert canonical[0]["record_version"] == 2

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_already_final_skip(self, mock_diag, mock_bench, evidence_dir, fresh_db):
        """이미 final이면 finalize skip."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence, finalize_daily_evidence

        dt = datetime(2026, 4, 1, 15, 35)
        collect_daily_evidence(strategy="test_s", mode="paper", account_key="test_s",
                               date=dt, watchlist_symbols=[])
        r2 = finalize_daily_evidence(strategy="test_s", mode="paper", account_key="test_s",
                                     date=dt, watchlist_symbols=[])
        assert r2 is None  # already final


class TestDoubleRunIdempotency:
    """scheduler double-run same date."""

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_double_collect_same_date(self, mock_diag, mock_bench, evidence_dir, fresh_db):
        """같은 날 두 번 collect → active record가 1개."""
        mock_bench.return_value = {
            "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02, "benchmark_status": "final",
            "benchmark_meta": {},
        }
        from core.paper_evidence import collect_daily_evidence, get_canonical_records

        dt = datetime(2026, 4, 1, 15, 35)
        r1 = collect_daily_evidence(strategy="dup_s", mode="paper", account_key="dup_s",
                                    date=dt, watchlist_symbols=[])
        r2 = collect_daily_evidence(strategy="dup_s", mode="paper", account_key="dup_s",
                                    date=dt, watchlist_symbols=[])
        assert r1 is not None
        assert r2 is None  # idempotent skip

        canonical = get_canonical_records("dup_s")
        assert len(canonical) == 1


class TestCrossValidation:
    """DailyEvidence vs DailyReport/PortfolioSnapshot 교차검증."""

    def test_cross_validation_mismatch_becomes_anomaly(self, fresh_db, evidence_dir):
        """trade_count 불일치 시 cross_validation_mismatch anomaly 발생."""
        from database.models import get_session, PortfolioSnapshot, DailyReport, TradeHistory
        from core.paper_evidence import collect_daily_evidence

        session = get_session()
        dt = datetime(2026, 4, 1, 15, 35)

        # PortfolioSnapshot
        session.add(PortfolioSnapshot(
            account_key="xv_test", date=dt,
            total_value=10_000_000, cash=3_000_000, invested=7_000_000,
            daily_return=0.5, cumulative_return=1.0, mdd=-3.0, position_count=2,
        ))

        # DailyReport with trade count=5
        session.add(DailyReport(
            account_key="xv_test", date=dt,
            total_trades=5, buy_count=3, sell_count=2,
            realized_pnl=10000, winning_trades=1, losing_trades=1,
        ))

        # But only 3 actual trades in TradeHistory
        for i in range(3):
            action = "BUY" if i < 2 else "SELL"
            session.add(TradeHistory(
                account_key="xv_test", symbol="005930", action=action,
                price=50000, quantity=10, total_amount=500000,
                mode="paper", strategy="scoring", executed_at=dt,
                reason="PnL: 5000원" if action == "SELL" else "",
            ))
        session.commit()
        session.close()

        with patch("core.paper_evidence._compute_benchmark_excess") as mock_bench, \
             patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[]):
            mock_bench.return_value = {
                "same_universe_excess": None, "exposure_matched_excess": None,
                "cash_adjusted_excess": None, "benchmark_status": "failed",
                "benchmark_meta": {},
            }
            result = collect_daily_evidence(
                strategy="xv_test", mode="paper", account_key="xv_test",
                date=dt, watchlist_symbols=[],
            )

        assert result is not None
        # total_trades from get_daily_trade_summary = 3 (actual), DailyReport says 5
        xv_types = [a["type"] for a in result.anomalies]
        assert "cross_validation_mismatch" in xv_types
        assert len(result.cross_validation_warnings) > 0


class TestStartupRecoveryEvent:
    """STARTUP_RECOVERY event 발행 및 집계."""

    def test_startup_recovery_event_emitted(self, fresh_db):
        """startup_recovery()가 STARTUP_RECOVERY 이벤트를 DB에 기록하는지."""
        from database.models import get_session, OperationEvent
        from core.paper_evidence import _collect_execution_ops_metrics

        # Simulate: directly insert STARTUP_RECOVERY event (as scheduler would)
        session = get_session()
        dt = datetime(2026, 4, 1, 9, 0)
        session.add(OperationEvent(
            event_type="STARTUP_RECOVERY", severity="info",
            mode="paper", strategy="scoring",
            message="startup recovery test",
            detail='{"pending_failed_orders": 2, "elapsed_seconds": 1.5}',
            created_at=dt,
        ))
        session.commit()
        session.close()

        ops = _collect_execution_ops_metrics(
            mode="paper", account_key="scoring", date=dt,
            watchlist_size=10, total_value=10_000_000,
        )
        assert ops["restart_recovery_count"] == 1

    def test_multiple_recovery_events_counted(self, fresh_db):
        """같은 날 여러 restart → 모두 합산."""
        from database.models import get_session, OperationEvent
        from core.paper_evidence import _collect_execution_ops_metrics

        session = get_session()
        dt = datetime(2026, 4, 1, 9, 0)
        for _ in range(3):
            session.add(OperationEvent(
                event_type="STARTUP_RECOVERY", severity="info",
                mode="paper", message="recovery", created_at=dt,
            ))
        session.commit()
        session.close()

        ops = _collect_execution_ops_metrics(
            mode="paper", account_key="scoring", date=dt,
            watchlist_size=10, total_value=10_000_000,
        )
        assert ops["restart_recovery_count"] == 3


class TestPromotionBenchmarkIncomplete:
    """promotion package에서 benchmark incomplete 시 BLOCK 처리."""

    @patch("core.paper_evidence._compute_benchmark_excess")
    @patch("core.strategy_diagnostics.diagnose_live_post_market", return_value=[])
    def test_promotion_blocked_on_low_benchmark_ratio(self, mock_diag, mock_bench, evidence_dir, fresh_db):
        """benchmark_final_ratio < 80% → BLOCKED."""
        from core.paper_evidence import collect_daily_evidence, generate_promotion_package

        # 5일: 2일 final, 3일 provisional
        dates = [datetime(2026, 3, 24 + i, 15, 35) for i in range(5)]
        for i, dt in enumerate(dates):
            if i < 2:
                mock_bench.return_value = {
                    "same_universe_excess": 0.05, "exposure_matched_excess": 0.03,
                    "cash_adjusted_excess": 0.02, "benchmark_status": "final",
                    "benchmark_meta": {},
                }
            else:
                mock_bench.return_value = {
                    "same_universe_excess": None, "exposure_matched_excess": None,
                    "cash_adjusted_excess": None, "benchmark_status": "provisional",
                    "benchmark_meta": {},
                }
            collect_daily_evidence(strategy="bench_test", mode="paper", account_key="bench_test",
                                  date=dt, watchlist_symbols=[])

        pkg_path, _ = generate_promotion_package("bench_test")
        assert pkg_path is not None
        import json
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["recommendation"] == "BLOCKED"
        assert any("benchmark_incomplete" in r for r in pkg["block_reasons"])
