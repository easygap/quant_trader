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
from types import SimpleNamespace


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

    def test_live_liquidate_requires_env_confirmation(self, monkeypatch):
        """live 설정의 liquidate는 환경변수 확인 없이는 포지션 조회 전 종료해야 한다."""
        import main as main_mod
        import database.repositories as repositories

        monkeypatch.setattr(
            main_mod.Config,
            "get",
            lambda: SimpleNamespace(trading={"mode": "live"}),
        )
        monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
        get_positions = MagicMock(return_value=[
            SimpleNamespace(symbol="005930", avg_price=60_000, account_key="")
        ])
        monkeypatch.setattr(repositories, "get_all_positions", get_positions)

        with pytest.raises(SystemExit):
            main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=False))

        get_positions.assert_not_called()

    def test_live_liquidate_requires_confirm_flag(self, monkeypatch):
        """ENABLE_LIVE_TRADING=true여도 --confirm-live 없이는 live liquidate를 막는다."""
        import main as main_mod
        import database.repositories as repositories

        monkeypatch.setattr(
            main_mod.Config,
            "get",
            lambda: SimpleNamespace(trading={"mode": "live"}),
        )
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        get_positions = MagicMock(return_value=[
            SimpleNamespace(symbol="005930", avg_price=60_000, account_key="")
        ])
        monkeypatch.setattr(repositories, "get_all_positions", get_positions)

        with pytest.raises(SystemExit):
            main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=False))

        get_positions.assert_not_called()

    def test_live_rebalance_requires_env_confirmation_before_basket_execution(self, monkeypatch):
        """live 설정의 rebalance는 환경변수 확인 없이는 바스켓 실행 전 종료해야 한다."""
        import main as main_mod

        config = SimpleNamespace(trading={"mode": "live"}, active_strategy="scoring")
        basket_cls = MagicMock()
        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", basket_cls)
        monkeypatch.setattr(
            "core.notifier.Notifier",
            lambda cfg: SimpleNamespace(send_message=lambda message: None),
        )
        monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)

        with pytest.raises(SystemExit):
            main_mod.run_rebalance(SimpleNamespace(
                basket="test_basket",
                dry_run=False,
                confirm_live=False,
                strategy="scoring",
            ))

        basket_cls.assert_not_called()

    def test_live_rebalance_gate_failure_blocks_before_basket_execution(self, monkeypatch):
        """확인 플래그가 있어도 live gate 실패 시 리밸런싱 주문 계획으로 넘어가지 않는다."""
        import main as main_mod

        config = SimpleNamespace(trading={"mode": "live"}, active_strategy="scoring")
        basket_cls = MagicMock()
        gate = MagicMock(return_value=["promotion evidence 부족"])
        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(main_mod, "_check_live_readiness_gate", gate)
        monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", basket_cls)
        monkeypatch.setattr(
            "core.notifier.Notifier",
            lambda cfg: SimpleNamespace(send_message=lambda message: None),
        )
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        with pytest.raises(SystemExit) as exc:
            main_mod.run_rebalance(SimpleNamespace(
                basket="test_basket",
                dry_run=False,
                confirm_live=True,
                strategy="scoring",
            ))

        assert exc.value.code == 1
        gate.assert_called_once_with(config, "basket_rebalance:test_basket")
        basket_cls.assert_not_called()

    def test_live_rebalance_passes_confirmed_gate_to_executor(self, monkeypatch):
        """live gate를 통과한 rebalance만 core 실행부에 confirmed 상태로 전달한다."""
        import main as main_mod

        calls = {}
        config = SimpleNamespace(trading={"mode": "live"}, active_strategy="scoring")

        class FakeRebalancer:
            def __init__(self, basket_name, config, account_key="", execution_strategy=""):
                calls["basket_name"] = basket_name
                calls["config"] = config
                calls["account_key"] = account_key
                calls["execution_strategy"] = execution_strategy
                self.portfolio_mgr = SimpleNamespace(
                    sync_with_broker=MagicMock(return_value={"ok": True})
                )

            def get_status_report(self):
                return "status"

            def should_rebalance(self):
                return True, "드리프트"

            def plan_rebalance(self):
                return ["order"]

            def execute(self, orders, dry_run=False, live_confirmed=False):
                calls["execute"] = {
                    "orders": orders,
                    "dry_run": dry_run,
                    "live_confirmed": live_confirmed,
                }
                return {"executed": 1, "skipped": 0, "failed": 0}

            def save_daily_nav_snapshot(self):
                return True

        gate = MagicMock(return_value=[])
        notifier = SimpleNamespace(send_message=MagicMock())
        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(main_mod, "_check_live_readiness_gate", gate)
        monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
        monkeypatch.setattr("core.notifier.Notifier", lambda cfg: notifier)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        main_mod.run_rebalance(SimpleNamespace(
            basket="test_basket",
            dry_run=False,
            confirm_live=True,
            strategy="scoring",
        ))

        gate.assert_called_once_with(config, "basket_rebalance:test_basket")
        assert calls["basket_name"] == "test_basket"
        assert calls["account_key"] == "basket_rebalance:test_basket"
        assert calls["execution_strategy"] == "basket_rebalance:test_basket"
        assert calls["execute"]["live_confirmed"] is True
        assert calls["execute"]["dry_run"] is False
        notifier.send_message.assert_called_once()

    def test_live_rebalance_sync_failure_blocks_before_orders(self, monkeypatch):
        """live 리밸런싱은 승인 계좌 잔고 동기화 실패 시 주문 계획 후 실행하지 않는다."""
        import main as main_mod

        calls = {}
        config = SimpleNamespace(trading={"mode": "live"}, active_strategy="scoring")

        class FakeRebalancer:
            def __init__(self, basket_name, config, account_key="", execution_strategy=""):
                calls["account_key"] = account_key
                calls["execution_strategy"] = execution_strategy
                self.portfolio_mgr = SimpleNamespace(
                    sync_with_broker=MagicMock(
                        return_value={"ok": False, "message": "position mismatch"}
                    )
                )

            def get_status_report(self):
                calls["status_report"] = True
                return "status"

            def should_rebalance(self):
                calls["should_rebalance"] = True
                return True, "드리프트"

            def plan_rebalance(self):
                calls["plan_rebalance"] = True
                return ["order"]

            def execute(self, *args, **kwargs):
                calls["execute"] = True
                return {"executed": 1, "skipped": 0, "failed": 0}

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(main_mod, "_check_live_readiness_gate", MagicMock(return_value=[]))
        monkeypatch.setattr("core.basket_rebalancer.BasketRebalancer", FakeRebalancer)
        monkeypatch.setattr(
            "core.notifier.Notifier",
            lambda cfg: SimpleNamespace(send_message=MagicMock()),
        )
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        with pytest.raises(SystemExit) as exc:
            main_mod.run_rebalance(SimpleNamespace(
                basket="test_basket",
                dry_run=False,
                confirm_live=True,
                strategy="scoring",
            ))

        assert exc.value.code == 1
        assert calls["account_key"] == "basket_rebalance:test_basket"
        assert calls["execution_strategy"] == "basket_rebalance:test_basket"
        assert "status_report" not in calls
        assert "execute" not in calls

    def test_paper_mode_forces_live_config_to_paper_before_executor(self, monkeypatch):
        """--mode paper 실행은 settings.trading.mode=live여도 OrderExecutor를 paper로 고정한다."""
        import pandas as pd
        import main as main_mod
        import database.repositories as repositories
        import strategies

        captured = {}
        config = SimpleNamespace(
            trading={"mode": "live"},
            active_strategy="scoring",
            watchlist=["005930"],
            risk_params={"position_limits": {"max_holding_days": 0}},
        )
        sample = pd.DataFrame(
            {
                "close": [60_000] * 31,
                "volume": [1_000_000] * 31,
            }
        )

        class FakeCollector:
            def fetch_stock(self, symbol):
                return sample

        class FakePortfolio:
            def __init__(self, cfg, account_key=""):
                captured["portfolio_mode"] = cfg.trading["mode"]

            def get_portfolio_summary(self):
                return {
                    "total_value": 10_000_000,
                    "total_return": 0.0,
                    "position_count": 0,
                    "cash": 10_000_000,
                    "current_value": 0,
                }

        class FakeExecutor:
            def __init__(self, cfg, account_key=""):
                captured["executor_mode"] = cfg.trading["mode"]

        class FakeStrategy:
            def generate_signal(self, df, symbol=None):
                return {
                    "signal": "HOLD",
                    "score": 0,
                    "details": "test",
                    "close": 60_000,
                }

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(strategies, "is_strategy_allowed", lambda strategy, mode: (True, "ok"))
        monkeypatch.setattr("core.data_collector.DataCollector", FakeCollector)
        monkeypatch.setattr("core.portfolio_manager.PortfolioManager", FakePortfolio)
        monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)
        monkeypatch.setattr(
            "core.notifier.Notifier",
            lambda cfg: SimpleNamespace(
                send_signal_alert=lambda *a, **kw: None,
                send_trade_alert=lambda *a, **kw: None,
            ),
        )
        monkeypatch.setattr(
            main_mod,
            "WatchlistManager",
            lambda cfg: SimpleNamespace(resolve=lambda: ["005930"]),
        )
        monkeypatch.setattr(main_mod, "_get_strategy", lambda strategy: FakeStrategy())
        monkeypatch.setattr(repositories, "get_all_positions", lambda account_key=None: [])
        monkeypatch.setattr(repositories, "get_position", lambda symbol, account_key="": None)

        main_mod.run_paper_trading(SimpleNamespace(strategy="scoring"))

        assert config.trading["mode"] == "paper"
        assert captured["portfolio_mode"] == "paper"
        assert captured["executor_mode"] == "paper"

    def test_live_liquidate_syncs_broker_positions_before_loading_db_positions(self, monkeypatch):
        """live 긴급 청산은 KIS-only 포지션을 DB에 보정한 뒤 청산 대상을 읽는다."""
        import main as main_mod
        import database.repositories as repositories

        calls = []
        sells = []
        config = SimpleNamespace(
            trading={"mode": "live"},
            kis_api={"accounts": {}},
            get_account_no=lambda account_key="": "12345678-01",
        )

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        class FakePortfolio:
            def __init__(self, cfg, account_key=""):
                self.account_key = account_key

            def sync_with_broker(self, auto_correct=True):
                calls.append(("sync", self.account_key, auto_correct))
                return {
                    "ok": False,
                    "corrected": [{"symbol": "005930", "type": "kis_only"}],
                    "message": "KIS-only 포지션 DB 반영",
                }

        def fake_get_all_positions():
            calls.append(("positions",))
            assert calls[0] == ("sync", "", True)
            return [
                SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
            ]

        class FakeKIS:
            def __init__(self, account_no=None):
                self.account_no = account_no

            def get_current_price(self, symbol):
                calls.append(("price", symbol, self.account_no))
                return {"price": 61_000}

        class FakeExecutor:
            def __init__(self, cfg, account_key=""):
                self.account_key = account_key

            def execute_sell(self, symbol, price, quantity=None, reason="", strategy=""):
                sells.append({
                    "account_key": self.account_key,
                    "symbol": symbol,
                    "price": price,
                    "quantity": quantity,
                    "reason": reason,
                    "strategy": strategy,
                })
                return {"success": True}

        monkeypatch.setattr("core.portfolio_manager.PortfolioManager", FakePortfolio)
        monkeypatch.setattr(repositories, "get_all_positions", fake_get_all_positions)
        monkeypatch.setattr("api.kis_api.KISApi", FakeKIS)
        monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)

        summary = main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=True))

        assert calls[:2] == [("sync", "", True), ("positions",)]
        assert sells == [{
            "account_key": "",
            "symbol": "005930",
            "price": 61_000.0,
            "quantity": None,
            "reason": "긴급 전량 청산 (--mode liquidate)",
            "strategy": "emergency_liquidate",
        }]
        assert summary["attempted"] == 1
        assert summary["succeeded"] == 1
        assert summary["failed"] == 0

    def test_live_liquidate_does_not_fallback_to_avg_price_when_current_price_missing(self, monkeypatch):
        """live 긴급 청산은 현재가 조회 실패 시 평균단가 지정가 매도를 내지 않는다."""
        import main as main_mod
        import database.repositories as repositories

        calls = []
        sells = []
        notifications = []
        config = SimpleNamespace(
            trading={"mode": "live"},
            kis_api={"accounts": {}},
            get_account_no=lambda account_key="": "12345678-01",
        )

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        monkeypatch.setattr(main_mod, "_sync_live_positions_before_liquidation", lambda cfg: [])
        monkeypatch.setattr(
            repositories,
            "get_all_positions",
            lambda: [
                SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
            ],
        )

        class FakeKIS:
            def __init__(self, account_no=None):
                self.account_no = account_no

            def get_current_price(self, symbol):
                calls.append(("price", symbol, self.account_no))
                return None

        class FakeExecutor:
            def __init__(self, cfg, account_key=""):
                self.account_key = account_key

            def execute_sell(self, symbol, price, quantity=None, reason="", strategy=""):
                sells.append({
                    "symbol": symbol,
                    "price": price,
                    "quantity": quantity,
                    "reason": reason,
                    "strategy": strategy,
                })
                raise AssertionError("현재가 실패 시 live 매도 주문을 호출하면 안 됨")

        class FakeNotifier:
            def __init__(self, cfg):
                self.cfg = cfg

            def send_message(self, text, critical=False):
                notifications.append({"text": text, "critical": critical})

        monkeypatch.setattr("api.kis_api.KISApi", FakeKIS)
        monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)
        monkeypatch.setattr("core.notifier.Notifier", FakeNotifier)

        summary = main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=True))

        assert calls == [("price", "005930", "12345678-01")]
        assert sells == []
        assert summary["attempted"] == 1
        assert summary["succeeded"] == 0
        assert summary["failed"] == 1
        assert summary["details"] == [{
            "symbol": "005930",
            "account_key": "",
            "status": "failed",
            "reason": "실전 긴급 청산 현재가 조회 실패",
        }]
        assert notifications and "실패 상세" in notifications[0]["text"]

    def test_live_liquidate_aborts_when_broker_sync_fails_before_position_load(self, monkeypatch):
        """live 긴급 청산 전 KIS↔DB 동기화 실패가 남으면 stale DB 포지션만으로 진행하지 않는다."""
        import main as main_mod
        import database.repositories as repositories

        config = SimpleNamespace(
            trading={"mode": "live"},
            kis_api={"accounts": {}},
            get_account_no=lambda account_key="": "12345678-01",
        )
        get_positions = MagicMock(return_value=[
            SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
        ])

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        class FakePortfolio:
            def __init__(self, cfg, account_key=""):
                pass

            def sync_with_broker(self, auto_correct=True):
                return {"ok": False, "corrected": [], "message": "잔고 조회 실패"}

        monkeypatch.setattr("core.portfolio_manager.PortfolioManager", FakePortfolio)
        monkeypatch.setattr(repositories, "get_all_positions", get_positions)

        with pytest.raises(SystemExit) as exc:
            main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=True))

        assert exc.value.code == 1
        get_positions.assert_not_called()

    def test_live_liquidate_aborts_when_broker_sync_partially_corrects_positions(self, monkeypatch):
        """일부 보정만 성공한 잔고 동기화 결과로는 live 긴급 청산을 시작하지 않는다."""
        import main as main_mod
        import database.repositories as repositories

        config = SimpleNamespace(
            trading={"mode": "live"},
            kis_api={"accounts": {}},
            get_account_no=lambda account_key="": "12345678-01",
        )
        get_positions = MagicMock(return_value=[
            SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
        ])

        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")

        class FakePortfolio:
            def __init__(self, cfg, account_key=""):
                pass

            def sync_with_broker(self, auto_correct=True):
                return {
                    "ok": False,
                    "mismatches": [
                        {"symbol": "005930", "type": "kis_only"},
                        {"symbol": "000660", "type": "kis_only"},
                    ],
                    "corrected": [{"symbol": "005930", "action": "added"}],
                    "message": "일부 포지션 보정 실패",
                }

        monkeypatch.setattr("core.portfolio_manager.PortfolioManager", FakePortfolio)
        monkeypatch.setattr(repositories, "get_all_positions", get_positions)

        with pytest.raises(SystemExit) as exc:
            main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=True))

        assert exc.value.code == 1
        get_positions.assert_not_called()

    def test_http_liquidate_passes_live_confirm_from_env(self, monkeypatch):
        """HTTP 긴급 청산은 별도 환경변수로 live 확인 플래그를 넘긴다."""
        import main as main_mod
        import database.models as db_models
        import monitoring.logger as logger_mod
        import monitoring.liquidate_trigger as trigger

        captured = {}
        monkeypatch.setattr(db_models, "init_database", lambda: None)
        monkeypatch.setattr(logger_mod, "setup_logger", lambda: None)
        monkeypatch.setenv("LIQUIDATE_TRIGGER_CONFIRM_LIVE", "true")

        def fake_run(args):
            captured["confirm_live"] = args.confirm_live

        monkeypatch.setattr(main_mod, "run_emergency_liquidate", fake_run)

        ok, message = trigger._run_liquidate()

        assert ok is True
        assert captured["confirm_live"] is True
        assert "청산 요청 처리 완료" in message

    def test_http_liquidate_converts_system_exit_to_failure(self, monkeypatch):
        """HTTP 긴급 청산 내부 guard 실패는 서버 프로세스를 죽이지 않고 실패 응답으로 변환한다."""
        import main as main_mod
        import database.models as db_models
        import monitoring.logger as logger_mod
        import monitoring.liquidate_trigger as trigger

        captured = {}
        monkeypatch.setattr(db_models, "init_database", lambda: None)
        monkeypatch.setattr(logger_mod, "setup_logger", lambda: None)
        monkeypatch.delenv("LIQUIDATE_TRIGGER_CONFIRM_LIVE", raising=False)

        def fake_run(args):
            captured["confirm_live"] = args.confirm_live
            raise SystemExit(1)

        monkeypatch.setattr(main_mod, "run_emergency_liquidate", fake_run)

        ok, message = trigger._run_liquidate()

        assert ok is False
        assert captured["confirm_live"] is False
        assert "종료 코드=1" in message
        assert "LIQUIDATE_TRIGGER_CONFIRM_LIVE=true" in message

    def test_liquidate_summary_reports_sell_failure(self, monkeypatch):
        """긴급 청산은 개별 매도 실패를 반환 summary에 남긴다."""
        import main as main_mod
        import database.repositories as repositories

        config = SimpleNamespace(trading={"mode": "paper"})
        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(
            repositories,
            "get_all_positions",
            lambda: [
                SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
            ],
        )

        class FakeExecutor:
            def __init__(self, cfg, account_key=""):
                pass

            def execute_sell(self, symbol, price, quantity=None, reason="", strategy=""):
                return {"success": False, "reason": "paper sell rejected"}

        monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)

        summary = main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=False))

        assert summary["attempted"] == 1
        assert summary["succeeded"] == 0
        assert summary["failed"] == 1
        assert summary["details"] == [{
            "symbol": "005930",
            "account_key": "",
            "status": "failed",
            "reason": "paper sell rejected",
        }]

    def test_liquidate_summary_sends_critical_notification(self, monkeypatch):
        """긴급 청산 결과는 통합 알림으로 전파된다."""
        import main as main_mod
        import database.repositories as repositories

        notifications = []
        config = SimpleNamespace(trading={"mode": "paper"})
        monkeypatch.setattr(main_mod.Config, "get", lambda: config)
        monkeypatch.setattr(
            repositories,
            "get_all_positions",
            lambda: [
                SimpleNamespace(symbol="005930", avg_price=60_000, quantity=3, account_key=""),
            ],
        )

        class FakeExecutor:
            def __init__(self, cfg, account_key=""):
                pass

            def execute_sell(self, symbol, price, quantity=None, reason="", strategy=""):
                return {"success": True}

        class FakeNotifier:
            def __init__(self, cfg):
                self.cfg = cfg

            def send_message(self, text, critical=False):
                notifications.append({"text": text, "critical": critical})

        monkeypatch.setattr("core.order_executor.OrderExecutor", FakeExecutor)
        monkeypatch.setattr("core.notifier.Notifier", FakeNotifier)

        summary = main_mod.run_emergency_liquidate(SimpleNamespace(confirm_live=False))

        assert summary["succeeded"] == 1
        assert notifications == [{
            "text": "긴급 청산 완료\n- mode: paper\n- 대상: 1건\n- 성공: 1건\n- 실패: 0건",
            "critical": True,
        }]

    def test_http_liquidate_reports_failed_summary_as_failure(self, monkeypatch):
        """HTTP 긴급 청산은 반환 summary에 실패가 있으면 성공 응답으로 포장하지 않는다."""
        import main as main_mod
        import database.models as db_models
        import monitoring.logger as logger_mod
        import monitoring.liquidate_trigger as trigger

        monkeypatch.setattr(db_models, "init_database", lambda: None)
        monkeypatch.setattr(logger_mod, "setup_logger", lambda: None)
        monkeypatch.setattr(
            main_mod,
            "run_emergency_liquidate",
            lambda args: {"attempted": 2, "succeeded": 1, "failed": 1, "details": []},
        )

        ok, message = trigger._run_liquidate()

        assert ok is False
        assert "실패 1건" in message
        assert "대상=2" in message

    def test_http_liquidate_get_is_method_not_allowed(self):
        """HTTP 긴급 청산은 GET 요청으로 실행되지 않는다."""
        import monitoring.liquidate_trigger as trigger

        captured = {}
        handler = object.__new__(trigger.LiquidateHandler)
        handler.path = "/liquidate?token=secret"
        handler._send = lambda code, body, headers=None: captured.update(
            {"code": code, "body": body, "headers": headers or {}}
        )

        handler.do_GET()

        assert captured["code"] == 405
        assert captured["headers"]["Allow"] == "POST"
        assert "POST /liquidate" in captured["body"]["error"]

    def test_http_liquidate_query_token_disabled_by_default(self, monkeypatch):
        """긴급 청산 token query 인증은 기본 비활성이다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.delenv("LIQUIDATE_TRIGGER_ALLOW_QUERY_TOKEN", raising=False)
        handler = SimpleNamespace(headers={}, path="/liquidate?token=secret")

        assert trigger._get_token_from_request(handler) is None

    def test_http_liquidate_query_token_requires_explicit_opt_in(self, monkeypatch):
        """필요한 경우에만 query token 인증을 명시적으로 허용한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.setenv("LIQUIDATE_TRIGGER_ALLOW_QUERY_TOKEN", "true")
        handler = SimpleNamespace(headers={}, path="/liquidate?token=secret")

        assert trigger._get_token_from_request(handler) == "secret"

    def test_http_liquidate_accepts_bearer_token_header(self, monkeypatch):
        """긴급 청산은 Authorization: Bearer 헤더 인증을 지원한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.delenv("LIQUIDATE_TRIGGER_ALLOW_QUERY_TOKEN", raising=False)
        handler = SimpleNamespace(
            headers={"Authorization": "Bearer secret"},
            path="/liquidate?token=ignored",
        )

        assert trigger._get_token_from_request(handler) == "secret"

    def test_http_liquidate_bind_host_defaults_to_loopback(self, monkeypatch):
        """긴급 청산 HTTP 서버는 기본적으로 로컬 루프백에만 바인드한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.delenv("LIQUIDATE_TRIGGER_HOST", raising=False)

        assert trigger._get_bind_host() == "127.0.0.1"

    def test_http_liquidate_bind_host_requires_explicit_external_override(self, monkeypatch):
        """외부 바인드는 환경변수로 명시했을 때만 사용한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.setenv("LIQUIDATE_TRIGGER_HOST", "0.0.0.0")

        assert trigger._get_bind_host() == "0.0.0.0"

    def test_web_dashboard_defaults_to_loopback(self, monkeypatch):
        """웹 대시보드는 설정이 없을 때 로컬 루프백에만 바인드한다."""
        from monitoring import web_dashboard as wd

        monkeypatch.setattr(wd.Config, "get", lambda: SimpleNamespace(settings={}))

        host, port = wd.resolve_dashboard_bind()

        assert host == "127.0.0.1"
        assert port == 8080

    def test_web_dashboard_uses_configured_or_explicit_host(self, monkeypatch):
        """외부 바인드는 설정 또는 CLI에서 명시한 경우에만 사용한다."""
        from monitoring import web_dashboard as wd

        monkeypatch.setattr(
            wd.Config,
            "get",
            lambda: SimpleNamespace(settings={"dashboard": {"host": "0.0.0.0", "port": 9090}}),
        )

        assert wd.resolve_dashboard_bind() == ("0.0.0.0", 9090)
        assert wd.resolve_dashboard_bind(host="127.0.0.1", port=7070) == ("127.0.0.1", 7070)

    def test_main_dashboard_passes_host_and_port(self, monkeypatch):
        """main dashboard 모드는 host/port 옵션을 web dashboard 실행부에 전달한다."""
        import main as main_mod

        captured = {}

        def fake_run_web_dashboard(host=None, port=None):
            captured["host"] = host
            captured["port"] = port

        monkeypatch.setattr("monitoring.web_dashboard.run_web_dashboard", fake_run_web_dashboard)

        main_mod.run_dashboard(SimpleNamespace(dashboard_host="127.0.0.1", dashboard_port=7777))

        assert captured == {"host": "127.0.0.1", "port": 7777}

    def test_main_dashboard_cli_parses_host_and_port(self, monkeypatch):
        """CLI에서 받은 dashboard host/port 옵션이 실행 args에 보존된다."""
        import main as main_mod

        captured = {}

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "--mode",
                "dashboard",
                "--dashboard-host",
                "0.0.0.0",
                "--dashboard-port",
                "9090",
            ],
        )
        monkeypatch.setattr(main_mod, "setup_logger", lambda: None)
        monkeypatch.setattr(main_mod, "init_database", lambda: None)
        monkeypatch.setattr(
            main_mod,
            "run_dashboard",
            lambda args: captured.update(
                {"host": args.dashboard_host, "port": args.dashboard_port}
            ),
        )

        main_mod.main()

        assert captured == {"host": "0.0.0.0", "port": 9090}

    def test_main_cli_unexpected_exception_exits_nonzero(self, monkeypatch):
        """CLI 실행 중 예상치 못한 예외는 배치가 실패로 인식하도록 non-zero 종료한다."""
        import main as main_mod

        monkeypatch.setattr(sys, "argv", ["main.py", "--mode", "dashboard"])
        monkeypatch.setattr(main_mod, "setup_logger", lambda: None)
        monkeypatch.setattr(main_mod, "init_database", lambda: None)
        monkeypatch.setattr(
            main_mod,
            "run_dashboard",
            lambda _args: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        with pytest.raises(SystemExit) as exc:
            main_mod.main()

        assert exc.value.code == 1

    def test_main_cli_keyboard_interrupt_exits_130(self, monkeypatch):
        """사용자 중단은 일반 성공 종료와 구분되는 130 코드로 반환한다."""
        import main as main_mod

        monkeypatch.setattr(sys, "argv", ["main.py", "--mode", "dashboard"])
        monkeypatch.setattr(main_mod, "setup_logger", lambda: None)
        monkeypatch.setattr(main_mod, "init_database", lambda: None)
        monkeypatch.setattr(
            main_mod,
            "run_dashboard",
            lambda _args: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        with pytest.raises(SystemExit) as exc:
            main_mod.main()

        assert exc.value.code == 130

    def test_dashboard_docs_and_config_default_to_loopback(self):
        """실제/예시 설정과 운영 문서의 대시보드 기본 host는 외부 공개가 아니다."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent

        settings_path = root / "config" / "settings.yaml"
        example = (root / "config" / "settings.yaml.example").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        project_guide = (root / "docs" / "PROJECT_GUIDE.md").read_text(encoding="utf-8")

        if settings_path.exists():
            settings = settings_path.read_text(encoding="utf-8")
            assert 'host: "127.0.0.1"' in settings
        assert 'host: "127.0.0.1"' in example
        assert "기본 바인드는 http://127.0.0.1:8080" in readme
        assert "| **dashboard** | host(127.0.0.1), port(8080) |" in project_guide
        assert "| **dashboard** | host(0.0.0.0), port(8080) |" not in project_guide

    def test_http_liquidate_token_validation_rejects_short_token(self, monkeypatch):
        """긴급 청산 HTTP 토큰은 기본 최소 길이를 만족해야 한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.delenv("LIQUIDATE_TRIGGER_MIN_TOKEN_LENGTH", raising=False)

        ok, error = trigger._validate_trigger_token("short")

        assert ok is False
        assert "at least 16 characters" in error

    def test_http_liquidate_token_validation_rejects_placeholder(self, monkeypatch):
        """흔한 placeholder 토큰은 길이와 무관하게 거부한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.setenv("LIQUIDATE_TRIGGER_MIN_TOKEN_LENGTH", "16")

        ok, error = trigger._validate_trigger_token("your_secret")

        assert ok is False
        assert "placeholder" in error

    def test_http_liquidate_token_validation_accepts_strong_token(self, monkeypatch):
        """충분히 긴 임의 토큰은 허용한다."""
        import monitoring.liquidate_trigger as trigger

        monkeypatch.delenv("LIQUIDATE_TRIGGER_MIN_TOKEN_LENGTH", raising=False)

        ok, error = trigger._validate_trigger_token("strong-liquidate-token-2026")

        assert ok is True
        assert error == ""

    def test_http_liquidate_rejects_weak_configured_token_before_auth(self, monkeypatch):
        """서버 요청 처리도 약한 설정 토큰을 503으로 차단한다."""
        import monitoring.liquidate_trigger as trigger

        captured = {}
        handler = object.__new__(trigger.LiquidateHandler)
        handler.path = "/liquidate"
        handler.headers = {"X-Token": "short"}
        handler._send = lambda code, body, headers=None: captured.update(
            {"code": code, "body": body, "headers": headers or {}}
        )
        monkeypatch.setenv("LIQUIDATE_TRIGGER_TOKEN", "short")

        handler._handle_liquidate()

        assert captured["code"] == 503
        assert "at least 16 characters" in captured["body"]["error"]


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


