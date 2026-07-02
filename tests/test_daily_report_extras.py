"""build_daily_report_extras — 일일 리포트 v2 부가 필드 포맷터 단위 테스트(순수 함수)."""

from core.basket_evaluation import build_daily_report_extras


def _eval(**over):
    base = {
        "verdict": "WAIT",
        "progress_days": 17,
        "snapshot_days": 16,
        "min_trading_days": 60,
        "progress_pct": 17 / 60,
        "snapshot_coverage": 16 / 17,
        "cost_drag_cum": 0.000961,
        "cost_drag_annualized": 0.014251,
        "metrics": {"nav_return_pct": -5.47, "benchmark_return_pct": 0.71},
    }
    base.update(over)
    return base


class TestBenchmarkGap:
    def test_gap_computed(self):
        out = build_daily_report_extras(eval_result=_eval())
        assert "NAV -5.47%" in out["benchmark_gap"]
        assert "KS11 +0.71%" in out["benchmark_gap"]
        assert "-6.18%p" in out["benchmark_gap"]  # -5.47 - 0.71

    def test_benchmark_missing_shows_nav_only(self):
        out = build_daily_report_extras(
            eval_result=_eval(metrics={"nav_return_pct": -5.47, "benchmark_return_pct": None})
        )
        assert "KS11 조회 불가" in out["benchmark_gap"]

    def test_no_nav_no_gap_field(self):
        out = build_daily_report_extras(eval_result=_eval(metrics={"nav_return_pct": None}))
        assert "benchmark_gap" not in out

    def test_nav_override_used_over_metrics(self):
        # 카드의 누적 수익률과 같은 소스로 맞추기 위한 override — 스냅샷 결측일 정합.
        out = build_daily_report_extras(eval_result=_eval(), nav_return_pct=-4.30)
        assert "NAV -4.30%" in out["benchmark_gap"]
        assert "-5.01%p" in out["benchmark_gap"]  # -4.30 - 0.71


class TestProgress:
    def test_progress_and_miss_budget(self):
        # 17일 중 16 커버 → 이미 1일 결측. 60일 허용 결측 = 3 → 예산 2일.
        out = build_daily_report_extras(eval_result=_eval())
        assert "17/60일" in out["progress"]
        assert "커버리지 94%" in out["progress"]
        assert "결측예산 2일" in out["progress"]

    def test_miss_budget_never_negative(self):
        # 이미 5일 결측(허용 3 초과) → 예산 0(음수 금지)
        out = build_daily_report_extras(
            eval_result=_eval(progress_days=20, snapshot_days=15, snapshot_coverage=15 / 20)
        )
        assert "결측예산 0일" in out["progress"]

    def test_budget_uses_progress_days_after_period(self):
        # 기간(60)을 넘겨 80일 운영, 3일 결측 → 허용은 max(60,80)*5%=4 → 예산 1일.
        # (min_days 고정이면 3-3=0으로 과소 표기되던 버그 수정)
        out = build_daily_report_extras(
            eval_result=_eval(
                progress_days=80, snapshot_days=77, min_trading_days=60,
                snapshot_coverage=77 / 80, progress_pct=1.0,
            )
        )
        assert "80/60일" in out["progress"]  # 표시 분모는 목표 기간
        assert "결측예산 1일" in out["progress"]

    def test_min_days_zero_falls_back_in_display(self):
        # 무의미 설정(min_trading_days=0)이라도 '5/0일' 대신 운영일수로 폴백.
        out = build_daily_report_extras(
            eval_result=_eval(
                progress_days=5, snapshot_days=5, min_trading_days=0,
                snapshot_coverage=1.0, progress_pct=1.0,
            )
        )
        assert "5/5일" in out["progress"]
        assert "5/0일" not in out["progress"]


class TestCost:
    def test_cost_annualized_marked_reference_before_period(self):
        out = build_daily_report_extras(eval_result=_eval())  # 17/60 미충족
        assert "누적 0.096%" in out["cost"]
        assert "(참고)" in out["cost"]  # 기간 미충족 → 연환산은 참고

    def test_cost_annualized_not_marked_after_period(self):
        out = build_daily_report_extras(
            eval_result=_eval(progress_days=60, snapshot_days=59, snapshot_coverage=59 / 60)
        )
        assert "(참고)" not in out["cost"]

    def test_cost_without_annualized_shows_cum_only(self):
        out = build_daily_report_extras(eval_result=_eval(cost_drag_annualized=None))
        assert out["cost"] == "누적 0.096%"
        assert "연환산" not in out["cost"]

    def test_no_cost_field_when_cum_absent(self):
        out = build_daily_report_extras(eval_result=_eval(cost_drag_cum=None))
        assert "cost" not in out


class TestDeployment:
    def test_deployment_ratio_and_gap(self):
        out = build_daily_report_extras(
            eval_result=None,
            deployment={"deployment_ratio": 0.589, "design_fraction": 0.80, "unfilled_slots": []},
        )
        assert "주식 59%" in out["deployment"]
        assert "설계 80%" in out["deployment"]
        assert "-21.1%p" in out["deployment"]
        assert "slot_warning" not in out

    def test_unfilled_slot_warning(self):
        out = build_daily_report_extras(
            eval_result=None,
            deployment={
                "deployment_ratio": 0.61,
                "design_fraction": 0.80,
                "unfilled_slots": [
                    {"symbol": "000660", "price": 2424000, "slot_amount": 756239, "target_weight": 0.1},
                ],
            },
        )
        assert "미체결 1개" in out["slot_warning"]
        assert "000660" in out["slot_warning"]
        assert "자본 결정 대기" in out["slot_warning"]

    def test_many_unfilled_slots_truncate_to_three(self):
        slots = [
            {"symbol": f"00{i}", "price": 1_000_000 + i, "slot_amount": 500_000, "target_weight": 0.1}
            for i in range(5)
        ]
        out = build_daily_report_extras(
            eval_result=None,
            deployment={"deployment_ratio": 0.3, "design_fraction": 0.8, "unfilled_slots": slots},
        )
        w = out["slot_warning"]
        assert "미체결 5개" in w          # 총 개수는 전체
        assert "외 2개" in w              # 3개만 상세, 나머지 2개 요약
        assert w.count("1주") == 3        # 상세는 정확히 3개
        assert w.endswith("자본 결정 대기(#422)")


class TestEmptyInputs:
    def test_all_none_returns_empty(self):
        assert build_daily_report_extras(eval_result=None, deployment=None) == {}
