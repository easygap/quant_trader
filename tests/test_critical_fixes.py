"""
감사 Critical/High 수정 검증 테스트

1. --force-live 제거 확인
2. OrderGuard mark_pending/clear 타이밍
3. sync_with_broker PositionLock
4. signal_at 마이그레이션
5. walk-forward non-zero windows
6. 벤치마크 거래비용 반영
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime


# ── 1. --force-live 제거 확인 ──

class TestForceLiveRemoved:
    def test_no_force_live_in_argparse(self):
        """main.py argparse에 --force-live 옵션이 없어야 한다."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py",
        )
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        # argparse에 --force-live가 add_argument로 등록되어 있지 않아야 함
        assert '"--force-live"' not in source, "--force-live가 argparse에 남아 있음"

    def test_gate_always_runs(self):
        """gate 체크가 조건 없이 항상 실행되어야 한다."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py",
        )
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        # "if not force_live:" 패턴이 없어야 함
        assert "if not force_live" not in source, "force_live 분기가 남아 있음"
        # gate 호출이 무조건 실행되어야 함
        assert "gate_issues = _check_live_readiness_gate(" in source

    def test_gate_missing_file_adds_error(self):
        """approved_strategies.json 미존재 시 issues에 에러가 추가되어야 한다."""
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py",
        )
        with open(main_path, "r", encoding="utf-8") as f:
            source = f.read()
        # 파일 미존재 시 에러 추가 코드가 있어야 함
        assert '승인 파일 없음' in source, "파일 미존재 시 에러 메시지가 없음"


# ── 2. OrderGuard 타이밍 ──

class TestOrderGuardTiming:
    def test_mark_pending_before_api_call_buy(self):
        """execute_buy에서 mark_pending이 KIS API 호출 이전이어야 한다."""
        from core.order_executor import OrderExecutor
        import inspect
        source = inspect.getsource(OrderExecutor)

        # mark_pending 위치가 buy_order 호출보다 앞에 있어야 함
        lines = source.split('\n')
        mark_idx = None
        api_idx = None
        for i, line in enumerate(lines):
            if 'mark_pending' in line and 'BUY' not in line.upper() or (mark_idx is None and 'OrderGuard.mark_pending' in line):
                if mark_idx is None:
                    mark_idx = i
            if 'self.kis_api.buy_order' in line:
                api_idx = i
                break

        # 코드에서 mark_pending이 buy_order 이전에 나타나야 함
        assert mark_idx is not None, "mark_pending 호출을 찾을 수 없음"
        assert api_idx is not None, "buy_order 호출을 찾을 수 없음"
        assert mark_idx < api_idx, f"mark_pending(line {mark_idx})이 buy_order(line {api_idx}) 이후에 있음"

    def test_clear_called_after_success(self):
        """체결 성공 후 OrderGuard.clear()가 호출되어야 한다."""
        from core.order_executor import OrderExecutor
        import inspect
        source = inspect.getsource(OrderExecutor)
        assert "OrderGuard.clear(symbol)" in source, "OrderGuard.clear() 호출이 없음"
        # clear가 2번 이상 있어야 함 (buy + sell)
        assert source.count("OrderGuard.clear(symbol)") >= 2, "clear()가 buy/sell 양쪽에 있어야 함"

    def test_clear_on_failure(self):
        """주문 실패 시에도 OrderGuard.clear()가 호출되어야 한다."""
        from core.order_executor import OrderExecutor
        import inspect
        source = inspect.getsource(OrderExecutor)
        # "order_result is None" 체크 근처에 clear가 있어야 함
        lines = source.split('\n')
        found_clear_on_fail = False
        for i, line in enumerate(lines):
            if 'order_result is None' in line:
                # 주변 3줄 안에 clear가 있어야 함
                context = '\n'.join(lines[max(0, i-1):i+3])
                if 'OrderGuard.clear' in context:
                    found_clear_on_fail = True
                    break
        assert found_clear_on_fail, "주문 실패 시 OrderGuard.clear() 호출이 없음"


# ── 3. sync_with_broker PositionLock ──

class TestSyncWithBrokerLock:
    def test_sync_uses_position_lock(self):
        """sync_with_broker가 PositionLock을 사용해야 한다."""
        from core.portfolio_manager import PortfolioManager
        import inspect
        source = inspect.getsource(PortfolioManager.sync_with_broker)
        assert "PositionLock" in source, "sync_with_broker에 PositionLock이 없음"

    def test_position_lock_imported(self):
        """portfolio_manager에 PositionLock이 import되어 있어야 한다."""
        import core.portfolio_manager as pm_module
        import inspect
        source = inspect.getsource(pm_module)
        assert "from core.position_lock import PositionLock" in source


# ── 4. signal_at 마이그레이션 ──

class TestSignalAtMigration:
    def test_migration_function_exists(self):
        """signal_at 마이그레이션 함수가 존재해야 한다."""
        from database.models import _migrate_trade_history_signal_columns
        assert callable(_migrate_trade_history_signal_columns)

    def test_init_database_calls_migration(self):
        """init_database가 signal 마이그레이션을 호출해야 한다."""
        import database.models as models_module
        import inspect
        source = inspect.getsource(models_module.init_database)
        assert "_migrate_trade_history_signal_columns" in source

    def test_migration_on_fresh_db(self):
        """신규 DB에서 마이그레이션이 에러 없이 실행되어야 한다."""
        import tempfile, os
        from sqlalchemy import create_engine, text
        from database.models import Base, _migrate_trade_history_signal_columns

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            # 마이그레이션 실행 (이미 컬럼이 있으므로 skip)
            _migrate_trade_history_signal_columns(engine)
            # 컬럼 확인
            with engine.connect() as conn:
                r = conn.execute(text("PRAGMA table_info(trade_history)"))
                cols = [row[1] for row in r.fetchall()]
            engine.dispose()
            assert "signal_at" in cols, f"signal_at 컬럼 없음: {cols}"
            assert "order_at" in cols, f"order_at 컬럼 없음: {cols}"
            assert "price_gap" in cols, f"price_gap 컬럼 없음: {cols}"
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_migration_on_existing_db_without_columns(self):
        """기존 DB(signal_at 없는)에서 마이그레이션이 컬럼을 추가해야 한다."""
        import tempfile, os
        from sqlalchemy import create_engine, text, Column, Integer, String, Float, DateTime
        from sqlalchemy.orm import DeclarativeBase
        from database.models import _migrate_trade_history_signal_columns

        class OldBase(DeclarativeBase):
            pass

        class OldTradeHistory(OldBase):
            __tablename__ = "trade_history"
            id = Column(Integer, primary_key=True)
            symbol = Column(String(20))
            action = Column(String(20))
            price = Column(Float)
            quantity = Column(Integer)
            total_amount = Column(Float)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            engine = create_engine(f"sqlite:///{db_path}")
            OldBase.metadata.create_all(engine)

            # 마이그레이션 전: signal_at 없음
            with engine.connect() as conn:
                r = conn.execute(text("PRAGMA table_info(trade_history)"))
                cols_before = [row[1] for row in r.fetchall()]
            assert "signal_at" not in cols_before

            # 마이그레이션 실행
            _migrate_trade_history_signal_columns(engine)

            # 마이그레이션 후: signal_at 있어야 함
            with engine.connect() as conn:
                r = conn.execute(text("PRAGMA table_info(trade_history)"))
                cols_after = [row[1] for row in r.fetchall()]
            engine.dispose()
            assert "signal_at" in cols_after, f"마이그레이션 후 signal_at 없음: {cols_after}"
            assert "order_at" in cols_after
            assert "price_gap" in cols_after
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


# ── 5. Walk-forward non-zero windows ──

class TestWalkForwardWindows:
    def test_wf_produces_nonzero_windows(self):
        """5년 데이터로 walk-forward 시 최소 1개 이상의 window가 생성되어야 한다."""
        from config.config_loader import Config
        from backtest.strategy_validator import StrategyValidator

        config = Config.get()
        v = StrategyValidator(config)
        result = v.run_walk_forward(
            symbol="005930",
            strategy_name="scoring",
            start_date="2021-01-01",
            end_date="2025-12-31",
            validation_years=5,
            train_days=504,
            test_days=252,
            step_days=252,
        )
        n_total = result.get("n_total", 0)
        assert n_total > 0, f"walk-forward windows={n_total}, 1개 이상이어야 함"

    def test_wf_result_has_flat_keys(self):
        """WF 결과가 flat 구조(summary 서브키 없음)로 반환되어야 한다."""
        from config.config_loader import Config
        from backtest.strategy_validator import StrategyValidator

        config = Config.get()
        v = StrategyValidator(config)
        result = v.run_walk_forward(
            symbol="005930",
            strategy_name="breakout_volume",
            start_date="2021-01-01",
            end_date="2025-12-31",
        )
        # flat 키 확인
        assert "n_total" in result, "n_total 키가 없음"
        assert "pass_rate" in result, "pass_rate 키가 없음"
        assert "avg_oos_sharpe" in result, "avg_oos_sharpe 키가 없음"


# ── 6. 벤치마크 거래비용 ──

class TestBenchmarkCosts:
    def test_benchmark_applies_transaction_costs(self):
        """벤치마크 B&H가 거래비용을 반영해야 한다."""
        from config.config_loader import Config
        from backtest.strategy_validator import StrategyValidator
        import inspect

        config = Config.get()
        v = StrategyValidator(config)
        source = inspect.getsource(v._buy_and_hold_metrics)
        assert "commission" in source, "벤치마크에 commission 반영 코드 없음"
        assert "tax" in source, "벤치마크에 tax 반영 코드 없음"
        assert "slippage" in source, "벤치마크에 slippage 반영 코드 없음"

    def test_benchmark_return_lower_than_gross(self):
        """거래비용 반영 시 벤치마크 수익률이 gross보다 낮아야 한다."""
        import pandas as pd
        import numpy as np
        from config.config_loader import Config
        from backtest.strategy_validator import StrategyValidator

        config = Config.get()
        v = StrategyValidator(config)

        # 테스트용 데이터 생성: 100일간 10% 상승
        dates = pd.date_range("2024-01-01", periods=100, freq="B")
        prices = np.linspace(50000, 55000, 100)
        df = pd.DataFrame({"close": prices}, index=dates)

        result = v._buy_and_hold_metrics(df, 10_000_000)
        # 비용 반영 후 total_return은 10% 미만이어야 함
        assert result["total_return"] < 10.0, (
            f"비용 반영 후 수익률({result['total_return']}%)이 gross(10%)보다 높음"
        )