# ── 5. TradeHistory execution link migration ──

class TestTradeHistoryExecutionLinkMigration:
    def test_execution_link_migration_function_exists(self):
        """실행 세션/주문 연결 마이그레이션 함수가 존재해야 한다."""
        from database.models import _migrate_trade_history_execution_link_columns
        assert callable(_migrate_trade_history_execution_link_columns)

    def test_execution_link_migration_on_existing_db_without_columns(self):
        """기존 DB에서 execution_session_id/order_id 컬럼을 추가해야 한다."""
        import tempfile, os
        from sqlalchemy import create_engine, text, Column, Integer, String, Float
        from sqlalchemy.orm import DeclarativeBase
        from database.models import _migrate_trade_history_execution_link_columns

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

            _migrate_trade_history_execution_link_columns(engine)

            with engine.connect() as conn:
                r = conn.execute(text("PRAGMA table_info(trade_history)"))
                cols_after = [row[1] for row in r.fetchall()]
            engine.dispose()
            assert "execution_session_id" in cols_after
            assert "order_id" in cols_after
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


# ── 6. Walk-forward non-zero windows ──

def _offline_walk_forward_ohlcv():
    import numpy as np
    import pandas as pd

    dates = pd.bdate_range("2021-01-01", "2025-12-31")
    steps = np.arange(len(dates), dtype=float)
    close = 50_000 + steps * 8 + np.sin(steps / 17.0) * 350
    open_ = close * (1 + np.sin(steps / 11.0) * 0.002)
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume = 1_000_000 + (steps % 20) * 10_000
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=dates,
    )


