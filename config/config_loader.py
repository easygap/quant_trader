"""
설정 로더 모듈
- YAML 설정 파일을 로드하여 딕셔너리로 반환
- settings.yaml, strategies.yaml, risk_params.yaml 통합 관리
- 환경변수 오버라이드: YAML 기본값 위에 환경변수가 우선
"""

import hashlib
import logging
import os
import yaml
from pathlib import Path

# 프로젝트 루트 디렉토리 (config/ 의 상위)
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_file_fallback(env_path: Path = ENV_PATH) -> None:
    """python-dotenv가 없어도 프로젝트 .env의 단순 KEY=value 항목을 로드한다."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


try:
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
except ImportError:
    _load_env_file_fallback(ENV_PATH)


def load_yaml(file_path: str) -> dict:
    """YAML 파일을 읽어 딕셔너리로 반환"""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _override_with_env(settings: dict) -> dict:
    """환경변수(.env) 값으로 yaml 설정을 덮어씁니다. API 키/시크릿은 환경변수 전용(보안)."""
    if "kis_api" not in settings:
        settings["kis_api"] = {}
    # 민감 정보: YAML 값 무시, 환경변수 전용 (settings.yaml 커밋 시에도 노출 방지)
    settings["kis_api"]["app_key"] = os.environ.get("KIS_APP_KEY", "")
    settings["kis_api"]["app_secret"] = os.environ.get("KIS_APP_SECRET", "")
    settings["kis_api"]["account_no"] = os.environ.get("KIS_ACCOUNT_NO", settings["kis_api"].get("account_no", ""))
    # 전략별 계좌 (다중 계좌): KIS_ACCOUNT_NO_SCORING, KIS_ACCOUNT_NO_MEAN_REVERSION 등으로 덮어씀.
    # 키 파생 시 영숫자 외 문자(':', '-')는 '_'로 정규화한다 — 바스켓 승인 단위
    # ('basket_rebalance:<name>')처럼 콜론이 든 키도 env로 설정 가능해야 한다
    # (콜론은 Windows env 이름에 쓸 수 없어 기존 파생식으로는 영구 설정 불가였다).
    import re as _re
    accounts = settings["kis_api"].get("accounts", {}) or {}
    consumed_env_keys = set()
    for key in list(accounts.keys()):
        env_key = "KIS_ACCOUNT_NO_" + _re.sub(r"[^A-Z0-9]", "_", key.upper())
        consumed_env_keys.add(env_key)
        accounts[key] = os.environ.get(env_key, accounts[key])
    settings["kis_api"]["accounts"] = accounts
    # YAML에 선언되지 않은 KIS_ACCOUNT_NO_* env는 조용히 무시되면 운영자가
    # "덮어썼다"고 믿은 채 기본 계좌로 라우팅된다(침묵 공유) — 명시 경고로 드러낸다.
    for env_name in os.environ:
        if (
            env_name.startswith("KIS_ACCOUNT_NO_")
            and env_name not in consumed_env_keys
        ):
            logging.getLogger("config_loader").warning(
                "%s 환경변수가 설정돼 있지만 kis_api.accounts에 대응하는 키가 없어 "
                "무시됩니다 — settings.yaml의 accounts에 해당 전략 키를 선언하세요.",
                env_name,
            )
    if "MAX_CALLS_PER_SEC" in os.environ:
        settings["kis_api"]["max_calls_per_sec"] = float(os.environ["MAX_CALLS_PER_SEC"])
    if "MAX_CALLS_PER_MIN" in os.environ:
        settings["kis_api"]["max_calls_per_min"] = int(os.environ["MAX_CALLS_PER_MIN"])
    if "MAX_RETRY" in os.environ:
        settings["kis_api"]["max_retry"] = int(os.environ["MAX_RETRY"])

    if "discord" in settings:
        settings["discord"]["webhook_url"] = os.environ.get(
            "DISCORD_WEBHOOK_URL", settings["discord"].get("webhook_url", "")
        )

    em = settings.setdefault("email", {})
    if "SMTP_SERVER" in os.environ:
        em["smtp_server"] = os.environ["SMTP_SERVER"]
    if "SMTP_PORT" in os.environ:
        em["smtp_port"] = int(os.environ["SMTP_PORT"])
    if "SMTP_USER" in os.environ:
        em["smtp_user"] = os.environ["SMTP_USER"]
    if "ALERT_EMAIL_TO" in os.environ:
        em["alert_to"] = os.environ["ALERT_EMAIL_TO"]

    dart = settings.setdefault("dart", {})
    if "DART_API_KEY" in os.environ:
        dart["api_key"] = os.environ["DART_API_KEY"].strip()

    # 데이터베이스 경로 오버라이드: 테스트 격리(운영 DB 보호) 및 배포 환경별 DB 분리에 사용.
    # QUANT_DB_PATH가 설정되면 SQLite 경로를 강제로 덮어쓴다.
    db = settings.setdefault("database", {})
    db_path_override = os.environ.get("QUANT_DB_PATH")
    if db_path_override:
        db["sqlite_path"] = db_path_override

    return settings


_BOOL_TRUE = frozenset({"true", "1", "on", "yes"})
_BOOL_FALSE = frozenset({"false", "0", "off", "no"})


def _coerce_bool_setting(value, *, default: bool, key: str) -> bool:
    """YAML/ENV에서 온 boolean 설정을 명시적으로 해석한다."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
    raise ValueError(
        f"{key} 값이 유효하지 않습니다: {value!r}. "
        "허용값: true/false/1/0/on/off/yes/no"
    )


