"""build_weekly_summary — 주간 요약 다이제스트 포맷터 단위 테스트(순수 함수)."""

from core.weekly_report import build_weekly_summary


def _eval(**over):
    base = {
        "verdict": "WAIT",
        "progress_days": 22,
        "min_trading_days": 60,
        "snapshot_coverage": 0.95,
        "metrics": {
            "nav_return_pct": -5.47,
            "benchmark_return_pct": 0.71,
            "execution_gap_pct": -2.04,
            "composition_gap_pct": -5.51,
            "total_gap_pct": -6.18,
        },
    }
    base.update(over)
    return base


def _fields(out):
    return {f["name"]: f["value"] for f in out["fields"]}


class TestBuildWeeklySummary:
    def test_title_has_verdict_and_basket(self):
        out = build_weekly_summary(basket_name="kr_diversified_hold", eval_result=_eval())
        assert "kr_diversified_hold" in out["title"]
        assert "WAIT" in out["title"]

    def test_performance_week_and_cumulative(self):
        out = build_weekly_summary(
            basket_name="b", eval_result=_eval(), week_nav_change_pct=-1.2,
        )
        perf = _fields(out)["💰 성과"]
        assert "주간 -1.20%" in perf
        assert "누적 -5.47%" in perf

    def test_attribution_fields_present(self):
        f = _fields(build_weekly_summary(basket_name="b", eval_result=_eval()))
        assert "NAV -5.47% vs KS11 +0.71%" in f["📊 vs KS11"]
        assert "실행 -2.04%p" in f["🔎 귀속 분해"]
        assert "구성 -5.51%p" in f["🔎 귀속 분해"]

    def test_progress_field(self):
        f = _fields(build_weekly_summary(basket_name="b", eval_result=_eval()))
        assert "22/60일" in f["📅 진행률"]
        assert "커버리지 95%" in f["📅 진행률"]

    def test_event_counts_and_no_incident(self):
        f = _fields(build_weekly_summary(
            basket_name="b", eval_result=_eval(), missing_days=0, cycle_errors=0,
        ))
        assert "무사고" in f["🛠 주간 이벤트"]

    def test_event_counts_with_incidents(self):
        f = _fields(build_weekly_summary(
            basket_name="b", eval_result=_eval(), missing_days=1, cycle_errors=2,
        ))
        ev = f["🛠 주간 이벤트"]
        assert "결측 1일" in ev and "사이클 오류 2건" in ev
        assert "무사고" not in ev

    def test_missing_days_counts_distinct_not_events(self):
        # 하루 결측이 여러 SNAPSHOT_GAP 이벤트로 재경보돼도 '1일'로만 집계돼야 한다
        # (호출부가 고유 일수를 넘기는 계약을 포맷터 수준에서 못박음).
        f = _fields(build_weekly_summary(
            basket_name="b", eval_result=_eval(), missing_days=1, cycle_errors=0,
        ))
        assert "결측 1일" in f["🛠 주간 이벤트"]

    def test_missing_week_change_omits_week_term(self):
        f = _fields(build_weekly_summary(basket_name="b", eval_result=_eval()))
        assert "주간" not in f["💰 성과"]
        assert "누적 -5.47%" in f["💰 성과"]

    def test_text_fallback_contains_fields(self):
        out = build_weekly_summary(basket_name="b", eval_result=_eval())
        for f in out["fields"]:
            assert f["value"] in out["text"]

    def test_missing_attribution_still_builds(self):
        out = build_weekly_summary(
            basket_name="b",
            eval_result={"verdict": "WAIT", "progress_days": 5, "min_trading_days": 60,
                         "snapshot_coverage": 1.0, "metrics": {"nav_return_pct": None}},
        )
        names = [f["name"] for f in out["fields"]]
        assert "🔎 귀속 분해" not in names  # 귀속 데이터 없으면 생략
        assert "🛠 주간 이벤트" in names    # 이벤트 필드는 항상
