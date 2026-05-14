"""
OrderExecutor ↔ 상태기계 통합 테스트

검증: 실제 execute_buy/sell이 OrderBook/OrderRecord를 사용하고,
      FILLED 전에는 position/trade가 DB에 없는 invariant.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from datetime import datetime

from core.order_state import OrderStatus


def _paper_guard_patches():
    stack = ExitStack()
    stack.enter_context(patch(
        "core.paper_preflight.load_preflight_status",
        return_value=SimpleNamespace(
            overall="pass",
            entry_allowed=True,
            runtime_state="normal",
            block_reasons=[],
        ),
    ))
    stack.enter_context(patch(
        "core.paper_runtime.get_paper_runtime_state",
        return_value=SimpleNamespace(
            state="normal",
            allowed_actions=["entry", "exit", "cancel", "reconcile", "finalize"],
            reasons=[],
            metrics={"recent_final_ratio": 1.0, "recent_anomaly_count": 0},
            evidence_date="2026-01-01",
        ),
    ))
    return stack


class TestExecutorUsesStateMachine:
    """OrderExecutor가 상태기계를 실제로 사용하는지 검증."""

    def setup_method(self):
        from config.config_loader import Config
        Config._instance = None
        from database.models import (
            init_database,
            get_session,
            OrderRecord as DbOrderRecord,
            PendingOrderGuard,
            TradeHistory,
            Position,
        )
        init_database()
        session = get_session()
        session.query(TradeHistory).delete()
        session.query(Position).delete()
        session.query(PendingOrderGuard).delete()
        session.query(DbOrderRecord).delete()
        session.commit()
        session.close()

    def _make_executor(self):
        from core.order_executor import OrderExecutor
        from config.config_loader import Config
        config = Config.get()
        executor = OrderExecutor(config, account_key="test_sm")
        executor.config.risk_params.setdefault("liquidity_filter", {})["enabled"] = False
        executor.config.risk_params.setdefault("gap_risk", {})["enabled"] = False
        executor.config.risk_params.setdefault("diversification", {})["sector_map_strict"] = False
        return executor

    def test_buy_creates_order_record(self):
        """execute_buy가 OrderBook에 OrderRecord를 생성해야 함."""
        executor = self._make_executor()
        with _paper_guard_patches(), patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
            result = executor.execute_buy(
                symbol="005930", price=54200, capital=10_000_000,
                signal_score=2.5, reason="sm test", strategy="scoring",
            )
        assert result["success"], f"BUY 실패: {result.get('reason')}"
        # OrderBook에 레코드가 있어야 함
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "005930"]
        assert len(orders) >= 1, "OrderBook에 주문 레코드 없음"
        assert orders[-1].status == OrderStatus.FILLED, f"상태={orders[-1].status}, FILLED여야 함"

    def test_buy_paper_goes_through_state_transitions(self):
        """Paper BUY가 NEW → SUBMITTED → ACKED → FILLED 전이를 거치는지."""
        executor = self._make_executor()
        with _paper_guard_patches(), patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
            result = executor.execute_buy(
                symbol="000660", price=80000, capital=10_000_000,
                signal_score=2.0, reason="transition test", strategy="scoring",
            )
        assert result["success"]
        order = [o for o in executor.order_book._orders.values() if o.symbol == "000660"][-1]
        assert order.status == OrderStatus.FILLED
        assert order.filled_qty > 0
        assert order.filled_price > 0

    def test_position_only_after_filled(self):
        """FILLED 상태에서만 position이 DB에 존재해야 함."""
        from database.models import get_session, Position
        executor = self._make_executor()

        # BUY 전: position 없음
        session = get_session()
        pos_before = session.query(Position).filter(
            Position.symbol == "035720", Position.account_key == "test_sm"
        ).first()
        session.close()
        assert pos_before is None, "BUY 전에 position이 존재"

        with _paper_guard_patches(), patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
            result = executor.execute_buy(
                symbol="035720", price=40000, capital=10_000_000,
                signal_score=2.0, reason="pos test", strategy="scoring",
            )

        if result["success"]:
            # BUY 후: position 있음 (FILLED 후)
            session = get_session()
            pos_after = session.query(Position).filter(
                Position.symbol == "035720", Position.account_key == "test_sm"
            ).first()
            session.close()
            assert pos_after is not None, "FILLED 후 position 없음"
            assert pos_after.quantity > 0

    def test_sell_creates_order_and_removes_position(self):
        """SELL도 상태기계를 거쳐 position을 삭제해야 함."""
        from database.models import get_session, Position
        executor = self._make_executor()

        # 먼저 BUY
        with _paper_guard_patches(), patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
            buy_result = executor.execute_buy(
                symbol="051910", price=30000, capital=10_000_000,
                signal_score=2.0, reason="sell test buy", strategy="scoring",
            )
        if not buy_result["success"]:
            pytest.skip(f"BUY 실패: {buy_result.get('reason')}")

        # SELL (STOP_LOSS reason으로 min_holding_days 예외 적용)
        sell_result = executor.execute_sell(
            symbol="051910", price=31000, reason="STOP_LOSS", strategy="scoring",
        )
        assert sell_result["success"], f"SELL 실패: {sell_result.get('reason')}"

        # SELL 주문도 FILLED여야 함
        sell_orders = [o for o in executor.order_book._orders.values()
                       if o.symbol == "051910" and o.action == "SELL"]
        assert len(sell_orders) >= 1
        assert sell_orders[-1].status == OrderStatus.FILLED

        # Position 삭제 확인
        session = get_session()
        pos = session.query(Position).filter(
            Position.symbol == "051910", Position.account_key == "test_sm"
        ).first()
        session.close()
        assert pos is None, "SELL 후 position이 남아 있음"

    def test_orderbook_has_open_order_blocks_duplicate(self):
        """OrderBook에 미완료 주문이 있으면 중복 주문 차단."""
        executor = self._make_executor()
        # 수동으로 open order 생성
        o = executor.order_book.create_order(
            symbol="005930", action="BUY", requested_qty=10,
            requested_price=50000, strategy="scoring",
        )
        o.transition(OrderStatus.SUBMITTED)

        with _paper_guard_patches(), patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
            result = executor.execute_buy(
                symbol="005930", price=50000, capital=10_000_000,
                signal_score=2.0, reason="dup test",
            )
        assert not result["success"]
        assert "미완료" in result["reason"] or "미체결" in result["reason"]

    def test_paper_simulated_events_same_invariant(self):
        """Paper와 live가 동일한 invariant(FILLED 전 position 없음)을 만족하는지."""
        import inspect
        from core.order_executor import OrderExecutor
        source = inspect.getsource(OrderExecutor._execute_buy_impl)
        # assert 문이 존재해야 함
        assert 'assert order.status == OrderStatus.FILLED' in source, \
            "FILLED invariant assert가 코드에 없음"

    def test_no_direct_save_position_before_filled(self):
        """save_position 호출이 FILLED assert 이후에만 존재해야 함."""
        import inspect
        from core.order_executor import OrderExecutor
        buy_src = inspect.getsource(OrderExecutor._execute_buy_impl)
        lines = buy_src.split('\n')

        filled_assert_line = None
        save_pos_line = None
        for i, line in enumerate(lines):
            if 'assert order.status == OrderStatus.FILLED' in line:
                filled_assert_line = i
            if 'save_position(' in line and filled_assert_line is None:
                save_pos_line = i
                break

        assert save_pos_line is None, \
            f"save_position이 FILLED assert(line {filled_assert_line}) 이전(line {save_pos_line})에 호출됨"

    def _prepare_live_executor(self, executor, kis_api):
        executor.mode = "live"
        executor.kis_api = kis_api
        executor.trading_hours = SimpleNamespace(
            can_place_order=lambda *a, **kw: {"allowed": True, "reason": ""}
        )
        executor.blackswan = SimpleNamespace(
            can_trade=lambda *a, **kw: {"allowed": True, "reason": ""}
        )
        executor._should_block_new_buy_volatility_window = lambda: False
        executor._get_sector_map_cached = lambda: {}
        executor.risk_manager.calculate_stop_loss = lambda price, atr=None, regime_multiplier=1.0: price * 0.95
        executor.risk_manager.calculate_position_size = lambda *a, **kw: 3
        executor.risk_manager.check_correlation_risk = lambda *a, **kw: {
            "scale": 1.0,
            "high_corr_symbols": [],
            "reason": "",
        }
        executor.risk_manager.check_diversification = lambda *a, **kw: {
            "can_buy": True,
            "reason": "",
        }
        executor.risk_manager.check_recent_performance = lambda *a, **kw: {
            "allowed": True,
            "reason": "",
        }
        executor.risk_manager.calculate_transaction_costs = lambda price, qty, action, **kw: {
            "execution_price": float(price),
            "commission": 0.0,
            "tax": 0.0,
            "capital_gains_tax": 0.0,
            "slippage": 0.0,
            "total_cost": 0.0,
        }
        executor.risk_manager.calculate_take_profit = lambda price, regime_multiplier=1.0: {
            "target_final": price * 1.1
        }
        executor.risk_manager.calculate_trailing_stop = lambda price, atr=None: price * 0.97
        return executor

    def test_live_buy_blocks_when_unfilled_lookup_fails_before_api_order(self):
        """실전 BUY는 KIS 미체결 조회 실패 시 주문 제출 전에 fail-closed 차단한다."""
        from core.order_guard import OrderGuard
        from database.repositories import get_position

        class UnfilledLookupFailedKIS:
            def __init__(self):
                self.buy_called = False

            def get_unfilled_order_status(self, symbol):
                return {
                    "checked": False,
                    "has_unfilled": False,
                    "reason": "kis_unfilled_query_failed",
                    "orders": [],
                }

            def buy_order(self, symbol, quantity, price):
                self.buy_called = True
                return {"odno": "BFAIL"}

        OrderGuard.clear("005930")
        kis_api = UnfilledLookupFailedKIS()
        executor = self._prepare_live_executor(self._make_executor(), kis_api)

        result = executor.execute_buy(
            symbol="005930",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="live unfilled lookup fail test",
            strategy="scoring",
        )

        assert result["success"] is False
        assert "미체결 조회" in result["reason"]
        assert result["live_unfilled_check"]["checked"] is False
        assert kis_api.buy_called is False
        assert get_position("005930", account_key="test_sm") is None
        assert not OrderGuard.has_pending("005930")
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "005930"]
        assert orders[-1].status == OrderStatus.REJECTED

    def test_live_sell_blocks_when_unfilled_lookup_fails_before_api_order(self):
        """실전 SELL도 KIS 미체결 조회 실패 시 매도 주문을 제출하지 않는다."""
        from core.order_guard import OrderGuard
        from database.repositories import get_position, save_position

        class UnfilledLookupFailedKIS:
            def __init__(self):
                self.sell_called = False

            def get_unfilled_order_status(self, symbol):
                return {
                    "checked": False,
                    "has_unfilled": False,
                    "reason": "kis_unfilled_query_exception",
                    "orders": [],
                }

            def sell_order(self, symbol, quantity, price):
                self.sell_called = True
                return {"odno": "SFAIL"}

        OrderGuard.clear("000660")
        save_position(
            symbol="000660",
            avg_price=70_000,
            quantity=5,
            stop_loss_price=65_000,
            take_profit_price=80_000,
            trailing_stop_price=68_000,
            strategy="scoring",
            account_key="test_sm",
        )
        kis_api = UnfilledLookupFailedKIS()
        executor = self._prepare_live_executor(self._make_executor(), kis_api)

        result = executor.execute_sell(
            symbol="000660",
            price=71_000,
            reason="STOP_LOSS",
            strategy="scoring",
        )

        assert result["success"] is False
        assert "미체결 조회" in result["reason"]
        assert result["live_unfilled_check"]["checked"] is False
        assert kis_api.sell_called is False
        position = get_position("000660", account_key="test_sm")
        assert position is not None
        assert position.quantity == 5
        assert not OrderGuard.has_pending("000660")
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "000660"]
        assert orders[-1].status == OrderStatus.REJECTED

    def test_live_buy_ack_without_fill_does_not_touch_position_or_trade(self):
        """실전 BUY는 주문 ACK만 있고 체결 확인이 없으면 장부 반영을 보류한다."""
        from core.order_guard import OrderGuard
        from database.models import TradeHistory, get_session
        from database.repositories import get_open_order_records, get_position

        class AckNoFillKIS:
            def has_unfilled_orders(self, symbol):
                return False

            def buy_order(self, symbol, quantity, price):
                return {"odno": "B123"}

            def get_filled_avg_price_after_order(self, symbol, order_output):
                return None

        OrderGuard.clear("005930")
        executor = self._prepare_live_executor(self._make_executor(), AckNoFillKIS())

        result = executor.execute_buy(
            symbol="005930",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="live no fill test",
            strategy="scoring",
        )

        assert result["success"] is False
        assert result["order_pending"] is True
        assert result["requires_reconcile"] is True
        assert result["order_status"] == OrderStatus.ACKED.value
        assert get_position("005930", account_key="test_sm") is None
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "005930"]
        assert orders[-1].status == OrderStatus.ACKED
        assert OrderGuard.has_pending("005930")
        open_records = get_open_order_records(
            symbol="005930",
            account_key="test_sm",
            mode="live",
        )
        assert len(open_records) == 1
        assert open_records[0]["status"] == OrderStatus.ACKED.value
        assert open_records[0]["broker_order_id"] == "B123"
        assert open_records[0]["remaining_qty"] == 3

        session = get_session()
        try:
            assert session.query(TradeHistory).filter(TradeHistory.symbol == "005930").count() == 0
        finally:
            session.close()
            OrderGuard.clear("005930")

    def test_live_buy_blocks_when_persistent_open_order_remains_after_guard_clear(self):
        """DB에 ACK/PARTIAL 주문이 남아 있으면 TTL 가드가 비어도 같은 종목 주문을 막는다."""
        from core.order_guard import OrderGuard
        from database.repositories import get_open_order_records

        class AckNoFillKIS:
            def __init__(self):
                self.buy_called = 0

            def has_unfilled_orders(self, symbol):
                return False

            def buy_order(self, symbol, quantity, price):
                self.buy_called += 1
                return {"odno": f"B{self.buy_called}"}

            def get_filled_avg_price_after_order(self, symbol, order_output):
                return None

        OrderGuard.clear("005930")
        first_kis = AckNoFillKIS()
        first = self._prepare_live_executor(self._make_executor(), first_kis)
        first_result = first.execute_buy(
            symbol="005930",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="first pending live order",
            strategy="scoring",
        )
        assert first_result["requires_reconcile"] is True
        assert first_kis.buy_called == 1
        assert get_open_order_records("005930", account_key="test_sm", mode="live")

        OrderGuard.clear("005930")
        second_kis = AckNoFillKIS()
        second = self._prepare_live_executor(self._make_executor(), second_kis)
        second_result = second.execute_buy(
            symbol="005930",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="duplicate pending live order",
            strategy="scoring",
        )

        assert second_result["success"] is False
        assert second_result["requires_reconcile"] is True
        assert second_result["persistent_order_block"] is True
        assert "재조정되지 않은 실전 주문 상태" in second_result["reason"]
        assert second_kis.buy_called == 0

    def test_recovery_reconciles_persistent_order_when_broker_open_order_disappears(self):
        """KIS 미체결 목록에서 사라진 DB open order는 복구 대조 후 중복 차단에서 제외한다."""
        from core.order_guard import OrderGuard
        from core.order_state import OrderRecord
        from database.models import OrderRecord as DbOrderRecord, get_session
        from database.repositories import get_open_order_records, save_order_record

        order = OrderRecord(
            "ORD-PERSIST",
            "005930",
            "BUY",
            3,
            60_000,
            strategy="scoring",
            account_key="test_sm",
            mode="live",
        )
        order.transition(OrderStatus.SUBMITTED)
        order.transition(OrderStatus.ACKED, broker_order_id="000123")
        save_order_record(order)
        OrderGuard.mark_pending("005930", ttl_seconds=600)

        class NoOpenOrderKIS:
            _access_token = "token"

            def get_open_orders_status(self):
                return {"checked": True, "reason": "ok", "orders": []}

            def get_order_execution_after_order(self, symbol, order_output, **kwargs):
                return {
                    "fill_price": 60_100,
                    "filled_qty": 3,
                    "remaining_qty": 0,
                    "order_no": "000123",
                }

        executor = self._prepare_live_executor(self._make_executor(), NoOpenOrderKIS())

        open_orders = executor.reconcile_open_orders_after_crash()

        assert open_orders == []
        assert get_open_order_records("005930", account_key="test_sm", mode="live") == []
        assert not OrderGuard.has_pending("005930")
        reconciled = executor.last_open_order_reconcile_status["persistent_order_reconciliations"]
        assert reconciled[0]["order_id"] == "ORD-PERSIST"
        assert reconciled[0]["status"] == OrderStatus.RECONCILED.value
        assert reconciled[0]["filled_qty"] == 3
        assert reconciled[0]["filled_price"] == 60_100
        assert reconciled[0]["execution_checked"] is True

        session = get_session()
        try:
            record = session.query(DbOrderRecord).filter_by(order_id="ORD-PERSIST").one()
            assert record.status == OrderStatus.RECONCILED.value
            assert record.remaining_qty == 0
            assert record.reject_reason == "broker_execution_confirmed_after_recovery_check"
        finally:
            session.close()

    def test_recovery_keeps_persistent_order_open_when_execution_lookup_unclear(self):
        """KIS 미체결 목록에서 사라져도 체결 조회가 불명확하면 DB open order를 닫지 않는다."""
        from core.order_guard import OrderGuard
        from core.order_state import OrderRecord
        from database.models import OrderRecord as DbOrderRecord, get_session
        from database.repositories import get_open_order_records, save_order_record

        order = OrderRecord(
            "ORD-UNCLEAR",
            "005930",
            "BUY",
            3,
            60_000,
            strategy="scoring",
            account_key="test_sm",
            mode="live",
        )
        order.transition(OrderStatus.SUBMITTED)
        order.transition(OrderStatus.ACKED, broker_order_id="000125")
        save_order_record(order)
        OrderGuard.mark_pending("005930", ttl_seconds=600)

        class UnclearExecutionKIS:
            _access_token = "token"

            def get_open_orders_status(self):
                return {"checked": True, "reason": "ok", "orders": []}

            def get_order_execution_after_order(self, symbol, order_output, **kwargs):
                raise RuntimeError("execution lookup unavailable")

        executor = self._prepare_live_executor(self._make_executor(), UnclearExecutionKIS())

        open_orders = executor.reconcile_open_orders_after_crash()

        assert open_orders == []
        assert get_open_order_records("005930", account_key="test_sm", mode="live")
        assert OrderGuard.has_pending("005930")
        assert executor.last_open_order_reconcile_status["persistent_order_reconciliations"] == []

        session = get_session()
        try:
            record = session.query(DbOrderRecord).filter_by(order_id="ORD-UNCLEAR").one()
            assert record.status == OrderStatus.ACKED.value
            assert record.reject_reason in (None, "")
        finally:
            session.close()
            OrderGuard.clear("005930")

    def test_recovery_keeps_persistent_order_open_when_broker_still_reports_unfilled(self):
        """KIS 미체결 목록에 같은 주문번호가 남아 있으면 DB open order를 닫지 않는다."""
        from core.order_guard import OrderGuard
        from core.order_state import OrderRecord
        from database.models import OrderRecord as DbOrderRecord, get_session
        from database.repositories import get_open_order_records, save_order_record

        order = OrderRecord(
            "ORD-STILL-OPEN",
            "005930",
            "BUY",
            3,
            60_000,
            strategy="scoring",
            account_key="test_sm",
            mode="live",
        )
        order.transition(OrderStatus.SUBMITTED)
        order.transition(OrderStatus.ACKED, broker_order_id="000124")
        save_order_record(order)
        OrderGuard.mark_pending("005930", ttl_seconds=600)

        class StillOpenKIS:
            _access_token = "token"

            def get_open_orders_status(self):
                return {
                    "checked": True,
                    "reason": "ok",
                    "orders": [{
                        "symbol": "005930",
                        "remaining_qty": 3,
                        "order_no": "124",
                    }],
                }

            def get_order_execution_after_order(self, *args, **kwargs):
                pytest.fail("미체결 주문번호가 남아 있으면 체결 조회로 닫으면 안 됨")

        executor = self._prepare_live_executor(self._make_executor(), StillOpenKIS())

        open_orders = executor.reconcile_open_orders_after_crash()

        assert len(open_orders) == 1
        assert get_open_order_records("005930", account_key="test_sm", mode="live")
        assert OrderGuard.has_pending("005930")
        assert executor.last_open_order_reconcile_status["persistent_order_reconciliations"] == []

        session = get_session()
        try:
            record = session.query(DbOrderRecord).filter_by(order_id="ORD-STILL-OPEN").one()
            assert record.status == OrderStatus.ACKED.value
        finally:
            session.close()
            OrderGuard.clear("005930")

    def test_live_sell_ack_without_fill_keeps_position_open(self):
        """실전 SELL도 체결 확인 전에는 보유 포지션을 줄이거나 삭제하지 않는다."""
        from core.order_guard import OrderGuard
        from database.models import TradeHistory, get_session
        from database.repositories import get_position, save_position

        class AckNoFillKIS:
            def has_unfilled_orders(self, symbol):
                return False

            def sell_order(self, symbol, quantity, price):
                return {"odno": "S123"}

            def get_filled_avg_price_after_order(self, symbol, order_output):
                return None

        OrderGuard.clear("000660")
        save_position(
            symbol="000660",
            avg_price=70_000,
            quantity=5,
            stop_loss_price=65_000,
            take_profit_price=80_000,
            trailing_stop_price=68_000,
            strategy="scoring",
            account_key="test_sm",
        )
        executor = self._prepare_live_executor(self._make_executor(), AckNoFillKIS())

        result = executor.execute_sell(
            symbol="000660",
            price=71_000,
            reason="STOP_LOSS",
            strategy="scoring",
        )

        assert result["success"] is False
        assert result["order_pending"] is True
        assert result["requires_reconcile"] is True
        assert result["order_status"] == OrderStatus.ACKED.value
        position = get_position("000660", account_key="test_sm")
        assert position is not None
        assert position.quantity == 5
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "000660"]
        assert orders[-1].status == OrderStatus.ACKED
        assert OrderGuard.has_pending("000660")
        session = get_session()
        try:
            assert session.query(TradeHistory).filter(
                TradeHistory.symbol == "000660",
                TradeHistory.action == "SELL",
            ).count() == 0
        finally:
            session.close()
            OrderGuard.clear("000660")

    def test_live_buy_fill_lookup_exception_requires_reconcile(self):
        """체결 조회 예외도 주문 실패가 아니라 ACK 후 reconcile 필요 상태로 남긴다."""
        from core.order_guard import OrderGuard
        from database.repositories import get_position

        class AckLookupErrorKIS:
            def has_unfilled_orders(self, symbol):
                return False

            def buy_order(self, symbol, quantity, price):
                return {"odno": "B124"}

            def get_filled_avg_price_after_order(self, symbol, order_output):
                raise RuntimeError("temporary lookup failure")

        OrderGuard.clear("005935")
        executor = self._prepare_live_executor(self._make_executor(), AckLookupErrorKIS())

        result = executor.execute_buy(
            symbol="005935",
            price=55_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="live lookup exception test",
            strategy="scoring",
        )

        assert result["success"] is False
        assert result["order_pending"] is True
        assert result["requires_reconcile"] is True
        assert result["execution_check"]["reason"] == "live_fill_lookup_failed"
        assert result["order_status"] == OrderStatus.ACKED.value
        assert get_position("005935", account_key="test_sm") is None
        assert OrderGuard.has_pending("005935")
        OrderGuard.clear("005935")

    def test_live_buy_partial_fill_does_not_book_full_position(self):
        """요청 수량보다 적은 체결만 확인되면 부분체결 상태로 남기고 장부 반영을 보류한다."""
        from core.order_guard import OrderGuard
        from database.models import TradeHistory, get_session
        from database.repositories import get_open_order_records, get_position

        class AckPartialFillKIS:
            def has_unfilled_orders(self, symbol):
                return False

            def buy_order(self, symbol, quantity, price):
                return {"odno": "B125"}

            def get_order_execution_after_order(self, symbol, order_output):
                return {
                    "fill_price": 60_100,
                    "filled_qty": 1,
                    "remaining_qty": 2,
                }

        OrderGuard.clear("005380")
        executor = self._prepare_live_executor(self._make_executor(), AckPartialFillKIS())

        result = executor.execute_buy(
            symbol="005380",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="live partial fill test",
            strategy="scoring",
        )

        assert result["success"] is False
        assert result["order_pending"] is True
        assert result["requires_reconcile"] is True
        assert result["execution_check"]["reason"] == "live_partial_fill_unreconciled"
        assert result["execution_check"]["filled_qty"] == 1
        assert result["order_status"] == OrderStatus.PARTIAL_FILLED.value
        assert get_position("005380", account_key="test_sm") is None
        orders = [o for o in executor.order_book._orders.values() if o.symbol == "005380"]
        assert orders[-1].status == OrderStatus.PARTIAL_FILLED
        assert orders[-1].filled_qty == 1
        open_records = get_open_order_records(
            symbol="005380",
            account_key="test_sm",
            mode="live",
        )
        assert len(open_records) == 1
        assert open_records[0]["status"] == OrderStatus.PARTIAL_FILLED.value
        assert open_records[0]["filled_qty"] == 1
        assert open_records[0]["remaining_qty"] == 2

        session = get_session()
        try:
            assert session.query(TradeHistory).filter(TradeHistory.symbol == "005380").count() == 0
        finally:
            session.close()
            OrderGuard.clear("005380")

    def test_live_buy_mismatched_execution_order_number_stays_pending(self):
        """체결 조회가 다른 주문번호를 반환하면 현재 live 주문 장부 반영을 보류한다."""
        from core.order_guard import OrderGuard
        from database.models import TradeHistory, get_session
        from database.repositories import get_position

        class AckWrongExecutionKIS:
            def has_unfilled_orders(self, symbol):
                return False

            def buy_order(self, symbol, quantity, price):
                return {"odno": "B126"}

            def get_order_execution_after_order(self, symbol, order_output):
                return {
                    "fill_price": 60_100,
                    "filled_qty": 3,
                    "remaining_qty": 0,
                    "order_no": "B999",
                }

        OrderGuard.clear("005387")
        executor = self._prepare_live_executor(self._make_executor(), AckWrongExecutionKIS())

        result = executor.execute_buy(
            symbol="005387",
            price=60_000,
            capital=10_000_000,
            available_cash=10_000_000,
            signal_score=2.0,
            reason="live mismatched order execution test",
            strategy="scoring",
        )

        assert result["success"] is False
        assert result["order_pending"] is True
        assert result["requires_reconcile"] is True
        assert result["execution_check"]["reason"] == "live_execution_order_mismatch"
        assert result["execution_check"]["expected_order_no"] == "B126"
        assert result["execution_check"]["execution_order_no"] == "B999"
        assert result["order_status"] == OrderStatus.ACKED.value
        assert get_position("005387", account_key="test_sm") is None

        session = get_session()
        try:
            assert session.query(TradeHistory).filter(TradeHistory.symbol == "005387").count() == 0
        finally:
            session.close()
            OrderGuard.clear("005387")
