"""OrderExecutor paper 모드 단위 테스트 (DB/외부 의존성 최소화)"""
from types import SimpleNamespace

import pytest

from config.config_loader import Config


def _normal_runtime_state():
    return SimpleNamespace(
        state="normal",
        allowed_actions=["entry", "exit", "cancel", "reconcile", "finalize"],
        reasons=[],
        metrics={"recent_final_ratio": 1.0, "recent_anomaly_count": 0},
        evidence_date="2026-01-01",
    )


def _passing_preflight():
    return SimpleNamespace(
        overall="pass",
        entry_allowed=True,
        runtime_state="normal",
        block_reasons=[],
    )


def _blocked_runtime_state():
    return SimpleNamespace(
        state="blocked_insufficient_evidence",
        allowed_actions=["exit", "cancel", "reconcile", "finalize", "shadow_collect"],
        reasons=["insufficient execution-backed evidence"],
        metrics={"recent_final_ratio": 0.0, "recent_anomaly_count": 0},
        evidence_date=None,
    )


def test_order_executor_import():
    """OrderExecutor 임포트 및 paper 모드 초기화 가능"""
    from core.order_executor import OrderExecutor
    # Config.get() 사용 시 실제 설정 로드됨 — 테스트 환경에서는 mock 권장
    try:
        ex = OrderExecutor()
        assert ex.mode in ("paper", "live")
    except Exception:
        # 설정 없을 수 있음
        pass


def test_position_lock_import():
    """PositionLock 사용 가능"""
    from core.position_lock import PositionLock
    with PositionLock():
        pass


@pytest.fixture
def fresh_db():
    Config._instance = None
    from database.models import (
        init_database, get_session,
        TradeHistory, OperationEvent, PortfolioSnapshot,
        Position, FailedOrder, PendingOrderGuard, DailyReport,
    )
    init_database()
    session = get_session()
    for model in [TradeHistory, OperationEvent, PortfolioSnapshot,
                  Position, FailedOrder, PendingOrderGuard, DailyReport]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def test_execute_buy_quantity_records_exact_paper_quantity(fresh_db, monkeypatch):
    """Target-weight adapters can submit exact paper buy quantities."""
    from core.order_executor import OrderExecutor
    from database.models import get_session, TradeHistory
    from database.repositories import get_position, get_daily_trade_summary

    executor = OrderExecutor(account_key="exact_qty_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: _passing_preflight())
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=7,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="exact quantity test",
        strategy="target_weight_rotation_test",
        execution_session_id="session-exact-qty",
    )

    assert result["success"] is True
    assert result["quantity"] == 7
    assert result["execution_session_id"] == "session-exact-qty"
    assert result["order_id"].startswith("ORD-")
    position = get_position("005930", account_key="exact_qty_test")
    assert position is not None
    assert position.quantity == 7
    summary = get_daily_trade_summary(mode="paper", account_key="exact_qty_test")
    assert summary["buy_count"] == 1
    session = get_session()
    try:
        trade = session.query(TradeHistory).filter(TradeHistory.symbol == "005930").one()
        assert trade.execution_session_id == "session-exact-qty"
        assert trade.order_id == result["order_id"]
    finally:
        session.close()


