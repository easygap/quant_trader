import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


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
        diagnostics={
            "missing_symbols": [],
            "benchmark_symbol": "KS11",
            "position_avg_prices_before": {},
            "liquidity": {
                "lookback_days": 20,
                "symbols": {
                    order.symbol: {
                        "complete": True,
                        "reason": "liquidity window available",
                        "observations": 20,
                        "avg_daily_value": 100_000_000.0,
                        "last_daily_value": 100_000_000.0,
                    }
                    for order in orders
                },
            },
        },
        target_quantities_after={order.symbol: order.target_quantity for order in orders},
        position_quantities_before={},
    )


def _complete_execution(plan):
    details = []
    for order in plan.orders:
        result = {
            "success": True,
            "symbol": order.symbol,
            "action": order.action,
            "price": order.price,
            "quantity": order.quantity,
            "mode": "paper",
        }
        if order.action == "BUY":
            result["paper_fixed_quantity"] = True
        details.append({
            "order": {
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity,
            },
            "status": "success",
            "result": result,
        })
    return {
        "executed": len(plan.orders),
        "skipped": 0,
        "failed": 0,
        "halted": False,
        "halt_reason": "",
        "details": details,
    }


def _complete_fills(plan):
    return [
        SimpleNamespace(
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            strategy=plan.candidate_id,
            mode="paper",
            account_key=plan.candidate_id,
            executed_at=f"{plan.trade_day} 09:00:00",
        )
        for order in plan.orders
    ]


def _existing_pilot_evidence_record(plan):
    return {
        "date": plan.trade_day,
        "strategy": plan.candidate_id,
        "evidence_mode": "pilot_paper",
        "session_mode": "pilot_paper",
        "execution_backed": True,
        "pilot_authorized": True,
        "pilot_caps_snapshot": {
            "target_weight_plan": {
                "candidate_id": plan.candidate_id,
                "trade_day": plan.trade_day,
                "params_hash": plan.params_hash,
            },
            "target_weight_execution": {
                "complete": True,
                "params_hash": plan.params_hash,
                "planned_orders": len(plan.orders),
                "idempotency_allowed": True,
                "pre_execution_complete": True,
                "liquidity_complete": True,
                "pre_trade_risk_complete": True,
                "order_count_complete": True,
                "order_result_complete": True,
                "order_complete": True,
                "order_result_reconciliation": {"complete": True},
                "fill_complete": True,
                "fill_reconciliation": {"complete": True},
                "position_reconciliation": {"complete": True},
            },
        },
    }


def _adapter_plan_for_date(day: str):
    score_day = {
        "2026-04-08": "2026-04-07",
        "2026-04-09": "2026-04-08",
        "2026-04-10": "2026-04-09",
    }.get(day, "2026-04-09")
    return replace(_adapter_plan(), as_of_date=day, trade_day=day, score_day=score_day)


class SimpleCostRiskManager:
    def __init__(
        self,
        *,
        commission_rate: float = 0.0,
        slippage_per_share: float = 0.0,
        tax_rate: float = 0.0,
        max_position_ratio: float = 0.90,
        max_investment_ratio: float = 1.20,
        min_cash_ratio: float = 0.0,
        max_positions: int = 10,
    ):
        self.commission_rate = commission_rate
        self.slippage_per_share = slippage_per_share
        self.tax_rate = tax_rate
        self.risk_params = {
            "diversification": {
                "max_position_ratio": max_position_ratio,
                "max_investment_ratio": max_investment_ratio,
                "min_cash_ratio": min_cash_ratio,
                "max_positions": max_positions,
            }
        }

    def calculate_transaction_costs(
        self,
        price,
        quantity,
        action="BUY",
        avg_daily_volume=None,
        avg_price=None,
    ):
        commission = round(price * quantity * self.commission_rate, 0)
        tax = round(price * quantity * self.tax_rate, 0) if action.upper() == "SELL" else 0
        slippage = round(self.slippage_per_share * quantity, 0)
        execution_price = price + self.slippage_per_share
        if action.upper() == "SELL":
            execution_price = max(0.0, price - self.slippage_per_share)
        return {
            "commission": commission,
            "tax": tax,
            "capital_gains_tax": 0,
            "slippage": slippage,
            "total_cost": commission + tax + slippage,
            "execution_price": execution_price,
            "participation_rate": None if avg_daily_volume is None else quantity / avg_daily_volume,
            "slippage_multiplier": 1.0,
        }


def test_target_weight_pilot_help_lists_shadow_days():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/target_weight_rotation_pilot.py", "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--shadow-days" in result.stdout
    assert "--readiness-audit" in result.stdout
    assert "--allow-rerun" in result.stdout


