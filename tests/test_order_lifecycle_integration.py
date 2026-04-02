"""
Order Lifecycle 통합 테스트 + sync_with_broker 경쟁 상태 + live gate 우회 재감사
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import threading
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


# ── 1. OrderGuard 상태 전이 ──

class TestOrderGuardLifecycle:
    def setup_method(self):
        from core.order_guard import OrderGuard
        OrderGuard._pending = {}

    def test_pending_set_and_cleared(self):
        from core.order_guard import OrderGuard
        OrderGuard.mark_pending("005930", ttl_seconds=10)
        assert OrderGuard.has_pending("005930")
        OrderGuard.clear("005930")
        assert not OrderGuard.has_pending("005930")

    def test_ttl_expiry(self):
        from core.order_guard import OrderGuard
        OrderGuard.mark_pending("005930", ttl_seconds=1)
        assert OrderGuard.has_pending("005930")
        time.sleep(1.2)
        assert not OrderGuard.has_pending("005930")

    def test_clear_idempotent(self):
        from core.order_guard import OrderGuard
        OrderGuard.clear("005930")
        OrderGuard.clear("005930")
        assert not OrderGuard.has_pending("005930")

    def test_multiple_symbols_independent(self):
        from core.order_guard import OrderGuard
        OrderGuard.mark_pending("005930", ttl_seconds=10)
        OrderGuard.mark_pending("000660", ttl_seconds=10)
        OrderGuard.clear("005930")
        assert not OrderGuard.has_pending("005930")
        assert OrderGuard.has_pending("000660")
        OrderGuard.clear("000660")

    def test_duplicate_order_blocked_via_orderbook(self):
        """OrderBook에서 미완료 주문이 있으면 동일 종목 중복이 차단되어야 함."""
        from core.order_state import OrderBook, OrderStatus
        book = OrderBook()
        o = book.create_order(symbol="005930", action="BUY", requested_qty=10,
                              requested_price=50000)
        o.transition(OrderStatus.SUBMITTED)
        # 미완료 주문 존재
        assert book.has_open_order("005930")
        # 이 상태에서 새 주문은 차단해야 함
        open_orders = book.get_open_orders("005930")
        assert len(open_orders) > 0
        # 체결 후 해제
        o.transition(OrderStatus.ACKED)
        o.transition(OrderStatus.FILLED, fill_qty=10, fill_price=50100)
        assert not book.has_open_order("005930")

    def test_api_failure_clears_guard(self):
        """KIS API 실패 시 OrderGuard가 해제되어야 함 — 코드 구조 확인."""
        from core.order_executor import OrderExecutor
        import inspect
        source = inspect.getsource(OrderExecutor)
        # "order_result is None" 다음에 "OrderGuard.clear" 가 있어야 함
        lines = source.split('\n')
        found = False
        for i, line in enumerate(lines):
            if 'order_result is None' in line:
                context = '\n'.join(lines[i:i+3])
                if 'OrderGuard.clear' in context:
                    found = True
                    break
        assert found, "API 실패 후 OrderGuard.clear()가 없음"


# ── 2. sync_with_broker PositionLock 경쟁 상태 ──

class TestSyncBrokerConcurrency:
    def test_sync_acquires_position_lock(self):
        """sync_with_broker가 PositionLock을 획득하는지 확인."""
        from core.portfolio_manager import PortfolioManager
        import inspect
        source = inspect.getsource(PortfolioManager.sync_with_broker)
        assert "PositionLock" in source

    def test_position_lock_is_reentrant(self):
        """PositionLock이 같은 스레드에서 재진입 가능해야 (RLock 기반)."""
        from core.position_lock import PositionLock
        with PositionLock():
            with PositionLock():
                pass  # deadlock 없으면 통과

    def test_concurrent_lock_acquisition(self):
        """두 스레드가 PositionLock을 동시에 잡을 수 없어야 함."""
        from core.position_lock import PositionLock

        results = []
        lock_held = threading.Event()
        proceed = threading.Event()

        def thread_a():
            with PositionLock():
                lock_held.set()
                results.append(("A", "acquired"))
                proceed.wait(timeout=2)
            results.append(("A", "released"))

        def thread_b():
            lock_held.wait(timeout=2)
            time.sleep(0.05)  # A가 확실히 lock 보유 중
            results.append(("B", "waiting"))
            with PositionLock():
                results.append(("B", "acquired"))

        t_a = threading.Thread(target=thread_a)
        t_b = threading.Thread(target=thread_b)
        t_a.start()
        t_b.start()

        time.sleep(0.1)
        # 이 시점에서 B는 waiting 상태여야 함
        b_states = [r for r in results if r[0] == "B"]
        assert any(s == "waiting" for _, s in b_states), "B가 대기 상태가 아님"
        assert not any(s == "acquired" for _, s in b_states), "B가 A보다 먼저 lock을 획득함"

        proceed.set()
        t_a.join(timeout=3)
        t_b.join(timeout=3)

        a_acquired = next(i for i, r in enumerate(results) if r == ("A", "acquired"))
        b_acquired = next(i for i, r in enumerate(results) if r == ("B", "acquired"))
        assert a_acquired < b_acquired, "A가 B보다 먼저 lock을 획득해야 함"


# ── 3. Live Gate 우회 재감사 ──

class TestLiveGateBypass:
    def test_no_force_live_flag(self):
        """--force-live가 argparse에 없어야 함."""
        main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert '"--force-live"' not in source

    def test_gate_runs_unconditionally(self):
        """gate 체크가 force_live 분기 없이 항상 실행."""
        main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "if not force_live" not in source

    def test_missing_approved_file_blocks(self):
        """approved_strategies.json 없으면 gate가 에러를 반환."""
        main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "승인 파일 없음" in source

    def test_malformed_json_blocks(self):
        """승인 파일 파싱 오류 시 gate가 에러를 반환."""
        main_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "승인 파일 파싱 오류" in source

    def test_strategy_gate_blocks_all_live(self):
        """모든 등록 전략이 live 모드에서 차단."""
        from strategies import STRATEGY_STATUS, is_strategy_allowed
        for name in STRATEGY_STATUS:
            allowed, _ = is_strategy_allowed(name, "live")
            assert not allowed, f"{name}이 live 허용됨"

    def test_direct_function_call_still_blocked(self):
        """_check_live_readiness_gate를 직접 호출해도 issues를 반환."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # gate 함수를 import하여 직접 호출
        import importlib
        main_mod = importlib.import_module("main")
        gate_fn = getattr(main_mod, "_check_live_readiness_gate", None)
        if gate_fn is None:
            pytest.skip("_check_live_readiness_gate를 import할 수 없음")
        from config.config_loader import Config
        config = Config.get()
        issues = gate_fn(config, "scoring")
        assert len(issues) > 0, "gate가 issues를 반환하지 않음"


# ── 4. signal_at migration idempotent ──

class TestSignalAtMigrationIdempotent:
    def test_double_migration_no_error(self):
        """마이그레이션을 2번 실행해도 에러가 나지 않아야 함."""
        import tempfile
        from sqlalchemy import create_engine
        from database.models import Base, _migrate_trade_history_signal_columns

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            _migrate_trade_history_signal_columns(engine)
            _migrate_trade_history_signal_columns(engine)  # 2번째 — idempotent
            engine.dispose()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass
