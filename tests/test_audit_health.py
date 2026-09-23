"""헬스 배치율 감시 회귀 테스트 (2026-09-23 점검).

8/26에 '복원'한 배치율 5%p 감시가 실제로는 울릴 수 없었다. 허용 하한을 보유 슬롯별
1주 가격의 합으로 잡아서 9종목 바스켓의 허용이 약 20%p였기 때문이다 — 8/07~8/26
현금 래칫(61% → 54.9%) 상태를 그대로 넣어도 OK가 나왔다.
"""

import pytest

from core.operator_health import (
    structural_deployment_tolerance,
    summarize_deployment,
    unfixable_deployment_gap,
)
from core.risk_overlays import applied_stock_fraction, invested_fraction

# 2026-09-23 kr_diversified_hold 실측 평균단가(8종목 보유)와 총자산
HOLD_PRICES = [395_500, 362_000, 291_667, 229_500, 37_262, 302_167, 99_900, 156_000]
HOLD_TOTAL = 9_240_010


def test_cash_ratchet_state_is_now_attention():
    unit = unfixable_deployment_gap(HOLD_PRICES, HOLD_TOTAL, min_trade=200_000, band=0.03)
    tol = structural_deployment_tolerance(unit, HOLD_TOTAL, 0.05)
    assert tol == pytest.approx(0.05, abs=0.01)   # 설정값(5%p) 근처로 돌아와야 한다
    assert summarize_deployment(0.549, 0.60, tolerance=tol)["verdict"] == "ATTENTION"


def test_old_sum_of_slots_rule_would_have_hidden_it():
    """예전 호출부가 넘기던 값(슬롯별 1주 가격 합)으로는 같은 상태가 OK였다."""
    old_tol = structural_deployment_tolerance(sum(HOLD_PRICES), HOLD_TOTAL, 0.05)
    assert old_tol > 0.2
    assert summarize_deployment(0.549, 0.60, tolerance=old_tol)["verdict"] == "OK"


def test_cheapest_executable_lot_respects_min_trade():
    # 3.7만원짜리는 min_trade 20만원을 넘기려면 6주(22.4만원)를 사야 한다.
    # 보충은 묶음이 남은 격차의 2배보다 작을 때만 사므로 못 메우는 격차는 그 절반.
    unit = unfixable_deployment_gap([37_262], 1_000_000, min_trade=200_000, band=0.0)
    assert unit == pytest.approx(6 * 37_262 / 2)


def test_gap_smaller_than_half_a_share_is_tolerated():
    # 잔고 40만, 지수 12.8만 단일 슬롯: 부족분 6만(15%p) < 1주의 절반(6.4만)이면
    # 1주를 사면 오히려 격차가 커진다 — 매수 보류가 맞고 경보도 없어야 한다.
    unit = unfixable_deployment_gap([128_000], 400_000, min_trade=50_000, band=0.03)
    tol = structural_deployment_tolerance(unit, 400_000, 0.10)
    assert tol == pytest.approx(0.16)
    assert summarize_deployment(0.35, 0.50, tolerance=tol)["verdict"] == "OK"


def test_gap_a_share_could_close_is_attention():
    # 같은 계좌에서 부족분 20%p(8만)는 1주(12.8만)로 줄일 수 있다 — 남아 있으면 이상
    unit = unfixable_deployment_gap([128_000], 400_000, min_trade=50_000, band=0.03)
    tol = structural_deployment_tolerance(unit, 400_000, 0.10)
    assert summarize_deployment(0.30, 0.50, tolerance=tol)["verdict"] == "ATTENTION"


def test_gap_inputs_missing_fall_back_to_configured_floor():
    assert unfixable_deployment_gap([], 1_000_000, min_trade=1, band=0.03) == 0.0
    assert unfixable_deployment_gap([100_000], 0, min_trade=1, band=0.03) == 0.0
    assert unfixable_deployment_gap(["x", None, -5], 1_000_000, min_trade=1) == 0.0
    assert structural_deployment_tolerance(0.0, 1_000_000, 0.05) == pytest.approx(0.05)


# ---------------------------------------------------------- 오버레이 적용 목표

POCKET = {"069500": 0.5, "357870": 0.5}


def test_invested_fraction_counts_defensive_symbol_as_invested():
    """주식을 절반으로 줄여도 줄인 만큼 CD ETF를 사므로 투자 비중은 95% 그대로."""
    state = {"scale": 0.5}
    assert invested_fraction(POCKET, 0.95, state, "357870") == pytest.approx(0.95)
    # 예전 헬스 기준(설계 × 배수)은 절반으로 내려가 CD ETF 매수 실패를 못 봤다
    assert applied_stock_fraction(0.95, state) == pytest.approx(0.475)


def test_invested_fraction_without_defensive_symbol_scales_down():
    assert invested_fraction({"a": 1, "b": 1}, 0.6, {"scale": 0.5}) == pytest.approx(0.3)
    assert invested_fraction({"a": 1}, 0.6, None) == pytest.approx(0.6)


def test_invested_fraction_ignores_bad_state():
    assert invested_fraction(POCKET, 0.95, {"scale": "x"}, "357870") == pytest.approx(0.95)