def test_target_weight_pilot_control_enable_guard_blocks_requested_caps(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    calls = {}

    def fake_build_preview_caps(**kwargs):
        calls["caps"] = kwargs
        return {"requested": True}

    def fake_run_pilot_readiness_audit(**kwargs):
        calls["audit"] = kwargs
        return {
            "audit": {
                "ready_for_cap_approval": True,
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": False,
                    "reason": "max order notional exceeds cap",
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.build_preview_caps",
        fake_build_preview_caps,
    )
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=2,
        max_positions=2,
        max_notional=1_000_000,
        max_exposure=3_000_000,
    )

    with pytest.raises(ValueError, match="requested target-weight pilot caps"):
        _target_weight_enable_guard(args)

    assert calls["caps"] == {
        "max_orders": 2,
        "max_positions": 2,
        "max_notional": 1_000_000,
        "max_exposure": 3_000_000,
    }
    assert calls["audit"]["preview_caps"] == {"requested": True}


def test_target_weight_pilot_control_enable_guard_passes_safe_requested_caps(monkeypatch, tmp_path):
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID
    from tools.paper_pilot_control import _target_weight_enable_guard

    def fake_run_pilot_readiness_audit(**kwargs):
        return {
            "audit": {
                "ready_for_cap_approval": True,
                "blocking_reasons": [],
                "cap_preview": {
                    "allowed": True,
                    "reason": "proposed pilot caps satisfied",
                },
            },
            "artifact_path": tmp_path / "audit.json",
            "report_path": tmp_path / "audit.md",
        }

    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot.run_pilot_readiness_audit",
        fake_run_pilot_readiness_audit,
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        max_orders=3,
        max_positions=3,
        max_notional=1_300_000,
        max_exposure=3_300_000,
    )

    result = _target_weight_enable_guard(args)

    assert result["audit"]["cap_preview"]["allowed"] is True


def test_paper_pilot_control_enable_stops_before_auth_when_target_weight_guard_fails(
    monkeypatch,
    capsys,
):
    import core.paper_pilot as pp
    import tools.paper_pilot_control as ppc
    from core.target_weight_rotation import DEFAULT_TARGET_WEIGHT_CANDIDATE_ID

    monkeypatch.setattr(pp, "check_pilot_prerequisites", lambda strategy: (True, "ok"))
    monkeypatch.setattr(pp, "enable_pilot", lambda *args, **kwargs: pytest.fail("pilot auth must not be written"))
    monkeypatch.setattr(
        ppc,
        "_target_weight_enable_guard",
        lambda args: (_ for _ in ()).throw(ValueError("target-weight audit blocked")),
    )
    args = SimpleNamespace(
        strategy=DEFAULT_TARGET_WEIGHT_CANDIDATE_ID,
        valid_from="2026-04-10",
        valid_to="2026-04-30",
        max_orders=2,
        max_positions=2,
        max_notional=1_000_000,
        max_exposure=3_000_000,
        reason="test",
    )

    with pytest.raises(SystemExit) as exc:
        ppc.run_enable(args)

    assert exc.value.code == 1
    assert "target-weight audit blocked" in capsys.readouterr().out


