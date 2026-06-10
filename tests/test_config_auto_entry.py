"""
config_loader auto_entry 환경변수 오버라이드 + 듀얼 해시 테스트.
"""

import os
import pytest


def _reset_config_singleton():
    """Config 싱글톤을 초기화하여 테스트 간 격리."""
    from config.config_loader import Config
    Config._instance = None
    Config._settings = None
    Config._strategies = None
    Config._risk_params = None


@pytest.fixture(autouse=True)
def _clean_singleton():
    """각 테스트 전후로 싱글톤 초기화."""
    _reset_config_singleton()
    yield
    _reset_config_singleton()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """QUANT_AUTO_ENTRY를 테스트 전에 제거."""
    monkeypatch.delenv("QUANT_AUTO_ENTRY", raising=False)


# ──────────────────────────────────────────────────────
# _resolve_auto_entry 단위 테스트
# ──────────────────────────────────────────────────────

class TestResolveAutoEntry:
    """_resolve_auto_entry 함수 직접 테스트."""

    def test_env_absent_uses_yaml_false(self):
        """env 없음 → YAML auto_entry=false 사용."""
        from config.config_loader import _resolve_auto_entry
        settings = {"trading": {"auto_entry": False, "mode": "paper"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is False
        assert result["trading"]["_auto_entry_source"] == "YAML"

    def test_env_absent_uses_yaml_true(self):
        """env 없음 → YAML auto_entry=true 사용."""
        from config.config_loader import _resolve_auto_entry
        settings = {"trading": {"auto_entry": True, "mode": "paper"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is True
        assert result["trading"]["_auto_entry_source"] == "YAML"

    def test_env_true_overrides_yaml_false(self, monkeypatch):
        """env=true → YAML false를 덮어씀."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        settings = {"trading": {"auto_entry": False, "mode": "paper"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is True
        assert result["trading"]["_auto_entry_source"] == "ENV"

    def test_env_false_overrides_yaml_true(self, monkeypatch):
        """env=false → YAML true를 덮어씀."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "false")
        settings = {"trading": {"auto_entry": True, "mode": "schedule"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is False
        assert result["trading"]["_auto_entry_source"] == "ENV"

    @pytest.mark.parametrize("env_val,expected", [
        ("true", True), ("True", True), ("TRUE", True),
        ("1", True), ("on", True), ("ON", True), ("yes", True),
        ("false", False), ("False", False), ("FALSE", False),
        ("0", False), ("off", False), ("OFF", False), ("no", False),
    ])
    def test_all_valid_boolean_values(self, monkeypatch, env_val, expected):
        """허용되는 모든 boolean 값 테스트."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", env_val)
        settings = {"trading": {"auto_entry": not expected, "mode": "paper"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is expected

    @pytest.mark.parametrize("bad_val", ["maybe", "2", "tru", "fals", ""])
    def test_invalid_env_raises_error(self, monkeypatch, bad_val):
        """유효하지 않은 env 값 → ValueError."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", bad_val)
        settings = {"trading": {"auto_entry": False, "mode": "paper"}}
        with pytest.raises(ValueError, match="QUANT_AUTO_ENTRY"):
            _resolve_auto_entry(settings)

    def test_live_mode_ignores_env_true(self, monkeypatch):
        """live 모드에서 env=true → auto_entry 활성화되지 않음."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        settings = {"trading": {"auto_entry": False, "mode": "live"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is False
        assert "live override" in result["trading"]["_auto_entry_source"]

    def test_live_mode_env_true_yaml_true_stays_true(self, monkeypatch):
        """live 모드에서 env=true이지만 YAML도 true → YAML 값(true) 유지."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        settings = {"trading": {"auto_entry": True, "mode": "live"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is True
        assert "live override" in result["trading"]["_auto_entry_source"]

    def test_whitespace_trimmed(self, monkeypatch):
        """env 값 앞뒤 공백은 무시."""
        from config.config_loader import _resolve_auto_entry
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "  true  ")
        settings = {"trading": {"auto_entry": False, "mode": "paper"}}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is True

    def test_missing_trading_section(self):
        """trading 섹션 없는 settings → 기본값 생성."""
        from config.config_loader import _resolve_auto_entry
        settings = {}
        result = _resolve_auto_entry(settings)
        assert result["trading"]["auto_entry"] is False
        assert result["trading"]["_auto_entry_source"] == "YAML"


class TestResolveDataSource:
    """데이터 소스 기본값과 boolean 해석 테스트."""

    def test_missing_data_source_defaults_to_kis_fallback_disabled(self):
        from config.config_loader import _resolve_data_source_defaults

        settings = _resolve_data_source_defaults({})

        assert settings["data_source"]["preferred"] == "auto"
        assert settings["data_source"]["allow_kis_fallback"] is False
        assert settings["data_source"]["warn_on_source_mismatch"] is True

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            ("0", False),
            ("no", False),
        ],
    )
    def test_string_boolean_values_are_explicitly_parsed(self, raw, expected):
        from config.config_loader import _resolve_data_source_defaults

        settings = _resolve_data_source_defaults(
            {"data_source": {"allow_kis_fallback": raw, "warn_on_source_mismatch": raw}}
        )

        assert settings["data_source"]["allow_kis_fallback"] is expected
        assert settings["data_source"]["warn_on_source_mismatch"] is expected

    def test_invalid_boolean_value_raises(self):
        from config.config_loader import _resolve_data_source_defaults

        with pytest.raises(ValueError, match="data_source.allow_kis_fallback"):
            _resolve_data_source_defaults({"data_source": {"allow_kis_fallback": "maybe"}})


class TestEnvFileFallback:
    """python-dotenv 미설치 환경의 .env fallback parser 테스트."""

    def test_fallback_loads_simple_env_file_without_overriding_existing(self, tmp_path, monkeypatch):
        from config.config_loader import _load_env_file_fallback

        env_path = tmp_path / ".env"
        env_path.write_text(
            "\n".join([
                "# comment",
                "DISCORD_WEBHOOK_URL=https://discord.test/webhook",
                "export KIS_ACCOUNT_NO='12345678'",
                'QUOTED_VALUE="hello world"',
                "EXISTING_VALUE=from-file",
            ]),
            encoding="utf-8",
        )
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)
        monkeypatch.delenv("QUOTED_VALUE", raising=False)
        monkeypatch.setenv("EXISTING_VALUE", "from-env")

        _load_env_file_fallback(env_path)

        assert os.environ["DISCORD_WEBHOOK_URL"] == "https://discord.test/webhook"
        assert os.environ["KIS_ACCOUNT_NO"] == "12345678"
        assert os.environ["QUOTED_VALUE"] == "hello world"
        assert os.environ["EXISTING_VALUE"] == "from-env"


# ──────────────────────────────────────────────────────
# Config 싱글톤 통합 테스트
# ──────────────────────────────────────────────────────

class TestConfigAutoEntry:
    """Config.get()을 통한 auto_entry 통합 테스트."""

    def test_config_auto_entry_default(self):
        """Config.get().auto_entry 기본값 = False."""
        from config.config_loader import Config
        config = Config.get()
        assert config.auto_entry is False
        assert config.auto_entry_source == "YAML"

    def test_config_auto_entry_env_override(self, monkeypatch):
        """Config.get().auto_entry가 ENV를 반영."""
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        from config.config_loader import Config
        config = Config.get()
        assert config.auto_entry is True
        assert config.auto_entry_source == "ENV"


# ──────────────────────────────────────────────────────
# 듀얼 해시 테스트
# ──────────────────────────────────────────────────────

class TestDualHash:
    """yaml_hash vs resolved_hash 분리 테스트."""

    def test_hashes_exist_and_differ_when_env_set(self, monkeypatch):
        """ENV 오버라이드 시 yaml_hash는 동일, resolved_hash는 변경."""
        from config.config_loader import Config

        # env 없이 로드
        config1 = Config.get()
        yaml_hash_1 = config1.yaml_hash
        resolved_hash_1 = config1.resolved_hash

        # 리셋 후 env 설정하고 다시 로드
        _reset_config_singleton()
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        config2 = Config.get()
        yaml_hash_2 = config2.yaml_hash
        resolved_hash_2 = config2.resolved_hash

        # yaml hash는 파일이 안 바뀌었으므로 동일
        assert yaml_hash_1 == yaml_hash_2
        # resolved hash는 auto_entry가 바뀌었으므로 다름
        assert resolved_hash_1 != resolved_hash_2

    def test_resolved_hash_changes_when_data_source_policy_changes(self):
        """데이터 소스 정책도 실행 설정 해시에 포함된다."""
        from config.config_loader import compute_resolved_hash

        base_settings = {
            "trading": {"mode": "paper", "auto_entry": False},
            "watchlist": {"mode": "manual", "symbols": ["005930"]},
            "data_source": {
                "preferred": "fdr",
                "allow_kis_fallback": False,
                "warn_on_source_mismatch": True,
            },
        }
        strategies = {"active_strategy": "scoring"}
        risk_params = {"position_sizing": {"initial_capital": 1000000}}

        h1 = compute_resolved_hash(base_settings, strategies, risk_params)
        changed = {
            **base_settings,
            "data_source": {
                **base_settings["data_source"],
                "allow_kis_fallback": True,
            },
        }
        h2 = compute_resolved_hash(changed, strategies, risk_params)

        assert h1 != h2

    def test_hashes_are_hex_strings(self):
        """해시가 유효한 hex 문자열."""
        from config.config_loader import Config
        config = Config.get()
        assert len(config.yaml_hash) == 64
        assert len(config.resolved_hash) == 64
        int(config.yaml_hash, 16)  # hex 파싱 가능
        int(config.resolved_hash, 16)

    def test_yaml_hash_stable_across_reloads(self):
        """파일 미변경 시 yaml_hash 일관성."""
        from config.config_loader import Config
        h1 = Config.get().yaml_hash
        _reset_config_singleton()
        h2 = Config.get().yaml_hash
        assert h1 == h2


# ──────────────────────────────────────────────────────
# Scheduler 통합 (config에서 읽는지 확인)
# ──────────────────────────────────────────────────────

class TestSchedulerUsesConfig:
    """Scheduler가 config.auto_entry를 사용하는지 확인."""

    def test_scheduler_reads_config_auto_entry_true(self, monkeypatch):
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "true")
        from core.scheduler import Scheduler
        scheduler = Scheduler(strategy_name="scoring")
        assert scheduler.auto_entry is True

    def test_scheduler_reads_config_auto_entry_false(self, monkeypatch):
        monkeypatch.setenv("QUANT_AUTO_ENTRY", "false")
        from core.scheduler import Scheduler
        scheduler = Scheduler(strategy_name="scoring")
        assert scheduler.auto_entry is False

    def test_scheduler_default_no_env(self):
        from core.scheduler import Scheduler
        os.environ.pop("QUANT_AUTO_ENTRY", None)
        scheduler = Scheduler(strategy_name="scoring")
        assert scheduler.auto_entry is False


