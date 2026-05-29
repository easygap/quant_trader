"""통계적 검증 유틸리티 — 다중검정(multiple testing)으로 부풀려진 Sharpe 보정.

연구 스윕은 한 전략 패밀리에서 수십 개 변형(파라미터 조합)을 탐색한 뒤 가장 좋은
변형을 고른다. 이렇게 "최고"를 고르면 우연히 좋아 보이는 변형이 선택되어 in-sample
Sharpe가 구조적으로 과대평가된다(selection bias / multiple testing).

이 모듈은 Bailey & López de Prado(2014, "The Deflated Sharpe Ratio")의 방법으로
관측 Sharpe를 시행 횟수에 맞춰 할인(deflate)한다:

- probabilistic_sharpe_ratio: 관측 Sharpe가 기준 Sharpe(SR*)를 초과할 확률(PSR).
  표본 길이·왜도·첨도를 반영(비정규 수익률 보정).
- expected_max_sharpe: N개 시행에서 모두 진짜 Sharpe=0일 때 기대되는 "최대 Sharpe"
  (운으로 얻는 최고치). 이것이 deflated 기준선이 된다.
- deflated_sharpe_ratio: SR* = expected_max_sharpe 로 둔 PSR. 값이 낮으면(예: <0.95)
  관측 Sharpe가 다중검정 운으로 설명 가능 → 실전 엣지 신뢰 낮음.

모든 함수는 외부 상태 없이 순수하게 동작하며 연-환산 Sharpe를 입력으로 받아 내부에서
관측 주기(periods_per_year) 단위로 변환한다.
"""

from __future__ import annotations

import math

# 표준정규 CDF/역CDF. scipy가 있으면 사용, 없으면 math 기반 폴백.
try:
    from scipy.stats import norm as _norm

    def _phi(x: float) -> float:
        return float(_norm.cdf(x))

    def _phi_inv(p: float) -> float:
        return float(_norm.ppf(p))
except Exception:  # pragma: no cover - scipy는 의존성에 포함되어 있음
    def _phi(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _phi_inv(p: float) -> float:
        # Acklam의 역정규 근사 (충분히 정확).
        if p <= 0.0:
            return -math.inf
        if p >= 1.0:
            return math.inf
        a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
             3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        if p > phigh:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                    ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


_EULER_MASCHERONI = 0.5772156649015329


def _to_per_period_sharpe(sharpe_annual: float, periods_per_year: int) -> float:
    """연-환산 Sharpe를 관측 주기(예: 일별) 단위로 변환."""
    if periods_per_year <= 0:
        return float(sharpe_annual)
    return float(sharpe_annual) / math.sqrt(periods_per_year)


def probabilistic_sharpe_ratio(
    sharpe_annual: float,
    n_obs: int,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    benchmark_sharpe_annual: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """PSR: 관측 Sharpe가 benchmark Sharpe를 초과할 확률 [0,1].

    n_obs는 수익률 관측치 수(예: 일수). kurtosis는 Pearson 정의(정규=3).
    """
    if n_obs is None or n_obs < 2:
        return 0.0
    sr = _to_per_period_sharpe(sharpe_annual, periods_per_year)
    sr_star = _to_per_period_sharpe(benchmark_sharpe_annual, periods_per_year)
    # 분모: 비정규 보정항. 정규(skew=0,kurt=3)이면 1.
    denom_var = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * (sr ** 2)
    if denom_var <= 0:
        return 0.0
    z = (sr - sr_star) * math.sqrt(n_obs - 1) / math.sqrt(denom_var)
    return _phi(z)


def expected_max_sharpe(
    n_trials: int,
    sharpe_variance_across_trials: float,
    *,
    periods_per_year: int = 252,
    annualize: bool = True,
) -> float:
    """N개 독립 시행에서 진짜 Sharpe=0일 때 기대되는 최대 Sharpe.

    sharpe_variance_across_trials: 시행들의 (연-환산) Sharpe 추정치 분산.
    annualize=True면 연-환산 Sharpe로 반환.
    """
    if n_trials is None or n_trials < 1 or sharpe_variance_across_trials <= 0:
        return 0.0
    if n_trials == 1:
        return 0.0
    std = math.sqrt(sharpe_variance_across_trials)
    if annualize:
        # 분산이 연-환산 Sharpe 기준이면 std도 연-환산 단위.
        pass
    # E[max] ≈ std * [(1-γ)Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))]
    term1 = (1 - _EULER_MASCHERONI) * _phi_inv(1 - 1.0 / n_trials)
    term2 = _EULER_MASCHERONI * _phi_inv(1 - 1.0 / (n_trials * math.e))
    return std * (term1 + term2)


def deflated_sharpe_ratio(
    sharpe_annual: float,
    n_obs: int,
    n_trials: int,
    sharpe_variance_across_trials: float,
    *,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    periods_per_year: int = 252,
) -> dict:
    """Deflated Sharpe Ratio (DSR).

    SR* = expected_max_sharpe(n_trials, var) 로 둔 PSR.
    반환: {dsr, psr_vs_zero, expected_max_sharpe_annual, n_trials, n_obs, ...}.
    dsr가 낮을수록(예: <0.95) 관측 Sharpe가 다중검정 운으로 설명 가능.
    """
    emax = expected_max_sharpe(
        n_trials, sharpe_variance_across_trials, periods_per_year=periods_per_year,
    )
    dsr = probabilistic_sharpe_ratio(
        sharpe_annual, n_obs, skew=skew, kurtosis=kurtosis,
        benchmark_sharpe_annual=emax, periods_per_year=periods_per_year,
    )
    psr0 = probabilistic_sharpe_ratio(
        sharpe_annual, n_obs, skew=skew, kurtosis=kurtosis,
        benchmark_sharpe_annual=0.0, periods_per_year=periods_per_year,
    )
    return {
        "dsr": round(dsr, 4),
        "psr_vs_zero": round(psr0, 4),
        "expected_max_sharpe_annual": round(emax, 4),
        "observed_sharpe_annual": round(float(sharpe_annual), 4),
        "n_trials": int(n_trials),
        "n_obs": int(n_obs),
        "passes": bool(dsr >= 0.95),
    }