def _resolve_data_source_defaults(settings: dict) -> dict:
    """
    데이터 소스 설정 기본값을 안전하게 채운다.

    KIS 일봉은 비수정주가를 반환할 수 있어, 설정 누락 시 fallback을 허용하지 않는다.
    """
    ds = settings.setdefault("data_source", {})
    ds["preferred"] = str(ds.get("preferred") or "auto").strip().lower()
    ds["allow_kis_fallback"] = _coerce_bool_setting(
        ds.get("allow_kis_fallback"),
        default=False,
        key="data_source.allow_kis_fallback",
    )
    ds["warn_on_source_mismatch"] = _coerce_bool_setting(
        ds.get("warn_on_source_mismatch"),
        default=True,
        key="data_source.warn_on_source_mismatch",
    )
    return settings


def _resolve_auto_entry(settings: dict) -> dict:
    """
    QUANT_AUTO_ENTRY 환경변수로 trading.auto_entry를 오버라이드.

    Precedence: ENV > YAML > default(false)
    허용값: true/false/1/0/on/off/yes/no (대소문자 무시)
    live 모드에서는 무시 + 경고 로그.
    """
    _log = logging.getLogger("config_loader")
    trading = settings.setdefault("trading", {})
    yaml_value = trading.get("auto_entry", False)
    mode = trading.get("mode", "paper")

    env_raw = os.environ.get("QUANT_AUTO_ENTRY")

    # YAML 값은 엄격 파서로 해석 — bool("false")==True 같은 문자열 함정 차단.
    # (따옴표 하나로 auto_entry 마스터 스위치가 뒤집히면 live에서 실돈 자동매수다)
    yaml_resolved = _coerce_bool_setting(
        yaml_value, default=False, key="trading.auto_entry",
    )

    if env_raw is not None:
        normalized = env_raw.strip().lower()
        if normalized in _BOOL_TRUE:
            resolved = True
        elif normalized in _BOOL_FALSE:
            resolved = False
        else:
            raise ValueError(
                f"QUANT_AUTO_ENTRY 환경변수 값이 유효하지 않습니다: {env_raw!r}. "
                f"허용값: true/false/1/0/on/off/yes/no"
            )
        source = "ENV"
    else:
        resolved = yaml_resolved
        source = "YAML"

    # live 모드에서는 ENV의 '켜는 방향' 오버라이드 무시 (끄는 방향은 fail-safe라 존중).
    # 주의: 정식 live 경로는 YAML mode=paper로 로드 후 mode를 플립하므로 이 분기만으로는
    # 부족하다 — run_live_trading이 플립 직후 enforce_live_auto_entry_policy()를 호출해
    # 같은 정책을 다시 적용한다(아래 보존 값 사용).
    if mode == "live" and resolved and source == "ENV":
        _log.warning(
            "QUANT_AUTO_ENTRY=true 이지만 live 모드에서는 무시됩니다. "
            "live 모드의 auto_entry는 YAML 설정(%s)을 따릅니다.", yaml_value,
        )
        resolved = yaml_resolved
        source = "YAML (live override)"

    trading["auto_entry"] = resolved
    trading["_auto_entry_source"] = source
    # 모드 플립 후 재적용을 위해 YAML 원천 값을 보존(엄격 파싱 결과)
    trading["_auto_entry_yaml"] = yaml_resolved

    _log.info(
        "auto_entry resolved: %s (source=%s, yaml=%s, env=%s, mode=%s)",
        resolved, source, yaml_value, env_raw, mode,
    )

    return settings