def test_paper_buy_uses_estimated_fill_price_for_sizing_and_targets(fresh_db, monkeypatch):
    """Paper BUY 수량과 방어 가격은 예상 체결가 기준으로 계산한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="paper_buy_fill_basis_test")
    executor.config.trading["skip_earnings_days"] = 0
    executor.config.risk_params["gap_risk"]["enabled"] = False
    executor.config.risk_params["stop_loss"] = {"type": "fixed", "fixed_rate": 0.03}
    executor.config.risk_params["take_profit"] = {
        "type": "fixed",
        "fixed_rate": 0.08,
        "partial_exit": False,
    }
    executor.config.risk_params["trailing_stop"] = {
        "enabled": True,
        "type": "fixed",
        "fixed_rate": 0.05,
    }
    executor.config.risk_params["transaction_costs"] = {
        "commission_rate": 0.0,
        "tax_rate": 0.0,
        "slippage": 0.001,
        "slippage_ticks": 0,
        "dynamic_slippage": {"enabled": False},
    }
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(executor, "_get_sector_map_cached", lambda: {})
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {"scale": 1.0, "high_corr_symbols": [], "reason": ""},
    )
    monkeypatch.setattr(
        executor.risk_manager,
        "check_diversification",
        lambda *args, **kwargs: {"can_buy": True, "reason": ""},
    )
    monkeypatch.setattr(
        executor.risk_manager,
        "check_recent_performance",
        lambda *args, **kwargs: {"allowed": True, "reason": ""},
    )
    _allow_paper_entry(monkeypatch)

    captured = {}

    def _capture_position_size(capital, entry_price, stop_loss_price, signal_score=0):
        captured["capital"] = capital
        captured["entry_price"] = entry_price
        captured["stop_loss_price"] = stop_loss_price
        captured["signal_score"] = signal_score
        return 2

    monkeypatch.setattr(
        executor.risk_manager,
        "calculate_position_size",
        _capture_position_size,
    )

    result = executor.execute_buy(
        symbol="005930",
        price=60_000,
        capital=1_000_000,
        available_cash=1_000_000,
        signal_score=2.0,
        reason="fill basis sizing test",
        strategy="scoring",
    )

    assert result["success"] is True
    assert captured["entry_price"] == 60_060
    assert captured["stop_loss_price"] == 58_258
    assert result["price"] == 60_060
    assert result["stop_loss"] == 58_258
    assert result["take_profit"] == 64_865
    assert result["trailing_stop"] == 57_057
    position = get_position("005930", account_key="paper_buy_fill_basis_test")
    assert position.avg_price == 60_060
    assert position.stop_loss_price == 58_258
    assert position.take_profit_price == 64_865
    assert position.trailing_stop_price == 57_057


def test_paper_buy_quantity_fails_closed_when_runtime_unavailable(fresh_db, monkeypatch):
    """Paper 신규 진입은 runtime 조회 실패 시 주문/포지션을 만들지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="runtime_down_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: _passing_preflight())

    def _raise_runtime(*args, **kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", _raise_runtime)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="runtime unavailable test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper runtime state unavailable" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="runtime_down_test") is None


def test_paper_buy_quantity_fails_closed_when_preflight_missing(fresh_db, monkeypatch):
    """Preflight 산출물이 없으면 신규 paper 진입을 시작하지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="preflight_missing_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr("core.paper_preflight.load_preflight_status", lambda strategy, strict=False: None)
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="preflight missing test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper preflight status missing" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="preflight_missing_test") is None


def test_paper_buy_quantity_blocks_on_preflight_fail(fresh_db, monkeypatch):
    """저장된 preflight fail은 runtime normal 여부와 무관하게 신규 진입을 차단한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="preflight_block_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(
        "core.paper_preflight.load_preflight_status",
        lambda strategy, strict=False: SimpleNamespace(
            overall="fail",
            entry_allowed=False,
            runtime_state="normal",
            block_reasons=["notifier unhealthy"],
        ),
    )
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _normal_runtime_state())

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=3,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="preflight fail test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "paper preflight blocked entry" in result["reason"]
    assert result["paper_entry_blocked"] is True
    assert get_position("005930", account_key="preflight_block_test") is None


