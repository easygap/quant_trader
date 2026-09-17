"""바스켓 주식 비중 리스크 오버레이 — 추세 필터·낙폭 제어·변동성 목표.

이 저장소의 확정 결론은 "종목 선택으로 시장을 이길 알파는 없다"이고, 손익을 가르는 것은
주식 비중이다. 그래서 여기서는 종목을 고르지 않고, **설계 주식 비중에 곱할 배수(0~1)**만
계산한다. 배수는 설계를 대체하지 않고 위임한다: 설계 60%에 배수 0.5면 목표 30%.

근거와 숫자는 tools/risk_overlay_backtest.py → docs/RISK_OVERLAY_FINDINGS.md.
  - 추세 필터: 지수가 200일선 아래로 band(2%)만큼 내려가면 off_scale(0.5)배, 위로
    band만큼 올라와야 복귀. 히스테리시스가 없으면 선 근처에서 왕복 매매가 난다.
  - 낙폭 제어: 시간가중 NAV가 고점 대비 trigger(-10%) 아래면 scale(0.5)배, release(-5%)
    안으로 회복해야 복귀.
  - 변동성 목표: 실현 변동성이 목표(20%)를 넘으면 목표/실현 비율로 축소(0.1 단위).
    한국 시장에선 수익을 많이 깎아 기본 off — 옵션으로만 둔다.

원칙
  - 모든 판단은 전일까지의 정보만 쓴다(리밸런싱은 장 시작 전에 전일 종가로 돌아간다).
  - 상태(추세 아래/낙폭 발동)는 파일로 이어진다. 히스테리시스는 상태가 있어야 성립한다.
  - 데이터가 없으면 새 상태를 지어내지 않고 직전 상태를 유지하며 data_issues에 남긴다.
    헬스가 그 사실을 표면화한다("오류 0건인데 설계대로 안 돈다"를 막기 위해).
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(value: Any, default: float) -> float:
    try:
        return float(value)
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
    return OverlayConfig(trend=trend, drawdown=drawdown, volatility=volatility)


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
    closes = [float(c) for c in index_closes if c is not None and not math.isnan(float(c))]
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
            continue
        try:
            idx = 1.0 + float(cr) / 100.0
        except (TypeError, ValueError):
            continue
        if math.isnan(idx):
            continue
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
    rets = [float(r) for r in daily_returns if r is not None and not math.isnan(float(r))]
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
    """설정된 오버레이를 모두 평가해 배수 하나로 합친다(곱)."""
    prev = prev_state or {}
    decision = OverlayDecision(evaluated_at=(now or datetime.now()).isoformat(timespec="seconds"))
    scale = 1.0

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
            decision.reasons.append(
                f"고점 대비 낙폭 {dd:+.1%}" if dd is not None else "낙폭 제어 발동 중"
            )
            decision.reasons[-1] += f" → 주식 비중 ×{cfg.drawdown.scale:g}"

    if cfg.volatility.enabled:
        vs, vol = volatility_scale(daily_returns or [], cfg.volatility)
        decision.realized_vol = None if vol is None else round(vol, 4)
        if vs is None:
            decision.data_issues.append("변동성 목표: 일간 수익률 부족 — 배수 1.0")
            prev_vs = prev.get("vol_scale")
            vs = float(prev_vs) if isinstance(prev_vs, (int, float)) else 1.0
        decision.vol_scale = vs
        if vs < 1.0:
            scale *= vs
            decision.reasons.append(
                f"실현 변동성 {vol:.0%} > 목표 {cfg.volatility.target:.0%} → 주식 비중 ×{vs:g}"
                if vol is not None else f"변동성 목표 배수 ×{vs:g}"
            )

    decision.scale = round(min(1.0, max(0.0, scale)), 4)
    return decision


def describe_decision(decision: OverlayDecision | dict[str, Any] | None, cfg: OverlayConfig | None = None) -> str:
    """대시보드·헬스용 한 줄 설명."""
    if decision is None:
        return "위험 조절 상태 없음 — 첫 실행 대기"
    d = decision.to_dict() if isinstance(decision, OverlayDecision) else dict(decision)
    scale = float(d.get("scale", 1.0))
    parts = list(d.get("reasons") or [])
    issues = list(d.get("data_issues") or [])
    if not parts:
        text = "위험 조절 발동 없음 · 설계 비중 그대로"
    else:
        text = " · ".join(parts)
    if issues:
        text += " · 확인 필요: " + "; ".join(issues)
    return f"{text} (배수 {scale:g})"


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


def load_overlay_state(basket_name: str, state_dir: str | os.PathLike | None = None) -> dict[str, Any] | None:
    path = overlay_state_path(basket_name, state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_overlay_state(
    basket_name: str, decision: OverlayDecision, state_dir: str | os.PathLike | None = None,
) -> Path:
    path = overlay_state_path(basket_name, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = decision.to_dict()
    payload["basket"] = str(basket_name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def applied_stock_fraction(design_fraction: float, state: dict[str, Any] | None) -> float:
    """설계 주식 비중 × 마지막 오버레이 배수. 상태가 없으면 설계 그대로."""
    if not state:
        return float(design_fraction)
    try:
        scale = float(state.get("scale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    return float(design_fraction) * min(1.0, max(0.0, scale))
