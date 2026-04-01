"""
전략 패키지 — 플러그인 레지스트리 + 상태 관리

전략 상태 (Strategy Status):
- disabled:          사용 불가. 코드 존재하나 검증 실패 또는 구조적 결함.
- experimental:      paper 모드에서만 실행 가능. 검증 진행 중.
- paper_candidate:   paper 운영 승인. WF 통과 + 벤치마크 초과수익 양수.
- live_candidate:    live 전환 대기. paper 60일 + GoLive 체크 통과.

승격 경로: disabled → experimental → paper_candidate → live_candidate
강등: 어느 단계에서든 기준 미달 시 즉시 하위 단계로 강등.
"""

from config.config_loader import Config


# ── 전략 코드 레지스트리 ──
_STRATEGY_REGISTRY: dict[str, tuple[str, str]] = {
    "scoring":             ("strategies.scoring_strategy",    "ScoringStrategy"),
    "mean_reversion":      ("strategies.mean_reversion",     "MeanReversionStrategy"),
    "trend_following":     ("strategies.trend_following",     "TrendFollowingStrategy"),
    "trend_pullback":      ("strategies.trend_pullback",      "TrendPullbackStrategy"),
    "breakout_volume":     ("strategies.breakout_volume",     "BreakoutVolumeStrategy"),
    "relative_strength_rotation": ("strategies.relative_strength_rotation", "RelativeStrengthRotationStrategy"),
    "fundamental_factor":  ("strategies.fundamental_factor",  "FundamentalFactorStrategy"),
    "fundamental_first":   ("strategies.fundamental_first",   "FundamentalFirstStrategy"),
    "momentum_factor":     ("strategies.momentum_factor",     "MomentumFactorStrategy"),
    "ensemble":            ("core.strategy_ensemble",         "StrategyEnsemble"),
    "trend_pullback":      ("strategies.trend_pullback",      "TrendPullbackStrategy"),
}

# ── 전략 상태 레지스트리 (Hard Gate 기준) ──
STRATEGY_STATUS: dict[str, dict] = {
    "scoring":            {"status": "experimental",  "allowed_modes": ["backtest", "paper"]},
    "mean_reversion":     {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "10종목 평균 Sharpe -2.50, OOS Sharpe -3.20"},
    "trend_following":    {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "미검증"},
    "trend_pullback":     {"status": "experimental",  "allowed_modes": ["backtest"],          "reason": "C-3A 구조 재설계 검증 중"},
    "fundamental_factor": {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "yfinance debtToEquity 한국 기준 불일치, WF 미실행"},
    "fundamental_first":  {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "fundamental_factor 종속, WF 미실행"},
    "momentum_factor":    {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "WF 미실행"},
    "ensemble":           {"status": "disabled",      "allowed_modes": ["backtest"],          "reason": "구성 전략 모두 미승인"},
    "breakout_volume":    {"status": "experimental",  "allowed_modes": ["backtest"],          "reason": "C-4 MVP, coarse sweep 검증 중"},
    "relative_strength_rotation": {"status": "experimental", "allowed_modes": ["backtest"], "reason": "C-5 MVP, sleeve 결합 검증 중"},
    "trend_pullback":     {"status": "experimental",  "allowed_modes": ["backtest", "paper"], "reason": "C-1 MVP, WF 미실행"},
}


def get_strategy_names() -> list[str]:
    return list(_STRATEGY_REGISTRY.keys())


def get_strategy_status(name: str) -> dict:
    """전략 상태 반환. 미등록 시 disabled."""
    return STRATEGY_STATUS.get(name, {"status": "disabled", "allowed_modes": []})


def is_strategy_allowed(name: str, mode: str) -> tuple[bool, str]:
    """주어진 모드에서 전략 실행이 허용되는지 판정.

    Returns:
        (allowed, reason)
    """
    st = get_strategy_status(name)
    status = st["status"]
    allowed_modes = st.get("allowed_modes", [])

    if mode == "backtest":
        return True, "backtest는 모든 전략 허용"

    if mode in ("paper", "schedule"):
        if status in ("experimental", "paper_candidate", "live_candidate"):
            return True, f"status={status}, paper 허용"
        return False, f"전략 '{name}'은 status={status}. paper 모드는 experimental 이상만 허용."

    if mode == "live":
        if status == "live_candidate":
            return True, f"status=live_candidate, live 허용"
        return False, f"전략 '{name}'은 status={status}. live 모드는 live_candidate만 허용."

    return False, f"알 수 없는 모드: {mode}"


def create_strategy(name: str, config: Config = None):
    """이름으로 전략 인스턴스를 생성합니다."""
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