def _build_offline_walk_forward_validator(monkeypatch, tmp_path):
    import pandas as pd
    import backtest.strategy_validator as validator_mod
    from config.config_loader import Config
    from core.data_collector import DataCollector

    class NoopNotifier:
        def __init__(self, config):
            self.config = config

        def send(self, message):
            return None

    data = _offline_walk_forward_ohlcv()
    fetch_calls = []
    run_calls = []

    def fake_fetch(self, symbol, start_date=None, end_date=None):
        fetch_calls.append((symbol, start_date, end_date))
        start = pd.Timestamp(start_date or data.index.min()).normalize()
        end = pd.Timestamp(end_date or data.index.max()).normalize()
        frame = data.loc[start:end].copy()
        if symbol == "KS11":
            frame["close"] = frame["close"] * 0.95
        return frame

    def fake_run(df, *, strategy_name, strict_lookahead=True):
        run_calls.append(
            {
                "strategy_name": strategy_name,
                "strict_lookahead": strict_lookahead,
                "rows": len(df),
            }
        )
        return {
            "initial_capital": 10_000_000,
            "metrics": {
                "sharpe_ratio": 1.35,
                "max_drawdown": -12.5,
                "profit_factor": 1.8,
                "total_trades": 14,
            },
        }

    monkeypatch.setattr(DataCollector, "fetch_korean_stock", fake_fetch)
    monkeypatch.setattr(validator_mod, "Notifier", NoopNotifier)
    validator = validator_mod.StrategyValidator(Config.get(), output_dir=tmp_path)
    monkeypatch.setattr(validator.backtester, "run", fake_run)
    return validator, fetch_calls, run_calls


class TestWalkForwardWindows:
    def test_wf_produces_nonzero_windows(self, monkeypatch, tmp_path):
        """5년 데이터로 walk-forward 시 최소 1개 이상의 window가 생성되어야 한다."""
        v, fetch_calls, run_calls = _build_offline_walk_forward_validator(
            monkeypatch,
            tmp_path,
        )
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
        assert fetch_calls == [
            ("005930", "2021-01-01", "2025-12-31"),
            ("KS11", "2021-01-01", "2025-12-31"),
        ]
        assert len(run_calls) == n_total
        assert all(call["strict_lookahead"] is True for call in run_calls)
        assert all(call["rows"] == 252 for call in run_calls)
        assert str(result["report_path"]).startswith(str(tmp_path))

    def test_wf_result_has_flat_keys(self, monkeypatch, tmp_path):
        """WF 결과가 flat 구조(summary 서브키 없음)로 반환되어야 한다."""
        v, _, run_calls = _build_offline_walk_forward_validator(monkeypatch, tmp_path)
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
        assert "summary" not in result
        assert result["n_total"] == len(run_calls)


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
