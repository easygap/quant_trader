from types import SimpleNamespace

import pytest


class DummyConfig:
    def __init__(self, *, mode="paper", active_strategy="scoring"):
        self._settings = {"trading": {"mode": mode}}
        self.trading = self._settings["trading"]
        self.active_strategy = active_strategy
        self.auto_entry = False
        self.auto_entry_source = "test"

    def enforce_live_auto_entry_policy(self):
        # 실제 Config 인터페이스 반영 — live 진입 시 ENV auto_entry 켜기 무시 정책(no-op 더블)
        pass

    def get_account_no(self, strategy=""):
        assert strategy == "scoring"
        return "12345678-01"


def test_run_live_trading_blocks_when_registry_disallows_live(monkeypatch):
    import main as main_mod

    config = DummyConfig()
    gate_called = False

    def fake_gate(cfg, strategy):
        nonlocal gate_called
        gate_called = True
        return []

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setattr(main_mod, "_check_live_readiness_gate", fake_gate)

    class DummyKIS:
        def __init__(self, *args, **kwargs):
            pytest.fail("registry live 불허 시 KIS 연결을 시도하면 안 됨")

    monkeypatch.setattr("api.kis_api.KISApi", DummyKIS)

    with pytest.raises(SystemExit) as exc:
        main_mod.run_live_trading(SimpleNamespace(strategy="scoring", confirm_live=True))

    assert exc.value.code == 1
    assert gate_called is False


def test_run_live_trading_starts_after_registry_and_canonical_gate_pass(monkeypatch):
    import main as main_mod

    config = DummyConfig()
    calls = {}

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setattr(main_mod, "_check_live_readiness_gate", lambda cfg, strategy: [])
    monkeypatch.setattr(
        "strategies.is_strategy_allowed",
        lambda strategy, mode: (True, "status=live_candidate, live 허용")
        if mode == "live"
        else (False, "unexpected mode"),
    )

    class DummyKIS:
        def __init__(self, account_no=None):
            assert account_no == "12345678-01"

        def authenticate(self):
            return True

        def verify_connection(self):
            return True

    class DummyPortfolio:
        def __init__(self, cfg, account_key):
            calls["portfolio_account_key"] = account_key

        def sync_with_broker(self):
            return {"ok": True, "message": "ok"}

    class DummyScheduler:
        def __init__(self, *, strategy_name, config, live_gate_validated=False):
            calls["scheduler_strategy"] = strategy_name
            calls["live_gate_validated"] = live_gate_validated
            calls["mode_at_scheduler_start"] = config.trading["mode"]

        def run(self):
            calls["scheduler_run"] = True

    monkeypatch.setattr("api.kis_api.KISApi", DummyKIS)
    monkeypatch.setattr(
        "core.blackswan_detector.BlackSwanDetector",
        lambda cfg: SimpleNamespace(is_on_cooldown=lambda: False),
    )
    monkeypatch.setattr("core.portfolio_manager.PortfolioManager", DummyPortfolio)
    monkeypatch.setattr("core.scheduler.Scheduler", DummyScheduler)

    main_mod.run_live_trading(SimpleNamespace(strategy="scoring", confirm_live=True))

    assert calls == {
        "portfolio_account_key": "scoring",
        "scheduler_strategy": "scoring",
        "live_gate_validated": True,
        "mode_at_scheduler_start": "live",
        "scheduler_run": True,
    }
    assert config.trading["mode"] == "paper"


def test_run_live_trading_keeps_unregistered_strategy_blocked(monkeypatch):
    import main as main_mod

    config = DummyConfig()
    gate_called = False

    def fake_gate(cfg, strategy):
        nonlocal gate_called
        gate_called = True
        return []

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setattr(main_mod, "_check_live_readiness_gate", fake_gate)

    with pytest.raises(SystemExit):
        main_mod.run_live_trading(SimpleNamespace(strategy="unknown_strategy", confirm_live=True))

    assert gate_called is False


def test_run_live_trading_blocks_when_kis_connection_check_fails(monkeypatch):
    import main as main_mod

    config = DummyConfig()
    calls = {}

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setattr(main_mod, "_check_live_readiness_gate", lambda cfg, strategy: [])
    monkeypatch.setattr(
        "strategies.is_strategy_allowed",
        lambda strategy, mode: (True, "status=live_candidate, live 허용")
        if mode == "live"
        else (False, "unexpected mode"),
    )

    class DummyKIS:
        def __init__(self, account_no=None):
            assert account_no == "12345678-01"

        def authenticate(self):
            return True

        def verify_connection(self):
            calls["verify_connection"] = True
            return False

    class DummyPortfolio:
        def __init__(self, *args, **kwargs):
            pytest.fail("KIS 연결 검증 실패 시 포트폴리오 동기화를 시도하면 안 됨")

    class DummyScheduler:
        def __init__(self, *args, **kwargs):
            pytest.fail("KIS 연결 검증 실패 시 실전 스케줄러를 시작하면 안 됨")

    monkeypatch.setattr("api.kis_api.KISApi", DummyKIS)
    monkeypatch.setattr("core.portfolio_manager.PortfolioManager", DummyPortfolio)
    monkeypatch.setattr("core.scheduler.Scheduler", DummyScheduler)

    with pytest.raises(SystemExit) as exc:
        main_mod.run_live_trading(SimpleNamespace(strategy="scoring", confirm_live=True))

    assert exc.value.code == 1
    assert calls == {"verify_connection": True}
    assert config.trading["mode"] == "paper"


