import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd


class FakeCollector:
    def __init__(self, frames):
        self.frames = frames

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["close"])
        return df.copy()


def _ohlcv(dates, close):
    close = np.array(close, dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [1_000_000] * len(close),
        }
    )


def _frames_for_rotation():
    dates = pd.bdate_range("2025-01-01", "2025-03-10")
    steps = np.arange(len(dates), dtype=float)
    a = 100 + steps * 0.5
    b = 100 + np.minimum(steps, 24) * 0.9 - np.maximum(steps - 24, 0) * 0.8
    c = 100 + np.maximum(steps - 24, 0) * 1.4
    benchmark = np.full(len(dates), 100.0)
    return {
        "AAA": _ohlcv(dates, a),
        "BBB": _ohlcv(dates, b),
        "CCC": _ohlcv(dates, c),
        "KS11": _ohlcv(dates, benchmark),
    }


def _params():
    return {
        "target_top_n": 2,
        "target_exposure": 0.80,
        "target_tolerance_pct": 0.0,
        "short_lookback": 2,
        "long_lookback": 3,
        "short_weight": 0.5,
        "score_mode": "benchmark_excess",
        "benchmark_symbol": "KS11",
    }


def _adapter_plan():
    from core.target_weight_rotation import TargetWeightOrder, TargetWeightPlan

    orders = [
        TargetWeightOrder(
            symbol="AAA",
            action="BUY",
            price=100.0,
            quantity=11_000,
            notional=1_100_000.0,
            current_quantity=0,
            target_quantity=11_000,
            current_weight_pct=0.0,
            target_weight_pct=11.0,
            reason="target_weight_rebalance_buy",
        ),
        TargetWeightOrder(
            symbol="BBB",
            action="BUY",
            price=200.0,
            quantity=4_500,
            notional=900_000.0,
            current_quantity=0,
            target_quantity=4_500,
            current_weight_pct=0.0,
            target_weight_pct=9.0,
            reason="target_weight_rebalance_buy",
        ),
        TargetWeightOrder(
            symbol="CCC",
            action="BUY",
            price=300.0,
            quantity=4_000,
            notional=1_200_000.0,
            current_quantity=0,
            target_quantity=4_000,
            current_weight_pct=0.0,
            target_weight_pct=12.0,
            reason="target_weight_rebalance_buy",
        ),
    ]
    return TargetWeightPlan(
        candidate_id="target_weight_candidate",
        as_of_date="2026-04-10",
        trade_day="2026-04-10",
        score_day="2026-04-09",
        params_hash="hash",
        symbols=["AAA", "BBB", "CCC"],
        targets=["AAA", "BBB", "CCC"],
        prices={"AAA": 100.0, "BBB": 200.0, "CCC": 300.0},
        target_exposure=0.32,
        base_target_exposure=0.8,
        risk_off=True,
        nav=10_000_000.0,
        cash_before=10_000_000.0,
        market_value_before=0.0,
        cash_after_estimate=6_800_000.0,
        gross_exposure_after=3_200_000.0,
        target_position_count=3,
        orders=orders,
        diagnostics={"missing_symbols": [], "benchmark_symbol": "KS11"},
    )


def _adapter_plan_for_date(day: str):
    score_day = {
        "2026-04-08": "2026-04-07",
        "2026-04-09": "2026-04-08",
        "2026-04-10": "2026-04-09",
    }.get(day, "2026-04-09")
    return replace(_adapter_plan(), as_of_date=day, trade_day=day, score_day=score_day)


def test_target_weight_plan_uses_prior_day_scores_for_targets():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    c = frames["CCC"].copy()
    c.loc[c["date"] == pd.Timestamp("2025-02-03"), "close"] = 500.0
    frames["CCC"] = c

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.score_day == "2025-01-31"
    assert set(plan.targets) == {"AAA", "BBB"}
    assert "CCC" not in plan.targets
    assert {order.symbol for order in plan.orders if order.action == "BUY"} == {"AAA", "BBB"}


def test_target_weight_plan_risk_overlay_uses_prior_day_benchmark():
    from core.target_weight_rotation import build_target_weight_plan

    frames = _frames_for_rotation()
    benchmark = frames["KS11"].copy()
    same_day_mask = benchmark["date"] == pd.Timestamp("2025-02-03")
    benchmark.loc[same_day_mask, "close"] = 50.0
    benchmark.loc[same_day_mask, "open"] = 50.0
    benchmark.loc[same_day_mask, "high"] = 50.0
    benchmark.loc[same_day_mask, "low"] = 50.0
    frames["KS11"] = benchmark

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={
            **_params(),
            "market_exposure_mode": "benchmark_risk",
            "market_ma_period": 5,
            "bear_target_exposure": 0.35,
            "benchmark_drawdown_lookback": 5,
            "benchmark_drawdown_trigger_pct": 4.0,
        },
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(frames),
    )

    assert plan.target_exposure == 0.8
    assert plan.risk_off is False


