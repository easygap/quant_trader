"""
주문 상태기계 (Order State Machine)

상태 전이:
  NEW → SUBMITTED → ACKED → FILLED / PARTIAL_FILLED / REJECTED / CANCELLED / EXPIRED
  PARTIAL_FILLED → FILLED / CANCELLED (잔여 취소)
  모든 최종 상태 → RECONCILED (브로커 대조 완료)

핵심 원칙:
  - fill 확인 전 position/cash 반영하지 않음
  - 상태 전이는 브로커 이벤트 또는 명시적 reconcile로만 진행
  - idempotent: 동일 이벤트 중복 수신 시 무해
  - order_id 단위 추적
"""
from enum import Enum
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILED = "RECONCILED"


# 최종 상태 (더 이상 전이 불가)
TERMINAL_STATES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.RECONCILED,
}

# 허용 전이 맵
VALID_TRANSITIONS = {
    OrderStatus.NEW: {OrderStatus.SUBMITTED, OrderStatus.REJECTED},
    OrderStatus.SUBMITTED: {OrderStatus.ACKED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.ACKED: {OrderStatus.FILLED, OrderStatus.PARTIAL_FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.PARTIAL_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.PARTIAL_FILLED},
    OrderStatus.FILLED: {OrderStatus.RECONCILED},
    OrderStatus.CANCELLED: {OrderStatus.RECONCILED},
    OrderStatus.REJECTED: {OrderStatus.RECONCILED},
    OrderStatus.EXPIRED: {OrderStatus.RECONCILED},
    OrderStatus.RECONCILED: set(),  # 최종
}


class OrderRecord:
    """주문 상태 레코드 — DB 모델과 1:1 대응."""

    def __init__(
        self,
        order_id: str,
        symbol: str,
        action: str,  # BUY / SELL
        requested_qty: int,
        requested_price: float,
        strategy: str = "",
        account_key: str = "",
        mode: str = "paper",
    ):
        self.order_id = order_id
        self.symbol = symbol
        self.action = action
        self.requested_qty = requested_qty
        self.requested_price = requested_price
        self.strategy = strategy
        self.account_key = account_key
        self.mode = mode

        self.status = OrderStatus.NEW
        self.broker_order_id: Optional[str] = None
        self.filled_qty: int = 0
        self.filled_price: float = 0.0
        self.commission: float = 0.0
        self.tax: float = 0.0
        self.slippage: float = 0.0
        self.reject_reason: str = ""

        self.created_at = datetime.now()
        self.submitted_at: Optional[datetime] = None
        self.filled_at: Optional[datetime] = None
        self.updated_at = datetime.now()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        return not self.is_terminal

    @property
    def remaining_qty(self) -> int:
        return self.requested_qty - self.filled_qty

    def transition(self, new_status: OrderStatus, **kwargs) -> bool:
        """상태 전이. 유효하지 않은 전이는 False 반환 (idempotent)."""
        if new_status == self.status:
            return True  # 동일 상태 전이는 무해

        if new_status not in VALID_TRANSITIONS.get(self.status, set()):
            logger.warning(
                "주문 상태 전이 거부: {} {} → {} (order_id={})",
                self.symbol, self.status, new_status, self.order_id,
            )
            return False

        old_status = self.status
        self.status = new_status
        self.updated_at = datetime.now()

        if new_status == OrderStatus.SUBMITTED:
            self.submitted_at = datetime.now()
            self.broker_order_id = kwargs.get("broker_order_id")

        elif new_status == OrderStatus.ACKED:
            self.broker_order_id = kwargs.get("broker_order_id", self.broker_order_id)

        elif new_status in (OrderStatus.FILLED, OrderStatus.PARTIAL_FILLED):
            fill_qty = kwargs.get("fill_qty", 0)
            fill_price = kwargs.get("fill_price", 0.0)
            if fill_qty > 0:
                # 가중평균 체결가
                total_cost = self.filled_price * self.filled_qty + fill_price * fill_qty
                self.filled_qty += fill_qty
                self.filled_price = total_cost / self.filled_qty if self.filled_qty > 0 else 0
            self.commission = kwargs.get("commission", self.commission)
            self.tax = kwargs.get("tax", self.tax)
            self.slippage = kwargs.get("slippage", self.slippage)
            if new_status == OrderStatus.FILLED:
                self.filled_at = datetime.now()

        elif new_status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            self.reject_reason = kwargs.get("reason", "")

        logger.info(
            "주문 상태 전이: {} {} → {} (order_id={}, filled={}/{})",
            self.symbol, old_status, new_status, self.order_id,
            self.filled_qty, self.requested_qty,
        )
        return True

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "action": self.action,
            "status": self.status.value,
            "requested_qty": self.requested_qty,
            "requested_price": self.requested_price,
            "filled_qty": self.filled_qty,
            "filled_price": self.filled_price,
            "remaining_qty": self.remaining_qty,
            "broker_order_id": self.broker_order_id,
            "commission": self.commission,
            "tax": self.tax,
            "strategy": self.strategy,
            "mode": self.mode,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
        }


