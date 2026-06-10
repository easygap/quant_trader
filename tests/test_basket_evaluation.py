"""바스켓 paper 승격 판정(core.basket_evaluation) 단위 테스트.

기준 근거: docs/BASKET_PAPER_EVALUATION.md — 베타 전략이므로 합격선은
시장 대비 초과수익이 아니라 (기간·무결성·비용)이다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

from core.basket_evaluation import (
    evaluate_basket_paper_operation,
    format_evaluation_report,
)


def _eval(**kw):
    defaults = dict(
        operation_start=date(2026, 6, 10),
        today=date(2026, 9, 10),
        trading_days_total=60,
        snapshot_days=60,
        pending_failed_orders=0,
        total_costs=10_000.0,          # 10M 대비 0.1% 누적 → 연환산 0.42%
        initial_capital=10_000_000.0,
        nav_return_pct=5.0,
        benchmark_return_pct=4.8,
    )
    defaults.update(kw)
    return evaluate_basket_paper_operation(**defaults)


def test_all_criteria_met_is_pass_candidate():
    out = _eval()
    assert out["verdict"] == "PASS_CANDIDATE"
    assert out["issues"] == []
    assert out["snapshot_coverage"] == 1.0


def test_insufficient_days_is_wait_with_progress():
    out = _eval(trading_days_total=1, snapshot_days=1, total_costs=651.0)
    assert out["verdict"] == "WAIT"
    assert out["progress_days"] == 1
    assert 0 < out["progress_pct"] < 0.05
    # 기간 미충족 시 연환산 비용은 판정에 쓰지 않는다(이슈 없음)
    assert out["issues"] == []


def test_wait_still_surfaces_integrity_issues():
    """기간 전이라도 커버리지 붕괴·실패주문은 이슈로 보여준다(판정은 WAIT 유지)."""
    out = _eval(trading_days_total=10, snapshot_days=5, pending_failed_orders=2)
    assert out["verdict"] == "WAIT"
    assert any("커버리지" in i for i in out["issues"])
    assert any("실패 주문" in i for i in out["issues"])


def test_low_snapshot_coverage_fails_after_period():
    out = _eval(snapshot_days=50)  # 50/60 = 83% < 95%
    assert out["verdict"] == "FAIL_REVIEW"
    assert any("커버리지" in i for i in out["issues"])


def test_pending_failed_orders_fail_after_period():
    out = _eval(pending_failed_orders=1)
    assert out["verdict"] == "FAIL_REVIEW"


def test_excessive_cost_drag_fails_after_period():
    # 60일에 1% 누적 → 연환산 4.2% > 1%
    out = _eval(total_costs=100_000.0)
    assert out["verdict"] == "FAIL_REVIEW"
    assert any("드래그" in i for i in out["issues"])


def test_market_underperformance_is_not_a_failure():
    """베타 전략 — 시장이 빠져 NAV가 음수여도 무결성·비용이 충족이면 합격 후보."""
    out = _eval(nav_return_pct=-12.0, benchmark_return_pct=-12.3)
    assert out["verdict"] == "PASS_CANDIDATE"


def test_report_formatting_contains_verdict_and_criteria():
    out = _eval(trading_days_total=1, snapshot_days=1)
    text = format_evaluation_report(out, basket_name="kr_diversified_hold")
    assert "WAIT" in text and "kr_diversified_hold" in text
    assert "합격 기준이 아님" in text  # 베타 전략 명시