def test_pilot_plan_validation_blocks_order_count_and_notional_caps():
    from core.target_weight_rotation import build_target_weight_plan, validate_plan_against_pilot

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params=_params(),
        cash=100_000.0,
        positions={},
        as_of_date="2025-02-03",
        collector=FakeCollector(_frames_for_rotation()),
    )
    pilot_check = SimpleNamespace(
        allowed=True,
        reason="pilot authorized",
        remaining_orders=1,
        caps_snapshot={
            "max_orders_per_day": 1,
            "max_concurrent_positions": 1,
            "max_notional_per_trade": 10_000,
            "max_gross_exposure": 30_000,
        },
    )

    validation = validate_plan_against_pilot(plan, pilot_check)

    assert validation.allowed is False
    assert "remaining_orders" in validation.reason
    assert "max order notional" in validation.reason
    assert "target positions" in validation.reason


def test_execute_plan_dry_run_preserves_sell_before_buy_ordering():
    from core.target_weight_rotation import build_target_weight_plan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={**_params(), "target_top_n": 1},
        cash=50_000.0,
        positions={"BBB": {"quantity": 20, "avg_price": 105.0}},
        as_of_date="2025-03-03",
        collector=FakeCollector(_frames_for_rotation()),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=True,
    )

    actions = [detail["order"]["action"] for detail in execution["details"]]
    assert actions == sorted(actions, reverse=True)
    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)


def test_execute_plan_stops_after_failed_sell_before_buy():
    from core.target_weight_rotation import TargetWeightOrder, TargetWeightPlan
    from tools.target_weight_rotation_pilot import execute_plan

    plan = TargetWeightPlan(
        candidate_id="candidate",
        as_of_date="2025-03-03",
        trade_day="2025-03-03",
        score_day="2025-02-28",
        params_hash="hash",
        symbols=["AAA", "BBB"],
        targets=["BBB"],
        prices={"AAA": 100.0, "BBB": 200.0},
        target_exposure=0.8,
        base_target_exposure=0.8,
        risk_off=False,
        nav=10_000.0,
        cash_before=1_000.0,
        market_value_before=9_000.0,
        cash_after_estimate=1_000.0,
        gross_exposure_after=8_000.0,
        target_position_count=1,
        orders=[
            TargetWeightOrder(
                symbol="AAA",
                action="SELL",
                price=100.0,
                quantity=10,
                notional=1_000.0,
                current_quantity=10,
                target_quantity=0,
                current_weight_pct=10.0,
                target_weight_pct=0.0,
                reason="sell first",
            ),
            TargetWeightOrder(
                symbol="BBB",
                action="BUY",
                price=200.0,
                quantity=5,
                notional=1_000.0,
                current_quantity=0,
                target_quantity=5,
                current_weight_pct=0.0,
                target_weight_pct=10.0,
                reason="buy second",
            ),
        ],
        diagnostics={},
    )

    class FakeExecutor:
        buy_calls = 0

        def __init__(self, config, account_key=""):
            pass

        def execute_sell(self, **kwargs):
            return {"success": False, "reason": "no position"}

        def execute_buy_quantity(self, **kwargs):
            FakeExecutor.buy_calls += 1
            return {"success": True}

    class FakePortfolio:
        def __init__(self, config, account_key=""):
            pass

        def get_available_cash(self):
            return 10_000.0

        def get_total_value(self):
            return 10_000.0

    with patch("core.order_executor.OrderExecutor", FakeExecutor), \
         patch("core.portfolio_manager.PortfolioManager", FakePortfolio):
        execution = execute_plan(
            plan,
            config=SimpleNamespace(trading={"mode": "paper"}),
            dry_run=False,
        )

    assert execution["failed"] == 1
    assert execution["halted"] is True
    assert execution["details"][1]["status"] == "skipped_after_failure"
    assert FakeExecutor.buy_calls == 0


def test_preview_plan_against_caps_flags_default_pilot_caps():
    from tools.target_weight_rotation_pilot import build_preview_caps, preview_plan_against_caps

    default_preview = preview_plan_against_caps(_adapter_plan())
    relaxed_preview = preview_plan_against_caps(
        _adapter_plan(),
        build_preview_caps(
            max_orders=3,
            max_positions=3,
            max_notional=1_300_000,
            max_exposure=3_300_000,
        ),
    )

    assert default_preview.allowed is False
    assert "remaining_orders" in default_preview.reason
    assert "max order notional" in default_preview.reason
    assert "target positions" in default_preview.reason
    assert "gross exposure" in default_preview.reason
    assert relaxed_preview.allowed is True