class OrderBook:
    """In-memory order book — 미완료 주문 추적.

    DB 영속화는 별도 레이어(repositories)에서 수행.
    프로세스 재시작 시 DB에서 open orders를 로드하여 복구.
    """

    def __init__(self):
        self._orders: dict[str, OrderRecord] = {}  # order_id → OrderRecord
        self._by_symbol: dict[str, list[str]] = {}  # symbol → [order_id, ...]

    def create_order(self, **kwargs) -> OrderRecord:
        """새 주문 생성. order_id는 자동 발급."""
        import uuid
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        record = OrderRecord(order_id=order_id, **kwargs)
        self._orders[order_id] = record
        self._by_symbol.setdefault(record.symbol, []).append(order_id)
        return record

    def get_order(self, order_id: str) -> Optional[OrderRecord]:
        return self._orders.get(order_id)

    def get_open_orders(self, symbol: str = None) -> list[OrderRecord]:
        """미완료(open) 주문 목록."""
        if symbol:
            ids = self._by_symbol.get(symbol, [])
            return [self._orders[oid] for oid in ids if oid in self._orders and self._orders[oid].is_open]
        return [o for o in self._orders.values() if o.is_open]

    def has_open_order(self, symbol: str) -> bool:
        """해당 종목에 미완료 주문이 있는지 확인."""
        return len(self.get_open_orders(symbol)) > 0

    def sweep_expired(self, max_age_seconds: int = 600) -> list[OrderRecord]:
        """제출 후 max_age_seconds 이상 경과한 SUBMITTED/ACKED 주문을 EXPIRED 처리."""
        now = datetime.now()
        expired = []
        for order in list(self._orders.values()):
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.ACKED):
                age = (now - (order.submitted_at or order.created_at)).total_seconds()
                if age > max_age_seconds:
                    order.transition(OrderStatus.EXPIRED, reason="timeout")
                    expired.append(order)
        return expired

    def cleanup_terminal(self, max_keep: int = 1000):
        """최종 상태 주문 정리 (메모리 관리)."""
        terminal = [o for o in self._orders.values() if o.is_terminal]
        if len(terminal) > max_keep:
            # 오래된 것부터 제거
            terminal.sort(key=lambda o: o.updated_at)
            for o in terminal[:len(terminal) - max_keep]:
                del self._orders[o.order_id]
                if o.symbol in self._by_symbol:
                    self._by_symbol[o.symbol] = [
                        oid for oid in self._by_symbol[o.symbol] if oid != o.order_id
                    ]

    def restore_from_records(self, records: list[dict]):
        """프로세스 재시작 시 DB에서 open orders 복구."""
        for rec in records:
            order = OrderRecord(
                order_id=rec["order_id"],
                symbol=rec["symbol"],
                action=rec["action"],
                requested_qty=rec["requested_qty"],
                requested_price=rec["requested_price"],
                strategy=rec.get("strategy", ""),
                account_key=rec.get("account_key", ""),
                mode=rec.get("mode", "paper"),
            )
            order.status = OrderStatus(rec.get("status", "NEW"))
            order.broker_order_id = rec.get("broker_order_id")
            order.filled_qty = rec.get("filled_qty", 0)
            order.filled_price = rec.get("filled_price", 0.0)
            order.created_at = rec.get("created_at", datetime.now())
            order.submitted_at = rec.get("submitted_at")
            self._orders[order.order_id] = order
            self._by_symbol.setdefault(order.symbol, []).append(order.order_id)
        logger.info("OrderBook 복구: {}건 (open={})", len(records),
                     len([o for o in self._orders.values() if o.is_open]))
