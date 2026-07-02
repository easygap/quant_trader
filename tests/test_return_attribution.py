"""성과 귀속(실행 격차/구성 격차) 분해 — 순수 함수 단위 테스트.

decompose_return_gap / compute_design_portfolio_return (fetch 주입)로 네트워크 없이 검증.
"""
from core.basket_evaluation import (
    compute_design_portfolio_return,
    decompose_return_gap,
)


class TestDecomposeReturnGap:
    def test_month1_numbers(self):
        # 리뷰 §2 실측: NAV -5.47, 설계 -2.97, KS11 +0.71
        out = decompose_return_gap(-5.47, -2.97, 0.71)
        assert round(out["execution_gap_pct"], 2) == -2.50   # 실제 - 설계
        assert round(out["composition_gap_pct"], 2) == -3.68  # 설계 - 벤치
        assert round(out["total_gap_pct"], 2) == -6.18        # 실제 - 벤치

    def test_execution_plus_composition_equals_total(self):
        out = decompose_return_gap(-5.0, -3.0, 1.0)
        assert round(out["execution_gap_pct"] + out["composition_gap_pct"], 6) == round(out["total_gap_pct"], 6)

    def test_none_inputs_propagate(self):
        out = decompose_return_gap(-5.0, None, 1.0)
        assert out["execution_gap_pct"] is None
        assert out["composition_gap_pct"] is None
        assert out["total_gap_pct"] == -6.0  # nav-bench 는 계산 가능


class TestComputeDesignPortfolioReturn:
    def _fetch(self, table):
        def f(start, end, symbol=None):
            return table.get(symbol)
        return f

    def test_weighted_average_times_stock_fraction(self):
        holdings = {"A": 0.5, "B": 0.5}
        # stock_leg = 0.5*10 + 0.5*0 = 5 ; design = 5 * 0.8 = 4.0
        out = compute_design_portfolio_return(
            holdings, 0.8, "2026-06-10", "2026-07-02",
            fetch=self._fetch({"A": 10.0, "B": 0.0}),
        )
        assert round(out, 4) == 4.0

    def test_cash_leg_earns_zero(self):
        holdings = {"A": 1.0}
        # stock_leg = 10 ; design = 10 * 0.5 = 5.0 (나머지 50% 현금 0%)
        out = compute_design_portfolio_return(
            holdings, 0.5, "s", "e", fetch=self._fetch({"A": 10.0}),
        )
        assert round(out, 4) == 5.0

    def test_failed_fetch_renormalizes(self):
        holdings = {"A": 0.5, "B": 0.5}
        # B 조회 실패 → A만 사용, 재정규화: stock_leg = 10 ; design = 10*0.8 = 8.0
        out = compute_design_portfolio_return(
            holdings, 0.8, "s", "e", fetch=self._fetch({"A": 10.0, "B": None}),
        )
        assert round(out, 4) == 8.0

    def test_all_fetch_fail_returns_none(self):
        out = compute_design_portfolio_return(
            {"A": 1.0}, 0.8, "s", "e", fetch=self._fetch({"A": None}),
        )
        assert out is None

    def test_empty_holdings_none(self):
        assert compute_design_portfolio_return({}, 0.8, "s", "e", fetch=self._fetch({})) is None

    def test_unnormalized_weights_handled(self):
        # 가중치 합이 1이 아니어도 정규화(합 2.0)
        holdings = {"A": 1.0, "B": 1.0}
        out = compute_design_portfolio_return(
            holdings, 1.0, "s", "e", fetch=self._fetch({"A": 4.0, "B": 8.0}),
        )
        assert round(out, 4) == 6.0  # (0.5*4 + 0.5*8) * 1.0
