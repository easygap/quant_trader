"""
중복 주문 방지 가드 (실전 10분 루프 타이밍 리스크 대응).

주문 전 검사 순서 (OrderExecutor live 모드):
  ① OrderGuard.has_pending(symbol) — 앱 레벨 TTL: 최근 주문 접수 후 N초 동안 동일 종목 추가 주문 차단
  ② KIS API 미체결 조회 — 해당 종목에 미체결 주문이 있으면 주문 보류 (브로커 실제 상태 반영)

루프 10분 초과 시 다음 모니터링 사이클 스킵은 Scheduler._run_monitoring finally 블록에서 처리.
"""

from datetime import datetime, timedelta
import threading


_lock = threading.RLock()
_pending_orders: dict[str, datetime] = {}


class OrderGuard:
    """동일 종목 중복 주문을 막기 위한 인메모리 가드 (주문 전 미체결·최근 주문 존재 여부 확인 ①)."""

    @classmethod
    def has_pending(cls, symbol: str) -> bool:
        with _lock:
            cls._purge_expired()
            return symbol in _pending_orders

    @classmethod
    def mark_pending(cls, symbol: str, ttl_seconds: int) -> None:
        with _lock:
            expires_at = datetime.now() + timedelta(seconds=max(1, ttl_seconds))
            _pending_orders[symbol] = expires_at

    @classmethod
    def clear(cls, symbol: str) -> None:
        with _lock:
            _pending_orders.pop(symbol, None)

    @classmethod
    def _purge_expired(cls) -> None:
        now = datetime.now()
        expired = [symbol for symbol, expires_at in _pending_orders.items() if expires_at <= now]
        for symbol in expired:
            _pending_orders.pop(symbol, None)
