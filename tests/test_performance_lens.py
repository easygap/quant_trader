"""국면 분해 · 리스크 지표 렌즈 테스트.

배경(2026-08-26): 수익률을 한 숫자로 보고하는 동안 '방어의 대가'가 통째로 숨어 있었다.
전체 구간 +7.8%p 초과성과와 반등 구간 -9.0%p 미스가 같은 포트폴리오의 같은 3개월이다.
이 모듈은 그 둘을 나란히 보이게 하는 도구이므로, 여기 산식이 틀리면 잘못된 안도를 준다.
"""

from datetime import date, datetime

import pytest

from core.performance_lens import (
    aligned_returns,
    daily_returns_from_nav,
    format_regime_line,
    format_risk_line,
    risk_metrics,
    split_by_regime,
)


# ------------------------------------------------------------- NAV → 수익률

def test_daily_returns_from_nav_basic():
    pts = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 110.0),
           (date(2026, 1, 3), 99.0)]
    out = daily_returns_from_nav(pts)
    assert [d for d, _ in out] == [date(2026, 1, 2), date(2026, 1, 3)]
    assert out[0][1] == pytest.approx(10.0)
    assert out[1][1] == pytest.approx(-10.0)


def test_deposits_are_neutralised():
    """입금은 수익이 아니다 — 분모에서 중화하지 않으면 가짜 수익이 잡힌다."""
    pts = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 200.0)]
    out = daily_returns_from_nav(pts, flows={date(2026, 1, 2): 100.0})
    assert out[0][1] == pytest.approx(0.0)


def test_invalid_nav_points_are_skipped():
    pts = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), None),
           (date(2026, 1, 3), 0), (date(2026, 1, 4), 110.0)]
    out = daily_returns_from_nav(pts)
    assert len(out) == 1 and out[0][1] == pytest.approx(10.0)


# ------------------------------------------------------- 기간 정렬 (핵심)

def test_aligned_returns_uses_same_span_for_benchmark():
    """스냅샷이 빠진 구간에서 벤치마크도 같은 구간으로 계산해야 한다.

    이게 회귀하면 NAV는 이틀치, 벤치는 하루치가 짝지어져 국면 분해가 통째로 왜곡된다
    (1차 구현에서 상승 국면 벤치마크가 +149%로 나와 전체 수익률과 아귀가 안 맞았다).
    """
    nav = [(date(2026, 1, 1), 100.0), (date(2026, 1, 3), 110.0)]  # 1/2 결측
    closes = {date(2026, 1, 1): 1000.0, date(2026, 1, 2): 1100.0,
              date(2026, 1, 3): 1200.0}
    out = aligned_returns(nav, closes)
    assert len(out) == 1
    _d, mine, bench = out[0]
    assert mine == pytest.approx(10.0)
    # 1/1 → 1/3 구간이므로 +20% (하루치 +9.09%가 아니다)
    assert bench == pytest.approx(20.0)


def test_aligned_returns_drops_spans_without_benchmark():
    nav = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 110.0)]
    assert aligned_returns(nav, {date(2026, 1, 1): 1000.0}) == []


def test_aligned_returns_accepts_datetime_keys():
    nav = [(datetime(2026, 1, 1), 100.0), (datetime(2026, 1, 2), 110.0)]
    closes = {date(2026, 1, 1): 1000.0, date(2026, 1, 2): 1100.0}
    assert len(aligned_returns(nav, closes)) == 1


# --------------------------------------------------------------- 국면 분해

def test_regime_split_compounds_and_reconciles():
    """상승·하락 복리를 곱하면 전체 수익률이 나와야 한다.

    단순 합산하면 변동성이 큰 구간에서 아귀가 안 맞는다(실제로 그 버그가 있었다).
    """
    pairs = [(1.0, 2.0), (-0.5, -1.0), (2.0, 3.0), (-1.0, -2.0)]
    r = split_by_regime(pairs)
    assert r["up"]["days"] == 2 and r["down"]["days"] == 2

    total_bench = 1.0
    for _m, b in pairs:
        total_bench *= 1 + b / 100
    combined = (1 + r["up"]["bench_pct"] / 100) * (1 + r["down"]["bench_pct"] / 100)
    assert combined == pytest.approx(total_bench)


def test_regime_capture_ratio():
    pairs = [(1.0, 2.0), (1.0, 2.0)]
    r = split_by_regime(pairs)
    # 지수의 절반만 따라간 경우 포착률 ≈ 0.5
    assert r["up"]["capture"] == pytest.approx(0.5, abs=0.01)
    assert r["down"]["days"] == 0


def test_flat_benchmark_days_excluded():
    """지수가 정확히 0인 날은 상승도 하락도 아니다."""
    r = split_by_regime([(5.0, 0.0), (1.0, 1.0)])
    assert r["up"]["days"] == 1 and r["down"]["days"] == 0


