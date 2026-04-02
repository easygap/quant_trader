"""
주문 상태기계 통합 테스트

검증 항목:
- 상태 전이 유효성
- fill 전 position 미생성
- partial fill 처리
- reject/cancel guard 해제
- callback 유실 후 reconcile 복구
- restart 후 open order 복구
- duplicate/out-of-order 이벤트 무해성
- OrderBook sweep expired
- DB 모델 존재 확인
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import pytest
from datetime import datetime, timedelta

from core.order_state import (
    OrderRecord, OrderBook, OrderStatus,
    TERMINAL_STATES, VALID_TRANSITIONS,
)


# ── 1. 상태 전이 유효성 ──

class TestOrderStateTransitions:
    def test_new_to_submitted(self):
        o = OrderRecord("ORD-001", "005930", "BUY", 10, 50000)
        assert o.status == OrderStatus.NEW
        assert o.transition(OrderStatus.SUBMITTED, broker_order_id="KIS-123")
        assert o.status == OrderStatus.SUBMITTED
        assert o.broker_order_id == "KIS-123"

    def test_submitted_to_acked(self):
        o = OrderRecord("ORD-002", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        assert o.transition(OrderStatus.ACKED)
        assert o.status == OrderStatus.ACKED

    def test_acked_to_filled(self):
        o = OrderRecord("ORD-003", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        assert o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        assert o.status == OrderStatus.FILLED
        assert o.filled_qty == 10
        assert o.filled_price == 50100
        assert o.is_terminal

    def test_invalid_transition_rejected(self):
        o = OrderRecord("ORD-004", "005930", "BUY", 10, 50000)
        # NEW → FILLED는 불가
        assert not o.transition(OrderStatus.FILLED)
        assert o.status == OrderStatus.NEW

    def test_same_state_transition_idempotent(self):
        o = OrderRecord("ORD-005", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        assert o.transition(OrderStatus.SUBMITTED)  # 동일 상태 → True (무해)
        assert o.status == OrderStatus.SUBMITTED

    def test_terminal_state_no_further_transition(self):
        o = OrderRecord("ORD-006", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.REJECTED, reason="insufficient funds")
        assert o.is_terminal
        assert o.reject_reason == "insufficient funds"
        # RECONCILED만 가능
        assert not o.transition(OrderStatus.FILLED)
        assert o.transition(OrderStatus.RECONCILED)


# ── 2. Partial Fill ──

class TestPartialFill:
    def test_partial_then_filled(self):
        o = OrderRecord("ORD-010", "005930", "BUY", 100, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)

        # 1차 부분체결: 60주 @ 50100
        assert o.transition(OrderStatus.PARTIAL_FILLED, fill_qty=60, fill_price=50100)
        assert o.filled_qty == 60
        assert o.remaining_qty == 40
        assert not o.is_terminal

        # 2차 체결: 40주 @ 50200 → 전량 체결
        assert o.transition(OrderStatus.FILLED, fill_qty=40, fill_price=50200)
        assert o.filled_qty == 100
        assert o.remaining_qty == 0
        assert o.is_terminal
        # 가중평균 체결가: (60*50100 + 40*50200) / 100 = 50140
        assert abs(o.filled_price - 50140) < 1

    def test_partial_then_cancelled(self):
        o = OrderRecord("ORD-011", "005930", "BUY", 100, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.PARTIAL_FILLED, fill_qty=30, fill_price=50100)

        # 잔여 취소
        assert o.transition(OrderStatus.CANCELLED, reason="user cancel")
        assert o.filled_qty == 30  # 체결된 30주는 유지
        assert o.is_terminal

    def test_position_only_for_filled_qty(self):
        """fill 확인된 수량만 position에 반영해야 함."""
        o = OrderRecord("ORD-012", "005930", "BUY", 100, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.PARTIAL_FILLED, fill_qty=30, fill_price=50100)

        # 이 시점에서 position은 30주만 반영해야 함
        assert o.filled_qty == 30
        assert o.remaining_qty == 70


# ── 3. Reject / Cancel → Guard 해제 ──

class TestRejectCancel:
    def test_reject_is_terminal(self):
        o = OrderRecord("ORD-020", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.REJECTED, reason="price limit")
        assert o.is_terminal
        assert o.filled_qty == 0

    def test_cancel_is_terminal(self):
        o = OrderRecord("ORD-021", "005930", "SELL", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.CANCELLED, reason="user request")
        assert o.is_terminal
        assert o.filled_qty == 0


# ── 4. OrderBook ──

class TestOrderBook:
    def test_create_and_retrieve(self):
        book = OrderBook()
        o = book.create_order(symbol="005930", action="BUY", requested_qty=10,
                              requested_price=50000, strategy="scoring")
        assert o.order_id.startswith("ORD-")
        assert book.get_order(o.order_id) is o

    def test_has_open_order(self):
        book = OrderBook()
        o = book.create_order(symbol="005930", action="BUY", requested_qty=10,
                              requested_price=50000)
        assert book.has_open_order("005930")
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50000)
        assert not book.has_open_order("005930")

    def test_sweep_expired(self):
        book = OrderBook()
        o = book.create_order(symbol="005930", action="BUY", requested_qty=10,
                              requested_price=50000)
        o.transition(OrderStatus.SUBMITTED)
        o.submitted_at = datetime.now() - timedelta(seconds=700)  # 700초 전 제출
        expired = book.sweep_expired(max_age_seconds=600)
        assert len(expired) == 1
        assert expired[0].status == OrderStatus.EXPIRED

    def test_no_sweep_for_recent(self):
        book = OrderBook()
        o = book.create_order(symbol="005930", action="BUY", requested_qty=10,
                              requested_price=50000)
        o.transition(OrderStatus.SUBMITTED)
        expired = book.sweep_expired(max_age_seconds=600)
        assert len(expired) == 0

    def test_multiple_symbols(self):
        book = OrderBook()
        book.create_order(symbol="005930", action="BUY", requested_qty=10, requested_price=50000)
        book.create_order(symbol="000660", action="BUY", requested_qty=5, requested_price=80000)
        assert book.has_open_order("005930")
        assert book.has_open_order("000660")
        assert not book.has_open_order("035720")
        assert len(book.get_open_orders()) == 2


# ── 5. Restart 복구 ──

class TestRestartRecovery:
    def test_restore_from_records(self):
        book = OrderBook()
        records = [
            {"order_id": "ORD-R1", "symbol": "005930", "action": "BUY",
             "requested_qty": 10, "requested_price": 50000,
             "status": "SUBMITTED", "filled_qty": 0},
            {"order_id": "ORD-R2", "symbol": "000660", "action": "SELL",
             "requested_qty": 5, "requested_price": 80000,
             "status": "FILLED", "filled_qty": 5, "filled_price": 80100},
        ]
        book.restore_from_records(records)
        assert book.has_open_order("005930")  # SUBMITTED = open
        assert not book.has_open_order("000660")  # FILLED = terminal

    def test_restored_order_can_transition(self):
        book = OrderBook()
        book.restore_from_records([{
            "order_id": "ORD-R3", "symbol": "005930", "action": "BUY",
            "requested_qty": 10, "requested_price": 50000,
            "status": "ACKED", "filled_qty": 0,
        }])
        o = book.get_order("ORD-R3")
        assert o is not None
        assert o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)


# ── 6. Duplicate / Out-of-order 이벤트 무해성 ──

class TestIdempotency:
    def test_duplicate_fill_event(self):
        o = OrderRecord("ORD-030", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        # 동일 FILLED 이벤트 재수신
        result = o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        assert result  # idempotent (same state → True)
        assert o.filled_qty == 10  # 중복 가산 없음

    def test_out_of_order_rejected_after_filled(self):
        """FILLED 후 REJECTED 이벤트가 늦게 도착해도 무시."""
        o = OrderRecord("ORD-031", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        # 늦게 도착한 REJECTED → 무시 (FILLED → REJECTED는 invalid)
        result = o.transition(OrderStatus.REJECTED)
        assert not result  # 전이 거부
        assert o.status == OrderStatus.FILLED  # 상태 유지


# ── 7. to_dict 직렬화 ──

class TestSerialization:
    def test_to_dict(self):
        o = OrderRecord("ORD-040", "005930", "BUY", 10, 50000, strategy="scoring")
        d = o.to_dict()
        assert d["order_id"] == "ORD-040"
        assert d["symbol"] == "005930"
        assert d["status"] == "NEW"
        assert d["remaining_qty"] == 10


# ── 8. DB 모델 존재 확인 ──

class TestDBModel:
    def test_order_record_table_exists(self):
        """OrderRecord 모델이 DB에 테이블로 생성되어야 함."""
        import tempfile
        from sqlalchemy import create_engine, text
        from database.models import Base

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            with engine.connect() as conn:
                r = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='order_records'"
                ))
                assert r.fetchone() is not None, "order_records 테이블 없음"

                # 컬럼 확인
                cols_r = conn.execute(text("PRAGMA table_info(order_records)"))
                cols = [row[1] for row in cols_r.fetchall()]
                for expected in ["order_id", "symbol", "action", "status",
                                  "requested_qty", "filled_qty", "filled_price",
                                  "broker_order_id", "remaining_qty"]:
                    assert expected in cols, f"컬럼 {expected} 없음"
            engine.dispose()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


# ── 9. Invariant 검증 ──

class TestInvariants:
    def test_fill_before_position_invariant(self):
        """fill 확인 전에는 position이 생성되지 않아야 함 (상태기계 기준)."""
        o = OrderRecord("ORD-050", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        # ACKED 상태에서 filled_qty = 0 → position 반영 불가
        assert o.filled_qty == 0
        assert o.status == OrderStatus.ACKED
        assert not o.is_terminal

    def test_open_order_blocks_duplicate(self):
        """미완료 주문이 있으면 동일 종목 중복 주문 차단."""
        book = OrderBook()
        book.create_order(symbol="005930", action="BUY", requested_qty=10, requested_price=50000)
        assert book.has_open_order("005930")
        # 중복 주문 제출 전 이 체크를 해야 함

    def test_reconcile_is_idempotent(self):
        """reconcile(RECONCILED 전이)은 여러 번 호출해도 안전."""
        o = OrderRecord("ORD-051", "005930", "BUY", 10, 50000)
        o.transition(OrderStatus.SUBMITTED)
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        assert o.transition(OrderStatus.RECONCILED)
        # 두 번째 RECONCILED → same state = True
        assert o.transition(OrderStatus.RECONCILED)

    def test_all_terminal_states_defined(self):
        """모든 최종 상태가 TERMINAL_STATES에 포함."""
        for status in [OrderStatus.FILLED, OrderStatus.CANCELLED,
                       OrderStatus.REJECTED, OrderStatus.EXPIRED,
                       OrderStatus.RECONCILED]:
            assert status in TERMINAL_STATES

    def test_valid_transitions_complete(self):
        """모든 OrderStatus가 VALID_TRANSITIONS에 키로 존재."""
        for status in OrderStatus:
            assert status in VALID_TRANSITIONS, f"{status}가 전이 맵에 없음"