class TestAccountRoutingVisibility:
    """다중 계좌 라우팅의 침묵 결함 가시화 — env 키 정규화·미선언 경고·live 폴백 경고."""

    def test_basket_key_env_override_with_colon_normalized(self, monkeypatch):
        """'basket_rebalance:<name>' 키도 콜론을 '_'로 정규화한 env 이름으로 덮어쓸 수 있다
        (콜론은 Windows env 이름에 불가 — 기존 파생식으로는 영구 설정 불가였다)."""
        from config.config_loader import _override_with_env

        monkeypatch.setenv("KIS_ACCOUNT_NO_BASKET_REBALANCE_KR_DIVERSIFIED_HOLD", "9999-01")
        s = _override_with_env({
            "kis_api": {"accounts": {"basket_rebalance:kr_diversified_hold": "1111-01"}},
        })
        assert s["kis_api"]["accounts"]["basket_rebalance:kr_diversified_hold"] == "9999-01"

    def test_undeclared_account_env_warns(self, monkeypatch, caplog):
        """YAML 미선언 KIS_ACCOUNT_NO_* env는 무시되되 명시 경고를 남긴다(침묵 라우팅 방지)."""
        import logging
        from config.config_loader import _override_with_env

        monkeypatch.setenv("KIS_ACCOUNT_NO_GHOST_STRATEGY", "7777-01")
        with caplog.at_level(logging.WARNING, logger="config_loader"):
            _override_with_env({"kis_api": {"accounts": {}}})
        assert any("KIS_ACCOUNT_NO_GHOST_STRATEGY" in r.message for r in caplog.records)

    def test_live_default_fallback_warns_once(self, caplog):
        """live에서 미선언 전략이 기본 계좌로 폴백하면 1회 경고(공유 가시화)."""
        import logging
        from config.config_loader import Config

        cfg = Config.__new__(Config)
        cfg._settings = {
            "trading": {"mode": "live"},
            "kis_api": {"account_no": "1111-01", "accounts": {}},
        }
        Config._default_account_warned = set()
        with caplog.at_level(logging.WARNING, logger="config_loader"):
            assert cfg.get_account_no("scoring") == "1111-01"
            assert cfg.get_account_no("scoring") == "1111-01"  # 2회째는 경고 없음
        warns = [r for r in caplog.records if "기본 계좌로 폴백" in r.message]
        assert len(warns) == 1

    def test_paper_default_fallback_silent(self, caplog):
        """paper에서는 기본 계좌 폴백이 정상 동작 — 경고 없음."""
        import logging
        from config.config_loader import Config

        cfg = Config.__new__(Config)
        cfg._settings = {
            "trading": {"mode": "paper"},
            "kis_api": {"account_no": "1111-01", "accounts": {}},
        }
        Config._default_account_warned = set()
        with caplog.at_level(logging.WARNING, logger="config_loader"):
            assert cfg.get_account_no("scoring") == "1111-01"
        assert not [r for r in caplog.records if "기본 계좌" in r.message]