def test_regime_handles_empty_and_garbage():
    r = split_by_regime([])
    assert r["up"]["days"] == 0 and r["up"]["capture"] is None
    r2 = split_by_regime([("x", None), (float("nan"), 1.0)])
    assert r2["up"]["days"] == 0


def test_format_regime_line_leads_with_capture():
    line = format_regime_line(split_by_regime([(1.0, 2.0), (-1.0, -2.0)]))
    assert "포착" in line and "상승" in line and "하락" in line


def test_format_regime_line_when_no_samples():
    assert "불가" in format_regime_line(split_by_regime([]))


# --------------------------------------------------------------- 리스크 지표

def test_risk_metrics_basic():
    m = risk_metrics([1.0, -1.0, 2.0, -2.0])
    assert m["samples"] == 4
    assert m["down_day_ratio"] == pytest.approx(0.5)
    assert m["worst_day_pct"] == pytest.approx(-2.0)
    assert m["best_day_pct"] == pytest.approx(2.0)
    assert m["vol_annual_pct"] > 0


def test_risk_metrics_refuses_to_invent_stability():
    """표본이 1개면 변동성을 0으로 채우지 않는다 — 없는 안정성을 주장하게 된다."""
    m = risk_metrics([1.0])
    assert m["samples"] == 1
    assert m["vol_annual_pct"] is None
    assert m["sharpe_annual"] is None
    assert "산출 불가" in format_risk_line(m)


def test_risk_metrics_empty():
    m = risk_metrics([])
    assert m["samples"] == 0 and m["mean_daily_pct"] is None


def test_risk_metrics_ignores_non_finite():
    m = risk_metrics([1.0, float("nan"), float("inf"), -1.0, "x", None])
    assert m["samples"] == 2


def test_zero_variance_series_has_no_sharpe():
    m = risk_metrics([0.0, 0.0, 0.0])
    assert m["vol_annual_pct"] == pytest.approx(0.0)
    assert m["sharpe_annual"] is None


class TestDepositNeutralisationWiring:
    """입금 중화는 함수만 지원해선 안 되고 호출부가 실제로 넘겨야 한다.

    2026-08-27 실측 버그: performance_lens는 flows 인자를 지원하는데 주간 리포트가
    안 넘겨서, 전날 kr_pocket 적립 10만원이 그날 +35% 수익으로 잡혔다.
    NAV 284,499 → 385,460. 그 결과 연환산 변동성 109%, 샤프 +2.28이 보고됐다
    (유입 중화 후 실제는 29.5%, -1.18). 적립식 트랙은 이 경로가 상시다.
    """

    def test_flow_helper_groups_by_day(self, monkeypatch):
        import main

        monkeypatch.setattr(
            "database.repositories.get_cash_flows",
            lambda account_key, mode="paper": [
                (datetime(2026, 8, 26, 17, 19), 100000.0),
                (datetime(2026, 8, 26, 18, 0), 50000.0),
                (datetime(2026, 9, 1, 9, 0), 100000.0),
            ],
        )
        flows = main.account_flows_by_day("acct")
        assert flows == {date(2026, 8, 26): 150000.0, date(2026, 9, 1): 100000.0}

    def test_no_flows_is_empty(self, monkeypatch):
        import main

        monkeypatch.setattr(
            "database.repositories.get_cash_flows",
            lambda account_key, mode="paper": [],
        )
        assert main.account_flows_by_day("acct") == {}

    def test_deposit_day_is_not_a_return(self):
        """실측 수치로 고정 — 중화하면 0%, 안 하면 +35%."""
        nav = [(date(2026, 8, 25), 280718.0), (date(2026, 8, 26), 385460.0)]
        flows = {date(2026, 8, 26): 100000.0}

        with_flow = daily_returns_from_nav(nav, flows=flows)[0][1]
        without = daily_returns_from_nav(nav)[0][1]

        assert without > 30, "중화 없이는 입금이 큰 수익으로 잡힌다(버그 재현)"
        assert abs(with_flow) < 2.0, f"중화 후에도 {with_flow:.1f}% — 입금이 수익에 남았다"

    def test_volatility_is_not_inflated_by_a_deposit(self):
        """입금 하루가 변동성을 통째로 왜곡하지 않는지."""
        base = [(date(2026, 8, 1 + i), 280000.0 + i * 500) for i in range(10)]
        nav = base + [(date(2026, 8, 11), 380000.0)]   # 마지막 날 10만원 입금
        flows = {date(2026, 8, 11): 100000.0}

        inflated = risk_metrics([r for _, r in daily_returns_from_nav(nav)])
        correct = risk_metrics([r for _, r in daily_returns_from_nav(nav, flows=flows)])

        assert inflated["vol_annual_pct"] > correct["vol_annual_pct"] * 3
        assert correct["vol_annual_pct"] < 30
