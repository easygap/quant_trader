"""
전략 승격 규칙 엔진 — metrics 기반 자동 판정

상태 정의:
  research_only:              backtest만 허용. 기본 상태.
  paper_only:                 backtest + paper. 절대수익>0, PF≥1.0, WF positive≥50%.
  provisional_paper_candidate: paper 60일 실험 대상. paper_only + WF Sharpe>0≥50%, MDD>-20%.
                               "내부 연구 우선순위"를 의미. 경제적 alpha 미확인.
  live_candidate:             live 전환 가능. provisional + eligible paper evidence package.
                               "경제적으로 유의미한 후보"를 의미.

핵심 원칙:
  - 상태는 metrics에서 자동 결정됨. 수동 override 불가.
  - "existing experiment"는 상태를 승격시키지 않음.
  - experiment_note 필드로 운영 사실을 분리 기록.
"""
from dataclasses import dataclass
from typing import Optional
from loguru import logger

from core.live_gate import LIVE_GATE_ARTIFACT_TYPE, LIVE_GATE_SCHEMA_VERSION


@dataclass
class StrategyMetrics:
    """전략 평가 지표 — debiased 평가 결과를 입력."""
    name: str
    total_return: float           # %
    profit_factor: float          # gross_profit / gross_loss
    mdd: float                    # % (음수)
    wf_positive_rate: float       # 0~1 (양수 수익 window 비율)
    wf_sharpe_positive_rate: float  # 0~1 (Sharpe>0 window 비율)
    wf_windows: int               # walk-forward window 수
    wf_total_trades: int          # WF 전체 거래 수
    sharpe: float                 # full-period Sharpe ratio
    # paper 실적 (live_candidate 판정용, 없으면 None)
    paper_days: Optional[int] = None
    paper_sharpe: Optional[float] = None
    paper_excess: Optional[float] = None  # same-universe excess return
    paper_evidence_recommendation: Optional[str] = None
    paper_benchmark_final_ratio: Optional[float] = None
    paper_sell_count: Optional[int] = None
    paper_win_rate: Optional[float] = None
    paper_frozen_days: Optional[int] = None
    paper_cumulative_return: Optional[float] = None


@dataclass
class PromotionResult:
    """승격 판정 결과."""
    name: str
    status: str                   # research_only / paper_only / provisional_paper_candidate / live_candidate
    allowed_modes: list[str]
    reason: str                   # 판정 이유 (pass/fail 상세)
    experiment_note: str = ""     # 운영 사실 메모 (상태와 독립)


# ── 승격 규칙 테이블 ──

def _check_paper_only(m: StrategyMetrics) -> tuple[bool, str]:
    """paper_only 조건: 절대수익>0, PF≥1.0, WF positive≥50%."""
    fails = []
    if m.total_return <= 0:
        fails.append(f"return {m.total_return}% ≤ 0")
    if m.profit_factor < 1.0:
        fails.append(f"PF {m.profit_factor} < 1.0")
    if m.wf_positive_rate < 0.5:
        fails.append(f"WF positive {m.wf_positive_rate*100:.0f}% < 50%")
    if fails:
        return False, "paper_only 미달: " + ", ".join(fails)
    return True, "paper_only 충족"


def _check_provisional_candidate(m: StrategyMetrics) -> tuple[bool, str]:
    """provisional_paper_candidate 조건: paper_only + WF Sharpe>0≥50%, MDD>-20%."""
    ok, reason = _check_paper_only(m)
    if not ok:
        return False, reason

    fails = []
    if m.wf_sharpe_positive_rate < 0.5:
        fails.append(f"WF Sharpe>0 {m.wf_sharpe_positive_rate*100:.0f}% < 50%")
    if m.mdd < -20:
        fails.append(f"MDD {m.mdd}% < -20%")
    if m.wf_windows < 3:
        fails.append(f"WF windows {m.wf_windows} < 3")
    if m.wf_total_trades < 30:
        fails.append(f"WF trades {m.wf_total_trades} < 30")
    if fails:
        return False, "provisional 미달: " + ", ".join(fails)
    return True, "provisional_paper_candidate 충족"


def _check_live_candidate(m: StrategyMetrics) -> tuple[bool, str]:
    """live_candidate 조건: provisional + eligible paper evidence package."""
    ok, reason = _check_provisional_candidate(m)
    if not ok:
        return False, reason

    fails = []
    if m.paper_days is None or m.paper_days < 60:
        fails.append(f"paper {m.paper_days or 0}일 < 60일")
    if m.paper_sharpe is None or m.paper_sharpe < 0.3:
        fails.append(f"paper Sharpe {m.paper_sharpe or 0} < 0.3")
    if m.paper_excess is None or m.paper_excess < 0:
        fails.append(f"paper excess {m.paper_excess or 0} < 0")
    if m.paper_evidence_recommendation != "ELIGIBLE":
        fails.append(f"paper evidence recommendation {m.paper_evidence_recommendation or 'missing'} != ELIGIBLE")
    if m.paper_benchmark_final_ratio is None or m.paper_benchmark_final_ratio < 0.8:
        fails.append(f"paper benchmark_final_ratio {m.paper_benchmark_final_ratio or 0} < 0.8")
    if m.paper_sell_count is None or m.paper_sell_count < 5:
        fails.append(f"paper sell_count {m.paper_sell_count or 0} < 5")
    if m.paper_win_rate is None or m.paper_win_rate < 45:
        fails.append(f"paper win_rate {m.paper_win_rate or 0} < 45")
    if m.paper_frozen_days is None:
        fails.append("paper frozen_days missing")
    elif m.paper_frozen_days > 0:
        fails.append(f"paper frozen_days {m.paper_frozen_days} > 0")
    if m.paper_cumulative_return is None or m.paper_cumulative_return <= 0:
        fails.append(f"paper cumulative_return {m.paper_cumulative_return or 0} <= 0")
    if fails:
        return False, "live 미달: " + ", ".join(fails)
    return True, "live_candidate 충족"


def promote(m: StrategyMetrics, experiment_note: str = "") -> PromotionResult:
    """metrics 기반 자동 승격 판정. 수동 override 없음."""

    # live_candidate 체크
    ok, reason = _check_live_candidate(m)
    if ok:
        return PromotionResult(
            name=m.name, status="live_candidate",
            allowed_modes=["backtest", "paper", "live"],
            reason=reason, experiment_note=experiment_note,
        )

    # provisional_paper_candidate 체크
    ok, reason = _check_provisional_candidate(m)
    if ok:
        return PromotionResult(
            name=m.name, status="provisional_paper_candidate",
            allowed_modes=["backtest", "paper"],
            reason=reason, experiment_note=experiment_note,
        )

    # paper_only 체크
    ok, reason = _check_paper_only(m)
    if ok:
        return PromotionResult(
            name=m.name, status="paper_only",
            allowed_modes=["backtest", "paper"],
            reason=reason, experiment_note=experiment_note,
        )

    # research_only
    return PromotionResult(
        name=m.name, status="research_only",
        allowed_modes=["backtest"],
        reason=reason, experiment_note=experiment_note,
    )


# ── Artifact-driven 입력 ──

ARTIFACT_DIR = "reports/promotion"
REQUIRED_ARTIFACTS = [
    "metrics_summary.json",
    "walk_forward_summary.json",
    "benchmark_comparison.json",
    "promotion_result.json",
    "run_metadata.json",
]


def load_promotion_artifact(artifact_dir: str = ARTIFACT_DIR) -> Optional[dict]:
    """최신 canonical 평가 산출물에서 promotion 결과를 로드.
    artifact가 없거나 schema가 다르면 None 반환 (fail closed).
    """
    import json
    from pathlib import Path

    base = Path(artifact_dir)
    for fname in REQUIRED_ARTIFACTS:
        if not (base / fname).exists():
            logger.warning("Promotion artifact 없음: {}", base / fname)
            return None

    try:
        promotion = json.loads((base / "promotion_result.json").read_text(encoding="utf-8"))
        metadata = json.loads((base / "run_metadata.json").read_text(encoding="utf-8"))
        benchmark = json.loads((base / "benchmark_comparison.json").read_text(encoding="utf-8"))
        # schema 검증
        if not isinstance(promotion, dict):
            logger.error("promotion_result.json이 dict가 아님")
            return None
        if metadata.get("schema_version") != LIVE_GATE_SCHEMA_VERSION:
            logger.error("run_metadata.json schema_version 오류: {}", metadata.get("schema_version"))
            return None
        if metadata.get("artifact_type") != LIVE_GATE_ARTIFACT_TYPE:
            logger.error("run_metadata.json artifact_type 오류: {}", metadata.get("artifact_type"))
            return None
        if not isinstance(benchmark.get("strategy_excess_return_pct"), dict):
            logger.error("benchmark_comparison.json strategy_excess_return_pct 누락")
            return None
        if not isinstance(benchmark.get("strategy_excess_sharpe"), dict):
            logger.error("benchmark_comparison.json strategy_excess_sharpe 누락")
            return None
        for name, p in promotion.items():
            if "status" not in p or "allowed_modes" not in p:
                logger.error("promotion_result.json schema 오류: {} 키 누락", name)
                return None
        return promotion
    except Exception as e:
        logger.error("Artifact 로드 실패: {}", e)
        return None


def load_metrics_from_artifact(artifact_dir: str = ARTIFACT_DIR) -> dict[str, "StrategyMetrics"]:
    """metrics_summary.json + walk_forward_summary.json에서 StrategyMetrics 구성."""
    import json
    from pathlib import Path

    base = Path(artifact_dir)
    try:
        metrics_raw = json.loads((base / "metrics_summary.json").read_text(encoding="utf-8"))
        wf_raw = json.loads((base / "walk_forward_summary.json").read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Artifact 로드 실패: {}", e)
        return {}

    result = {}
    for name, m in metrics_raw.items():
        wf = wf_raw.get(name, {})
        result[name] = StrategyMetrics(
            name=name,
            total_return=m.get("total_return", 0),
            profit_factor=m.get("profit_factor", 0),
            mdd=m.get("mdd", 0),
            wf_positive_rate=m.get("wf_positive_rate", 0),
            wf_sharpe_positive_rate=m.get("wf_sharpe_positive_rate", 0),
            wf_windows=m.get("wf_windows", 0),
            wf_total_trades=m.get("wf_total_trades", wf.get("total_trades", 0)),
            sharpe=m.get("sharpe", 0),
        )
    return result


def get_all_promotions(source: str = "artifact") -> dict[str, PromotionResult]:
    """전략 승격 판정.
    source="artifact": reports/promotion/에서 로드 (production)
    source="inline": 테스트용 inline metrics 사용
    """
    if source == "artifact":
        metrics = load_metrics_from_artifact()
        if not metrics:
            logger.warning("Artifact에서 metrics 로드 실패. 빈 결과 반환.")
            return {}
    else:
        # 테스트 fixture용 — 테스트에서 직접 주입
        return {}

    results = {}
    for name, m in metrics.items():
        results[name] = promote(m)
    return results