def test_shadow_batch_cli_exits_nonzero_when_target_unmet(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    calls = {}

    def fake_run_shadow_bootstrap(**kwargs):
        calls.update(kwargs)
        return {
            "summary": {
                "recorded": 1,
                "already_recorded": 0,
                "duplicate_trade_day": 0,
                "failed": 1,
                "covered_unique_trade_days": 1,
                "target_unique_trade_days": 2,
                "target_met": False,
            },
            "launch_artifacts": {"attempted": False},
            "artifact_path": tmp_path / "shadow_batch.json",
            "start_date": "2026-04-10",
            "end_date": "2026-04-13",
            "requested_dates": ["2026-04-10", "2026-04-13"],
        }

    monkeypatch.setattr(twp, "run_shadow_bootstrap", fake_run_shadow_bootstrap)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--shadow-days",
            "2",
            "--shadow-end-date",
            "2026-04-13",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert calls["target_unique_trade_days"] == 2
    assert "met=NO" in output
    assert "status: BLOCKED - shadow bootstrap incomplete" in output


def test_pilot_cli_exits_nonzero_when_execution_fidelity_blocked(monkeypatch, tmp_path, capsys):
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()

    def fake_run_pilot(**kwargs):
        return {
            "plan": plan,
            "validation": SimpleNamespace(allowed=True, reason="pilot caps satisfied"),
            "cap_preview": SimpleNamespace(allowed=True, reason="proposed pilot caps"),
            "cap_recommendation": {
                "suggested_caps": {
                    "max_orders_per_day": 3,
                    "max_concurrent_positions": 3,
                    "max_notional_per_trade": 1_260_000,
                    "max_gross_exposure": 3_360_000,
                }
            },
            "execution": {
                "executed": len(plan.orders),
                "failed": 0,
                "skipped": 0,
                "halted": False,
                "halt_reason": "",
                "details": [],
            },
            "execution_evidence": {
                "complete": False,
                "reason": "target_weight_position_mismatch: CCC actual=0 target=4000",
            },
            "evidence_collection": {"attempted": False, "recorded": False},
            "shadow_evidence_summary": {"attempted": False, "recorded": False},
            "launch_artifacts": {"attempted": False},
            "artifact_path": tmp_path / "session.json",
        }

    monkeypatch.setattr(twp, "run_pilot", fake_run_pilot)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "target_weight_rotation_pilot.py",
            "--execute",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        twp.main()

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "execution fidelity: BLOCKED" in output
    assert "target_weight_position_mismatch" in output


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


def test_target_weight_plan_records_full_expected_quantities_after_rebalance():
    from core.target_weight_rotation import build_target_weight_plan

    plan = build_target_weight_plan(
        candidate_id="candidate",
        symbols=["AAA", "BBB", "CCC"],
        params={**_params(), "target_tolerance_pct": 100.0},
        cash=100_000.0,
        positions={"AAA": {"quantity": 100, "avg_price": 100.0}},
        as_of_date="2025-02-03",
        collector=FakeCollector(_frames_for_rotation()),
    )

    assert plan.orders == []
    assert plan.target_quantities_after is not None
    assert plan.expected_position_quantities == plan.target_quantities_after
    assert set(plan.targets).issubset(set(plan.target_quantities_after))
    assert plan.target_quantities_after["AAA"] == 100
    assert plan.position_quantities_before == {"AAA": 100}
    assert plan.starting_position_quantities == {"AAA": 100}
    assert plan.to_dict()["target_quantities_after"] == plan.target_quantities_after
    assert plan.to_dict()["position_quantities_before"] == plan.position_quantities_before


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


def test_execute_plan_stops_after_failed_sell_before_buy(tmp_path):
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
         patch("core.portfolio_manager.PortfolioManager", FakePortfolio), \
         patch("core.paper_pilot.RUNTIME_DIR", tmp_path / "paper_runtime"), \
         patch(
             "tools.target_weight_rotation_pilot._load_positions",
             lambda account_key: {"AAA": SimpleNamespace(quantity=10)},
         ):
        execution = execute_plan(
            plan,
            config=SimpleNamespace(trading={"mode": "paper"}),
            dry_run=False,
            liquidity_check={"checked": True, "complete": True, "reason": "ok"},
            pre_trade_risk_check={"checked": True, "complete": True, "reason": "ok"},
        )

    assert execution["failed"] == 1
    assert execution["halted"] is True
    assert execution["details"][1]["status"] == "skipped_after_failure"
    assert FakeExecutor.buy_calls == 0


def test_execute_plan_blocks_stale_starting_positions_before_order_submission(monkeypatch, tmp_path):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    monkeypatch.setattr("core.paper_pilot.RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot._load_positions",
        lambda account_key: {"ZZZ": SimpleNamespace(quantity=3)},
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_pre_execution_position_drift" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_pre_execution_position_drift"


def test_execute_plan_blocks_duplicate_session_before_order_submission(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.target_weight_rotation_pilot._load_positions",
        lambda account_key: pytest.fail("duplicate execution must block before position reads"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert "target_weight_duplicate_execution_attempt" in execution["halt_reason"]
    assert execution["details"][0]["status"] == "skipped_duplicate_execution_attempt"


def test_reconcile_order_results_flags_quantity_mismatch():
    from tools.target_weight_rotation_pilot import reconcile_order_results

    plan = _adapter_plan()
    execution = _complete_execution(plan)
    execution["details"][0]["result"]["quantity"] = plan.orders[0].quantity - 1

    reconciliation = reconcile_order_results(plan, execution)

    assert reconciliation["complete"] is False
    assert "target_weight_order_result_mismatch" in reconciliation["reason"]
    assert reconciliation["mismatches"][0]["type"] == "quantity"
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[0].symbol


def test_reconcile_plan_fills_flags_missing_trade_history_fill():
    from tools.target_weight_rotation_pilot import reconcile_plan_fills

    plan = _adapter_plan()
    fills = _complete_fills(plan)[:-1]

    reconciliation = reconcile_plan_fills(plan, fills)

    assert reconciliation["complete"] is False
    assert "target_weight_fill_reconciliation_mismatch" in reconciliation["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol
    assert reconciliation["mismatches"][0]["actual_quantity"] == 0


def test_reconcile_plan_fills_flags_partial_quantity():
    from tools.target_weight_rotation_pilot import reconcile_plan_fills

    plan = _adapter_plan()
    fills = _complete_fills(plan)
    fills[0].quantity = plan.orders[0].quantity - 1

    reconciliation = reconcile_plan_fills(plan, fills)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[0].symbol
    assert reconciliation["mismatches"][0]["expected_quantity"] == plan.orders[0].quantity
    assert reconciliation["mismatches"][0]["actual_quantity"] == plan.orders[0].quantity - 1


def test_verify_existing_pilot_evidence_accepts_complete_record(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [_existing_pilot_evidence_record(plan)])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is True
    assert verification["reason"] == "existing pilot_paper evidence verified"


def test_verify_existing_pilot_evidence_rejects_non_pilot_record(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    record["evidence_mode"] = "real_paper"
    record["session_mode"] = "normal_paper"
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} >= {
        "record.evidence_mode",
        "record.session_mode",
    }


def test_verify_existing_pilot_evidence_rejects_missing_pre_trade_risk(monkeypatch):
    import core.paper_evidence as pe
    from tools.target_weight_rotation_pilot import verify_existing_pilot_evidence_record

    plan = _adapter_plan()
    record = _existing_pilot_evidence_record(plan)
    del record["pilot_caps_snapshot"]["target_weight_execution"]["pre_trade_risk_complete"]
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [record])

    verification = verify_existing_pilot_evidence_record(plan)

    assert verification["valid"] is False
    assert "target_weight_existing_evidence_invalid" in verification["reason"]
    assert {item["field"] for item in verification["mismatches"]} == {
        "target_weight_execution.pre_trade_risk_complete"
    }


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


def test_assess_plan_liquidity_blocks_large_adv_order():
    from tools.target_weight_rotation_pilot import assess_plan_liquidity

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            "AAA": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            },
            "BBB": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 100_000_000.0,
                "last_daily_value": 100_000_000.0,
            },
            "CCC": {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 100_000_000.0,
                "last_daily_value": 100_000_000.0,
            },
        },
    }
    plan = replace(plan, diagnostics=diagnostics)

    liquidity = assess_plan_liquidity(plan, max_order_adv_pct=5.0)

    assert liquidity["complete"] is False
    assert "target_weight_liquidity_preflight_failed" in liquidity["reason"]
    assert liquidity["orders"][0]["symbol"] == "AAA"
    assert liquidity["orders"][0]["order_adv_pct"] == 11.0
    assert "AAA" in liquidity["violations"][0]


