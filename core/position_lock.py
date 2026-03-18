"""
포지션/주문 상태 접근 직렬화용 Lock
- 스케줄러와 웹소켓 등 동시 접근 시 race condition 방지
- threading.RLock 기반 컨텍스트 매니저
"""

import threading

_position_lock = threading.RLock()


class PositionLock:
    """
    포지션·주문 관련 공유 자원 접근 시 사용하는 Lock.
    OrderExecutor.execute_buy/execute_sell, Scheduler._run_monitoring 등에서 사용.
    """

    @staticmethod
    def acquire():
        _position_lock.acquire()

    @staticmethod
    def release():
        _position_lock.release()

    def __enter__(self):
        _position_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        _position_lock.release()
        return False
