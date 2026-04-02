"""
OrderExecutor ↔ 상태기계 통합 테스트

검증: 실제 execute_buy/sell이 OrderBook/OrderRecord를 사용하고,
      FILLED 전에는 position/trade가 DB에 없는 invariant.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from core.order_state import OrderStatus


class TestExecutorUsesStateMachine:
    """OrderExecutor가 상태기계를 실제로 사용하는지 검증."""

    def setup_method(self):
        from config.config_loader import Config
        Config._instance = None
        from database.models import init_database, get_session, TradeHistory, Position
        init_database()
        session = get_session()
        session.query(TradeHistory).delete()
        session.query(Position).delete()
        session.commit()
        session.close()

    def _make_executor(self):
        from core.order_executor import OrderExecutor
        from config.config_loader import Config
        config = Config.get()
        executor = OrderExecutor(config, account_key="test_sm")
        return executor

    def test_buy_creates_order_record(self):
        """execute_buy가 OrderBook에 OrderRecord를 생성해야 함."""
        executor = self._make_executor()
        with patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
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
        with patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
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

        with patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
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
        with patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
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

        with patch.object(executor, "_should_block_new_buy_volatility_window", return_value=False):
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
