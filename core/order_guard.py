"""
중복 주문 방지 가드 (실전 10분 루프 타이밍 리스크 대응).

주문 전 검사 순서 (OrderExecutor live 모드):
  ① OrderGuard.has_pending(symbol) — DB 기반 TTL: 최근 주문 접수 후 N초 동안 동일 종목 추가 주문 차단
  ② KIS API 미체결 조회 — 해당 종목에 미체결 주문이 있으면 주문 보류 (브로커 실제 상태 반영)

v2: 인메모리 → DB 기반으로 전환. 프로세스 재시작 시에도 중복 방지 유지.
인메모리 캐시도 병행하여 DB 조회 빈도를 줄임.
"""

from datetime import datetime, timedelta
import threading

from loguru import logger


_lock = threading.RLock()
_pending_orders: dict[str, datetime] = {}  # 인메모리 캐시 (DB와 병행)


class OrderGuard:
    """동일 종목 중복 주문을 막기 위한 DB+인메모리 가드."""

    @classmethod
    def has_pending(cls, symbol: str) -> bool:
        """유효한 가드가 있거나 상태를 안전하게 확인할 수 없으면 ``True``.

        주문 경로에서 DB 장애를 "가드 없음"으로 해석하면 같은 주문을 다시
        보낼 수 있다. 조회 실패는 일시적인 주문 중단보다 훨씬 위험하므로
        fail-closed로 취급한다.
        """
        with _lock:
            cls._purge_expired()
            # 1차: 인메모리 캐시 확인
            if symbol in _pending_orders:
                return True
            # 2차: DB 확인 (프로세스 재시작 후 복구용)
            try:
                from database.repositories import has_pending_order_guard
                return has_pending_order_guard(symbol)
            except Exception as exc:
                logger.error(
                    "OrderGuard DB 조회 실패 — 중복 주문 방지를 위해 pending으로 간주: {}",
                    exc,
                )
                return True

    @classmethod
    def mark_pending(cls, symbol: str, ttl_seconds: int) -> bool:
        """동일 종목 주문권을 원자적으로 획득한다.

        ``has_pending()`` 뒤 별도 upsert를 하는 check-then-set 방식은 서로
        다른 프로세스가 동시에 통과할 수 있다. DB UNIQUE 제약을 이용한
        ``claim_order_guard`` 한 번으로 판정과 기록을 묶는다. ``False``는
        다른 실행 주체가 이미 주문권을 갖고 있다는 뜻이고, DB 오류는
        호출자에게 전파해 주문을 fail-closed 시킨다.
        """
        with _lock:
            cls._purge_expired()
            if symbol in _pending_orders:
                return False

            expires_at = datetime.now() + timedelta(seconds=max(1, ttl_seconds))
            from database.repositories import claim_order_guard

            claimed = claim_order_guard(symbol, expires_at)
            if not claimed:
                return False

            _pending_orders[symbol] = expires_at
            return True

    @classmethod
    def clear(cls, symbol: str) -> None:
        with _lock:
            _pending_orders.pop(symbol, None)
            try:
                from database.repositories import clear_order_guard
                clear_order_guard(symbol)
            except Exception:
                pass

    @classmethod
    def extend_pending(cls, symbol: str, ttl_seconds: int) -> None:
        """이미 획득한 가드를 원장 재조정용 장기 TTL로 연장한다."""
        with _lock:
            expires_at = datetime.now() + timedelta(seconds=max(1, ttl_seconds))
            _pending_orders[symbol] = expires_at
            from database.repositories import save_order_guard

            # save_order_guard는 기존 행을 갱신한다. DB 장애가 나더라도 인메모리
            # 가드는 현재 프로세스에서 유지되며, 호출자는 별도로 global HALT를 건다.
            save_order_guard(symbol, expires_at)

    @classmethod
    def _purge_expired(cls) -> None:
        now = datetime.now()
        expired = [symbol for symbol, expires_at in _pending_orders.items() if expires_at <= now]
        for symbol in expired:
            _pending_orders.pop(symbol, None)
