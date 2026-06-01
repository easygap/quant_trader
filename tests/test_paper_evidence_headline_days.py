"""promotion headline 재검증의 promotable 일수 정합성 회귀 테스트.

shadow-only fallback에서 verifier가 promotable_evidence_days를 len(records)로
부풀려 author(generate_promotion_package)의 0과 어긋나던 문제를 막는다.
fail-closed라 손실 전략을 승격시키진 않지만, 두 경로 값이 일치해야 한다.
"""
from core.paper_evidence import _promotion_headline_summary


def _shadow_rec(date):
    # 승격 불가(shadow) 기록: execution_backed가 아니거나 provenance 없음.
    return {"date": date, "daily_return": 0.1, "execution_backed": False,
            "evidence_mode": "shadow_bootstrap", "benchmark_status": "final"}


def _real_rec(date):
    return {"date": date, "daily_return": 0.1, "execution_backed": True,
            "evidence_mode": "real_paper", "benchmark_status": "final"}


def test_headline_promotable_days_zero_for_shadow_only():
    """records가 전부 shadow면 promotable_evidence_days/real_paper_days=0."""
    shadow = [_shadow_rec(f"2026-04-{i:02d}") for i in range(1, 6)]
    # shadow-only fallback 상황: records=shadow, execution_records=[]
    summary = _promotion_headline_summary(shadow, shadow, [])
    assert summary["promotable_evidence_days"] == 0
    assert summary["real_paper_days"] == 0
    # author와 동일하게 promotable 기준이라 shadow-only면 0
    assert summary["real_paper_days_total"] == 0


def test_headline_promotable_days_counts_real_paper():
    """promotable 기록이 있으면 그 개수가 그대로 잡힌다."""
    real = [_real_rec(f"2026-04-{i:02d}") for i in range(1, 4)]
    summary = _promotion_headline_summary(real, real, real)
    assert summary["promotable_evidence_days"] == 3
    assert summary["real_paper_days"] == 3
    assert summary["real_paper_days_total"] == 3


def test_headline_promotable_days_mixed():
    """real + shadow 섞이면 promotable은 real만 센다."""
    recs = [_real_rec("2026-04-01"), _real_rec("2026-04-02"), _shadow_rec("2026-04-03")]
    summary = _promotion_headline_summary(recs, recs, recs[:2])
    assert summary["promotable_evidence_days"] == 2