def test_recommend_pilot_caps_matches_target_weight_plan():
    from tools.target_weight_rotation_pilot import recommend_pilot_caps

    rec = recommend_pilot_caps(_adapter_plan())
    minimum = rec["minimum_caps"]
    suggested = rec["suggested_caps"]

    assert minimum["max_orders_per_day"] == 3
    assert minimum["max_concurrent_positions"] == 3
    assert minimum["max_notional_per_trade"] == 1_200_000
    assert minimum["max_gross_exposure"] == 3_200_000
    assert suggested["max_orders_per_day"] == 3
    assert suggested["max_concurrent_positions"] == 3
    assert suggested["max_notional_per_trade"] == 1_260_000
    assert suggested["max_gross_exposure"] == 3_360_000
    assert rec["suggested_preview"]["allowed"] is True
    assert "--max-orders 3 --max-positions 3" in rec["enable_command"]
    assert "--max-notional 1260000 --max-exposure 3360000" in rec["enable_command"]


def test_record_shadow_evidence_for_plan_is_non_promotable(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import record_shadow_evidence_for_plan

    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    plan = _adapter_plan()

    ev = record_shadow_evidence_for_plan(
        plan,
        validation=SimpleNamespace(allowed=False, reason="no active pilot authorization"),
    )
    records = pe.get_canonical_records(plan.candidate_id)

    assert ev is not None
    assert ev.execution_backed is False
    assert ev.evidence_mode == "shadow_bootstrap"
    assert ev.benchmark_status == "final"
    assert ev.same_universe_excess is None
    assert len(records) == 1
    assert records[0]["benchmark_meta"]["source"] == "target_weight_shadow_plan"
    assert records[0]["diagnostics"][0]["dry_run_only"] is True
    assert records[0]["diagnostics"][0]["pilot_validation_reason"] == "no active pilot authorization"


def test_run_pilot_shadow_generates_readiness_and_runbook(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())

    result = twp.run_pilot(
        record_shadow_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    readiness = result["launch_artifacts"]["launch_readiness"]

    assert result["shadow_evidence"] is not None
    assert result["launch_artifacts"]["attempted"] is True
    assert Path(readiness["json_path"]).exists()
    assert Path(readiness["md_path"]).exists()
    runbook_path = Path(result["launch_artifacts"]["runbook_path"])
    assert runbook_path.exists()
    assert readiness["launch_ready"] is False
    assert readiness["shadow_days"] >= 1
    assert payload["cap_recommendation"]["suggested_caps"]["max_orders_per_day"] == 3
    assert payload["cap_recommendation"]["suggested_caps"]["max_concurrent_positions"] == 3
    assert payload["cap_recommendation"]["suggested_preview"]["allowed"] is True
    assert payload["launch_artifacts"]["attempted"] is True
    assert payload["launch_artifacts"]["launch_readiness"]["clean_final_days_current"] == 1
    assert "clean_final_days" in payload["launch_artifacts"]["launch_readiness"]["blocking_requirements"][0]
    runbook_text = runbook_path.read_text(encoding="utf-8")
    assert "## Target-weight Cap Recommendation" in runbook_text
    assert "--max-orders 3 --max-positions 3" in runbook_text
    assert "--max-notional 1260000 --max-exposure 3360000" in runbook_text


def test_run_pilot_without_shadow_does_not_generate_readiness(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())

    result = twp.run_pilot(
        record_shadow_evidence=False,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["launch_artifacts"] == {"attempted": False}
    assert not (runtime_dir / "target_weight_candidate_pilot_launch_readiness.json").exists()
    assert not (runtime_dir / "target_weight_candidate_pilot_runbook.md").exists()


def test_run_shadow_bootstrap_records_range_and_skips_duplicates(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(
        twp,
        "build_plan",
        lambda **kwargs: _adapter_plan_for_date(kwargs["as_of_date"]),
    )

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-08",
        end_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    records = pe.get_canonical_records("target_weight_candidate")
    readiness = result["launch_artifacts"]["launch_readiness"]

    assert result["summary"]["recorded"] == 3
    assert result["summary"]["already_recorded"] == 0
    assert payload["summary"]["recorded"] == 3
    assert [record["date"] for record in records] == ["2026-04-08", "2026-04-09", "2026-04-10"]
    assert all(record["execution_backed"] is False for record in records)
    assert readiness["clean_final_days_current"] == 3
    assert Path(result["launch_artifacts"]["runbook_path"]).exists()

    second = twp.run_shadow_bootstrap(
        start_date="2026-04-08",
        end_date="2026-04-10",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert second["summary"]["recorded"] == 0
    assert second["summary"]["already_recorded"] == 3
    assert len(pe.get_canonical_records("target_weight_candidate")) == 3


def test_run_shadow_bootstrap_skips_duplicate_trade_day_in_batch(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import core.paper_runtime as pr
    import tools.target_weight_rotation_pilot as twp

    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pe, "EVIDENCE_DIR", tmp_path / "paper_evidence")
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(pr, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "_check_pilot_eligibility", lambda strategy: None)
    monkeypatch.setattr(
        twp,
        "build_plan",
        lambda **kwargs: _adapter_plan_for_date("2026-04-10"),
    )

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-10",
        end_date="2026-04-13",
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["summary"]["recorded"] == 1
    assert result["summary"]["duplicate_trade_day"] == 1
    assert len(pe.get_canonical_records("target_weight_candidate")) == 1
