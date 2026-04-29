"""
전략 패키지 — 플러그인 레지스트리 + 상태 관리

전략 상태 (Strategy Status) — 승격 규칙 v2 (2026-04-02 debiased 평가 기준):

  research_only:    backtest만 허용. 절대수익 음수 또는 PF<1.0.
  paper_only:       backtest + paper. 절대수익>0, PF≥1.0, WF positive≥50%.
                    벤치마크 초과수익 불요구. opportunity cost 기준 음수 허용.
  paper_candidate:  paper 60영업일 실험 대상. paper_only + WF Sharpe>0≥50%,
                    exposure-matched excess > -100%p, MDD > -20%.
                    (provisional) 표기 시 일부 기준 경계 미달.
  live_candidate:   live 전환 대기. paper_candidate + ELIGIBLE paper evidence package
                    (60영업일 execution-backed, 양의 excess, frozen day 0).

승격 경로: research_only → paper_only → paper_candidate → live_candidate
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
}

# ── 전략 상태 레지스트리 (Hard Gate 기준, v2 승격 규칙 적용) ──
STRATEGY_STATUS: dict[str, dict] = {
    # ── provisional paper candidate (자동 판정: ret>0, PF≥1.0, WF P≥50%, WF Sh+≥50%, MDD>-20%) ──
    "relative_strength_rotation": {
        "status": "provisional_paper_candidate",
        "allowed_modes": ["backtest", "paper"],
        "reason": (
            "debiased +18.09%, PF 1.62, WF 6/6 positive, 5/6 Sharpe>0. MDD -5.66%. "
            "provisional: 내부 연구 우선순위. 경제적 alpha 미확인 (same-universe excess 미검증)."
        ),
    },

    "scoring": {
        "status": "provisional_paper_candidate",
        "allowed_modes": ["backtest", "paper"],
        "reason": (
            "debiased +11.22%, PF 1.07, WF 5/6 positive, 3/6 Sharpe>0 (50% 경계 통과). MDD -14.55%. "
            "provisional: 가중치 미최적화, 다중공선성 미해결. 최적화 후 재평가 필요."
        ),
    },

    # ── research only (자동 판정: ret≤0 또는 PF<1 또는 WF P<50%) ──
    "breakout_volume": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased -13.31%, PF 0.79<1.0, WF 0/6. [운영 메모: BV50/R50 Paper Sleeve A 가동 중이나 상태와 무관]",
    },
    "mean_reversion": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased -8.36%, PF 0.85<1.0, WF 2/6",
    },
    "trend_following": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased -6.94%, PF 0.67<1.0, WF 1/6",
    },
    "trend_pullback": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased 미실행. WF 0 windows",
    },
    "fundamental_factor": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased 미실행. yfinance 불일치",
    },
    "fundamental_first": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased 미실행. fundamental_factor 종속",
    },
    "momentum_factor": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "debiased 미실행",
    },
    "ensemble": {
        "status": "disabled",
        "allowed_modes": ["backtest"],
        "reason": "구성 전략 대부분 disabled. 독립성 부족",
    },
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

    if mode == "backtest":
        return True, "backtest는 모든 전략 허용"

    if mode in ("paper", "schedule"):
        if status in ("paper_only", "paper_candidate", "provisional_paper_candidate", "live_candidate"):
            return True, f"status={status}, paper 허용"
        return False, f"전략 '{name}'은 status={status}. paper 모드는 paper_only 이상만 허용."

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
