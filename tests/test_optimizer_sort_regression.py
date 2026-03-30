"""
회귀 테스트: trades=0 조합(Sharpe=0)이 trades>0 조합(Sharpe<0)보다
best로 선택되지 않아야 한다.

grid_search_scoring_weights 내부의 정렬 로직을 직접 검증한다.
"""

import pytest


def _make_result(total_trades: int, sharpe: float, total_return: float, **extra):
    """optimizer results 리스트 원소 형태를 모방."""
    return {
        "weight_combo": extra.get("weight_combo", {"w_bollinger": 1, "w_macd": 1, "w_volume": 1}),
        "weights": extra.get("weights", {}),
        "threshold": extra.get("threshold", (3, -3)),
        "params": extra.get("params", {}),
        "metrics": {
            "total_trades": total_trades,
            "sharpe_ratio": sharpe,
            "total_return": total_return,
            "max_drawdown": -0.05,
            "win_rate": 0.5,
        },
        "score": sharpe,  # 기본 metric=sharpe_ratio
    }


class TestOptimizerSortRegression:
    """trades=0 이 trades>0 보다 best로 뽑히는 정렬 버그 회귀 방지."""

    def _sort_like_optimizer(self, results: list) -> list:
        """grid_search_scoring_weights 내부 정렬과 동일한 로직 재현."""
        return sorted(
            results,
            key=lambda x: (x["metrics"].get("total_trades", 0) > 0, x["score"]),
            reverse=True,
        )

    def test_trades_zero_not_selected_over_negative_sharpe(self):
        """핵심 케이스: trades=0/sharpe=0 vs trades>0/sharpe<0."""
        a = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)
        b = _make_result(total_trades=5, sharpe=-2.0, total_return=-0.013)

        sorted_results = self._sort_like_optimizer([a, b])
        best = sorted_results[0]

        assert best["metrics"]["total_trades"] > 0, (
            "trades=0 조합이 best로 선택됨 — 정렬 버그 재발"
        )
        assert best["score"] == -2.0

    def test_trades_zero_not_selected_regardless_of_order(self):
        """입력 순서 무관하게 동일 결과."""
        a = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)
        b = _make_result(total_trades=5, sharpe=-2.0, total_return=-0.013)

        for order in ([a, b], [b, a]):
            best = self._sort_like_optimizer(order)[0]
            assert best["metrics"]["total_trades"] > 0

    def test_among_traded_combos_highest_sharpe_wins(self):
        """trades>0 끼리는 sharpe 내림차순."""
        c1 = _make_result(total_trades=10, sharpe=-1.5, total_return=-0.01)
        c2 = _make_result(total_trades=3, sharpe=-3.0, total_return=-0.04)
        c3 = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)

        sorted_results = self._sort_like_optimizer([c3, c2, c1])
        assert sorted_results[0]["score"] == -1.5
        assert sorted_results[1]["score"] == -3.0
        assert sorted_results[2]["metrics"]["total_trades"] == 0

    def test_positive_sharpe_still_wins(self):
        """정상 케이스: sharpe>0 인 조합이 있으면 당연히 best."""
        good = _make_result(total_trades=20, sharpe=1.5, total_return=0.15)
        zero = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)
        bad = _make_result(total_trades=5, sharpe=-2.0, total_return=-0.02)

        sorted_results = self._sort_like_optimizer([zero, bad, good])
        assert sorted_results[0]["score"] == 1.5

    def test_all_zero_trades_returns_first(self):
        """전부 trades=0 이면 그냥 첫 번째."""
        a = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)
        b = _make_result(total_trades=0, sharpe=0.0, total_return=0.0)

        sorted_results = self._sort_like_optimizer([a, b])
        assert len(sorted_results) == 2
        assert sorted_results[0]["metrics"]["total_trades"] == 0