def test_assess_plan_liquidity_blocks_missing_diagnostics():
    from tools.target_weight_rotation_pilot import assess_plan_liquidity

    plan = replace(_adapter_plan(), diagnostics={"missing_symbols": []})

    liquidity = assess_plan_liquidity(plan)

    assert liquidity["checked"] is True
    assert liquidity["complete"] is False
    assert "target_weight_liquidity_preflight_failed" in liquidity["reason"]
    assert "missing liquidity diagnostics" in liquidity["violations"]


def test_assess_plan_pre_trade_risk_passes_when_cash_covers_costed_orders():
    from tools.target_weight_rotation_pilot import assess_plan_pre_trade_risk

    plan = _adapter_plan()
    risk = assess_plan_pre_trade_risk(
        plan,
        risk_manager=SimpleCostRiskManager(
            commission_rate=0.001,
            slippage_per_share=1.0,
        ),
    )

    assert risk["complete"] is True
    assert risk["reason"] == "target_weight_pre_trade_risk_passed"
    assert risk["projected_cash_after_costs"] < plan.cash_after_estimate
    assert risk["cost_summary"]["commission"] > 0
    assert risk["cost_summary"]["slippage"] > 0
    assert risk["order_costs"][0]["avg_daily_volume"] == 1_000_000.0


def test_assess_plan_pre_trade_risk_blocks_cash_shortfall_after_costs():
    from tools.target_weight_rotation_pilot import assess_plan_pre_trade_risk

    plan = replace(_adapter_plan(), cash_before=3_200_000.0)
    risk = assess_plan_pre_trade_risk(
        plan,
        risk_manager=SimpleCostRiskManager(slippage_per_share=1.0),
    )

    assert risk["complete"] is False
    assert "target_weight_pre_trade_risk_failed" in risk["reason"]
    assert "required cash" in risk["violations"][0]
    assert risk["projected_cash_after_costs"] < 0


