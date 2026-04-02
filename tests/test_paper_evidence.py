"""
Paper Evidence 수집 체계 테스트

검증:
- DailyEvidence 생성/저장/로드
- Anomaly 탐지 규칙
- Approval gate 판정
- 승격 패키지 생성
- kill-switch 조건
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pathlib import Path
from core.paper_evidence import (
    DailyEvidence, AnomalyRecord,
    check_anomalies, check_approval,
    save_daily_evidence, save_anomalies,
    load_all_evidence, load_anomalies,
    generate_promotion_package,
    APPROVAL_RULES, ANOMALY_RULES,
)


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── 1. DailyEvidence ──

class TestDailyEvidence:
    def test_create_and_serialize(self):
        e = DailyEvidence(
            date="2026-04-02", strategy="rotation", day_number=2,
            absolute_return=0.82, cumulative_return=0.82,
            portfolio_value=10_082_023,
        )
        d = e.to_dict()
        assert d["date"] == "2026-04-02"
        assert d["strategy"] == "rotation"
        assert d["absolute_return"] == 0.82

    def test_save_and_load(self, tmp_dir):
        e = DailyEvidence(date="2026-04-01", strategy="rotation", day_number=1,
                          absolute_return=0.5, cumulative_return=0.5)
        save_daily_evidence(e, tmp_dir)

        records = load_all_evidence("rotation", tmp_dir)
        assert len(records) == 1
        assert records[0]["date"] == "2026-04-01"

    def test_append_multiple_days(self, tmp_dir):
        for i in range(5):
            e = DailyEvidence(date=f"2026-04-0{i+1}", strategy="scoring",
                              day_number=i+1, cumulative_return=i * 0.3)
            save_daily_evidence(e, tmp_dir)

        records = load_all_evidence("scoring", tmp_dir)
        assert len(records) == 5
        assert records[4]["day_number"] == 5


# ── 2. Anomaly Detection ──

class TestAnomalyDetection:
    def test_no_anomaly_normal(self):
        e = DailyEvidence(date="2026-04-02", strategy="rotation", day_number=2)
        anomalies = check_anomalies(e)
        assert len(anomalies) == 0

    def test_phantom_position_critical(self):
        e = DailyEvidence(date="2026-04-02", strategy="rotation", day_number=2,
                          phantom_position_count=1)
        anomalies = check_anomalies(e)
        assert any(a.rule == "phantom_position" for a in anomalies)
        assert any(a.severity == "critical" for a in anomalies)

    def test_deep_drawdown_critical(self):
        e = DailyEvidence(date="2026-04-02", strategy="rotation", day_number=2,
                          drawdown=-18.0)
        anomalies = check_anomalies(e)
        assert any(a.rule == "deep_drawdown" for a in anomalies)

    def test_duplicate_flood_warning(self):
        e = DailyEvidence(date="2026-04-02", strategy="rotation", day_number=2,
                          duplicate_blocked_count=15)
        anomalies = check_anomalies(e)
        assert any(a.rule == "duplicate_flood" for a in anomalies)
        assert all(a.severity == "warning" for a in anomalies if a.rule == "duplicate_flood")

    def test_save_and_load_anomalies(self, tmp_dir):
        anomalies = [AnomalyRecord(
            timestamp="2026-04-02T10:00:00",
            strategy="rotation", rule="phantom_position",
            severity="critical", detail="test",
        )]
        save_anomalies(anomalies, tmp_dir)
        loaded = load_anomalies(tmp_dir)
        assert len(loaded) == 1
        assert loaded[0]["rule"] == "phantom_position"


# ── 3. Approval Gate ──

class TestApprovalGate:
    def test_all_pass(self):
        metrics = {
            "paper_days": 65,
            "phantom_positions": 0,
            "stale_pendings_total": 2,
            "cumulative_return": 5.0,
            "profit_factor": 1.5,
            "max_drawdown": -8.0,
            "paper_sharpe": 0.5,
            "same_universe_excess": 1.0,
            "anomaly_count": 0,
        }
        gates = check_approval(metrics)
        assert all(g.passed for g in gates), \
            f"미통과 게이트: {[g.name for g in gates if not g.passed]}"

    def test_insufficient_days(self):
        metrics = {
            "paper_days": 30,
            "phantom_positions": 0,
            "stale_pendings_total": 0,
            "cumulative_return": 5.0,
            "profit_factor": 1.5,
            "max_drawdown": -8.0,
            "paper_sharpe": 0.5,
            "same_universe_excess": 1.0,
            "anomaly_count": 0,
        }
        gates = check_approval(metrics)
        days_gate = [g for g in gates if g.name == "paper_days"][0]
        assert not days_gate.passed

    def test_phantom_blocks(self):
        metrics = {"paper_days": 60, "phantom_positions": 3, "cumulative_return": 5}
        gates = check_approval(metrics)
        phantom_gate = [g for g in gates if g.name == "phantom_positions"][0]
        assert not phantom_gate.passed

    def test_negative_return_blocks(self):
        metrics = {"paper_days": 60, "cumulative_return": -2.0}
        gates = check_approval(metrics)
        ret_gate = [g for g in gates if g.name == "cumulative_return"][0]
        assert not ret_gate.passed

    def test_missing_data_fails(self):
        """데이터 없으면 모든 게이트 실패."""
        gates = check_approval({})
        assert all(not g.passed for g in gates)


# ── 4. Promotion Package ──

class TestPromotionPackage:
    def test_generate_with_evidence(self, tmp_dir):
        for i in range(5):
            e = DailyEvidence(
                date=f"2026-04-0{i+1}", strategy="rotation", day_number=i+1,
                cumulative_return=i * 0.5, drawdown=-2.0,
                phantom_position_count=0, stale_pending_count=0,
            )
            save_daily_evidence(e, tmp_dir)

        pkg = generate_promotion_package("rotation", tmp_dir)
        assert pkg["paper_days"] == 5
        assert "approval_gates" in pkg
        assert pkg["all_gates_passed"] is False  # 5일 < 60일

    def test_generate_without_evidence(self, tmp_dir):
        pkg = generate_promotion_package("nonexistent", tmp_dir)
        assert "error" in pkg

    def test_critical_anomalies_block_promotion(self, tmp_dir):
        for i in range(60):
            e = DailyEvidence(
                date=f"2026-{4+i//30:02d}-{i%30+1:02d}", strategy="rotation",
                day_number=i+1, cumulative_return=5.0, drawdown=-3.0,
            )
            save_daily_evidence(e, tmp_dir)

        # 4건 critical anomaly 추가
        for _ in range(4):
            save_anomalies([AnomalyRecord(
                timestamp="2026-05-01", strategy="rotation",
                rule="phantom_position", severity="critical", detail="test",
            )], tmp_dir)

        pkg = generate_promotion_package("rotation", tmp_dir)
        anomaly_gate = [g for g in pkg["approval_gates"] if g["name"] == "anomaly_count"][0]
        assert not anomaly_gate["passed"]  # 4 > 3


# ── 5. Evidence Schema 완전성 ──

class TestEvidenceSchema:
    def test_all_fields_present(self):
        e = DailyEvidence(date="2026-04-02", strategy="test", day_number=1)
        d = e.to_dict()
        required = [
            "date", "strategy", "day_number",
            "absolute_return", "cumulative_return",
            "same_universe_excess", "exposure_matched_excess", "cash_adjusted_excess",
            "turnover", "signal_density", "raw_fill_rate", "effective_fill_rate",
            "drawdown", "slippage_vs_model",
            "reconcile_count", "stale_pending_count", "phantom_position_count",
            "restart_recovery_count", "duplicate_blocked_count", "reject_count",
        ]
        for field in required:
            assert field in d, f"필수 필드 누락: {field}"

    def test_approval_rules_complete(self):
        """모든 승격 규칙이 정의되어 있어야 함."""
        rule_names = [r[0] for r in APPROVAL_RULES]
        assert "paper_days" in rule_names
        assert "phantom_positions" in rule_names
        assert "same_universe_excess" in rule_names
        assert "paper_sharpe" in rule_names

    def test_anomaly_rules_complete(self):
        """모든 anomaly 규칙이 정의되어 있어야 함."""
        rule_names = [r[0] for r in ANOMALY_RULES]
        assert "phantom_position" in rule_names
        assert "deep_drawdown" in rule_names
        assert "repeated_reject" in rule_names
