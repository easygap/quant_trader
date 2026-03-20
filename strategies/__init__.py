"""
전략 패키지 — 플러그인 레지스트리

새 전략 추가 시 이 파일의 STRATEGY_REGISTRY에 등록만 하면
backtest, main, scheduler 등 모든 곳에서 자동으로 인식됩니다.
"""

from config.config_loader import Config


# 전략명 → (모듈 경로, 클래스명)  — lazy import로 순환 참조 방지
_STRATEGY_REGISTRY: dict[str, tuple[str, str]] = {
    "scoring":         ("strategies.scoring_strategy",  "ScoringStrategy"),
    "mean_reversion":  ("strategies.mean_reversion",    "MeanReversionStrategy"),
    "trend_following": ("strategies.trend_following",    "TrendFollowingStrategy"),
    "fundamental_factor": ("strategies.fundamental_factor", "FundamentalFactorStrategy"),
    "ensemble":        ("core.strategy_ensemble",       "StrategyEnsemble"),
}


def get_strategy_names() -> list[str]:
    """등록된 전략명 목록 반환 (argparse choices 등에 활용)."""
    return list(_STRATEGY_REGISTRY.keys())


def create_strategy(name: str, config: Config = None):
    """
    이름으로 전략 인스턴스를 생성합니다.

    Args:
        name: 전략명 (registry에 등록된 키)
        config: Config 객체 (None이면 싱글톤 사용)

    Returns:
        BaseStrategy 인스턴스

    Raises:
        ValueError: 등록되지 않은 전략명
    """
    import importlib

    config = config or Config.get()

    if name not in _STRATEGY_REGISTRY:
        available = ", ".join(_STRATEGY_REGISTRY.keys())
        raise ValueError(f"알 수 없는 전략: '{name}'. 사용 가능: {available}")

    module_path, class_name = _STRATEGY_REGISTRY[name]
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(config)


def register_strategy(name: str, module_path: str, class_name: str):
    """런타임에 전략을 추가 등록합니다 (외부 플러그인 용)."""
    _STRATEGY_REGISTRY[name] = (module_path, class_name)
