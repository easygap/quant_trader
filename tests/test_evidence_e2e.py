"""
Paper Evidence End-to-End 테스트

5영업일 synthetic replay:
- Day 1: 정상 거래
- Day 2: stale pending 발생
- Day 3: 정상 (anomaly 없음)
- Day 4: deep drawdown (-16%)
- Day 5: 정상, weekly summary 생성

검증: DailyEvidence 누적, anomaly 기록, weekly summary, promotion package
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pathlib import Path
from datetime import date

from core.paper_evidence import (
    DailyEvidence, save_daily_evidence, load_all_evidence,
    save_anomalies, load_anomalies, check_anomalies,
    generate_promotion_package, EVIDENCE_DIR,
)
from core.evidence_collector import collect_daily_evidence


@pytest.fixture
def tmp_evidence_dir(monkeypatch):
    d = Path(tempfile.mkdtemp())
    # paper_evidence 모듈의 EVIDENCE_DIR를 임시 디렉토리로 교체
    import core.paper_evidence as pe
    monkeypatch.setattr(pe, "EVIDENCE_DIR", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestE2EReplay:
    """5영업일 synthetic replay."""

    def _make_summary(self, total_value, cash, n_positions, mdd=0):
        return {
            "total_value": total_value,
            "cash": cash,
            "position_count": n_positions,
            "realized_pnl": 0,
            "unrealized_pnl": total_value - 10_000_000 - cash,
            "total_return": (total_value / 10_000_000 - 1) * 100,
            "mdd": mdd,
        }

    def _make_trade_summary(self, buys=0, sells=0):
        return {
            "total_trades": buys + sells,
            "buy_count": buys,
            "sell_count": sells,
            "realized_pnl": 0,
            "total_commission": 0,
            "total_tax": 0,
            "winning_trades": 0,
            "losing_trades": 0,
        }

    def test_5day_replay(self, tmp_evidence_dir):
        """5영업일 evidence 누적 + anomaly + weekly + promotion."""
        strategy = "rotation_test"
        initial = 10_000_000

        # Day 1: 정상
        e1 = DailyEvidence(
            date="2026-04-01", strategy=strategy, day_number=1,
            absolute_return=0.82, cumulative_return=0.82,
            portfolio_value=10_082_000, cash=5_000_000, n_positions=1,
            drawdown=0.0,
        )
        save_daily_evidence(e1, tmp_evidence_dir)
        a1 = check_anomalies(e1)
        assert len(a1) == 0, "Day 1: 정상인데 anomaly 발생"

        # Day 2: stale pending 3건
        e2 = DailyEvidence(
            date="2026-04-02", strategy=strategy, day_number=2,
            absolute_return=0.0, cumulative_return=0.82,
            portfolio_value=10_082_000, cash=5_000_000, n_positions=1,
            stale_pending_count=3, drawdown=-0.01,
        )
        save_daily_evidence(e2, tmp_evidence_dir)
        a2 = check_anomalies(e2)
        assert len(a2) == 1
        assert a2[0].rule == "stale_pending"
        save_anomalies(a2, tmp_evidence_dir)

        # Day 3: 정상
        e3 = DailyEvidence(
            date="2026-04-03", strategy=strategy, day_number=3,
            absolute_return=1.5, cumulative_return=2.32,
            portfolio_value=10_232_000, cash=5_000_000, n_positions=1,
            drawdown=-0.01,
        )
        save_daily_evidence(e3, tmp_evidence_dir)

        # Day 4: deep drawdown
        e4 = DailyEvidence(
            date="2026-04-04", strategy=strategy, day_number=4,
            absolute_return=-18.0, cumulative_return=-15.68,
            portfolio_value=8_432_000, cash=5_000_000, n_positions=1,
            drawdown=-16.0,
        )
        save_daily_evidence(e4, tmp_evidence_dir)
        a4 = check_anomalies(e4)
        assert any(a.rule == "deep_drawdown" for a in a4), "Day 4: deep drawdown 미감지"
        assert any(a.severity == "critical" for a in a4)
        save_anomalies(a4, tmp_evidence_dir)

        # Day 5: 회복
        e5 = DailyEvidence(
            date="2026-04-07", strategy=strategy, day_number=5,
            absolute_return=3.0, cumulative_return=-12.68,
            portfolio_value=8_732_000, cash=5_000_000, n_positions=1,
            drawdown=-13.0,
        )
        save_daily_evidence(e5, tmp_evidence_dir)

        # 검증: 5일 누적
        all_ev = load_all_evidence(strategy, tmp_evidence_dir)
        assert len(all_ev) == 5, f"5일 누적이어야 하나 {len(all_ev)}건"

        # 검증: anomaly 누적
        all_anomalies = load_anomalies(tmp_evidence_dir)
        assert len(all_anomalies) >= 2, f"anomaly 2건 이상이어야 하나 {len(all_anomalies)}건"

        # 검증: promotion package
        pkg = generate_promotion_package(strategy, tmp_evidence_dir)
        assert pkg["paper_days"] == 5
        assert not pkg["all_gates_passed"]  # 5일 < 60일
        assert pkg["critical_anomalies"] >= 1  # deep drawdown

        # 검증: 미통과 게이트
        failed_gates = [g["name"] for g in pkg["approval_gates"] if not g["passed"]]
        assert "paper_days" in failed_gates

    def test_idempotent_same_day(self, tmp_evidence_dir):
        """같은 날짜 중복 기록 방지."""
        strategy = "idem_test"
        e = DailyEvidence(date="2026-04-01", strategy=strategy, day_number=1,
                          cumulative_return=0.5, portfolio_value=10_050_000)
        save_daily_evidence(e, tmp_evidence_dir)
        save_daily_evidence(e, tmp_evidence_dir)  # 중복

        records = load_all_evidence(strategy, tmp_evidence_dir)
        # JSONL append이므로 2건 저장됨 — collector에서 날짜 체크로 방지
        # 여기서는 save_daily_evidence 자체가 append-only이므로 2건
        # collect_daily_evidence가 날짜 중복을 체크함
        assert len(records) == 2  # raw append는 2건

    def test_collect_daily_evidence_dedup(self, tmp_evidence_dir, monkeypatch):
        """collect_daily_evidence가 같은 날짜 중복을 방지."""
        import core.evidence_collector as ec
        import core.paper_evidence as pe
        monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_evidence_dir)
        # evidence_collector도 같은 EVIDENCE_DIR을 사용하도록 패치
        monkeypatch.setattr(ec, "EVIDENCE_DIR", tmp_evidence_dir)
        # load_all_evidence의 기본 경로도 패치
        original_load = pe.load_all_evidence
        def patched_load(strategy, base_dir=None):
            return original_load(strategy, base_dir or tmp_evidence_dir)
        monkeypatch.setattr(pe, "load_all_evidence", patched_load)

        summary = {"total_value": 10_050_000, "cash": 5_000_000,
                    "position_count": 1, "mdd": 0, "total_return": 0.5}
        trades = {"total_trades": 1, "buy_count": 1, "sell_count": 0}

        # 첫 번째: 수동으로 오늘 날짜 레코드 추가
        e = DailyEvidence(
            date=date.today().isoformat(), strategy="dedup_test", day_number=1,
            cumulative_return=0.5, portfolio_value=10_050_000,
        )
        save_daily_evidence(e, tmp_evidence_dir)

        # 두 번째: collect_daily_evidence는 load_all_evidence로 중복 체크
        result = collect_daily_evidence(
            strategy="dedup_test", portfolio_summary=summary,
            trade_summary=trades,
        )
        assert result is None, "중복 날짜에서 None이 아닌 값 반환"

    def test_promotion_file_generated(self, tmp_evidence_dir):
        """promotion package가 JSON 파일로 저장."""
        strategy = "file_test"
        for i in range(3):
            e = DailyEvidence(date=f"2026-04-0{i+1}", strategy=strategy,
                              day_number=i+1, cumulative_return=i*0.5,
                              drawdown=-2.0)
            save_daily_evidence(e, tmp_evidence_dir)

        pkg = generate_promotion_package(strategy, tmp_evidence_dir)
        path = tmp_evidence_dir / f"promotion_evidence_{strategy}.json"
        assert path.exists(), "promotion package 파일 미생성"

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["paper_days"] == 3
        assert "approval_gates" in loaded