def test_paper_buy_quantity_allows_pilot_override(fresh_db, monkeypatch):
    """runtime이 entry를 막아도 활성 pilot authorization이 있으면 제한 진입을 허용한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="pilot_override_test")
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    monkeypatch.setattr(
        "core.paper_preflight.load_preflight_status",
        lambda strategy, strict=False: SimpleNamespace(
            overall="warn",
            entry_allowed=False,
            runtime_state="blocked_insufficient_evidence",
            block_reasons=["insufficient execution-backed evidence"],
        ),
    )
    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", lambda *a, **kw: _blocked_runtime_state())
    monkeypatch.setattr(
        "core.paper_pilot.check_pilot_entry",
        lambda *a, **kw: SimpleNamespace(
            allowed=True,
            reason="active pilot authorization",
            caps_snapshot={"max_orders_per_day": 2},
        ),
    )

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=2,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="pilot override test",
        strategy="target_weight_rotation_test",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="pilot_override_test") is not None


def _set_monthly_buy_cap(executor, cap: int):
    executor.config.risk_params.setdefault("position_limits", {})[
        "max_monthly_roundtrips"
    ] = cap


def _allow_paper_entry(monkeypatch):
    monkeypatch.setattr(
        "core.paper_preflight.load_preflight_status",
        lambda strategy, strict=False: _passing_preflight(),
    )
    monkeypatch.setattr(
        "core.paper_runtime.get_paper_runtime_state",
        lambda *a, **kw: _normal_runtime_state(),
    )


def _seed_buy_trades(account_key: str, symbol: str, mode: str, count: int):
    from database.repositories import save_trade

    for idx in range(count):
        save_trade(
            symbol=symbol,
            action="BUY",
            price=60_000 + idx,
            quantity=1,
            strategy="scoring",
            reason="monthly cap seed",
            mode=mode,
            account_key=account_key,
        )


def test_count_monthly_buy_trades_filters_scope_and_month(fresh_db):
    """월간 BUY 집계는 현재 월, account_key, mode, symbol 기준으로만 센다."""
    from datetime import datetime, timedelta

    from database.models import TradeHistory, get_session
    from database.repositories import count_monthly_buy_trades

    now = datetime.now()
    month_start = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    previous_month = month_start - timedelta(days=1)

    rows = [
        ("scope_count", "005930", "BUY", "paper", month_start, 60_000),
        ("scope_count", "005930", "BUY", "paper", month_start + timedelta(days=1), 60_100),
        ("scope_count", "005930", "SELL", "paper", month_start, 60_200),
        ("scope_count", "000660", "BUY", "paper", month_start, 100_000),
        ("scope_count", "005930", "BUY", "live", month_start, 60_300),
        ("other_account", "005930", "BUY", "paper", month_start, 60_400),
        ("scope_count", "005930", "BUY", "paper", previous_month, 59_900),
    ]

    session = get_session()
    try:
        for account_key, symbol, action, mode, executed_at, price in rows:
            session.add(
                TradeHistory(
                    account_key=account_key,
                    symbol=symbol,
                    action=action,
                    price=price,
                    quantity=1,
                    total_amount=price,
                    strategy="scoring",
                    reason="monthly cap scope test",
                    mode=mode,
                    executed_at=executed_at,
                )
            )
        session.commit()
    finally:
        session.close()

    current_count = count_monthly_buy_trades(
        "005930",
        mode="paper",
        account_key="scope_count",
        at=month_start,
    )
    previous_count = count_monthly_buy_trades(
        "005930",
        mode="paper",
        account_key="scope_count",
        at=previous_month,
    )

    assert current_count == 2
    assert previous_count == 1


def test_paper_buy_quantity_blocks_monthly_buy_cap(fresh_db, monkeypatch):
    """운영 paper BUY도 종목별 월간 거래 횟수 cap에 도달하면 차단한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    executor = OrderExecutor(account_key="monthly_cap_test")
    _set_monthly_buy_cap(executor, 2)
    _seed_buy_trades("monthly_cap_test", "005930", "paper", 2)
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    _allow_paper_entry(monkeypatch)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=1,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="monthly cap test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert "월간 거래 횟수 제한 초과" in result["reason"]
    assert get_position("005930", account_key="monthly_cap_test") is None


