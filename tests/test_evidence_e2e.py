"""
Paper Evidence v2 end-to-end tests.

The old v1 helper API was retired. These tests exercise the current
append-only JSONL -> canonical records -> weekly summary/promotion/quality
report contract directly.
"""

import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _isolate_evidence_dir(monkeypatch, tmp_path):
    import core.paper_evidence as pe

    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    return tmp_path / "paper_evidence"


@pytest.fixture
def evidence_dir(_isolate_evidence_dir):
    return _isolate_evidence_dir


def _append_record(
    evidence_dir,
    strategy,
    day,
    day_number,
    *,
    daily_return=0.2,
    cumulative_return=1.0,
    total_value=10_100_000,
    mdd=-1.0,
    anomalies=None,
    status="normal",
    stale_pending_count=0,
):
    from core.paper_evidence import _append_jsonl

    anomalies = anomalies or []
    jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
    _append_jsonl(
        jsonl_path,
        {
            "date": day.strftime("%Y-%m-%d"),
            "day_number": day_number,
            "strategy": strategy,
            "total_value": total_value,
            "cash": 4_000_000,
            "invested": max(total_value - 4_000_000, 0),
            "daily_return": daily_return,
            "cumulative_return": cumulative_return,
            "mdd": mdd,
            "position_count": 1,
            "total_trades": 2,
            "buy_count": 1,
            "sell_count": 1,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "winning_trades": 1,
            "losing_trades": 0,
            "same_universe_excess": 0.05,
            "exposure_matched_excess": 0.03,
            "cash_adjusted_excess": 0.02,
            "benchmark_status": "final",
            "benchmark_meta": {"completeness": 1.0},
            "raw_fill_rate": 1.0,
            "effective_fill_rate": 1.0,
            "turnover": 12.0,
            "signal_density": 1.0,
            "reconcile_count": 0,
            "stale_pending_count": stale_pending_count,
            "phantom_position_count": 0,
            "restart_recovery_count": 0,
            "duplicate_blocked_count": 0,
            "reject_count": 0,
            "diagnostics": [],
            "cross_validation_warnings": [],
            "anomalies": anomalies,
            "status": status,
            "record_version": 1,
            "schema_version": 2,
            "evidence_mode": "real_paper",
            "execution_backed": True,
            "order_submit_count": 2,
            "fill_count": 2,
            "session_mode": "normal_paper",
            "pilot_authorized": False,
            "pilot_caps_snapshot": {},
        },
    )


class TestEvidenceE2E:
    def test_5day_replay_generates_v2_reports(self, evidence_dir):
        from core.paper_evidence import (
            generate_evidence_quality_report,
            generate_promotion_package,
            generate_weekly_summary,
            get_canonical_records,
        )

        strategy = "rotation_test"
        start = datetime(2026, 4, 1)
        days = [start + timedelta(days=i) for i in [0, 1, 2, 3, 6]]

        _append_record(evidence_dir, strategy, days[0], 1, cumulative_return=0.82)
        _append_record(
            evidence_dir,
            strategy,
            days[1],
            2,
            daily_return=0.0,
            cumulative_return=0.82,
            stale_pending_count=3,
            anomalies=[{
                "type": "stale_pending",
                "severity": "warning",
                "detail": "stale_pending_count=3",
            }],
            status="degraded",
        )
        _append_record(evidence_dir, strategy, days[2], 3, cumulative_return=2.32)
        _append_record(
            evidence_dir,
            strategy,
            days[3],
            4,
            daily_return=-18.0,
            cumulative_return=-15.68,
            total_value=8_432_000,
            mdd=-16.0,
            anomalies=[{
                "type": "deep_drawdown",
                "severity": "critical",
                "detail": "mdd=-16.0, daily_return=-18.0",
            }],
            status="frozen",
        )
        _append_record(
            evidence_dir,
            strategy,
            days[4],
            5,
            daily_return=3.0,
            cumulative_return=-12.68,
            total_value=8_732_000,
            mdd=-13.0,
        )

        records = get_canonical_records(strategy)
        assert [r["date"] for r in records] == [
            "2026-04-01",
            "2026-04-02",
            "2026-04-03",
            "2026-04-04",
            "2026-04-07",
        ]

        weekly = generate_weekly_summary(strategy, week_end_date="2026-04-07")
        assert weekly is not None
        weekly_text = weekly.read_text(encoding="utf-8")
        assert "Paper Evidence Weekly Summary" in weekly_text
        assert "stale_pending" in weekly_text
        assert "deep_drawdown" in weekly_text

        pkg_path, checklist_path = generate_promotion_package(strategy)
        assert pkg_path is not None
        assert checklist_path is not None
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        assert pkg["total_days"] == 5
        assert pkg["recommendation"] == "BLOCKED"
        assert "insufficient_days=5/60" in pkg["block_reasons"]
        assert "frozen_days=1" in pkg["block_reasons"]

        report, report_path = generate_evidence_quality_report(strategy)
        assert report_path is not None
        assert report["total_days"] == 5
        assert report["anomaly_type_breakdown"]["stale_pending"] == 1
        assert report["anomaly_type_breakdown"]["deep_drawdown"] == 1

    def test_same_day_canonical_keeps_latest_final_record(self, evidence_dir):
        from core.paper_evidence import _append_jsonl, get_canonical_records

        strategy = "idem_test"
        jsonl_path = evidence_dir / f"daily_evidence_{strategy}.jsonl"
        _append_jsonl(jsonl_path, {
            "date": "2026-04-01",
            "strategy": strategy,
            "record_version": 1,
            "benchmark_status": "provisional",
            "schema_version": 2,
        })
        _append_jsonl(jsonl_path, {
            "date": "2026-04-01",
            "strategy": strategy,
            "record_version": 2,
            "benchmark_status": "final",
            "schema_version": 2,
        })

        records = get_canonical_records(strategy)
        assert len(records) == 1
        assert records[0]["record_version"] == 2
        assert records[0]["benchmark_status"] == "final"

    def test_promotion_files_are_written(self, evidence_dir):
        from core.paper_evidence import generate_promotion_package

        strategy = "file_test"
        start = datetime(2026, 4, 1)
        for i in range(3):
            _append_record(
                evidence_dir,
                strategy,
                start + timedelta(days=i),
                i + 1,
                cumulative_return=float(i),
            )

        pkg_path, checklist_path = generate_promotion_package(strategy)
        assert pkg_path == evidence_dir / f"promotion_evidence_{strategy}.json"
        assert checklist_path == evidence_dir / f"approval_checklist_{strategy}.md"
        assert pkg_path.exists()
        assert checklist_path.exists()
