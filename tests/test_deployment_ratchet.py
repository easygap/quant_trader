"""현금 래칫 · 결측 경보 피로 · 적립 이행 점검 회귀 테스트.

배경(2026-08-26 3주 점검): 트랙이 표면상 정상(오류 0건, 스냅샷 저장 정상, 헬스 ✅ OK)
인데 설계대로 굴러가지 않는 상태가 3주간 감지되지 않았다. 세 가지가 겹쳤다.

  1) 현금 래칫 — 비중 초과 매도는 min_trade를 넘겨 집행되는데(현금 증가), 그 현금을
     되돌리는 매수는 9종목에 얇게 퍼져 전부 min_trade 미만이라 집행되지 않았다.
     배치율 61.0% → 54.9% 단조 감소, 19거래일간 재투자 0건. 그 사이 KOSPI가 +8.17%
     반등해 유휴 현금이 반등분의 45%를 깎아먹었다.
  2) 결측 경보 피로 — 복구 불가능한 8/18 결측 하나가 3주간 16건의 warning을 만들었다.
  3) 적립 미실행 — kr_pocket 입금이 47일간 0건인데 헬스는 계속 ✅ OK였다.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.basket_rebalancer import BasketRebalancer
from core.operator_health import summarize_contribution_plan


def _pos(symbol, avg_price, quantity):
    return SimpleNamespace(symbol=symbol, avg_price=avg_price, quantity=quantity)


def _rebalancer(*, holdings, target_stock_weight, min_trade, positions,
                drift_threshold=0.08, deployment_band=0.03, turnover=1.0):
    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "t"
    rb.basket = {"target_stock_weight": target_stock_weight, "risk": {}}
    rb.holdings = holdings
    rb.account_key = "acct"
    rb.execution_strategy = "acct"
    rb.rebalance_cfg = {
        "min_trade_amount": min_trade,
        "max_turnover_ratio": turnover,
        "drift_threshold": drift_threshold,
        "deployment_band": deployment_band,
    }
    rb._target_stock_weight = target_stock_weight
    rb._risk_params = {"diversification": {"min_cash_ratio": 0.0}}
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    rb._positions = positions
    return rb


@pytest.fixture
def wired(monkeypatch):
    def _apply(rb, total_value):
        rb.portfolio_mgr = MagicMock()
        rb.portfolio_mgr.get_portfolio_summary.return_value = {"total_value": total_value}
        monkeypatch.setattr(
            "core.basket_rebalancer.get_all_positions", lambda **kw: rb._positions,
        )
        monkeypatch.setattr(
            "core.basket_rebalancer.symbols_in_reentry_cooldown", lambda *a, **k: {},
        )
    return _apply


# ------------------------------------------------------------- 현금 래칫

def test_thin_shortfall_is_topped_up_not_left_as_idle_cash(wired):
    """종목별 드리프트가 전부 min_trade 미만이어도 집계 미달은 채운다.

    회귀하면 매도로 늘어난 현금이 영영 재투자되지 않는다(2026-08 실측).
    """
    holdings = {"A%d" % i: 1 / 9 for i in range(9)}
    prices = {"A%d" % i: 100_000 for i in range(9)}
    positions = [_pos("A%d" % i, 100_000, 6) for i in range(9)]  # 5,400,000
    rb = _rebalancer(holdings=holdings, target_stock_weight=0.60,
                     min_trade=200_000, positions=positions)
    wired(rb, 10_000_000)

    should, reason = rb.should_rebalance(prices)
    assert should is True and "집계 배치율" in reason

    orders = rb.plan_rebalance(prices)
    assert orders, "집계 미달이 보충되지 않았다 — 현금 래칫 회귀"
    bought = sum(o.quantity * o.price for o in orders if o.action == "BUY")
    shortfall = 6_000_000 - 5_400_000
    assert bought > 0
    assert abs(shortfall - bought) < shortfall, "매수가 격차를 줄이지 못했다"


def test_topup_never_pushes_a_symbol_past_its_own_target(wired):
    """집계를 맞추자고 개별 종목 비중을 무너뜨리지 않는다.

    1주 단가가 총자산 대비 클 때 이 가드가 없으면 한 종목에 몰아 사고, 다음 사이클이
    그걸 되팔아 왕복매매가 된다.
    """
    holdings = {"CHEAP": 0.5, "PRICEY": 0.5}
    prices = {"CHEAP": 10_000, "PRICEY": 400_000}
    positions = [_pos("CHEAP", 10_000, 20), _pos("PRICEY", 400_000, 1)]
    rb = _rebalancer(holdings=holdings, target_stock_weight=0.95,
                     min_trade=50_000, positions=positions, drift_threshold=0.08)
    wired(rb, 1_000_000)

    investable = 1_000_000 * 0.95
    for o in rb.plan_rebalance(prices):
        if o.action != "BUY":
            continue
        held = next((p.quantity * prices[p.symbol] for p in positions
                     if p.symbol == o.symbol), 0)
        projected_w = (held + o.quantity * o.price) / investable
        assert projected_w <= 0.5 + 0.08 + 1e-9, (
            "%s 보충 후 비중 %.1f%%가 목표+허용을 넘었다" % (o.symbol, projected_w * 100)
        )


def test_topup_skips_symbols_in_reentry_cooldown(monkeypatch):
    """손절로 나간 종목은 배치율 보충 경로로도 되사지 않는다."""
    holdings = {"A": 0.5, "B": 0.5}
    prices = {"A": 100_000, "B": 100_000}
    positions = [_pos("A", 100_000, 20), _pos("B", 100_000, 20)]
    rb = _rebalancer(holdings=holdings, target_stock_weight=0.60,
                     min_trade=200_000, positions=positions)
    rb.portfolio_mgr = MagicMock()
    rb.portfolio_mgr.get_portfolio_summary.return_value = {"total_value": 10_000_000}
    monkeypatch.setattr("core.basket_rebalancer.get_all_positions", lambda **kw: positions)
    monkeypatch.setattr(
        "core.basket_rebalancer.symbols_in_reentry_cooldown",
        lambda *a, **k: {"A": "60일 재진입 차단"},
    )
    assert "A" not in {o.symbol for o in rb.plan_rebalance(prices) if o.action == "BUY"}


def test_no_topup_when_deployment_is_within_band(wired):
    """설계 배치율 안에 있으면 아무것도 사지 않는다(불필요한 회전 억제)."""
    holdings = {"A%d" % i: 1 / 9 for i in range(9)}
    prices = {"A%d" % i: 100_000 for i in range(9)}
    positions = [_pos("A%d" % i, 100_000, 6) for i in range(9)]
    rb = _rebalancer(holdings=holdings, target_stock_weight=0.545,
                     min_trade=200_000, positions=positions)
    wired(rb, 10_000_000)
    should, _ = rb.should_rebalance(prices)
    assert should is False
    assert rb.plan_rebalance(prices) == []


def test_deployment_gap_sign(wired):
    rb = _rebalancer(holdings={"A": 1.0}, target_stock_weight=0.60,
                     min_trade=100_000, positions=[_pos("A", 100_000, 50)])
    wired(rb, 10_000_000)
    assert rb._deployment_gap({"A": 100_000}) == pytest.approx(-0.10, abs=1e-9)


def test_topup_counts_buys_already_in_the_plan(wired):
    """일반 매수와 보충 매수가 같은 부족분을 두 번 채우면 안 된다."""
    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.25, "C": 0.25},
        target_stock_weight=0.60, min_trade=100_000,
        positions=[_pos("B", 100_000, 1), _pos("C", 100_000, 1)],
        drift_threshold=0.10,
    )
    wired(rb, 1_000_000)
    orders = rb.plan_rebalance({"A": 100_000, "B": 100_000, "C": 100_000})

    bought = sum(o.quantity * o.price for o in orders if o.action == "BUY")
    assert bought == 400_000  # A 30만원 + 보충 10만원. 50만원이면 중복 매수다.
    assert 200_000 + bought == 600_000


def test_regular_buys_restore_band_without_extra_topups(wired):
    """일반 주문으로 허용 범위에 돌아왔다면 작은 보충 주문은 붙이지 않는다."""
    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.25, "C": 0.25},
        target_stock_weight=0.60, min_trade=100_000,
        positions=[_pos("B", 50_000, 29), _pos("C", 50_000, 29)],
    )
    wired(rb, 10_000_000)
    orders = rb.plan_rebalance({"A": 100_000, "B": 50_000, "C": 50_000})

    assert [(o.symbol, o.action, o.quantity) for o in orders] == [("A", "BUY", 30)]


def test_topups_stop_once_deployment_returns_to_band(wired):
    holdings = {f"A{i}": 1 / 9 for i in range(9)}
    positions = [_pos(s, 100_000, 6) for s in holdings]
    rb = _rebalancer(
        holdings=holdings, target_stock_weight=0.60,
        min_trade=200_000, positions=positions,
    )
    wired(rb, 10_000_000)
    orders = rb.plan_rebalance({s: 100_000 for s in holdings})

    bought = sum(o.quantity * o.price for o in orders)
    assert bought == 400_000  # 54% → 58%. 57~63% 안에 들어왔으므로 여기서 멈춘다.


def test_topup_accounts_for_planned_sell_proceeds(wired):
    """과다 보유 종목을 줄인 뒤 생기는 부족분도 같은 계획에서 계산한다."""
    holdings = {f"A{i}": 0.1 for i in range(10)}
    positions = [_pos("A0", 100_000, 15)] + [
        _pos(f"A{i}", 100_000, 5) for i in range(1, 10)
    ]
    rb = _rebalancer(
        holdings=holdings, target_stock_weight=0.60,
        min_trade=200_000, positions=positions,
    )
    wired(rb, 10_000_000)
    orders = rb.plan_rebalance({s: 100_000 for s in holdings})

    assert orders[0].action == "SELL"
    projected = 6_000_000 + sum(
        (1 if o.action == "BUY" else -1) * o.quantity * o.price for o in orders
    )
    assert 5_700_000 <= projected <= 6_300_000


@pytest.mark.parametrize("held", [6, 9], ids=["buy", "sell"])
def test_minimum_trade_uses_rounded_share_quantity(wired, held):
    """차액 6만원이어도 1주 4만원 주문이면 최소 거래금액 5만원 미만이다."""
    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.5}, target_stock_weight=0.60,
        min_trade=50_000, deployment_band=0.10,
        positions=[_pos("A", 40_000, held), _pos("B", 50_000, 6)],
    )
    wired(rb, 1_000_000)

    assert rb.plan_rebalance({"A": 40_000, "B": 50_000}) == []


@pytest.mark.parametrize("turnover", [0.25, 0.35, 0.45])
def test_regular_and_topup_orders_share_one_turnover_budget(wired, turnover):
    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.25, "C": 0.25},
        target_stock_weight=0.60, min_trade=100_000, turnover=turnover,
        positions=[_pos("B", 100_000, 1), _pos("C", 100_000, 1)],
        drift_threshold=0.10,
    )
    wired(rb, 1_000_000)
    orders = rb.plan_rebalance({"A": 100_000, "B": 100_000, "C": 100_000})

    assert orders
    assert all(o.quantity * o.price >= 100_000 for o in orders)
    assert sum(o.quantity * o.price for o in orders) <= 1_000_000 * turnover
    assert len({o.symbol for o in orders}) == len(orders)


def test_plan_leaves_cash_reserve_after_buy_costs(wired):
    """현금 5%를 딱 남기는 주문에 비용을 더해 두 번째 매수가 전부 거절되는 경우."""
    from core.risk_manager import RiskManager

    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.5}, target_stock_weight=0.95,
        min_trade=50_000, positions=[],
    )
    rb.basket["min_cash_ratio"] = 0.05
    wired(rb, 1_000_000)
    orders = rb.plan_rebalance({"A": 25_000, "B": 25_000})

    assert [o.quantity for o in orders] == [19, 18]
    rm = RiskManager(SimpleNamespace(risk_params=rb._risk_params))
    spent = 0
    for o in orders:
        costs = rm.calculate_transaction_costs(o.price, o.quantity, "BUY", symbol=o.symbol)
        spent += costs["execution_price"] * o.quantity + costs["commission"]
    assert 1_000_000 - spent >= 50_000


def test_cash_reduction_keeps_minimum_trade(wired):
    rb = _rebalancer(
        holdings={"A": 1.0}, target_stock_weight=0.60,
        min_trade=50_000, positions=[],
    )
    wired(rb, 100_000)
    rb.portfolio_mgr.get_portfolio_summary.return_value["cash"] = 49_000
    assert rb.plan_rebalance({"A": 30_000}) == []


def test_cash_budget_uses_net_sell_proceeds(wired):
    from core.risk_manager import RiskManager

    rb = _rebalancer(
        holdings={"A": 0.5, "B": 0.5}, target_stock_weight=0.60,
        min_trade=50_000, positions=[_pos("A", 25_000, 40)],
    )
    rb.basket["min_cash_ratio"] = 0.40
    rb._risk_params["transaction_costs"] = {
        "commission_rate": 0.01, "tax_rate": 0.02, "slippage": 0.01, "slippage_ticks": 0,
    }
    wired(rb, 1_000_000)
    orders = rb.plan_rebalance({"A": 25_000, "B": 25_000})

    assert [o.action for o in orders] == ["SELL", "BUY"]
    assert orders[-1].quantity < 12  # 매도 대금 70만원 전액을 쓸 수 없다.
    rm = RiskManager(SimpleNamespace(risk_params=rb._risk_params))
    cash = 0
    for o in orders:
        costs = rm.calculate_transaction_costs(o.price, o.quantity, o.action, symbol=o.symbol)
        amount = costs["execution_price"] * o.quantity
        cash += amount if o.action == "SELL" else -amount
        cash -= costs["commission"] + costs["tax"]
    assert cash >= 400_000


# --------------------------------------------------- 결측 경보 중복 억제

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)

    def close(self):
        pass


def test_already_reported_gap_is_not_realarmed(monkeypatch):
    """같은 결측일은 한 번만 알린다 — 3주간 16건 중복 경보 재발 방지."""
    from core import cycle_observability as co

    rows = [("⚠️ NAV 스냅샷 결측 1일: 2026-08-18 — 재실행 권장",)]
    monkeypatch.setattr("database.models.get_session", lambda: _FakeSession(rows))
    out = co.unreported_snapshot_gaps("acct", [date(2026, 8, 18), date(2026, 8, 25)])
    assert out == [date(2026, 8, 25)], "이미 알린 결측이 다시 경보로 나갔다"


def test_gap_dedupe_falls_back_to_reporting_on_query_failure(monkeypatch):
    """조회가 실패하면 경보를 삼키지 않는다 — 중복이 침묵보다 낫다."""
    from core import cycle_observability as co

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("database.models.get_session", _boom)
    gaps = [date(2026, 8, 18)]
    assert co.unreported_snapshot_gaps("acct", gaps) == gaps


def test_no_gaps_is_noop():
    from core import cycle_observability as co
    assert co.unreported_snapshot_gaps("acct", []) == []


# ------------------------------------------------------------- 적립 이행

def test_contribution_plan_absent_is_not_flagged():
    assert summarize_contribution_plan(
        "b", None, None, date(2026, 1, 1), date(2026, 8, 26))["verdict"] == "OK"
    assert summarize_contribution_plan(
        "b", {"enabled": False}, None, date(2026, 1, 1), date(2026, 8, 26),
    )["verdict"] == "OK"


def test_missing_deposit_is_flagged_after_grace():
    """입금이 한 번도 없고 개시 후 주기+유예를 넘기면 ATTENTION."""
    plan = {"enabled": True, "cadence": "monthly", "amount": 100000}
    r = summarize_contribution_plan("kr_pocket", plan, None,
                                    date(2026, 7, 10), date(2026, 8, 26))
    assert r["verdict"] == "ATTENTION"
    assert "미실행" in r["note"]
    assert r["days_since"] == 47


def test_new_track_within_first_period_is_not_flagged():
    """개시 직후에는 아직 적립 시점이 오지 않았으므로 경보하지 않는다."""
    plan = {"enabled": True, "cadence": "monthly", "amount": 100000}
    r = summarize_contribution_plan("kr_pocket", plan, None,
                                    date(2026, 8, 1), date(2026, 8, 26))
    assert r["verdict"] == "OK"


def test_stale_deposit_is_flagged():
    plan = {"enabled": True, "cadence": "monthly", "amount": 100000}
    r = summarize_contribution_plan("kr_pocket", plan, datetime(2026, 6, 1),
                                    date(2026, 5, 1), date(2026, 8, 26))
    assert r["verdict"] == "ATTENTION"
    assert "지연" in r["note"]


def test_recent_deposit_is_ok():
    plan = {"enabled": True, "cadence": "monthly", "amount": 100000}
    r = summarize_contribution_plan("kr_pocket", plan, datetime(2026, 8, 1),
                                    date(2026, 5, 1), date(2026, 8, 26))
    assert r["verdict"] == "OK"


# --------------------------------------------------------- 운영 설정 불변식

def test_enabled_baskets_declare_deployment_band():
    """집계 배치율 밴드가 빠지면 현금 래칫이 조용히 되살아난다."""
    for name, cfg in BasketRebalancer._load_baskets_config().items():
        if not cfg.get("enabled"):
            continue
        band = (cfg.get("rebalance") or {}).get("deployment_band")
        assert band is not None, "%s: deployment_band 미선언" % name
        assert 0 < float(band) < 0.5


def test_deployment_monitoring_is_not_disabled():
    """tolerance 1.0은 감시 해제다 — 배치율 누수를 헬스가 못 본다."""
    for name, cfg in BasketRebalancer._load_baskets_config().items():
        if not cfg.get("enabled"):
            continue
        tol = (cfg.get("monitoring") or {}).get("deployment_tolerance", 0.05)
        assert float(tol) < 1.0, (
            "%s: 배치율 감시가 사실상 꺼져 있다(tolerance=%s)" % (name, tol)
        )


# ------------------------------------------------------------- 방어 자산 보충 (2026-09-23)

def _pocket(wired, positions, total_value, cash):
    holdings = {"069500": 0.5, "357870": 0.5}
    rb = _rebalancer(holdings=holdings, target_stock_weight=0.95, min_trade=50_000,
                     positions=positions, turnover=0.6)
    rb.basket["overlays"] = {"defensive_symbol": "357870"}
    rb._risk_params = {"diversification": {"min_cash_ratio": 0.05}}
    rb.basket["min_cash_ratio"] = 0.05
    wired(rb, total_value)
    rb.portfolio_mgr.get_portfolio_summary.return_value = {
        "total_value": total_value, "cash": cash,
    }
    return rb


def test_idle_cash_goes_to_defensive_parking_etf(wired):
    """kr_pocket 9/23 실제 상태: 지수 1주·파킹 3주·현금 10.4만 원 — 두 종목 다 상한에 막혀
    한 달째 현금이 놀았다. 파킹 ETF 1주는 다음 사이클 매도 규칙이 되팔지 않는 범위다."""
    prices = {"069500": 113_800, "357870": 57_900}
    positions = [_pos("069500", 122_345, 1), _pos("357870", 57_913, 3)]
    total = 113_800 + 3 * 57_900 + 103_870
    rb = _pocket(wired, positions, total, 103_870)

    orders = rb.plan_rebalance(prices)

    buys = [o for o in orders if o.action == "BUY"]
    assert [(o.symbol, o.quantity) for o in buys] == [("357870", 1)]
    assert "배치율 보충" in buys[0].reason


def test_defensive_exemption_never_applies_to_equity(wired):
    """주식 종목은 여전히 목표+허용을 넘겨 사지 않는다 — 주식 비중이 설계를 넘는다."""
    prices = {"069500": 113_800, "357870": 57_900}
    # 파킹 ETF가 이미 목표 이상이라 보충할 곳은 지수뿐인 상태
    positions = [_pos("069500", 113_800, 1), _pos("357870", 57_900, 4)]
    total = 113_800 + 4 * 57_900 + 120_000
    rb = _pocket(wired, positions, total, 120_000)

    for o in rb.plan_rebalance(prices):
        if o.action == "BUY" and o.symbol == "069500":
            investable = total * 0.95
            projected = (113_800 + o.quantity * o.price) / investable
            assert projected <= 0.5 + 0.08 + 1e-9


def test_defensive_topup_stays_below_sell_back_threshold(wired):
    """다음 사이클에 되팔릴 만큼(초과분 ≥ max(min_trade, 1주)) 사지는 않는다."""
    prices = {"069500": 113_800, "357870": 57_900}
    positions = [_pos("069500", 113_800, 1), _pos("357870", 57_900, 3)]
    total = 113_800 + 3 * 57_900 + 103_870
    rb = _pocket(wired, positions, total, 103_870)
    investable = total * 0.95
    for o in rb.plan_rebalance(prices):
        if o.action == "BUY" and o.symbol == "357870":
            over = (3 + o.quantity) * 57_900 - investable * 0.5
            assert over < max(50_000, 57_900)