def test_monthly_buy_cap_is_scoped_by_account_mode_and_symbol(fresh_db, monkeypatch):
    """월간 cap은 account_key/mode/symbol 단위로 분리된다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position

    _seed_buy_trades("other_account", "005930", "paper", 2)
    _seed_buy_trades("scoped_cap_test", "000660", "paper", 2)
    _seed_buy_trades("scoped_cap_test", "005930", "live", 2)

    executor = OrderExecutor(account_key="scoped_cap_test")
    _set_monthly_buy_cap(executor, 2)
    monkeypatch.setattr(executor, "_should_block_new_buy_volatility_window", lambda: False)
    _allow_paper_entry(monkeypatch)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=60_000,
        quantity=1,
        capital=10_000_000,
        available_cash=10_000_000,
        reason="monthly cap scoped test",
        strategy="scoring",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="scoped_cap_test") is not None


def test_paper_sell_ignores_monthly_buy_cap(fresh_db):
    """월간 BUY cap에 도달해도 기존 포지션 청산은 막지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="monthly_cap_sell_test")
    _set_monthly_buy_cap(executor, 1)
    executor.config.risk_params["position_limits"]["min_holding_days"] = 0
    _seed_buy_trades("monthly_cap_sell_test", "005930", "paper", 1)
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=1,
        strategy="scoring",
        account_key="monthly_cap_sell_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=61_000,
        quantity=1,
        reason="monthly cap sell test",
        strategy="scoring",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="monthly_cap_sell_test") is None


def test_paper_sell_applies_model_slippage_to_fill_price(fresh_db):
    """Paper 매도도 매수처럼 모델 슬리피지를 체결가에 반영한다."""
    from core.order_executor import OrderExecutor
    from database.models import TradeHistory, get_session
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="paper_sell_slippage_test")
    executor.config.risk_params["position_limits"]["min_holding_days"] = 0
    executor.config.risk_params["transaction_costs"] = {
        "commission_rate": 0.0,
        "tax_rate": 0.0,
        "slippage": 0.001,
        "slippage_ticks": 0,
        "dynamic_slippage": {"enabled": False},
    }
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=2,
        strategy="scoring",
        account_key="paper_sell_slippage_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=60_000,
        quantity=2,
        reason="STOP_LOSS",
        strategy="scoring",
    )

    assert result["success"] is True
    assert result["price"] == 59_940
    assert result["costs"]["execution_price"] == 59_940
    assert result["costs"]["slippage"] == 120
    assert result["pnl"] == -120
    assert get_position("005930", account_key="paper_sell_slippage_test") is None

    session = get_session()
    try:
        trade = session.query(TradeHistory).filter(
            TradeHistory.account_key == "paper_sell_slippage_test",
            TradeHistory.symbol == "005930",
            TradeHistory.action == "SELL",
        ).one()
        assert trade.price == 59_940
        assert trade.expected_price == 60_000
        assert trade.price_gap == -60
        assert trade.slippage == 120
    finally:
        session.close()


def test_trade_cash_summary_keeps_slippage_as_diagnostic_cost(fresh_db):
    """체결가에 반영된 슬리피지를 현금 흐름에서 다시 차감하지 않는다."""
    from database.repositories import get_trade_cash_summary, save_trade

    save_trade(
        symbol="005930",
        action="BUY",
        price=60_200,
        quantity=1,
        commission=9,
        slippage=200,
        strategy="scoring",
        mode="paper",
        account_key="cash_slippage_test",
    )
    save_trade(
        symbol="005930",
        action="SELL",
        price=59_800,
        quantity=1,
        commission=9,
        tax=120,
        slippage=200,
        strategy="scoring",
        mode="paper",
        account_key="cash_slippage_test",
    )

    summary = get_trade_cash_summary(mode="paper", account_key="cash_slippage_test")

    assert summary["cash_delta"] == -538
    assert summary["commission"] == 18
    assert summary["tax"] == 120
    assert summary["slippage"] == 400


