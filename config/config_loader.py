"""
설정 로더 모듈
- YAML 설정 파일을 로드하여 딕셔너리로 반환
- settings.yaml, strategies.yaml, risk_params.yaml 통합 관리
"""

import os
import yaml
from pathlib import Path

try:
    from dotenv import load_dotenv
    # 프로젝트 루트의 .env 파일 로드
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# 프로젝트 루트 디렉토리 (config/ 의 상위)
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


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
    # 전략별 계좌 (다중 계좌): KIS_ACCOUNT_NO_SCORING, KIS_ACCOUNT_NO_MEAN_REVERSION 등으로 덮어씀
    accounts = settings["kis_api"].get("accounts", {}) or {}
    for key in list(accounts.keys()):
        env_key = f"KIS_ACCOUNT_NO_{key.upper().replace('-', '_')}"
        accounts[key] = os.environ.get(env_key, accounts[key])
    settings["kis_api"]["accounts"] = accounts
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

    # Paper 전용 프로필: QUANT_AUTO_ENTRY=true 환경변수로만 auto_entry 활성화.
    # 기본 config(settings.yaml)는 auto_entry 미설정 = False 유지.
    # live 모드에서는 auto_entry와 무관하게 hard gate가 차단.
    trading = settings.setdefault("trading", {})
    if os.environ.get("QUANT_AUTO_ENTRY", "").lower() == "true":
        trading["auto_entry"] = True

    return settings

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
    return _override_with_env(settings)


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

    def get_account_no(self, strategy: str = "") -> str:
        """
        전략에 해당하는 계좌번호 반환 (다중 계좌 분리).
        kis_api.accounts에 전략명이 있으면 해당 계좌, 없으면 kis_api.account_no(기본) 사용.
        """
        kis = self.kis_api
        accounts = kis.get("accounts", {}) or {}
        if strategy and strategy in accounts:
            return accounts[strategy] or kis.get("account_no", "")
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
