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
        with _lock:
            cls._purge_expired()
            # 1차: 인메모리 캐시 확인
            if symbol in _pending_orders:
                return True
            # 2차: DB 확인 (프로세스 재시작 후 복구용)
            try:
                from database.repositories import has_pending_order_guard
                return has_pending_order_guard(symbol)
            except Exception:
                return False

    @classmethod
    def mark_pending(cls, symbol: str, ttl_seconds: int) -> None:
        with _lock:
            expires_at = datetime.now() + timedelta(seconds=max(1, ttl_seconds))
            _pending_orders[symbol] = expires_at
            # DB에도 기록
            try:
                from database.repositories import save_order_guard
                save_order_guard(symbol, expires_at)
            except Exception as e:
                logger.debug("OrderGuard DB 저장 실패 (인메모리만 사용): {}", e)

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
    def _purge_expired(cls) -> None:
        now = datetime.now()
        expired = [symbol for symbol, expires_at in _pending_orders.items() if expires_at <= now]
        for symbol in expired:
            _pending_orders.pop(symbol, None)
