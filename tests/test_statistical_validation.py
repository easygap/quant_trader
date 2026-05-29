"""Deflated/Probabilistic Sharpe 검증 (다중검정 보정)."""

import math

from backtest.statistical_validation import (
    probabilistic_sharpe_ratio,
    expected_max_sharpe,
    deflated_sharpe_ratio,
)


class TestProbabilisticSharpe:
    def test_psr_in_unit_interval(self):
        psr = probabilistic_sharpe_ratio(1.0, 252, periods_per_year=252)
        assert 0.0 <= psr <= 1.0

    def test_higher_sharpe_higher_psr(self):
        lo = probabilistic_sharpe_ratio(0.5, 252)
        hi = probabilistic_sharpe_ratio(2.0, 252)
        assert hi > lo

    def test_more_observations_more_confident(self):
        """동일 Sharpe(>0)에서 표본이 길수록 PSR(vs 0)이 커진다."""
        short = probabilistic_sharpe_ratio(1.0, 60)
        long = probabilistic_sharpe_ratio(1.0, 1000)
        assert long > short

    def test_zero_sharpe_is_half(self):
        """Sharpe=0, 정규분포면 PSR(vs 0) ≈ 0.5."""
        psr = probabilistic_sharpe_ratio(0.0, 252)
        assert abs(psr - 0.5) < 1e-6

    def test_insufficient_obs_returns_zero(self):
        assert probabilistic_sharpe_ratio(2.0, 1) == 0.0

    def test_negative_skew_reduces_psr(self):
        """음의 왜도(좌측 꼬리)는 같은 Sharpe라도 신뢰를 낮춘다."""
        base = probabilistic_sharpe_ratio(1.5, 252, skew=0.0, kurtosis=3.0)
        skewed = probabilistic_sharpe_ratio(1.5, 252, skew=-1.0, kurtosis=6.0)
        assert skewed < base


class TestExpectedMaxSharpe:
    def test_single_trial_is_zero(self):
        assert expected_max_sharpe(1, 0.25) == 0.0

    def test_no_variance_is_zero(self):
        assert expected_max_sharpe(100, 0.0) == 0.0

    def test_increases_with_trials(self):
        few = expected_max_sharpe(5, 0.25)
        many = expected_max_sharpe(500, 0.25)
        assert many > few > 0

    def test_increases_with_variance(self):
        lo = expected_max_sharpe(50, 0.1)
        hi = expected_max_sharpe(50, 0.5)
        assert hi > lo


class TestDeflatedSharpe:
    def test_single_trial_dsr_equals_psr_vs_zero(self):
        """시행 1개면 expected_max=0 → DSR == PSR(vs 0)."""
        out = deflated_sharpe_ratio(1.5, 252, n_trials=1, sharpe_variance_across_trials=0.25)
        assert out["expected_max_sharpe_annual"] == 0.0
        assert abs(out["dsr"] - out["psr_vs_zero"]) < 1e-9

    def test_many_trials_deflate_sharpe(self):
        """동일 관측 Sharpe라도 시행이 많을수록 DSR이 낮아진다(다중검정 할인)."""
        one = deflated_sharpe_ratio(1.4, 750, n_trials=1, sharpe_variance_across_trials=0.3)
        many = deflated_sharpe_ratio(1.4, 750, n_trials=50, sharpe_variance_across_trials=0.3)
        assert many["dsr"] < one["dsr"]

    def test_overfit_signature_fails(self):
        """모더릿한 Sharpe + 많은 시행 + 큰 분산 → DSR 낮음 → passes=False."""
        out = deflated_sharpe_ratio(
            0.8, 250, n_trials=100, sharpe_variance_across_trials=0.5,
        )
        assert out["dsr"] < 0.95
        assert out["passes"] is False

    def test_robust_edge_passes(self):
        """매우 높은 Sharpe + 긴 표본 + 적은 시행 → DSR 높음 → passes=True."""
        out = deflated_sharpe_ratio(
            2.5, 2000, n_trials=3, sharpe_variance_across_trials=0.05,
        )
        assert out["dsr"] >= 0.95
        assert out["passes"] is True

    def test_output_schema(self):
        out = deflated_sharpe_ratio(1.0, 252, 10, 0.2)
        for k in ("dsr", "psr_vs_zero", "expected_max_sharpe_annual",
                  "observed_sharpe_annual", "n_trials", "n_obs", "passes"):
            assert k in out
