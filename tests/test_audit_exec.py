"""주문 실행 경로 회귀 테스트 (2026-09-23 점검).

- 노출 상한·낙폭 가드를 계획과 같은 시가로 잰다(원가로 재면 하락장 보충 매수가 거부되고,
  평가익이 쌓이면 가짜 '일일 손실'로 매수가 막힌다).
- 목표 비중 주문은 계좌 낙폭 가드 대신 바스켓 낙폭 규칙을 따른다.
- 매수 거부 사유를 로그·이벤트로 남긴다(요약의 '실패 N건'만으로는 원인을 모른다).
- 손절 청산은 최소 보유 기간보다 우선한다(호출부가 emergency로 명시).
- 재매수 차단 종목의 빈 슬롯이 드리프트 트리거를 매일 켜 두지 않는다.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.config_loader import Config


@pytest.fixture
def executor(monkeypatch):
    Config._instance = None
    from database.models import init_database

    init_database()
    from core.order_executor import OrderExecutor

    ex = OrderExecutor(account_key="audit_exec_test")
    ex.config.risk_params["drawdown"]["max_portfolio_mdd"] = 0.15
    ex.config.risk_params["drawdown"]["max_daily_loss"] = 0.03
    return ex


def _fake_pm(summary, seen=None):
    class FakePortfolioManager:
        def __init__(self, config=None, account_key=""):
            pass

        def get_portfolio_summary(self, current_prices=None):
            if seen is not None:
                seen.append(current_prices)
            return dict(summary)

    return FakePortfolioManager


# ------------------------------------------------------------ 목표 비중 주문과 낙폭 가드

def test_mdd_breach_blocks_discretionary_buy(executor, monkeypatch):
    monkeypatch.setattr("core.portfolio_manager.PortfolioManager",
                        _fake_pm({"total_value": 8_500_000, "mdd": 16.0}))
    monkeypatch.setattr(executor, "_daily_loss_baseline", lambda: None)
    r = executor._drawdown_pre_order_check("BUY")
    assert r["allowed"] is False and r["drawdown_guard_type"] == "mdd"


def test_mdd_breach_is_delegated_for_weight_policy_orders(executor, monkeypatch):
    seen = []
    monkeypatch.setattr("core.portfolio_manager.PortfolioManager",
                        _fake_pm({"total_value": 8_500_000, "mdd": 16.0}, seen))
    monkeypatch.setattr(executor, "_daily_loss_baseline", lambda: None)

    r = executor._drawdown_pre_order_check(
        "BUY", mark_prices={"005930": 70_000.0}, delegated=True,
    )

    assert r["allowed"] is True
    assert "MDD" in r["drawdown_guard_delegated"]
    assert seen == [{"005930": 70_000.0}]   # 판정 자체는 시가로 계산


def test_infrastructure_failure_is_not_delegated(executor, monkeypatch):
    """평가 불가·설정 오류는 넘기지 않는다 — 판단 근거가 없으면 여전히 막는다."""
    executor.config.risk_params["drawdown"]["max_portfolio_mdd"] = "x"
    r = executor._drawdown_pre_order_check("BUY", delegated=True)
    assert r["allowed"] is False and r["drawdown_guard_type"] == "invalid_config"


# ------------------------------------------------------------ 시가 노출

def test_marked_position_value_uses_market_price(executor):
    pos = SimpleNamespace(symbol="005930", quantity=10, avg_price=90_000, total_invested=900_000)
    assert executor._marked_position_value(pos, {"005930": 70_000}) == pytest.approx(700_000)
    # 가격이 없으면 기존 기준(투자원금)
    assert executor._marked_position_value(pos, {"000660": 1}) == pytest.approx(900_000)
    assert executor._marked_position_value(pos, None) == pytest.approx(900_000)


def test_exposure_check_sees_market_values(executor, monkeypatch):
    """원가 기준이면 거부되는 보충 매수가 시가 기준으로는 설계 범위 안이다.

    원가: 보유 5,936,700 / 총자산 9,556,410 = 62.1% → +269,000 매수 시 64.9% > 63%
    시가: 보유 4,760,200 / 총자산 8,379,910 = 56.8% → +269,000 매수 시 60.0% ≤ 63%
    """
    positions = [SimpleNamespace(symbol="005490", quantity=17, avg_price=349_217.6,
                                 total_invested=5_936_700)]
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kw: positions)
    captured = {}

    def _fake_div(**kw):
        captured.update(kw)
        return {"can_buy": False, "reason": "stop-here"}

    monkeypatch.setattr(executor.risk_manager, "check_diversification", _fake_div)
    monkeypatch.setattr(executor, "_pre_order_check", lambda **kw: {"allowed": True})
    monkeypatch.setattr(executor, "_report_buy_rejection", lambda *a, **k: None)
    # 장 초반·마감 진입 차단 시간대 판정은 실제 시계를 본다 — 실행 시각에 따라 노출
    # 판정까지 가지 못하므로 고정한다.
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)

    executor.execute_buy_quantity(
        symbol="005490", price=269_000, quantity=1, capital=8_379_910,
        available_cash=3_619_710, weight_policy_managed=True,
        exposure_limits={"max_investment_ratio": 0.63},
        mark_prices={"005490": 280_011.76},
    )

    assert captured["current_invested"] == pytest.approx(17 * 280_011.76)
    assert captured["total_value"] == pytest.approx(8_379_910)


# ------------------------------------------------------------ 거부 사유 기록

def test_buy_rejection_is_logged_and_recorded_once_per_day(executor, monkeypatch):
    import core.cycle_observability as co

    recorded = []
    monkeypatch.setattr(
        co, "record_event_once_per_day",
        lambda event_type, message, **kw: recorded.append((event_type, kw)) or True,
    )
    monkeypatch.setattr(
        executor, "_execute_buy_quantity_impl",
        lambda **kw: {"success": False, "reason": "전체 투자 비중 63% 초과"},
    )

    r = executor.execute_buy_quantity(
        symbol="005490", price=269_000, quantity=1, capital=1, available_cash=1,
        strategy="basket_rebalance:kr_diversified_hold",
    )

    assert r["success"] is False
    assert recorded and recorded[0][0] == "ORDER_REJECTED"
    assert recorded[0][1]["symbol"] == "005490"
    assert recorded[0][1]["dedupe_key"] == "전체 투자 비중 63% 초과"


# ------------------------------------------------------------ 손절 청산 우선

def test_emergency_flag_reaches_sell_impl(executor, monkeypatch):
    seen = {}

    def _impl(*args, emergency=False, **kw):
        seen["emergency"] = emergency
        return {"success": True}

    monkeypatch.setattr(executor, "_execute_sell_impl", _impl)
    executor.execute_sell("005930", 70_000, 1, reason="리밸런싱: RISK_EXIT STOP_LOSS: 손절",
                          emergency=True)
    assert seen["emergency"] is True


# ------------------------------------------------------------ 드리프트 트리거

def test_cooldown_slot_does_not_arm_drift_trigger(monkeypatch):
    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "t"
    rb.basket = {"risk": {"reentry_cooldown_days": 60}}
    rb.account_key = "acct"
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    rb.rebalance_cfg = {"trigger": "drift", "drift_threshold": 0.08, "deployment_band": 0.03}
    # 손절로 비운 012330 슬롯만 크게 벌어져 있고 나머지는 거의 목표대로
    monkeypatch.setattr(rb, "calculate_drift", lambda prices=None: {
        "012330": {"drift": 0.111}, "005930": {"drift": -0.01}, "035720": {"drift": 0.02},
    })
    monkeypatch.setattr(rb, "_deployment_gap", lambda prices=None: -0.01)
    monkeypatch.setattr(
        "core.basket_rebalancer.symbols_in_reentry_cooldown",
        lambda *a, **k: {"012330": "손절 후 재매수 차단"},
    )

    should, reason = rb.should_rebalance({})

    assert should is False, reason


def test_tradable_drift_still_arms_trigger(monkeypatch):
    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "t"
    rb.basket = {}
    rb.account_key = "acct"
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    rb.rebalance_cfg = {"trigger": "drift", "drift_threshold": 0.08}
    monkeypatch.setattr(rb, "calculate_drift", lambda prices=None: {
        "012330": {"drift": 0.111}, "005930": {"drift": -0.09},
    })
    monkeypatch.setattr(
        "core.basket_rebalancer.symbols_in_reentry_cooldown",
        lambda *a, **k: {"012330": "손절 후 재매수 차단"},
    )

    should, _ = rb.should_rebalance({})
    assert should is True


# ------------------------------------------------------------ 피크 오염

def test_peak_only_advances_on_market_priced_summary(monkeypatch):
    """가격 없이(평균단가로) 부른 요약이 피크를 올리면 다음 스냅샷이 그 값을 저장한다."""
    from database.models import init_database

    init_database()
    from core.portfolio_manager import PortfolioManager

    pos = SimpleNamespace(symbol="005930", quantity=10, avg_price=100_000.0)
    monkeypatch.setattr("core.portfolio_manager.get_all_positions", lambda **kw: [pos])
    pm = PortfolioManager(account_key="audit_peak_test", initial_capital=1_000_000)
    start_peak = pm._peak_value

    pm.get_portfolio_summary()                         # 원가 평가 — 총액 2,000,000
    assert pm._peak_value == pytest.approx(start_peak)

    pm.get_portfolio_summary(current_prices={"005930": 90_000.0})   # 시가 1,900,000
    assert pm._peak_value == pytest.approx(1_900_000)
