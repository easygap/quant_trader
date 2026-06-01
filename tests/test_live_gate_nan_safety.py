"""live gate / promotion 엔진의 NaN·Inf fail-closed 회귀 테스트.

NaN은 모든 임계값 비교(nan <= 0, nan < 1.1 등)를 False로 만들어 게이트를 조용히
통과시킨다. JSON은 NaN/Infinity를 그대로 직렬화/역직렬화하므로, artifact에 들어간
NaN 지표가 라이브 게이트를 뚫을 수 있었다. _as_float가 비유한값을 None으로 처리해
fail-closed가 되는지 검증한다.
"""
import math

import core.live_gate as live_gate
import core.promotion_engine as promotion_engine
from core.promotion_engine import StrategyMetrics, promote


def test_live_gate_as_float_rejects_non_finite():
    f = live_gate._as_float
    assert f(float("nan")) is None
    assert f(float("inf")) is None
    assert f(float("-inf")) is None
    # 정상 값은 그대로
    assert f(1.5) == 1.5
    assert f("2.0") == 2.0
    assert f(None) is None
    assert f("not a number") is None


def test_promotion_as_float_rejects_non_finite():
    f = promotion_engine._as_float
    assert f(float("nan")) is None
    assert f(float("inf")) is None
    assert f(float("-inf")) is None
    assert f(0.0) == 0.0
    assert f(3) == 3.0


def test_json_roundtrip_preserves_nan_then_rejected():
    """JSON이 NaN을 그대로 보존함을 확인하고, _as_float가 그걸 막는지 본다."""
    import json
    blob = json.dumps({"sharpe": float("nan"), "inf": float("inf")})
    parsed = json.loads(blob)
    assert math.isnan(parsed["sharpe"])  # 라운드트립으로 NaN 보존됨(위험의 근거)
    # 게이트 입력 정규화는 이를 None으로 막아야 한다
    assert live_gate._as_float(parsed["sharpe"]) is None
    assert live_gate._as_float(parsed["inf"]) is None


def test_promote_does_not_pass_with_nan_benchmark_excess():
    """benchmark excess가 NaN이면 live_candidate로 승격되면 안 된다(fail-closed)."""
    # 다른 지표는 충분히 좋게 두고 excess만 NaN으로 — NaN이 게이트를 뚫는지 확인.
    m = StrategyMetrics(
        name="nan_test",
        total_return=150.0,
        profit_factor=2.5,
        mdd=-15.0,
        wf_positive_rate=1.0,
        wf_sharpe_positive_rate=1.0,
        wf_windows=6,
        wf_total_trades=80,
        sharpe=1.5,
        canonical_benchmark_required=True,
        benchmark_excess_return=float("nan"),
        benchmark_excess_sharpe=float("nan"),
    )
    result = promote(m)
    # NaN excess는 통과 신호가 아니어야 한다 → live_candidate/provisional 아님
    assert result.status not in ("live_candidate", "provisional_paper_candidate")

    # 대조군: 동일 지표에 양(+)의 excess면 최소한 provisional은 통과해야 한다
    m_ok = StrategyMetrics(
        name="ok_test",
        total_return=150.0, profit_factor=2.5, mdd=-15.0,
        wf_positive_rate=1.0, wf_sharpe_positive_rate=1.0, wf_windows=6,
        wf_total_trades=80, sharpe=1.5, canonical_benchmark_required=True,
        benchmark_excess_return=5.0, benchmark_excess_sharpe=0.3,
    )
    assert promote(m_ok).status in ("provisional_paper_candidate", "live_candidate")
