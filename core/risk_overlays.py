"""추세·낙폭·변동성에 따라 주식 목표 비중을 조절한다.

전일까지의 자료와 직전 상태로 위험 배수(0~1)를 계산한다. product는 기존처럼
배수를 곱하고, minimum은 가장 낮은 배수 하나를 사용한다. 방어 자산이 지정돼
있으면 줄인 주식 비중을 해당 자산에 배분한다. 자료가 부족하면 확대를 보류한다.

검증 근거와 한계: docs/RISK_REVIEW_20260917.md.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR_ENV = "QUANT_OVERLAY_STATE_DIR"


@dataclass(frozen=True)
class TrendFilterConfig:
    enabled: bool = False
    index_symbol: str = "KS200"
    ma_days: int = 200
    band: float = 0.02
    off_scale: float = 0.5


@dataclass(frozen=True)
class DrawdownGuardConfig:
    enabled: bool = False
    trigger: float = -0.10
    release: float = -0.05
    scale: float = 0.5


@dataclass(frozen=True)
class VolatilityTargetConfig:
    enabled: bool = False
    target: float = 0.20
    lookback_days: int = 60
    min_scale: float = 0.5
    max_scale: float = 1.0
    step: float = 0.1


@dataclass(frozen=True)
class OverlayConfig:
    trend: TrendFilterConfig = TrendFilterConfig()
    drawdown: DrawdownGuardConfig = DrawdownGuardConfig()
    volatility: VolatilityTargetConfig = VolatilityTargetConfig()
    combination: str = "product"

    @property
    def any_enabled(self) -> bool:
        return self.trend.enabled or self.drawdown.enabled or self.volatility.enabled


@dataclass
class OverlayDecision:
    scale: float = 1.0
    reasons: list[str] = field(default_factory=list)
    data_issues: list[str] = field(default_factory=list)
    trend_below: bool | None = None
    trend_rel_to_ma: float | None = None
    drawdown_active: bool | None = None
    drawdown: float | None = None
    vol_scale: float | None = None
    realized_vol: float | None = None
    evaluated_at: str = ""
    source_dates: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(value: Any, default: float) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def parse_overlay_config(basket_cfg: dict[str, Any] | None) -> OverlayConfig:
    """baskets.yaml의 overlays 블록을 읽는다. 없거나 비정상이면 전부 off."""
    raw = (basket_cfg or {}).get("overlays") or {}
    if not isinstance(raw, dict):
        return OverlayConfig()
    t = raw.get("trend_filter") or {}
    d = raw.get("drawdown_guard") or {}
    v = raw.get("volatility_target") or {}
    trend = TrendFilterConfig(
        enabled=bool(t.get("enabled", False)),
        index_symbol=str(t.get("index_symbol") or "KS200"),
        ma_days=max(20, int(_f(t.get("ma_days"), 200))),
        band=max(0.0, _f(t.get("band"), 0.02)),
        off_scale=min(1.0, max(0.0, _f(t.get("off_scale"), 0.5))),
    )
    drawdown = DrawdownGuardConfig(
        enabled=bool(d.get("enabled", False)),
        trigger=min(0.0, _f(d.get("trigger"), -0.10)),
        release=min(0.0, _f(d.get("release"), -0.05)),
        scale=min(1.0, max(0.0, _f(d.get("scale"), 0.5))),
    )
    volatility = VolatilityTargetConfig(
        enabled=bool(v.get("enabled", False)),
        target=max(0.01, _f(v.get("target"), 0.20)),
        lookback_days=max(10, int(_f(v.get("lookback_days"), 60))),
        min_scale=min(1.0, max(0.0, _f(v.get("min_scale"), 0.5))),
        max_scale=min(1.0, max(0.0, _f(v.get("max_scale"), 1.0))),
        step=max(0.0, _f(v.get("step"), 0.1)),
    )
    combination = str(raw.get("combination", "product"))
    if combination not in {"product", "minimum"}:
        raise ValueError("overlays.combination은 product 또는 minimum이어야 합니다")
    return OverlayConfig(trend=trend, drawdown=drawdown, volatility=volatility, combination=combination)


# ------------------------------------------------------------------
# 개별 신호 (순수 함수)
# ------------------------------------------------------------------

def trend_below_ma(
    index_closes: Sequence[float], cfg: TrendFilterConfig, prev_below: bool | None,
) -> tuple[bool | None, float | None]:
    """지수가 이동평균 아래인가 — 히스테리시스 적용.

    반환 (below, rel). 데이터가 ma_days 미만이면 (prev_below, None): 판단을 지어내지 않는다.
    prev_below가 None(첫 실행)이면 band 없이 선 아래/위로 초기화한다.
    """
    closes = list(index_closes)
    if len(closes) >= cfg.ma_days:
        try:
            closes = [float(c) for c in closes[-cfg.ma_days:]]
        except (TypeError, ValueError):
            return prev_below, None
        if any(not math.isfinite(c) or c <= 0 for c in closes):
            return prev_below, None
    if len(closes) < cfg.ma_days:
        return prev_below, None
    window = closes[-cfg.ma_days:]
    ma = sum(window) / len(window)
    if ma <= 0:
        return prev_below, None
    rel = closes[-1] / ma - 1.0
    if prev_below is None:
        return rel < 0.0, rel
    if prev_below and rel > cfg.band:
        return False, rel
    if (not prev_below) and rel < -cfg.band:
        return True, rel
    return bool(prev_below), rel


def drawdown_from_cumulative_returns(cumulative_returns_pct: Sequence[float]) -> float | None:
    """시간가중 누적수익률(%) 시계열 → 현재 고점 대비 낙폭(음수 비율). 고점은 시작 자본(지수 1.0)부터."""
    peak = 1.0
    last = None
    for cr in cumulative_returns_pct:
        if cr is None:
            return None
        try:
            idx = 1.0 + float(cr) / 100.0
        except (TypeError, ValueError):
            return None
        if not math.isfinite(idx) or idx <= 0:
            return None
        peak = max(peak, idx)
        last = idx
    if last is None:
        return None
    return last / peak - 1.0


def drawdown_guard_active(drawdown: float | None, cfg: DrawdownGuardConfig, prev_active: bool | None) -> bool | None:
    """낙폭 제어 발동 여부 — trigger 아래로 내려가면 켜지고 release 안으로 회복해야 꺼진다."""
    if drawdown is None:
        return prev_active
    active = bool(prev_active)
    if active and drawdown >= cfg.release:
        return False
    if (not active) and drawdown <= cfg.trigger:
        return True
    return active


def realized_volatility(daily_returns: Sequence[float], lookback_days: int) -> float | None:
    """최근 lookback_days 일간수익률의 연환산 표준편차."""
    try:
        rets = [float(r) for r in list(daily_returns)[-lookback_days:]]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(r) or r <= -1 for r in rets):
        return None
    if len(rets) < lookback_days:
        return None
    window = rets[-lookback_days:]
    mean = sum(window) / len(window)
    var = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
    return math.sqrt(var) * math.sqrt(252)


def volatility_scale(daily_returns: Sequence[float], cfg: VolatilityTargetConfig) -> tuple[float | None, float | None]:
    """변동성 목표 배수. 데이터 부족이면 (None, None)."""
    vol = realized_volatility(daily_returns, cfg.lookback_days)
    if vol is None or vol <= 0:
        return None, vol
    scale = cfg.target / vol
    scale = min(cfg.max_scale, max(cfg.min_scale, scale))
    if cfg.step > 0:
        scale = round(scale / cfg.step) * cfg.step
        scale = min(cfg.max_scale, max(cfg.min_scale, scale))
    return round(scale, 4), vol


# ------------------------------------------------------------------
# 결합
# ------------------------------------------------------------------

def compute_decision(
    cfg: OverlayConfig,
    *,
    index_closes: Sequence[float] | None = None,
    cumulative_returns_pct: Sequence[float] | None = None,
    daily_returns: Sequence[float] | None = None,
    prev_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> OverlayDecision:
    """설정한 결합 방식에 따라 하나의 위험 배수를 계산한다."""
    prev = prev_state or {}
    decision = OverlayDecision(evaluated_at=(now or datetime.now()).isoformat(timespec="seconds"))
    scale = 1.0
    scales = []

    if cfg.trend.enabled:
        below, rel = trend_below_ma(index_closes or [], cfg.trend, prev.get("trend_below"))
        decision.trend_rel_to_ma = None if rel is None else round(rel, 4)
        if rel is None:
            decision.data_issues.append(
                f"추세 필터: {cfg.trend.index_symbol} 종가 {cfg.trend.ma_days}일치 부족 — 직전 상태 유지"
            )
        decision.trend_below = below
        if below:
            scale *= cfg.trend.off_scale
            scales.append(cfg.trend.off_scale)
            decision.reasons.append(
                f"{cfg.trend.index_symbol} {cfg.trend.ma_days}일선 아래"
                + (f"({rel:+.1%})" if rel is not None else "")
                + f" → 주식 비중 ×{cfg.trend.off_scale:g}"
            )

    if cfg.drawdown.enabled:
        dd = drawdown_from_cumulative_returns(cumulative_returns_pct or [])
        active = drawdown_guard_active(dd, cfg.drawdown, prev.get("drawdown_active"))
        decision.drawdown = None if dd is None else round(dd, 4)
        if dd is None:
            decision.data_issues.append("낙폭 제어: NAV 기록 없음 — 직전 상태 유지")
        decision.drawdown_active = active
        if active:
            scale *= cfg.drawdown.scale
            scales.append(cfg.drawdown.scale)
            decision.reasons.append(
                f"고점 대비 낙폭 {dd:+.1%}" if dd is not None else "낙폭 제어 발동 중"
            )
            decision.reasons[-1] += f" → 주식 비중 ×{cfg.drawdown.scale:g}"

    if cfg.volatility.enabled:
        vs, vol = volatility_scale(daily_returns or [], cfg.volatility)
        decision.realized_vol = None if vol is None else round(vol, 4)
        if vs is None:
            decision.data_issues.append("변동성 계산에 필요한 일간 수익률이 부족해 비중 확대를 보류했습니다")
            prev_vs = prev.get("vol_scale")
            vs = float(prev_vs) if isinstance(prev_vs, (int, float)) else 1.0
        decision.vol_scale = vs
        if vs < 1.0:
            scale *= vs
            scales.append(vs)
            decision.reasons.append(
                f"실현 변동성 {vol:.0%} > 목표 {cfg.volatility.target:.0%} → 주식 비중 ×{vs:g}"
                if vol is not None else f"변동성 목표 배수 ×{vs:g}"
            )

    if cfg.combination == "minimum":
        scale = min(scales, default=1.0)
    if decision.data_issues:
        # 자료가 없다는 이유로 직전보다 투자 위험을 늘리지 않는다.
        previous_scale = _f(prev.get("scale"), min(
            [1.0] + ([cfg.trend.off_scale] if cfg.trend.enabled else [])
            + ([cfg.drawdown.scale] if cfg.drawdown.enabled else [])
            + ([cfg.volatility.min_scale] if cfg.volatility.enabled else [])
        ))
        scale = min(scale, previous_scale)
    decision.scale = round(min(1.0, max(0.0, scale)), 4)
    return decision


def describe_decision(decision: OverlayDecision | dict[str, Any] | None, cfg: OverlayConfig | None = None) -> str:
    """대시보드·헬스용 한 줄 설명."""
    if decision is None:
        return "첫 실행 대기: 위험 관리 조건을 아직 확인하지 않았습니다"
    d = decision.to_dict() if isinstance(decision, OverlayDecision) else dict(decision)
    scale = float(d.get("scale", 1.0))
    parts = list(d.get("reasons") or [])
    issues = list(d.get("data_issues") or [])
    if not parts:
        text = "자료 확인 전 비중 확대 보류" if issues else "기본 투자 비중 유지"
    else:
        text = " · ".join(parts)
    if issues:
        text += " · 확인 필요: " + "; ".join(issues)
    return f"{text}. 주식 목표 비중은 기본 설정의 {scale:.0%}입니다"


def overlay_target_weights(
    weights: dict[str, float], design_fraction: float, scale: float,
    defensive_symbol: str | None = None,
) -> dict[str, float]:
    """총자산 기준 목표 비중. 방어 자산이 있으면 주식 축소분을 그 자산에 배분한다.

    예: 주식 47.5%, CD ETF 47.5%, 현금 5%에서 위험 배수 0.5는
    주식 23.75%, CD ETF 71.25%, 현금 5%다. CD ETF를 주식처럼 매도하지 않는다.
    방어 자산 미지정 시에는 기존처럼 모든 보유 비중을 줄여 현금으로 둔다.
    """
    clean = {str(s): float(w) for s, w in weights.items()}
    if any(not math.isfinite(w) or w < 0 for w in clean.values()):
        raise ValueError("목표 비중은 유한한 0 이상의 값이어야 합니다")
    total = sum(clean.values())
    if total <= 0:
        return {}
    if not math.isfinite(design_fraction) or not 0 <= design_fraction <= 1:
        raise ValueError("투자 비중은 0~1 사이여야 합니다")
    if not math.isfinite(scale) or not 0 <= scale <= 1:
        raise ValueError("위험 배수는 0~1 사이여야 합니다")
    if defensive_symbol and clean.get(defensive_symbol, 0) <= 0:
        raise ValueError("방어 자산은 보유 비중에 포함된 종목이어야 합니다")
    target = {s: w / total * design_fraction * scale for s, w in clean.items()}
    if defensive_symbol:
        target[defensive_symbol] += design_fraction * (1.0 - scale)
    return target


# ------------------------------------------------------------------
# 상태 파일
# ------------------------------------------------------------------

def overlay_state_dir(state_dir: str | os.PathLike | None = None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ.get(STATE_DIR_ENV)
    return Path(env) if env else _ROOT / "data" / "overlay_state"


def overlay_state_path(basket_name: str, state_dir: str | os.PathLike | None = None) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(basket_name))
    return overlay_state_dir(state_dir) / f"{safe}.json"


def load_overlay_state(
    basket_name: str, state_dir: str | os.PathLike | None = None, *, mode: str | None = None,
) -> dict[str, Any] | None:
    directory = overlay_state_dir(state_dir)
    if mode is not None and mode not in {"paper", "live"}:
        raise ValueError("위험 관리 기록의 모드는 paper 또는 live여야 합니다")
    path = overlay_state_path(basket_name, directory / mode if mode else directory)
    # 이전 버전의 공용 기록은 모의투자만 이어받는다. 실전 계좌로 전파하지 않는다.
    if mode == "paper" and not path.exists():
        path = overlay_state_path(basket_name, directory)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        scale = float(data["scale"])
        if not math.isfinite(scale) or not 0 <= scale <= 1:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    for key in ("trend_below", "drawdown_active"):
        if data.get(key) is not None and not isinstance(data[key], bool):
            return None
    for key in ("reasons", "data_issues"):
        if key in data and (not isinstance(data[key], list)
                            or any(not isinstance(item, str) for item in data[key])):
            return None
    data["scale"] = scale
    return data


def save_overlay_state(
    basket_name: str, decision: OverlayDecision, state_dir: str | os.PathLike | None = None,
    *, mode: str | None = None,
) -> Path:
    directory = overlay_state_dir(state_dir)
    if mode is not None and mode not in {"paper", "live"}:
        raise ValueError("위험 관리 기록의 모드는 paper 또는 live여야 합니다")
    path = overlay_state_path(basket_name, directory / mode if mode else directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = decision.to_dict()
    payload["basket"] = str(basket_name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def invested_fraction(
    weights: dict[str, float],
    design_fraction: float,
    state: dict[str, Any] | None,
    defensive_symbol: str | None = None,
) -> float:
    """리밸런서가 실제로 맞추는 투자 비중(방어 자산 포함) — BasketRebalancer._stock_fraction과 같은 규칙.

    방어 자산(CD ETF)이 있으면 오버레이가 주식을 줄인 만큼 방어 자산을 사므로 투자
    비중은 설계 그대로다. 헬스가 applied_stock_fraction(설계 × 배수)으로 감시하면
    오버레이가 켜진 날 기준이 절반으로 내려가, 방어 자산 매수가 실패해 현금이 쌓여도
    '정상'으로 읽힌다. 방어 자산이 없으면 두 값은 같다.
    """
    scale = 1.0
    if state:
        try:
            scale = min(1.0, max(0.0, float(state.get("scale", 1.0))))
        except (TypeError, ValueError):
            scale = 1.0
    if not weights:
        return float(design_fraction) * scale
    return sum(overlay_target_weights(weights, float(design_fraction), scale, defensive_symbol).values())


def applied_stock_fraction(design_fraction: float, state: dict[str, Any] | None) -> float:
    """설계 주식 비중 × 마지막 오버레이 배수. 상태가 없으면 설계 그대로."""
    if not state:
        return float(design_fraction)
    try:
        scale = float(state.get("scale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    return float(design_fraction) * min(1.0, max(0.0, scale))
