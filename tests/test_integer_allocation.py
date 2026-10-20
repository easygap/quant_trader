"""소액 ETF 계좌의 정수 주 조합 계획 (2026-10-20).

10/12 적립 계좌: KODEX 200 1주(101,605원)·CD ETF 4주(58,195원)·현금 45,707원.
목표는 주식 47.5%·CD 47.5%인데 주식이 26.7%였다. 종목별로 부족분(약 7.9만 원)과
초과분(약 5.2만 원)을 1주 가격으로 내림하면 둘 다 0주라 계획이 0건이었다.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.integer_allocation import choose_integer_holdings

KODEX, CD = "069500", "357870"


def _plan(**over):
    args = dict(
        total_value=380_092.0, cash=45_707.0,
        prices={KODEX: 101_605.0, CD: 58_195.0},
        current={KODEX: 1, CD: 4},
        targets={KODEX: 0.475, CD: 0.475},
        min_cash_ratio=0.05, min_trade=50_000.0, max_turnover=0.6,
        defensive_symbol=CD,
    )
    args.update(over)
    return choose_integer_holdings(**args)


def test_swaps_defensive_shares_into_equity_when_rounding_blocks_both_sides():
    plan = _plan()
    assert plan.trades == {KODEX: 1, CD: -2}
    assert plan.weights_after[KODEX] == pytest.approx(203_210 / 380_092)
    assert plan.loss_after < plan.loss_before
    assert plan.cash_after >= 0.05 * 380_092


def test_keeps_holdings_when_current_combination_is_closest():
    """10/20 재현: 추세 필터로 주식 목표 23.75% — KODEX 1주가 정확히 목표, CD 1주 더 사면 현금 부족."""
    plan = _plan(
        total_value=365_267.0, prices={KODEX: 86_755.0, CD: 58_300.0},
        targets={KODEX: 0.2375, CD: 0.7125},
    )
    assert not plan.changed
    assert plan.trades == {KODEX: 0, CD: 0}


def test_new_deposit_buys_underweight_without_selling():
    """적립금은 매도 없이 부족한 자산부터 산다 — 같은 개선이면 매도 비용이 없는 쪽."""
    plan = _plan(
        total_value=465_267.0, cash=145_707.0, prices={KODEX: 86_755.0, CD: 58_300.0},
        targets={KODEX: 0.2375, CD: 0.7125},
    )
    assert plan.trades == {KODEX: 0, CD: 2}
    assert all(delta >= 0 for delta in plan.trades.values())


def test_never_spends_cash_below_reserve():
    plan = _plan(min_cash_ratio=0.30)
    reserve = 0.30 * 380_092
    assert plan.cash_after >= min(reserve, 45_707.0) - 1e-6
    # 이미 하한 아래였다면 현금을 더 줄이는 조합은 고르지 않는다
    assert plan.cash_after >= 45_707.0 - 1e-6


def test_turnover_cap_and_min_trade_are_respected():
    assert not _plan(max_turnover=0.2).changed          # 스왑 거래액 21.8만 원 > 한도 7.6만 원
    assert not _plan(min_trade=110_000.0).changed       # KODEX 1주 10.2만 원 < 최소 거래
    assert _plan(max_turnover=0.6).changed


def test_blocked_buy_symbol_is_not_bought():
    plan = _plan(blocked_buys={KODEX})
    assert plan.trades[KODEX] <= 0


def test_sell_costs_can_outweigh_a_marginal_improvement():
    """개선이 비용보다 작으면 거래하지 않는다 — 매도 1주 비용을 아주 크게 두면 스왑을 포기."""
    costly = {KODEX: {"buy": 50.0, "sell": 50.0}, CD: {"buy": 20.0, "sell": 30_000.0}}
    assert not _plan(unit_costs=costly).changed


def test_not_applicable_inputs_return_none():
    many = {str(i): 0.1 for i in range(6)}
    assert _plan(targets=many, prices={s: 10_000.0 for s in many}, current={}) is None
    assert _plan(prices={KODEX: 101_605.0, CD: 0.0}) is None
    assert _plan(total_value=0.0) is None


# ------------------------------------------------------------- 리밸런서 연결

def _pocket_rebalancer(monkeypatch, positions, summary, planner="integer"):
    from config.config_loader import Config
    from core.basket_rebalancer import BasketRebalancer

    Config._instance = None
    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "kr_pocket_test"
    rb.basket = {
        "target_stock_weight": 0.95, "min_cash_ratio": 0.05,
        "risk": {"stop_loss_pct": 0},
        "overlays": {"defensive_symbol": CD},
    }
    rb.account_key = "basket_rebalance:kr_pocket_test"
    rb.execution_strategy = rb.account_key
    rb.holdings = {KODEX: 0.5, CD: 0.5}
    rb.rebalance_cfg = {
        "trigger": "drift", "drift_threshold": 0.08, "min_trade_amount": 50_000,
        "max_turnover_ratio": 0.6, "deployment_band": 0.03, "planner": planner,
    }
    rb._target_stock_weight = 0.95
    rb._risk_params = Config.get().risk_params
    rb.portfolio_mgr = MagicMock()
    rb.portfolio_mgr.get_portfolio_summary.return_value = summary
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    rb._market_snapshot = {}
    monkeypatch.setattr("core.basket_rebalancer.get_all_positions", lambda **kw: positions)
    monkeypatch.setattr("core.basket_rebalancer.symbols_in_reentry_cooldown", lambda *a, **k: {})
    return rb


def _positions():
    return [
        SimpleNamespace(symbol=KODEX, quantity=1, avg_price=122_345.0),
        SimpleNamespace(symbol=CD, quantity=4, avg_price=57_973.5),
    ]


def test_pocket_plan_uses_integer_planner_and_sells_first(monkeypatch):
    rb = _pocket_rebalancer(monkeypatch, _positions(), {"total_value": 380_092.0, "cash": 45_707.0})
    orders = rb.plan_rebalance(prices={KODEX: 101_605.0, CD: 58_195.0})
    assert [(o.action, o.symbol, o.quantity) for o in orders] == [("SELL", CD, 2), ("BUY", KODEX, 1)]
    assert "1주 단위 조합" in orders[1].reason


def test_legacy_planner_reproduces_the_zero_order_case(monkeypatch):
    """비교 기준: 기존 계획기는 같은 상태에서 주문을 하나도 만들지 못한다(10/13 보고)."""
    rb = _pocket_rebalancer(monkeypatch, _positions(), {"total_value": 380_092.0, "cash": 45_707.0}, planner="")
    assert rb.plan_rebalance(prices={KODEX: 101_605.0, CD: 58_195.0}) == []


def test_integer_hold_is_explained_in_diagnostics(monkeypatch):
    positions = [
        SimpleNamespace(symbol=KODEX, quantity=1, avg_price=122_345.0),
        SimpleNamespace(symbol=CD, quantity=4, avg_price=57_973.5),
    ]
    rb = _pocket_rebalancer(monkeypatch, positions, {"total_value": 365_267.0, "cash": 45_707.0})
    rb.basket["overlays"]["trend_filter"] = {"enabled": False}
    monkeypatch.setattr(type(rb), "_overlay_scale", lambda self: 0.5)
    assert rb.plan_rebalance(prices={KODEX: 86_755.0, CD: 58_300.0}) == []
    codes = {(d["symbol"], d["code"]) for d in rb._plan_diagnostics}
    assert (CD, "INTEGER_HOLD") in codes


def test_shipped_pocket_uses_integer_planner():
    from core.basket_rebalancer import BasketRebalancer

    cfg = BasketRebalancer._load_baskets_config()["kr_pocket"]
    assert cfg["rebalance"]["planner"] == "integer"
    # 소액 특화값은 그대로다(바꾸면 바스켓이 조용히 멈춘다 — OPERATING_PRINCIPLES)
    assert cfg["rebalance"]["min_trade_amount"] == 50000
    assert cfg["rebalance"]["max_turnover_ratio"] == 0.6


# ------------------------------------------------- 배분 진단의 구조적 차이

def test_cd_cash_split_from_one_share_rounding_is_not_an_alarm():
    """스왑 뒤 상태: 주식은 목표 근처, CD·현금 합계도 목표 근처인데 둘 사이만 1주 단위로 갈렸다."""
    from core.basket_observability import allocation_diagnostics

    total, cash = 380_092.0, 60_492.0
    positions = [{"symbol": KODEX, "quantity": 2, "price": 101_605.0},
                 {"symbol": CD, "quantity": 2, "price": 58_195.0}]
    diag = allocation_diagnostics(total, cash, positions, {KODEX: 0.475, CD: 0.475}, CD, 0.08)
    assert diag["status"] == "OK"
    assert any(a.get("structural") for a in diag["assets"] if a["asset"] == "defensive")


def test_real_equity_shortfall_is_still_an_alarm():
    """10/12 실제 상태: 주식 26.7% — 이건 조치할 수 있는 위험 비중 차이다."""
    from core.basket_observability import allocation_diagnostics

    positions = [{"symbol": KODEX, "quantity": 1, "price": 101_605.0},
                 {"symbol": CD, "quantity": 4, "price": 58_195.0}]
    diag = allocation_diagnostics(380_092.0, 45_707.0, positions, {KODEX: 0.475, CD: 0.475}, CD, 0.08)
    assert diag["status"] == "ATTENTION"
    assert any("주식" in issue for issue in diag["issues"])


def test_idle_cash_that_can_buy_a_defensive_share_is_an_alarm():
    from core.basket_observability import allocation_diagnostics

    positions = [{"symbol": KODEX, "quantity": 2, "price": 90_000.0},
                 {"symbol": CD, "quantity": 2, "price": 58_000.0}]
    # 현금 13.4만 원: 목표 현금(5%)을 빼도 CD 1주(5.8만)를 살 수 있다
    diag = allocation_diagnostics(430_000.0, 134_000.0, positions, {KODEX: 0.475, CD: 0.475}, CD, 0.08)
    assert diag["status"] == "ATTENTION"
