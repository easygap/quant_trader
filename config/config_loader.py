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
    """환경변수(.env) 값으로 yaml 설정을 덮어씁니다."""
    
    # KIS API 시크릿
    if "kis_api" in settings:
        settings["kis_api"]["app_key"] = os.environ.get("KIS_APP_KEY", settings["kis_api"].get("app_key"))
        settings["kis_api"]["app_secret"] = os.environ.get("KIS_APP_SECRET", settings["kis_api"].get("app_secret"))
        settings["kis_api"]["account_no"] = os.environ.get("KIS_ACCOUNT_NO", settings["kis_api"].get("account_no"))
        
        # Rate Limiting
        if "MAX_CALLS_PER_SEC" in os.environ:
            settings["kis_api"]["max_calls_per_sec"] = float(os.environ["MAX_CALLS_PER_SEC"])
        if "MAX_RETRY" in os.environ:
            settings["kis_api"]["max_retry"] = int(os.environ["MAX_RETRY"])

    # Discord 알림
    if "discord" in settings:
        settings["discord"]["webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL", settings["discord"].get("webhook_url"))

    return settings

def load_settings() -> dict:
    """전체 설정(settings.yaml) 및 환경변수 오버라이드 로드"""
    settings = load_yaml(CONFIG_DIR / "settings.yaml")
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
        """설정 파일 로드"""
        self._settings = load_settings()
        self._strategies = load_strategies()
        self._risk_params = load_risk_params()

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
    def discord(self) -> dict:
        """디스코드 설정"""
        return self._settings.get("discord", {})

    @property
    def watchlist(self) -> list:
        """관심 종목 리스트"""
        wl = self._settings.get("watchlist", {})
        return wl.get("symbols", [])

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