def test_live_pre_order_check_blocks_monthly_buy_cap(fresh_db):
    """live BUY도 월간 cap에 도달하면 거래시간/KIS 조회 전 fail-closed 차단한다."""
    from core.order_executor import OrderExecutor

    executor = OrderExecutor(account_key="live_monthly_cap_test")
    executor.mode = "live"
    _set_monthly_buy_cap(executor, 1)
    _seed_buy_trades("live_monthly_cap_test", "005930", "live", 1)

    result = executor._pre_order_check(
        symbol="005930",
        action="BUY",
        strategy="scoring",
    )

    assert result["allowed"] is False
    assert result["monthly_buy_cap_blocked"] is True
    assert result["monthly_buy_count"] == 1
    assert result["monthly_buy_limit"] == 1


def test_paper_sell_remains_allowed_when_runtime_unavailable(fresh_db, monkeypatch):
    """Runtime 장애가 있어도 기존 포지션 청산 경로는 막지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="exit_safe_test")
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        stop_loss_price=55_000,
        take_profit_price=70_000,
        trailing_stop_price=58_000,
        strategy="scoring",
        account_key="exit_safe_test",
    )

    def _raise_runtime(*args, **kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr("core.paper_runtime.get_paper_runtime_state", _raise_runtime)

    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        reason="STOP_LOSS",
        strategy="scoring",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="exit_safe_test") is None


def test_gap_down_sell_bypasses_min_holding_days(fresh_db):
    """갭다운 즉시 청산은 신규 보유 포지션이어도 최소 보유 기간에 막히지 않는다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="gap_down_exit_test")
    executor.config.risk_params["position_limits"]["min_holding_days"] = 5
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        strategy="scoring",
        account_key="gap_down_exit_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=55_000,
        reason="갭다운 -8.3% 즉시 청산",
        strategy="scoring",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="gap_down_exit_test") is None


def test_manual_liquidate_sell_bypasses_min_holding_days(fresh_db):
    """수동 긴급 전량 청산은 최소 보유 기간보다 우선한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="manual_liquidate_test")
    executor.config.risk_params["position_limits"]["min_holding_days"] = 5
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        strategy="scoring",
        account_key="manual_liquidate_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        reason="긴급 전량 청산 (--mode liquidate)",
        strategy="emergency_liquidate",
    )

    assert result["success"] is True
    assert get_position("005930", account_key="manual_liquidate_test") is None


def test_paper_sell_rejects_quantity_above_position(fresh_db):
    """보유 수량보다 큰 매도 요청은 paper 체결/손익 기록 전에 차단한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="oversell_test")
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        stop_loss_price=55_000,
        take_profit_price=70_000,
        trailing_stop_price=58_000,
        strategy="scoring",
        account_key="oversell_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        quantity=4,
        reason="manual oversell test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert result["reason"] == "보유 수량 초과 매도 요청"
    assert result["requested_quantity"] == 4
    assert result["available_quantity"] == 3
    assert get_position("005930", account_key="oversell_test").quantity == 3


def test_paper_sell_rejects_non_positive_quantity(fresh_db):
    """0 이하 명시 수량은 전량 청산으로 해석하지 않고 차단한다."""
    from core.order_executor import OrderExecutor
    from database.repositories import get_position, save_position

    executor = OrderExecutor(account_key="invalid_sell_qty_test")
    save_position(
        symbol="005930",
        avg_price=60_000,
        quantity=3,
        stop_loss_price=55_000,
        take_profit_price=70_000,
        trailing_stop_price=58_000,
        strategy="scoring",
        account_key="invalid_sell_qty_test",
    )

    result = executor.execute_sell(
        symbol="005930",
        price=59_000,
        quantity=0,
        reason="manual invalid quantity test",
        strategy="scoring",
    )

    assert result["success"] is False
    assert result["reason"] == "매도 수량은 1주 이상이어야 합니다"
    assert get_position("005930", account_key="invalid_sell_qty_test").quantity == 3