def test_run_live_trading_blocks_when_initial_broker_sync_fails(monkeypatch):
    import main as main_mod

    config = DummyConfig()
    calls = {}

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setattr(main_mod, "_check_live_readiness_gate", lambda cfg, strategy: [])
    monkeypatch.setattr(
        "strategies.is_strategy_allowed",
        lambda strategy, mode: (True, "status=live_candidate, live 허용")
        if mode == "live"
        else (False, "unexpected mode"),
    )

    class DummyKIS:
        def __init__(self, account_no=None):
            assert account_no == "12345678-01"

        def authenticate(self):
            return True

        def verify_connection(self):
            return True

    class DummyPortfolio:
        def __init__(self, cfg, account_key):
            calls["portfolio_account_key"] = account_key

        def sync_with_broker(self):
            calls["sync_with_broker"] = True
            return {"ok": False, "message": "broker/db mismatch"}

    class DummyScheduler:
        def __init__(self, *args, **kwargs):
            pytest.fail("초기 잔고 동기화 실패 시 실전 스케줄러를 시작하면 안 됨")

    monkeypatch.setattr("api.kis_api.KISApi", DummyKIS)
    monkeypatch.setattr(
        "core.blackswan_detector.BlackSwanDetector",
        lambda cfg: SimpleNamespace(is_on_cooldown=lambda: False),
    )
    monkeypatch.setattr("core.portfolio_manager.PortfolioManager", DummyPortfolio)
    monkeypatch.setattr("core.scheduler.Scheduler", DummyScheduler)

    with pytest.raises(SystemExit) as exc:
        main_mod.run_live_trading(SimpleNamespace(strategy="scoring", confirm_live=True))

    assert exc.value.code == 1
    assert calls == {
        "portfolio_account_key": "scoring",
        "sync_with_broker": True,
    }
    assert config.trading["mode"] == "paper"


def test_scheduler_live_requires_explicit_live_gate_validation(monkeypatch):
    import core.scheduler as scheduler_mod

    config = DummyConfig(mode="live")

    monkeypatch.setattr(
        "strategies.is_strategy_allowed",
        lambda strategy, mode: (True, "status=live_candidate, live 허용")
        if mode == "live"
        else (False, "unexpected mode"),
    )
    monkeypatch.setattr(scheduler_mod, "TradingHours", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "BlackSwanDetector", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "PortfolioManager", lambda cfg, account_key: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "Notifier", lambda cfg: SimpleNamespace())

    with pytest.raises(ValueError, match="live readiness gate"):
        scheduler_mod.Scheduler("scoring", config=config)

    scheduler = scheduler_mod.Scheduler(
        "scoring",
        config=config,
        live_gate_validated=True,
    )

    assert scheduler.strategy_name == "scoring"


def test_scheduler_live_gate_validation_does_not_override_registry_block(monkeypatch):
    import core.scheduler as scheduler_mod

    config = DummyConfig(mode="live")

    monkeypatch.setattr(scheduler_mod, "TradingHours", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "BlackSwanDetector", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "PortfolioManager", lambda cfg, account_key: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "Notifier", lambda cfg: SimpleNamespace())

    with pytest.raises(ValueError, match="live 모드 불허"):
        scheduler_mod.Scheduler(
            "scoring",
            config=config,
            live_gate_validated=True,
        )


def test_scheduler_live_gate_validation_does_not_allow_unknown_strategy(monkeypatch):
    import core.scheduler as scheduler_mod

    config = DummyConfig(mode="live")

    monkeypatch.setattr(scheduler_mod, "TradingHours", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "BlackSwanDetector", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "PortfolioManager", lambda cfg, account_key: SimpleNamespace())
    monkeypatch.setattr(scheduler_mod, "Notifier", lambda cfg: SimpleNamespace())

    with pytest.raises(ValueError, match="live 모드 불허"):
        scheduler_mod.Scheduler(
            "unknown_strategy",
            config=config,
            live_gate_validated=True,
        )