def compute_yaml_hash() -> str:
    """YAML 파일 원본의 SHA-256 해시 (파일 동결 확인용)."""
    h = hashlib.sha256()
    for fname in sorted([
        "strategies.yaml", "risk_params.yaml",
        "settings.yaml", "settings.yaml.example", "baskets.yaml",
    ]):
        fpath = CONFIG_DIR / fname
        if fpath.exists():
            h.update(fpath.read_bytes())
    return h.hexdigest()


def compute_resolved_hash(settings: dict, strategies: dict, risk_params: dict) -> str:
    """환경변수 오버라이드 반영 후 실제 실행 설정의 SHA-256 해시."""
    import json
    # 해시에 포함할 키만 선별 (민감 정보 제외, 실행 동작에 영향 주는 설정만)
    trading_keys = {
        k: v for k, v in settings.get("trading", {}).items()
        if not k.startswith("_")  # _auto_entry_source 같은 메타 필드 제외
    }
    watchlist_keys = settings.get("watchlist", {})
    data_source_keys = settings.get("data_source", {})
    payload = json.dumps(
        {
            "trading": trading_keys,
            "watchlist": watchlist_keys,
            "data_source": data_source_keys,
            "strategies": strategies,
            "risk_params": risk_params,
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def load_settings() -> dict:
    """전체 설정 로드. settings.yaml 없으면 기본 dict 사용(키는 환경변수 전용)."""
    try:
        settings = load_yaml(CONFIG_DIR / "settings.yaml")
    except FileNotFoundError:
        settings = {
            "kis_api": {},
            "database": {},
            "trading": {},
            "discord": {},
            "email": {},
            "watchlist": {},
            "dart": {},
        }
    settings = _override_with_env(settings)
    settings = _resolve_data_source_defaults(settings)
    settings = _resolve_auto_entry(settings)
    return settings


def load_strategies() -> dict:
    """전략 파라미터(strategies.yaml) 로드"""
    return load_yaml(CONFIG_DIR / "strategies.yaml")


def load_risk_params() -> dict:
    """리스크 관리 파라미터(risk_params.yaml) 로드"""
    return load_yaml(CONFIG_DIR / "risk_params.yaml")


def load_all_config() -> dict:
    """모든 설정을 통합하여 반환"""
    return {
        "settings": load_settings(),
        "strategies": load_strategies(),
        "risk_params": load_risk_params(),
    }


class Config:
    """
    설정 싱글톤 클래스
    - Config.get() 으로 전체 설정에 접근
    - 최초 1회만 파일을 읽고 이후 캐시된 값 반환
    """
    _instance = None
    _settings = None
    _strategies = None
    _risk_params = None

    @classmethod
    def get(cls) -> "Config":
        """싱글톤 인스턴스 반환"""
        if cls._instance is None:
            cls._instance = Config()
            cls._instance._load()
        return cls._instance

    def _load(self):
        """설정 파일 로드 및 필수 파라미터 검증."""
        self._settings = load_settings()
        self._strategies = load_strategies()
        self._risk_params = load_risk_params()
        self._yaml_hash = compute_yaml_hash()
        self._resolved_hash = compute_resolved_hash(
            self._settings, self._strategies, self._risk_params,
        )
        self._validate_critical_params()

    def _validate_critical_params(self):
        """운영에 치명적인 설정 값을 로드 시점에 검증. 문제 시 즉시 예외."""
        import logging
        _log = logging.getLogger("config_loader")
        errors = []

        # risk_params 필수 키
        ps = self._risk_params.get("position_sizing", {})
        ic = ps.get("initial_capital")
        if ic is None or not isinstance(ic, (int, float)) or ic <= 0:
            errors.append(f"risk_params.position_sizing.initial_capital이 유효하지 않습니다: {ic!r}")

        rr = ps.get("risk_ratio")
        if rr is not None and (not isinstance(rr, (int, float)) or rr <= 0 or rr > 1):
            errors.append(f"risk_params.position_sizing.risk_ratio 범위 오류 (0 < x ≤ 1): {rr!r}")

        # drawdown 한도
        dd = self._risk_params.get("drawdown", {})
        mdd = dd.get("max_portfolio_mdd")
        if mdd is not None and (not isinstance(mdd, (int, float)) or mdd <= 0 or mdd > 1):
            errors.append(f"risk_params.drawdown.max_portfolio_mdd 범위 오류 (0 < x ≤ 1): {mdd!r}")

        # trading 모드
        mode = (self._settings.get("trading") or {}).get("mode", "paper")
        if mode not in ("paper", "live", "backtest", "schedule"):
            errors.append(f"settings.trading.mode가 유효하지 않습니다: {mode!r}")

        if errors:
            msg = "설정 검증 실패:\n  - " + "\n  - ".join(errors)
            _log.error(msg)
            raise ValueError(msg)

    def reload(self):
        """설정 파일 다시 로드 (런타임 변경 반영)"""
        self._load()

    @property
    def settings(self) -> dict:
        return self._settings

    @property
    def strategies(self) -> dict:
        return self._strategies

    @property
    def risk_params(self) -> dict:
        return self._risk_params

    # --- 자주 쓰는 설정 편의 프로퍼티 ---

    @property
    def kis_api(self) -> dict:
        """KIS API 설정"""
        return self._settings.get("kis_api", {})

    @property
    def database(self) -> dict:
        """데이터베이스 설정"""
        return self._settings.get("database", {})

    @property
    def logging_config(self) -> dict:
        """로깅 설정"""
        return self._settings.get("logging", {})

    @property
    def trading(self) -> dict:
        """매매 시간 설정"""
        return self._settings.get("trading", {})

    @property
    def markets(self) -> dict:
        """시장(국가)별 브로커·통화 설정 (korea / us)"""
        return self._settings.get("markets", {})

    @property
    def dart(self) -> dict:
        """DART Open API (전자공시) 설정"""
        return self._settings.get("dart", {})

    @property
    def discord(self) -> dict:
        """디스코드 설정"""
        return self._settings.get("discord", {})

    @property
    def email(self) -> dict:
        """이메일(SMTP) 설정"""
        return self._settings.get("email", {})

    @property
    def watchlist(self) -> list:
        """관심 종목 리스트"""
        wl = self._settings.get("watchlist", {})
        return wl.get("symbols", [])

    @property
    def watchlist_settings(self) -> dict:
        """관심 종목 원본 설정"""
        return self._settings.get("watchlist", {})

    @property
    def indicators(self) -> dict:
        """기술 지표 파라미터"""
        return self._strategies.get("indicators", {})

    @property
    def active_strategy(self) -> str:
        """활성 전략 이름"""
        return self._strategies.get("active_strategy", "scoring")

    @property
    def position_sizing(self) -> dict:
        """포지션 사이징 설정"""
        return self._risk_params.get("position_sizing", {})

    @property
    def stop_loss(self) -> dict:
        """손절매 설정"""
        return self._risk_params.get("stop_loss", {})

    @property
    def take_profit(self) -> dict:
        """익절매 설정"""
        return self._risk_params.get("take_profit", {})

    @property
    def trailing_stop(self) -> dict:
        """트레일링 스탑 설정"""
        return self._risk_params.get("trailing_stop", {})

    @property
    def diversification(self) -> dict:
        """분산 투자 설정"""
        return self._risk_params.get("diversification", {})

    @property
    def drawdown(self) -> dict:
        """MDD 제한 설정"""
        return self._risk_params.get("drawdown", {})

    @property
    def transaction_costs(self) -> dict:
        """거래 비용 설정"""
        return self._risk_params.get("transaction_costs", {})

    @property
    def auto_entry(self) -> bool:
        """resolved auto_entry 값 (ENV > YAML > default)."""
        return self.trading.get("auto_entry", False)

    @property
    def auto_entry_source(self) -> str:
        """auto_entry 값의 출처: 'ENV', 'YAML', 'YAML (live override)'."""
        return self.trading.get("_auto_entry_source", "YAML")

    def enforce_live_auto_entry_policy(self) -> None:
        """live 진입 시 auto_entry의 ENV '켜는 방향' 오버라이드를 YAML 값으로 강제 복귀.

        _resolve_auto_entry의 live-ignore 분기는 '로드 시점 YAML mode'를 보므로,
        정식 live 경로(YAML mode=paper로 로드 → run_live_trading이 mode 플립)에서는
        발동하지 않는다 — paper 실험용 QUANT_AUTO_ENTRY=true가 셸/.env에 남아 있으면
        signal-only 설정의 live가 자동매수하게 되는 구멍. 모드 플립 직후 이 메서드를
        호출해 같은 정책을 재적용한다. 끄는 방향(ENV false)은 fail-safe라 존중.
        """
        trading = self.trading
        if (
            trading.get("_auto_entry_source") == "ENV"
            and trading.get("auto_entry")
        ):
            yaml_resolved = bool(trading.get("_auto_entry_yaml", False))
            logging.getLogger("config_loader").warning(
                "live 진입: QUANT_AUTO_ENTRY=true(ENV)는 무시되고 YAML 값(%s)을 따릅니다.",
                yaml_resolved,
            )
            trading["auto_entry"] = yaml_resolved
            trading["_auto_entry_source"] = "YAML (live override)"

    @property
    def yaml_hash(self) -> str:
        """YAML 파일 원본 해시 (동결 확인용)."""
        return self._yaml_hash

    @property
    def resolved_hash(self) -> str:
        """환경변수 반영 후 실행 설정 해시."""
        return self._resolved_hash

    # live에서 기본 계좌 폴백 경고를 전략당 1회만 내기 위한 기록 (프로세스 전역)
    _default_account_warned: set = set()

    def get_account_no(self, strategy: str = "") -> str:
        """
        전략에 해당하는 계좌번호 반환 (다중 계좌 분리).
        kis_api.accounts에 전략명이 있으면 해당 계좌, 없으면 kis_api.account_no(기본) 사용.

        live 모드에서 전략 키가 미선언/빈 값이라 기본 계좌로 폴백하면 경고를 남긴다 —
        침묵 폴백은 여러 전략·바스켓이 모르게 같은 실계좌(자본 풀)를 공유하게 만들고,
        DB상 account_key는 서로 달라 보여 공유 사실이 가려진다.
        """
        kis = self.kis_api
        accounts = kis.get("accounts", {}) or {}
        if strategy and strategy in accounts and accounts[strategy]:
            return accounts[strategy]
        if strategy and str(self.trading.get("mode", "paper")).lower() == "live":
            if strategy not in Config._default_account_warned:
                Config._default_account_warned.add(strategy)
                logging.getLogger("config_loader").warning(
                    "live 계좌 라우팅: 전략 '%s'의 계좌가 kis_api.accounts에 %s — "
                    "기본 계좌로 폴백합니다(다른 전략과 자본 풀 공유 가능).",
                    strategy,
                    "선언되지 않음" if strategy not in accounts else "빈 값",
                )
        return kis.get("account_no", "")

    def with_strategy_overrides(self, strategy_name: str, overrides: dict) -> "ConfigOverlay":
        """
        전략 파라미터만 덮어쓴 Config 래퍼 반환 (파라미터 최적화 등에서 사용).
        """
        return ConfigOverlay(self, strategy_name, overrides)


class ConfigOverlay:
    """
    Config 래퍼: 특정 전략의 파라미터만 덮어써서 전략 인스턴스에 전달.
    나머지 속성은 base_config 에 위임.
    """
    def __init__(self, base_config: Config, strategy_name: str, overrides: dict):
        self._base = base_config
        self._strategy_name = strategy_name
        self._overrides = overrides or {}

    @property
    def strategies(self) -> dict:
        merged = dict(self._base.strategies)
        section = merged.get(self._strategy_name, {})
        merged[self._strategy_name] = {**section, **self._overrides}
        return merged

    def __getattr__(self, name: str):
        return getattr(self._base, name)