def test_execute_plan_blocks_liquidity_before_order_submission(monkeypatch):
    from tools.target_weight_rotation_pilot import assess_plan_liquidity, execute_plan

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("liquidity failure must not submit orders"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        execution_idempotency={"allowed": True},
        pre_execution_reconciliation={"complete": True},
        liquidity_check=assess_plan_liquidity(plan, max_order_adv_pct=5.0),
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert execution["details"][0]["status"] == "skipped_liquidity_preflight"
    assert "target_weight_liquidity_preflight_failed" in execution["halt_reason"]


def test_execute_plan_blocks_pre_trade_risk_before_order_submission(monkeypatch):
    from tools.target_weight_rotation_pilot import execute_plan

    plan = _adapter_plan()
    risk = {
        "checked": True,
        "complete": False,
        "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
        "violations": ["cash shortfall"],
        "warnings": [],
        "cost_summary": {},
        "order_costs": [],
    }
    monkeypatch.setattr(
        "core.order_executor.OrderExecutor",
        lambda *args, **kwargs: pytest.fail("pre-trade risk failure must not submit orders"),
    )

    execution = execute_plan(
        plan,
        config=SimpleNamespace(trading={"mode": "paper"}),
        dry_run=False,
        execution_idempotency={"allowed": True},
        pre_execution_reconciliation={"complete": True},
        liquidity_check={"checked": True, "complete": True, "reason": "ok"},
        pre_trade_risk_check=risk,
    )

    assert execution["executed"] == 0
    assert execution["skipped"] == len(plan.orders)
    assert execution["halted"] is True
    assert execution["details"][0]["status"] == "skipped_pre_trade_risk"
    assert "target_weight_pre_trade_risk_failed" in execution["halt_reason"]


def test_reconcile_plan_positions_uses_full_expected_book():
    from tools.target_weight_rotation_pilot import reconcile_plan_positions

    plan = replace(
        _adapter_plan(),
        target_quantities_after={
            "AAA": 11_000,
            "BBB": 4_500,
            "CCC": 4_000,
            "DDD": 12,
        },
    )
    positions = {
        order.symbol: SimpleNamespace(quantity=order.target_quantity)
        for order in plan.orders
    }

    reconciliation = reconcile_plan_positions(plan, positions)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == [
        {"symbol": "DDD", "target_quantity": 12, "actual_quantity": 0}
    ]
    assert "DDD actual=0 target=12" in reconciliation["reason"]


def test_reconcile_plan_positions_blocks_unexpected_positive_positions():
    from tools.target_weight_rotation_pilot import reconcile_plan_positions

    plan = _adapter_plan()
    positions = {
        order.symbol: SimpleNamespace(quantity=order.target_quantity)
        for order in plan.orders
    }
    positions["ZZZ"] = SimpleNamespace(quantity=3)

    reconciliation = reconcile_plan_positions(plan, positions)

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == []
    assert reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert "unexpected: ZZZ actual=3" in reconciliation["reason"]


def test_reconcile_plan_starting_positions_blocks_stale_plan_inputs():
    from tools.target_weight_rotation_pilot import reconcile_plan_starting_positions

    plan = replace(
        _adapter_plan(),
        position_quantities_before={"AAA": 7},
    )

    reconciliation = reconcile_plan_starting_positions(
        plan,
        {
            "AAA": SimpleNamespace(quantity=9),
            "ZZZ": SimpleNamespace(quantity=3),
        },
    )

    assert reconciliation["complete"] is False
    assert reconciliation["mismatches"] == [
        {"symbol": "AAA", "expected_quantity": 7, "actual_quantity": 9}
    ]
    assert reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert "target_weight_pre_execution_position_drift" in reconciliation["reason"]


def test_resolve_shadow_batch_range_supports_auto_days():
    from tools.target_weight_rotation_pilot import resolve_shadow_batch_range

    start, end, dates = resolve_shadow_batch_range(
        shadow_days=3,
        shadow_end_date="2026-04-13",
    )

    assert start == "2026-04-09"
    assert end == "2026-04-13"
    assert dates == ["2026-04-09", "2026-04-10", "2026-04-13"]

    explicit_start, explicit_end, explicit_dates = resolve_shadow_batch_range(
        shadow_start_date="2026-04-08",
        shadow_end_date="2026-04-10",
    )
    assert explicit_start == "2026-04-08"
    assert explicit_end == "2026-04-10"
    assert explicit_dates == ["2026-04-08", "2026-04-09", "2026-04-10"]

    with pytest.raises(ValueError, match="cannot be combined"):
        resolve_shadow_batch_range(
            shadow_start_date="2026-04-08",
            shadow_end_date="2026-04-10",
            shadow_days=3,
        )
    with pytest.raises(ValueError, match="must be provided together"):
        resolve_shadow_batch_range(shadow_end_date="2026-04-10")
    with pytest.raises(ValueError, match="must be positive"):
        resolve_shadow_batch_range(shadow_days=0, shadow_end_date="2026-04-10")


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
    assert "Liquidity preflight:" in runbook_text
    assert "Pre-trade risk validation:" in runbook_text
    assert "--max-orders 3 --max-positions 3" in runbook_text
    assert "--max-notional 1260000 --max-exposure 3360000" in runbook_text


def test_run_pilot_readiness_audit_writes_no_order_artifact(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=False,
            reason="no active pilot authorization",
            remaining_orders=None,
            remaining_exposure=None,
            caps_snapshot=None,
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "remaining_clean_days": 0,
            "evidence_fresh": True,
            "benchmark_ready": True,
            "notifier_ready": True,
            "pilot_authorization_present": False,
            "strategy_eligible": True,
            "runtime_state": "normal",
            "real_paper_days": 0,
            "shadow_days": 3,
            "eligible_records": 3,
            "quarantined_records": 0,
            "infra_ready": True,
            "launch_ready": False,
            "blocking_requirements": [],
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    audit = result["audit"]
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is True
    assert audit["ready_for_capped_pilot"] is False
    assert audit["next_action"] == "enable pilot with suggested caps, then rerun readiness audit"
    assert "--readiness-audit" in audit["operator_commands"]["rerun_readiness_audit"]
    assert "--max-orders 3 --max-positions 3" in audit["operator_commands"]["enable_suggested_caps"]
    assert "--execute --collect-evidence" in audit["operator_commands"]["execute_capped_paper"]
    assert any("pilot_authorization" in reason for reason in audit["blocking_reasons"])
    assert any("pilot_validation" in reason for reason in audit["blocking_reasons"])
    assert audit["execution_idempotency"]["allowed"] is True
    assert audit["pre_execution_reconciliation"]["complete"] is True
    assert audit["liquidity_check"]["complete"] is True
    assert audit["pre_trade_risk_check"]["complete"] is True
    assert audit["cap_recommendation"]["suggested_caps"]["max_orders_per_day"] == 3
    assert payload["artifact_type"] == "target_weight_rotation_pilot_readiness_audit"
    assert payload["no_order_safety"]["orders_submitted"] is False
    assert payload["no_order_safety"]["shadow_evidence_recorded"] is False
    assert payload["no_order_safety"]["pilot_evidence_recorded"] is False
    assert result["report_path"].exists()
    assert "# Target-weight Pilot Readiness Audit" in report_text
    assert "CAP_APPROVAL_READY" in report_text
    assert "## Liquidity Preflight" in report_text
    assert "## Pre-trade Risk" in report_text
    assert "## Operator Commands" in report_text
    assert "--max-notional 1260000 --max-exposure 3360000" in report_text


def test_run_pilot_readiness_audit_blocks_liquidity_preflight(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["liquidity_check"]["complete"] is False
    assert any("liquidity_preflight" in reason for reason in audit["blocking_reasons"])
    assert "BLOCKED" in report_text
    assert "target_weight_liquidity_preflight_failed" in report_text


def test_run_pilot_readiness_audit_blocks_missing_liquidity_diagnostics(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = replace(_adapter_plan(), diagnostics={"missing_symbols": []})
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["liquidity_check"]["complete"] is False
    assert any("missing liquidity diagnostics" in reason for reason in audit["blocking_reasons"])
    assert "missing liquidity diagnostics" in report_text


def test_run_pilot_readiness_audit_blocks_pre_trade_risk(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(pp, "PILOT_AUTH_FILE", runtime_dir / "pilot_authorizations.jsonl")
    monkeypatch.setattr(pp, "PILOT_AUDIT_FILE", runtime_dir / "pilot_audit.jsonl")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "assess_plan_pre_trade_risk",
        lambda *args, **kwargs: {
            "checked": True,
            "complete": False,
            "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
            "violations": ["cash shortfall"],
            "warnings": [],
            "cost_summary": {"total_explicit_costs": 1000.0},
            "projected_cash_after_costs": -1000.0,
            "projected_cash_ratio_after_costs": -0.01,
            "projected_investment_ratio_after_costs": 1.01,
        },
    )
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "compute_launch_readiness",
        lambda *args, **kwargs: {
            "strategy": plan.candidate_id,
            "clean_final_days_current": 3,
            "clean_final_days_required": 3,
            "pilot_authorization_present": True,
            "infra_ready": True,
            "launch_ready": True,
            "blocking_requirements": [],
            "runtime_state": "normal",
        },
    )

    result = twp.run_pilot_readiness_audit(
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    audit = result["audit"]
    report_text = result["report_path"].read_text(encoding="utf-8")

    assert audit["ready_for_cap_approval"] is False
    assert audit["ready_for_capped_pilot"] is False
    assert audit["pre_trade_risk_check"]["complete"] is False
    assert any("pre_trade_risk" in reason for reason in audit["blocking_reasons"])
    assert "## Pre-trade Risk" in report_text
    assert "target_weight_pre_trade_risk_failed" in report_text


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


def test_run_pilot_blocks_evidence_when_execution_incomplete(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: _adapter_plan())
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: {
            "executed": 1,
            "skipped": 1,
            "failed": 1,
            "halted": True,
            "halt_reason": "BUY CCC failed: rejected",
            "details": [],
        },
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: [])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("incomplete execution must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_execution_incomplete" in result["evidence_collection"]["reason"]
    assert saved_sessions[0]["execution_complete"] is False
    assert saved_sessions[0]["evidence_collectible"] is False
    assert payload["evidence_collection"]["status"] == "blocked"


def test_run_pilot_blocks_evidence_when_position_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(pp, "save_pilot_session_artifact", lambda **kwargs: None)
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders[:-1]
        },
    ])
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("position mismatch must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    reconciliation = result["execution_evidence"]["position_reconciliation"]
    assert result["execution_evidence"]["pre_execution_complete"] is True
    assert result["execution_evidence"]["order_complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_position_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol


def test_run_pilot_blocks_evidence_when_order_result_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    execution = _complete_execution(plan)
    execution["details"][0]["result"]["quantity"] = plan.orders[0].quantity - 1
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: execution)
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("mismatched order result must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    reconciliation = result["execution_evidence"]["order_result_reconciliation"]
    assert result["execution_evidence"]["pre_execution_complete"] is True
    assert result["execution_evidence"]["order_count_complete"] is True
    assert result["execution_evidence"]["order_result_complete"] is False
    assert result["execution_evidence"]["order_complete"] is False
    assert result["execution_evidence"]["position_reconciliation"]["complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_order_result_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["type"] == "quantity"
    assert saved_sessions[0]["target_weight_execution"]["order_result_complete"] is False


def test_run_pilot_blocks_evidence_when_fill_reconciliation_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    incomplete_fills = _complete_fills(plan)[:-1]
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: incomplete_fills)
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("mismatched fills must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    reconciliation = result["execution_evidence"]["fill_reconciliation"]
    assert result["execution_evidence"]["order_complete"] is True
    assert result["execution_evidence"]["fill_complete"] is False
    assert result["execution_evidence"]["position_reconciliation"]["complete"] is True
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_fill_reconciliation_mismatch" in result["evidence_collection"]["reason"]
    assert reconciliation["mismatches"][0]["symbol"] == plan.orders[-1].symbol
    assert saved_sessions[0]["target_weight_execution"]["fill_complete"] is False


def test_run_pilot_blocks_order_submission_when_starting_positions_drift(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("stale plan must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: {"ZZZ": SimpleNamespace(quantity=3)},
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("stale plan must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    pre_reconciliation = result["execution_evidence"]["pre_execution_reconciliation"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["halted"] is True
    assert result["execution_evidence"]["pre_execution_complete"] is False
    assert result["execution_evidence"]["order_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pre_execution_position_drift" in result["evidence_collection"]["reason"]
    assert pre_reconciliation["unexpected_positions"] == [
        {"symbol": "ZZZ", "actual_quantity": 3}
    ]
    assert saved_sessions[0]["target_weight_execution"]["pre_execution_complete"] is False


def test_run_pilot_blocks_order_submission_when_liquidity_preflight_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    diagnostics = dict(plan.diagnostics)
    diagnostics["liquidity"] = {
        "lookback_days": 20,
        "symbols": {
            order.symbol: {
                "complete": True,
                "reason": "liquidity window available",
                "observations": 20,
                "avg_daily_value": 10_000_000.0,
                "last_daily_value": 10_000_000.0,
            }
            for order in plan.orders
        },
    }
    plan = replace(plan, diagnostics=diagnostics)
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("liquidity failure must not submit orders"),
    )
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan: pytest.fail("liquidity failure must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("liquidity failure must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["liquidity_check"]["complete"] is False
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution_evidence"]["liquidity_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_liquidity_preflight_failed" in result["evidence_collection"]["reason"]
    assert saved_sessions[0]["target_weight_execution"]["liquidity_complete"] is False


def test_run_pilot_blocks_order_submission_when_pre_trade_risk_fails(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    risk = {
        "checked": True,
        "complete": False,
        "reason": "target_weight_pre_trade_risk_failed: cash shortfall",
        "violations": ["cash shortfall"],
        "warnings": [],
        "cost_summary": {"total_explicit_costs": 1000.0},
        "order_costs": [],
    }
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(twp, "assess_plan_pre_trade_risk", lambda *args, **kwargs: risk)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("pre-trade risk failure must not submit orders"),
    )
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: {})
    monkeypatch.setattr(
        twp,
        "load_paper_trade_fills",
        lambda plan: pytest.fail("pre-trade risk failure must not reconcile fills"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("pre-trade risk failure must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["pre_trade_risk_check"]["complete"] is False
    assert result["execution"]["executed"] == 0
    assert result["execution"]["skipped"] == len(plan.orders)
    assert result["execution"]["details"][0]["status"] == "skipped_pre_trade_risk"
    assert result["execution_evidence"]["pre_trade_risk_complete"] is False
    assert result["execution_evidence"]["complete"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_pre_trade_risk_failed" in result["evidence_collection"]["reason"]
    assert saved_sessions[0]["target_weight_execution"]["pre_trade_risk_complete"] is False


def test_run_pilot_blocks_duplicate_execute_session(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("duplicate attempt must not overwrite the prior pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("duplicate attempt must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("duplicate attempt must block before position reads"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("duplicate attempt must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_idempotency"]["allowed"] is False
    assert result["execution_idempotency"]["previous_session_found"] is True
    assert result["execution"]["halted"] is True
    assert result["execution_evidence"]["idempotency_allowed"] is False
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_duplicate_execution_attempt" in result["evidence_collection"]["reason"]
    assert payload["execution_idempotency"]["allowed"] is False


def test_check_execution_idempotency_blocks_completed_rerun_even_when_allowed(monkeypatch, tmp_path):
    import core.paper_pilot as pp
    from tools.target_weight_rotation_pilot import check_execution_idempotency

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )

    idempotency = check_execution_idempotency(plan, allow_rerun=True)

    assert idempotency["allowed"] is False
    assert idempotency["allow_rerun"] is True
    assert "target_weight_completed_execution_rerun_blocked" in idempotency["reason"]


def test_run_pilot_blocks_completed_rerun_even_when_allowed(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": True,
                "orders_planned": len(plan.orders),
                "orders_executed": len(plan.orders),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: pytest.fail("completed rerun must not overwrite prior pilot session"),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: pytest.fail("completed rerun must not submit orders"),
    )
    monkeypatch.setattr(
        twp,
        "_load_positions",
        lambda account_key: pytest.fail("completed rerun must block before position reads"),
    )
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("completed rerun must not collect pilot evidence"),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        allow_rerun=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["execution_idempotency"]["allowed"] is False
    assert "target_weight_completed_execution_rerun_blocked" in result["execution_idempotency"]["reason"]
    assert result["execution"]["executed"] == 0
    assert result["execution"]["halted"] is True
    assert result["evidence_collection"]["status"] == "blocked"


def test_run_pilot_allows_duplicate_session_with_explicit_rerun(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    collected = []
    saved_sessions = []
    plan = _adapter_plan()
    runtime_dir = tmp_path / "paper_runtime"
    monkeypatch.setattr(pp, "RUNTIME_DIR", runtime_dir)
    runtime_dir.mkdir(parents=True)
    pp.pilot_session_artifact_path(plan.candidate_id, plan.trade_day).write_text(
        json.dumps({
            "strategy": plan.candidate_id,
            "date": plan.trade_day,
            "generated_at": "2026-04-10T09:00:00",
            "pilot_session": {
                "session_mode": "pilot_paper",
                "execution_complete": False,
                "orders_planned": len(plan.orders),
                "orders_executed": 0,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(
        pe,
        "collect_daily_evidence",
        lambda **kwargs: collected.append(kwargs) or SimpleNamespace(date=kwargs["date"].strftime("%Y-%m-%d")),
    )

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        allow_rerun=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["execution_idempotency"]["allowed"] is True
    assert result["execution_idempotency"]["previous_session_found"] is True
    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "recorded"
    assert len(collected) == 1
    assert saved_sessions[0]["target_weight_execution"]["idempotency_allowed"] is True


def test_run_pilot_records_evidence_after_complete_execution(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    collected = []
    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(
        twp,
        "execute_plan",
        lambda *args, **kwargs: _complete_execution(plan),
    )
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))

    def collect_daily_evidence(**kwargs):
        collected.append(kwargs)
        return SimpleNamespace(date=kwargs["date"].strftime("%Y-%m-%d"))

    monkeypatch.setattr(pe, "collect_daily_evidence", collect_daily_evidence)

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "recorded"
    assert len(collected) == 1
    caps = collected[0]["pilot_caps_snapshot"]
    assert caps["target_weight_execution"]["complete"] is True
    assert caps["target_weight_execution"]["pre_execution_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["liquidity_complete"] is True
    assert caps["target_weight_execution"]["liquidity_check"]["complete"] is True
    assert caps["target_weight_execution"]["pre_trade_risk_complete"] is True
    assert caps["target_weight_execution"]["pre_trade_risk_check"]["complete"] is True
    assert caps["target_weight_execution"]["order_result_complete"] is True
    assert caps["target_weight_execution"]["order_result_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["fill_complete"] is True
    assert caps["target_weight_execution"]["fill_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["position_reconciliation"]["complete"] is True
    assert caps["target_weight_execution"]["planned_orders"] == len(plan.orders)
    assert caps["target_weight_plan"]["params_hash"] == plan.params_hash
    assert caps["target_weight_plan"]["position_quantities_before"] == {}
    assert saved_sessions[0]["execution_complete"] is True


def test_run_pilot_accepts_verified_already_recorded_pilot_evidence(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(pe, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [_existing_pilot_evidence_record(plan)])

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "already_recorded"
    assert result["evidence_collection"]["existing_evidence"]["valid"] is True
    assert payload["evidence_collection"]["existing_evidence"]["valid"] is True
    assert saved_sessions[0]["execution_complete"] is True


def test_run_pilot_blocks_unverified_already_recorded_evidence(monkeypatch, tmp_path):
    import core.paper_evidence as pe
    import core.paper_pilot as pp
    import tools.target_weight_rotation_pilot as twp

    saved_sessions = []
    plan = _adapter_plan()
    stale_record = _existing_pilot_evidence_record(plan)
    stale_record["evidence_mode"] = "real_paper"
    stale_record["session_mode"] = "normal_paper"
    monkeypatch.setattr(pp, "RUNTIME_DIR", tmp_path / "paper_runtime")
    monkeypatch.setattr(twp, "build_plan", lambda **kwargs: plan)
    monkeypatch.setattr(
        pp,
        "check_pilot_entry",
        lambda *args, **kwargs: SimpleNamespace(
            allowed=True,
            reason="ok",
            remaining_orders=10,
            remaining_exposure=10_000_000,
            caps_snapshot={
                "max_orders_per_day": 10,
                "max_concurrent_positions": 10,
                "max_notional_per_trade": 2_000_000,
                "max_gross_exposure": 10_000_000,
            },
        ),
    )
    monkeypatch.setattr(
        pp,
        "save_pilot_session_artifact",
        lambda **kwargs: saved_sessions.append(kwargs["pilot_session"]),
    )
    monkeypatch.setattr(twp, "execute_plan", lambda *args, **kwargs: _complete_execution(plan))
    monkeypatch.setattr(twp, "load_paper_trade_fills", lambda plan: _complete_fills(plan))
    position_snapshots = iter([
        {},
        {
            order.symbol: SimpleNamespace(quantity=order.target_quantity)
            for order in plan.orders
        },
    ])
    monkeypatch.setattr(twp, "_load_positions", lambda account_key: next(position_snapshots))
    monkeypatch.setattr(pe, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(pe, "get_canonical_records", lambda strategy: [stale_record])

    result = twp.run_pilot(
        execute=True,
        collect_evidence=True,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

    assert result["execution_evidence"]["complete"] is True
    assert result["evidence_collection"]["status"] == "blocked"
    assert "target_weight_existing_evidence_invalid" in result["evidence_collection"]["reason"]
    assert result["evidence_collection"]["existing_evidence"]["valid"] is False
    assert payload["evidence_collection"]["status"] == "blocked"
    assert saved_sessions[0]["execution_complete"] is True


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


def test_run_shadow_bootstrap_auto_days_backfills_unique_trade_days(monkeypatch, tmp_path):
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

    def build_plan(**kwargs):
        as_of = kwargs["as_of_date"]
        if as_of == "2026-04-13":
            return replace(_adapter_plan_for_date("2026-04-10"), as_of_date=as_of)
        return _adapter_plan_for_date(as_of)

    monkeypatch.setattr(twp, "build_plan", build_plan)

    result = twp.run_shadow_bootstrap(
        start_date="2026-04-10",
        end_date="2026-04-13",
        target_unique_trade_days=2,
        max_scan_weekdays=3,
        output_dir=tmp_path / "sessions",
        config=SimpleNamespace(trading={"mode": "paper"}),
    )
    payload = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
    records = pe.get_canonical_records("target_weight_candidate")

    assert result["requested_dates"] == ["2026-04-09", "2026-04-13"]
    assert result["summary"]["recorded"] == 2
    assert result["summary"]["duplicate_trade_day"] == 0
    assert result["summary"]["covered_unique_trade_days"] == 2
    assert result["summary"]["target_unique_trade_days"] == 2
    assert result["summary"]["target_met"] is True
    assert [record["date"] for record in records] == ["2026-04-09", "2026-04-10"]
    assert payload["summary"]["covered_unique_trade_days"] == 2
    assert payload["summary"]["target_met"] is True
