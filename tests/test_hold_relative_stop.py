"""대형주 관찰 계좌의 시장 대비 손절과 계좌 낙폭 가드 위임 (2026-10-20).

배경: 평균 매입가 대비 -25% 손절은 시장 전체가 빠질 때 연달아 걸렸다(9/21 현대모비스,
10/15 삼성전자·현대차 — 그 사이 KODEX 200도 -30% 가까이 하락). 그렇게 판 현금은 전역
계좌 낙폭 15% 가드에 막혀 다시 들어가지 못했고, 주식 비중이 설계 60%에서 45%로 내려앉았다.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.basket_risk import (
    basket_risk_levels,
    delegates_account_drawdown_guard,
    evaluate_basket_stops,
    relative_stop_basis,
    stop_basis,
)

REL = {"risk": {"stop_loss_pct": 0.25, "stop_basis": "relative", "stop_benchmark": "069500"}}


def _pos(symbol, avg_price, quantity=1):
    return SimpleNamespace(symbol=symbol, avg_price=avg_price, quantity=quantity, highest_price=0)


def _trade(symbol, action, quantity, when):
    return SimpleNamespace(symbol=symbol, action=action, quantity=quantity, executed_at=when)


# ------------------------------------------------------------------ 설정 해석

def test_stop_basis_defaults_to_absolute_and_unknown_falls_back():
    assert stop_basis({"risk": {"stop_loss_pct": 0.25}}) == ("absolute", None)
    assert stop_basis(REL) == ("relative", "069500")
    assert stop_basis({"risk": {"stop_basis": "relative"}}) == ("relative", "069500")
    assert stop_basis({"risk": {"stop_basis": "relatve"}}) == ("absolute", None)


def test_account_guard_delegation_sources():
    assert delegates_account_drawdown_guard({}) is False
    assert delegates_account_drawdown_guard({"risk": {"account_drawdown_guard": "block"}}) is False
    assert delegates_account_drawdown_guard({"risk": {"account_drawdown_guard": "delegate"}}) is True
    assert delegates_account_drawdown_guard({"overlays": {"drawdown_guard": {"enabled": True}}}) is True
    # 오타는 기존 동작(차단)으로 — 조용히 가드를 끄면 안 된다
    assert delegates_account_drawdown_guard({"risk": {"account_drawdown_guard": "delegat"}}) is False


def test_relative_basis_records_no_fixed_stop_price():
    """상대 손절은 시장이 움직이면 손절 가격도 움직인다 — 고정 가격을 장부에 적지 않는다."""
    levels = basket_risk_levels(REL, 300_000)
    assert levels["stop_loss_price"] is None
    assert basket_risk_levels({"risk": {"stop_loss_pct": 0.25}}, 300_000)["stop_loss_price"] == 225_000


# ------------------------------------------------------------- 매입 시점 기준

def test_relative_basis_is_quantity_weighted_like_average_price():
    closes = {date(2026, 6, 10): 120_000, date(2026, 9, 21): 100_000}
    trades = [
        _trade("005930", "BUY", 2, datetime(2026, 6, 10, 10, 1)),
        _trade("005930", "BUY", 1, datetime(2026, 9, 21, 10, 8)),
    ]
    basis = relative_stop_basis(trades, closes)
    assert basis["005930"]["quantity"] == 3
    assert basis["005930"]["basis"] == pytest.approx((120_000 * 2 + 100_000) / 3)


def test_full_sell_starts_a_new_holding_period():
    closes = {date(2026, 6, 10): 120_000, date(2026, 8, 7): 110_000, date(2026, 8, 8): 105_000}
    trades = [
        _trade("005380", "BUY", 1, datetime(2026, 6, 10, 10)),
        _trade("005380", "SELL", 1, datetime(2026, 8, 7, 10)),
        _trade("005380", "BUY", 1, datetime(2026, 8, 8, 10)),
    ]
    assert relative_stop_basis(trades, closes)["005380"]["basis"] == pytest.approx(105_000)


def test_partial_sell_keeps_basis_and_missing_close_voids_it():
    closes = {date(2026, 6, 10): 120_000}
    trades = [
        _trade("055550", "BUY", 4, datetime(2026, 6, 10, 10)),
        _trade("055550", "SELL", 1, datetime(2026, 7, 1, 10)),
        _trade("035720", "BUY", 3, datetime(2026, 1, 5, 10)),   # 종가 자료 없음
    ]
    basis = relative_stop_basis(trades, closes)
    assert basis["055550"] == {"basis": pytest.approx(120_000), "quantity": 3, "missing": []}
    assert basis["035720"]["basis"] is None
    assert basis["035720"]["missing"] == ["2026-01-05"]


def test_basis_uses_previous_close_within_a_week_only():
    closes = {date(2026, 10, 2): 100_000}
    trades = [_trade("012330", "BUY", 1, datetime(2026, 10, 6, 10))]   # 연휴 뒤 첫 거래일
    assert relative_stop_basis(trades, closes)["012330"]["basis"] == pytest.approx(100_000)
    far = [_trade("012330", "BUY", 1, datetime(2026, 10, 20, 10))]
    assert relative_stop_basis(far, closes)["012330"]["basis"] is None


# ------------------------------------------------------------------ 손절 판단

def test_market_wide_drop_does_not_trigger_relative_stop():
    """2026-10-15 재현: 삼성전자 -27%였지만 같은 기간 KODEX 200도 -30% — 시장보다 낫다."""
    ctx = {"benchmark": "069500", "price": 86_755, "basis": {"005930": 123_546}}
    hits = evaluate_basket_stops(REL, [_pos("005930", 291_667, 3)], {"005930": 213_000}, ctx)
    assert hits == []
    # 같은 상황을 절대 기준으로 재면 손절이 걸린다(바뀐 동작의 근거)
    assert evaluate_basket_stops({"risk": {"stop_loss_pct": 0.25}}, [_pos("005930", 291_667, 3)],
                                 {"005930": 213_000})


def test_idiosyncratic_drop_triggers_relative_stop_even_in_flat_market():
    ctx = {"benchmark": "069500", "price": 100_000, "basis": {"035720": 100_000}}
    hits = evaluate_basket_stops(REL, [_pos("035720", 40_000, 10)], {"035720": 29_000}, ctx)
    assert [h["action"] for h in hits] == ["STOP_LOSS"]
    assert hits[0]["basis"] == "relative"
    assert hits[0]["relative_return"] == pytest.approx(-0.275)
    assert "시장 대비 손절" in hits[0]["reason"]
    # 지금 시장 수준에서 걸리는 가격(참고): 40,000 × 0.75 × 1.0
    assert hits[0]["level"] == 30_000


def test_relative_stop_can_fire_when_stock_lags_a_rising_market():
    """시장이 +40%일 때 제자리인 종목은 시장 대비 -29% — 포트폴리오를 끌어내리는 종목이다."""
    ctx = {"benchmark": "069500", "price": 140_000, "basis": {"051910": 100_000}}
    hits = evaluate_basket_stops(REL, [_pos("051910", 300_000)], {"051910": 300_000}, ctx)
    assert hits and hits[0]["relative_return"] == pytest.approx(1 / 1.4 - 1, abs=1e-4)


def test_missing_current_benchmark_defers_decision_instead_of_absolute_sell():
    """지금 시장 수준을 모르면 판단을 미룬다 — 절대 기준으로 팔면 자료 장애가 시장 전체
    하락 손절을 다시 만든다."""
    ctx = {"benchmark": "069500", "price": None, "basis": {"005930": 123_546}}
    assert evaluate_basket_stops(REL, [_pos("005930", 291_667, 3)], {"005930": 150_000}, ctx) == []
    assert evaluate_basket_stops(REL, [_pos("005930", 291_667, 3)], {"005930": 150_000}, None) == []


def test_missing_entry_basis_falls_back_to_absolute_for_that_symbol_only():
    ctx = {"benchmark": "069500", "price": 86_755, "basis": {"005930": 123_546}}
    positions = [_pos("005930", 291_667, 3), _pos("035420", 229_500, 4)]
    hits = evaluate_basket_stops(REL, positions, {"005930": 213_000, "035420": 160_000}, ctx)
    assert [h["symbol"] for h in hits] == ["035420"]
    assert hits[0]["basis"] == "absolute"
    assert "절대 기준으로 판단" in hits[0]["reason"]


# ------------------------------------------------------------- 리밸런서 연결

class _Collector:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def fetch_korean_stock(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return self.frame


def _rebalancer(basket, monkeypatch, positions, trades, frame, snapshot=None):
    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "hold"
    rb.basket = basket
    rb.account_key = "basket_rebalance:hold"
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    rb.data_collector = _Collector(frame)
    rb._market_snapshot = snapshot or {}
    monkeypatch.setattr("core.basket_rebalancer.get_all_positions", lambda **kw: positions)
    monkeypatch.setattr("database.repositories.get_trade_history", lambda **kw: list(trades))
    events = []
    monkeypatch.setattr(
        "core.cycle_observability.record_event_once_per_day",
        lambda *a, **k: events.append((a, k)),
    )
    return rb, events


def _bench_frame():
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-06-10", "2026-10-19", "2026-10-20"]),
        "close": [123_546.0, 88_000.0, 86_755.0],
    })


def test_plan_risk_exits_uses_market_relative_context(monkeypatch):
    positions = [_pos("005930", 291_667, 3), _pos("035720", 37_262.5, 18)]
    trades = [
        _trade("005930", "BUY", 3, datetime(2026, 6, 10, 10)),
        _trade("035720", "BUY", 18, datetime(2026, 6, 10, 10)),
    ]
    snapshot = {"005930": {"quote_date": "2026-10-20"}, "035720": {"quote_date": "2026-10-20"}}
    rb, events = _rebalancer(REL, monkeypatch, positions, trades, _bench_frame(), snapshot)

    # 삼성전자 -27%(시장 -30%) → 유지, 카카오 -47%(시장 대비 -24%) → 유지
    assert rb.plan_risk_exits(prices={"005930": 213_000, "035720": 19_800}) == []
    # 카카오가 시장보다 25% 넘게 더 빠지면 판다
    orders = rb.plan_risk_exits(prices={"005930": 213_000, "035720": 18_000})
    assert [(o.symbol, o.action, o.quantity) for o in orders] == [("035720", "SELL", 18)]
    assert "RISK_EXIT STOP_LOSS" in orders[0].reason
    assert events == []


def test_stale_benchmark_defers_relative_stops_and_records_issue(monkeypatch):
    positions = [_pos("005930", 291_667, 3)]
    trades = [_trade("005930", "BUY", 3, datetime(2026, 6, 10, 10))]
    snapshot = {"005930": {"quote_date": "2026-10-21"}}   # 시세는 하루 더 새 것
    rb, events = _rebalancer(REL, monkeypatch, positions, trades, _bench_frame(), snapshot)

    assert rb.plan_risk_exits(prices={"005930": 100_000}) == []
    assert events and "판단 보류" in events[0][0][1]


def test_ledger_mismatch_falls_back_to_absolute_for_that_symbol(monkeypatch):
    positions = [_pos("005930", 291_667, 4)]                 # 장부 4주, 매수 기록은 3주
    trades = [_trade("005930", "BUY", 3, datetime(2026, 6, 10, 10))]
    snapshot = {"005930": {"quote_date": "2026-10-20"}}
    rb, events = _rebalancer(REL, monkeypatch, positions, trades, _bench_frame(), snapshot)

    orders = rb.plan_risk_exits(prices={"005930": 213_000})
    assert [o.symbol for o in orders] == ["005930"]
    assert "절대 기준" in orders[0].reason
    assert events and "보유 수량이 달라" in events[0][0][1]


def test_absolute_basket_does_not_fetch_benchmark(monkeypatch):
    positions = [_pos("005930", 291_667, 3)]
    rb, _ = _rebalancer({"risk": {"stop_loss_pct": 0.25}}, monkeypatch, positions, [], _bench_frame())
    orders = rb.plan_risk_exits(prices={"005930": 213_000})
    assert [o.symbol for o in orders] == ["005930"]
    assert rb.data_collector.calls == []


def test_rebalancer_delegation_follows_basket_policy():
    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket = {"risk": {"account_drawdown_guard": "delegate"}}
    assert rb._has_drawdown_rule() is True
    rb.basket = {"risk": {"stop_loss_pct": 0.25}}
    assert rb._has_drawdown_rule() is False


def test_entry_status_is_not_blocked_by_global_mdd_when_delegated():
    from core.basket_observability import snapshot_entry_status

    snap = {"created_at": "2026-10-20 10:11", "mdd": 18.3, "daily_return": 0.5}
    limits = {"drawdown": {"max_portfolio_mdd": 0.15, "max_daily_loss": 0.03}}
    blocked = snapshot_entry_status(snap, {"risk": {}}, limits)
    assert blocked["status"] == "BLOCKED"
    delegated = snapshot_entry_status(snap, {"risk": {"account_drawdown_guard": "delegate"}}, limits)
    assert delegated["status"] != "BLOCKED"


# ----------------------------------------------------------- 운영 설정 고정

def test_shipped_hold_basket_uses_relative_stop_and_delegation():
    """운영 설정에서 이 결정이 조용히 되돌아가지 않게 고정한다(근거: HOLD_RISK_REVIEW_20261020)."""
    from core.basket_rebalancer import BasketRebalancer

    cfg = BasketRebalancer._load_baskets_config()["kr_diversified_hold"]
    assert stop_basis(cfg) == ("relative", "069500")
    assert delegates_account_drawdown_guard(cfg) is True
    assert float(cfg["risk"]["stop_loss_pct"]) == 0.25
    assert int(cfg["risk"]["reentry_cooldown_days"]) == 60
