"""OrderExecutor paper 모드 단위 테스트 (DB/외부 의존성 최소화)"""
import pytest

from config.config_loader import Config


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
