"""
전략 승격 규칙 엔진 — metrics 기반 자동 판정

상태 정의:
  research_only:              backtest만 허용. 기본 상태.
  paper_only:                 backtest + paper 관찰. 절대수익>0, PF≥1.0, WF positive≥50%.
  provisional_paper_candidate: paper 60일 우선 실험 대상. paper_only + Sharpe/PF/WF 안정성 충족.
                               "제한된 운영 우선순위"를 의미. live alpha는 별도 검증 필요.
  live_candidate:             live 전환 가능. provisional + eligible paper evidence package.
                               "경제적으로 유의미한 후보"를 의미.

핵심 원칙:
  - 상태는 metrics에서 자동 결정됨. 수동 override 불가.
  - "existing experiment"는 상태를 승격시키지 않음.
  - experiment_note 필드로 운영 사실을 분리 기록.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from loguru import logger

from core.live_gate import (
    LIVE_GATE_ARTIFACT_TYPE,
    LIVE_GATE_SCHEMA_VERSION,
    validate_canonical_metadata_integrity,
)


# Canonical metrics are rounded to 2 decimals, so 0.45 keeps near-0.5 candidates
# available for capped paper study while still demoting clearly weak strategies.
MIN_PROVISIONAL_SHARPE = 0.45
MIN_PROVISIONAL_PROFIT_FACTOR = 1.2
MIN_PROVISIONAL_WF_POSITIVE_RATE = 0.6
MIN_PROVISIONAL_WF_SHARPE_RATE = 0.6
MAX_PROVISIONAL_TURNOVER_PCT = 1000.0
PAPER_EVIDENCE_MAX_STALENESS_DAYS = 14
TARGET_WEIGHT_BASE_STRATEGIES = frozenset({"target_weight_rotation"})


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
    benchmark_excess_return: Optional[float] = None  # %p, same-universe EW B&H 대비
    benchmark_excess_sharpe: Optional[float] = None
    canonical_benchmark_required: bool = False
    canonical_data_integrity_ok: Optional[bool] = None
    canonical_data_integrity_issues: Optional[list[str]] = None
    ev_per_trade: Optional[float] = None              # 원/trade
    cost_adjusted_cagr: Optional[float] = None        # %
    turnover_per_year: Optional[float] = None         # %/year
    # paper 실적 (live_candidate 판정용, 없으면 None)
    paper_days: Optional[int] = None
    paper_sharpe: Optional[float] = None
    paper_excess: Optional[float] = None  # same-universe excess return
    paper_cash_adjusted_excess: Optional[float] = None
    paper_evidence_recommendation: Optional[str] = None
    paper_evidence_block_reasons: Optional[list[str]] = None
    paper_benchmark_final_ratio: Optional[float] = None
    paper_sell_count: Optional[int] = None
    paper_win_rate: Optional[float] = None
    paper_frozen_days: Optional[int] = None
    paper_cumulative_return: Optional[float] = None
    paper_latest_evidence_date: Optional[str] = None
    paper_evidence_age_days: Optional[int] = None
    paper_evidence_fresh: Optional[bool] = None
    paper_trade_quality_status: Optional[str] = None
    paper_trade_quality_adverse_gap_bps: Optional[float] = None
    paper_trade_quality_missing_expected_ratio: Optional[float] = None
    paper_trade_quality_missing_expected_count: Optional[int] = None
    paper_trade_quality_missing_execution_link_ratio: Optional[float] = None
    paper_trade_quality_missing_execution_link_count: Optional[int] = None
    target_weight_strategy_required: Optional[bool] = None
    target_weight_evidence_required: Optional[bool] = None
    target_weight_verified_pilot_days: Optional[int] = None
    target_weight_invalid_days: Optional[int] = None
    target_weight_all_promotable_days_verified: Optional[bool] = None
    target_weight_params_hash_consistent: Optional[bool] = None
    target_weight_params_hash: Optional[str] = None
    target_weight_canonical_params_hash: Optional[str] = None
    target_weight_params_hash_matches_canonical: Optional[bool] = None


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
    """provisional_paper_candidate 조건: paper_only + risk-adjusted 품질."""
    ok, reason = _check_paper_only(m)
    if not ok:
        return False, reason

    fails = []
    if m.sharpe < MIN_PROVISIONAL_SHARPE:
        fails.append(f"Sharpe {m.sharpe} < {MIN_PROVISIONAL_SHARPE}")
    if m.profit_factor < MIN_PROVISIONAL_PROFIT_FACTOR:
        fails.append(f"PF {m.profit_factor} < {MIN_PROVISIONAL_PROFIT_FACTOR}")
    if m.wf_positive_rate < MIN_PROVISIONAL_WF_POSITIVE_RATE:
        fails.append(
            f"WF positive {m.wf_positive_rate*100:.0f}% < {MIN_PROVISIONAL_WF_POSITIVE_RATE*100:.0f}%"
        )
    if m.wf_sharpe_positive_rate < MIN_PROVISIONAL_WF_SHARPE_RATE:
        fails.append(
            f"WF Sharpe>0 {m.wf_sharpe_positive_rate*100:.0f}% < {MIN_PROVISIONAL_WF_SHARPE_RATE*100:.0f}%"
        )
    if m.mdd < -20:
        fails.append(f"MDD {m.mdd}% < -20%")
    if m.wf_windows < 3:
        fails.append(f"WF windows {m.wf_windows} < 3")
    if m.wf_total_trades < 30:
        fails.append(f"WF trades {m.wf_total_trades} < 30")
    if m.ev_per_trade is not None and m.ev_per_trade <= 0:
        fails.append(f"EV/trade {m.ev_per_trade} <= 0")
    if m.cost_adjusted_cagr is not None and m.cost_adjusted_cagr <= 0:
        fails.append(f"cost_adjusted_cagr {m.cost_adjusted_cagr}% <= 0")
    if m.turnover_per_year is not None and m.turnover_per_year >= MAX_PROVISIONAL_TURNOVER_PCT:
        fails.append(f"turnover {m.turnover_per_year}%/y >= {MAX_PROVISIONAL_TURNOVER_PCT}%/y")
    if m.canonical_benchmark_required:
        if m.benchmark_excess_return is None:
            fails.append("benchmark excess return missing")
        elif m.benchmark_excess_return <= 0:
            fails.append(f"benchmark excess return {m.benchmark_excess_return} <= 0")
        if m.benchmark_excess_sharpe is None:
            fails.append("benchmark excess Sharpe missing")
        elif m.benchmark_excess_sharpe <= 0:
            fails.append(f"benchmark excess Sharpe {m.benchmark_excess_sharpe} <= 0")
    if m.canonical_data_integrity_ok is False:
        detail = "; ".join((m.canonical_data_integrity_issues or [])[:3])
        fails.append(f"canonical data integrity failed: {detail or 'unknown'}")
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
    if m.paper_excess is None or m.paper_excess <= 0:
        fails.append(f"paper same-universe excess {m.paper_excess or 0} <= 0")
    if m.paper_cash_adjusted_excess is None or m.paper_cash_adjusted_excess <= 0:
        fails.append(f"paper cash-adjusted excess {m.paper_cash_adjusted_excess or 0} <= 0")
    if m.paper_evidence_recommendation != "ELIGIBLE":
        detail = ""
        if m.paper_evidence_block_reasons:
            detail = ": " + "; ".join(m.paper_evidence_block_reasons[:3])
        fails.append(
            f"paper evidence recommendation {m.paper_evidence_recommendation or 'missing'} != ELIGIBLE"
            f"{detail}"
        )
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
    if m.paper_trade_quality_status is None:
        fails.append("paper trade quality status missing")
    elif m.paper_trade_quality_status != "ok":
        details = []
        if m.paper_trade_quality_adverse_gap_bps is not None:
            details.append(f"adverse_gap_bps={m.paper_trade_quality_adverse_gap_bps}")
        if m.paper_trade_quality_missing_expected_count is not None:
            details.append(f"missing_expected={m.paper_trade_quality_missing_expected_count}")
        if m.paper_trade_quality_missing_execution_link_count is not None:
            details.append(
                "missing_execution_link="
                f"{m.paper_trade_quality_missing_execution_link_count}"
            )
        suffix = " (" + ", ".join(details) + ")" if details else ""
        fails.append(f"paper trade quality status {m.paper_trade_quality_status} != ok{suffix}")
    if m.paper_evidence_fresh is not True:
        latest = m.paper_latest_evidence_date or "missing"
        if m.paper_evidence_age_days is None:
            fails.append(f"paper evidence freshness missing latest={latest}")
        elif m.paper_evidence_age_days < 0:
            fails.append(f"paper evidence latest date future latest={latest}")
        else:
            fails.append(
                "paper evidence stale "
                f"latest={latest} age={m.paper_evidence_age_days}d "
                f"> {PAPER_EVIDENCE_MAX_STALENESS_DAYS}d"
            )
    if m.target_weight_strategy_required is True or m.name.startswith("target_weight_"):
        if m.target_weight_evidence_required is not True:
            fails.append("target-weight evidence required flag missing")
        if m.target_weight_verified_pilot_days is None or m.target_weight_verified_pilot_days < 60:
            fails.append(
                f"target-weight verified pilot days {m.target_weight_verified_pilot_days or 0} < 60"
            )
        if m.target_weight_invalid_days is None:
            fails.append("target-weight invalid days missing")
        elif m.target_weight_invalid_days > 0:
            fails.append(f"target-weight invalid days {m.target_weight_invalid_days} > 0")
        if m.target_weight_all_promotable_days_verified is not True:
            fails.append("target-weight promotable evidence not fully verified")
        if m.target_weight_params_hash_consistent is not True:
            fails.append("target-weight params_hash not consistent")
        if not m.target_weight_params_hash:
            fails.append("target-weight params_hash missing")
        if not m.target_weight_canonical_params_hash:
            fails.append("target-weight canonical params_hash missing")
        if m.target_weight_params_hash_matches_canonical is not True:
            fails.append("target-weight evidence params_hash does not match canonical params_hash")
    if fails:
        return False, "live 미달: " + ", ".join(fails)
    return True, "live_candidate 충족"


def promote(m: StrategyMetrics, experiment_note: str = "") -> PromotionResult:
    """metrics 기반 자동 승격 판정. 수동 override 없음."""

    # live_candidate 체크
    live_ok, live_reason = _check_live_candidate(m)
    if live_ok:
        return PromotionResult(
            name=m.name, status="live_candidate",
            allowed_modes=["backtest", "paper", "live"],
            reason=live_reason, experiment_note=experiment_note,
        )

    # provisional_paper_candidate 체크
    provisional_ok, provisional_reason = _check_provisional_candidate(m)
    if provisional_ok:
        return PromotionResult(
            name=m.name, status="provisional_paper_candidate",
            allowed_modes=["backtest", "paper"],
            reason=f"{provisional_reason}; live 차단: {live_reason}", experiment_note=experiment_note,
        )

    # paper_only 체크
    ok, reason = _check_paper_only(m)
    if ok:
        detailed_reason = f"{reason}; provisional 차단: {provisional_reason}"
        return PromotionResult(
            name=m.name, status="paper_only",
            allowed_modes=["backtest", "paper"],
            reason=detailed_reason, experiment_note=experiment_note,
        )

    # research_only
    return PromotionResult(
        name=m.name, status="research_only",
        allowed_modes=["backtest"],
        reason=reason, experiment_note=experiment_note,
    )


# ── Artifact-driven 입력 ──

ARTIFACT_DIR = "reports/promotion"
PAPER_EVIDENCE_DIR = "reports/paper_evidence"
REQUIRED_ARTIFACTS = [
    "metrics_summary.json",
    "walk_forward_summary.json",
    "benchmark_comparison.json",
    "promotion_result.json",
    "run_metadata.json",
]


def _as_int(value) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date_like(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _latest_evidence_date_from_package(package: dict) -> Optional[date]:
    latest = _parse_date_like(package.get("latest_evidence_date"))
    if latest is not None:
        return latest
    period = package.get("period")
    if isinstance(period, str) and "~" in period:
        return _parse_date_like(period.split("~")[-1].strip())
    return None


def load_paper_evidence_package(
    strategy_name: str,
    evidence_dir: str = PAPER_EVIDENCE_DIR,
) -> Optional[dict]:
    """paper promotion evidence package를 로드한다. 없으면 None으로 둔다."""
    import json
    from pathlib import Path

    path = Path(evidence_dir) / f"promotion_evidence_{strategy_name}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("paper evidence package 로드 실패: {} ({})", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("paper evidence package schema 오류: {}", path)
        return None
    package_strategy = payload.get("strategy")
    if package_strategy != strategy_name:
        if package_strategy is None:
            logger.warning(
                "paper evidence package strategy 누락: {} (expected={})",
                path,
                strategy_name,
            )
            return None
        logger.warning(
            "paper evidence package strategy 불일치: {} != {}",
            package_strategy,
            strategy_name,
        )
        return None
    return payload


def paper_evidence_metrics_from_package(
    package: Optional[dict],
    reference_date: object | None = None,
) -> dict[str, object]:
    """promotion package를 StrategyMetrics의 paper_* 필드로 변환한다."""
    if not isinstance(package, dict):
        return {}
    target_weight_evidence = package.get("target_weight_evidence") or {}
    trade_quality = package.get("trade_quality") or {}
    block_reasons = package.get("block_reasons")
    if not isinstance(block_reasons, list):
        block_reasons = []
    latest_evidence_date = _latest_evidence_date_from_package(package)
    reference = _parse_date_like(reference_date) or datetime.now().date()
    evidence_age_days = (
        (reference - latest_evidence_date).days
        if latest_evidence_date is not None
        else None
    )
    evidence_fresh = (
        evidence_age_days is not None
        and 0 <= evidence_age_days <= PAPER_EVIDENCE_MAX_STALENESS_DAYS
    )
    return {
        "paper_days": _as_int(
            package.get("promotable_evidence_days", package.get("real_paper_days"))
        ),
        "paper_sharpe": _as_float(package.get("paper_sharpe", package.get("sharpe"))),
        "paper_excess": _as_float(package.get("avg_same_universe_excess")),
        "paper_cash_adjusted_excess": _as_float(
            package.get("avg_cash_adjusted_excess", package.get("cash_adjusted_excess"))
        ),
        "paper_evidence_recommendation": package.get("recommendation"),
        "paper_evidence_block_reasons": [str(reason) for reason in block_reasons],
        "paper_benchmark_final_ratio": _as_float(package.get("benchmark_final_ratio")),
        "paper_sell_count": _as_int(package.get("sell_count")),
        "paper_win_rate": _as_float(package.get("win_rate")),
        "paper_frozen_days": _as_int(package.get("frozen_days")),
        "paper_cumulative_return": _as_float(package.get("cumulative_return")),
        "paper_latest_evidence_date": (
            latest_evidence_date.isoformat()
            if latest_evidence_date is not None
            else None
        ),
        "paper_evidence_age_days": evidence_age_days,
        "paper_evidence_fresh": evidence_fresh,
        "paper_trade_quality_status": trade_quality.get("status"),
        "paper_trade_quality_adverse_gap_bps": _as_float(
            trade_quality.get("adverse_gap_bps_of_notional")
        ),
        "paper_trade_quality_missing_expected_ratio": _as_float(
            trade_quality.get("missing_expected_price_ratio")
        ),
        "paper_trade_quality_missing_expected_count": _as_int(
            trade_quality.get("missing_expected_price_count")
        ),
        "paper_trade_quality_missing_execution_link_ratio": _as_float(
            trade_quality.get("missing_execution_link_ratio")
        ),
        "paper_trade_quality_missing_execution_link_count": _as_int(
            trade_quality.get("missing_execution_link_count")
        ),
        "target_weight_evidence_required": target_weight_evidence.get("required"),
        "target_weight_verified_pilot_days": _as_int(package.get("target_weight_verified_pilot_days")),
        "target_weight_invalid_days": _as_int(package.get("target_weight_invalid_days")),
        "target_weight_all_promotable_days_verified": target_weight_evidence.get("all_promotable_days_verified"),
        "target_weight_params_hash_consistent": target_weight_evidence.get("params_hash_consistent"),
        "target_weight_params_hash": package.get("target_weight_params_hash")
            or target_weight_evidence.get("params_hash"),
    }


def attach_paper_evidence_metrics(
    metrics: StrategyMetrics,
    paper_metrics: dict[str, object],
) -> StrategyMetrics:
    """StrategyMetrics에 paper evidence 필드를 반영한다."""
    for field in (
        "paper_days",
        "paper_sharpe",
        "paper_excess",
        "paper_cash_adjusted_excess",
        "paper_evidence_recommendation",
        "paper_evidence_block_reasons",
        "paper_benchmark_final_ratio",
        "paper_sell_count",
        "paper_win_rate",
        "paper_frozen_days",
        "paper_cumulative_return",
        "paper_latest_evidence_date",
        "paper_evidence_age_days",
        "paper_evidence_fresh",
        "paper_trade_quality_status",
        "paper_trade_quality_adverse_gap_bps",
        "paper_trade_quality_missing_expected_ratio",
        "paper_trade_quality_missing_expected_count",
        "paper_trade_quality_missing_execution_link_ratio",
        "paper_trade_quality_missing_execution_link_count",
        "target_weight_strategy_required",
        "target_weight_evidence_required",
        "target_weight_verified_pilot_days",
        "target_weight_invalid_days",
        "target_weight_all_promotable_days_verified",
        "target_weight_params_hash_consistent",
        "target_weight_params_hash",
        "target_weight_canonical_params_hash",
        "target_weight_params_hash_matches_canonical",
    ):
        value = paper_metrics.get(field)
        if value is not None:
            setattr(metrics, field, value)
    return metrics


def _is_target_weight_strategy_spec(spec: dict) -> bool:
    """canonical strategy spec이 target-weight 후보인지 판정한다."""
    base_strategy = spec.get("base_strategy") or spec.get("strategy")
    if isinstance(base_strategy, str) and base_strategy in TARGET_WEIGHT_BASE_STRATEGIES:
        return True
    candidate_id = spec.get("candidate_id")
    return isinstance(candidate_id, str) and candidate_id.startswith("target_weight_")


def target_weight_params_hashes_from_strategy_specs(specs: object) -> dict[str, str]:
    """canonical strategy_specs에서 target-weight candidate_id별 params_hash를 추출한다."""
    if not isinstance(specs, list):
        return {}
    result: dict[str, str] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        candidate_id = spec.get("candidate_id")
        params_hash = spec.get("params_hash")
        if (
            _is_target_weight_strategy_spec(spec)
            and isinstance(candidate_id, str)
            and isinstance(params_hash, str)
            and params_hash
        ):
            result[candidate_id] = params_hash
    return result


def canonical_params_hashes_from_metadata(metadata: dict) -> dict[str, str]:
    """canonical bundle metadata에서 target-weight 후보 params_hash를 추출한다."""
    specs = metadata.get("strategy_specs") if isinstance(metadata, dict) else None
    return target_weight_params_hashes_from_strategy_specs(specs)


def attach_target_weight_canonical_hash_check(
    strategy_name: str,
    paper_metrics: dict[str, object],
    target_weight_params_hashes: dict[str, str],
) -> dict[str, object]:
    """target-weight paper evidence hash와 canonical 후보 hash를 연결한다."""
    is_target_weight = (
        str(strategy_name).startswith("target_weight_")
        or strategy_name in target_weight_params_hashes
    )
    if not is_target_weight:
        return paper_metrics
    metrics = dict(paper_metrics)
    metrics["target_weight_strategy_required"] = True
    canonical_hash = target_weight_params_hashes.get(strategy_name)
    evidence_hash = metrics.get("target_weight_params_hash")
    if canonical_hash:
        metrics["target_weight_canonical_params_hash"] = canonical_hash
    metrics["target_weight_params_hash_matches_canonical"] = (
        bool(canonical_hash)
        and isinstance(evidence_hash, str)
        and evidence_hash == canonical_hash
    )
    return metrics


def load_promotion_artifact(
    artifact_dir: str = ARTIFACT_DIR,
    evidence_dir: Optional[str] = None,
) -> Optional[dict]:
    """최신 canonical 평가 산출물에서 promotion 결과를 로드.
    artifact가 없거나 schema가 다르면 None 반환 (fail closed).
    """
    import json
    from pathlib import Path

    base = Path(artifact_dir)
    resolved_evidence_dir = (
        str(base.parent / "paper_evidence")
        if evidence_dir is None
        else evidence_dir
    )
    for fname in REQUIRED_ARTIFACTS:
        if not (base / fname).exists():
            logger.warning("Promotion artifact 없음: {}", base / fname)
            return None

    try:
        promotion = json.loads((base / "promotion_result.json").read_text(encoding="utf-8"))
        metrics = json.loads((base / "metrics_summary.json").read_text(encoding="utf-8"))
        walk_forward = json.loads((base / "walk_forward_summary.json").read_text(encoding="utf-8"))
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
        metadata_issues = validate_canonical_metadata_integrity(metadata)
        if metadata_issues:
            logger.error("run_metadata.json canonical integrity 오류: {}", "; ".join(metadata_issues))
            return None
        source_issues = validate_metrics_source_artifact_sync(
            metrics,
            walk_forward,
            benchmark,
        )
        if source_issues:
            logger.error("promotion source artifact 동기화 오류: {}", "; ".join(source_issues))
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
        metrics_by_strategy = load_metrics_from_artifact(
            str(base),
            evidence_dir=resolved_evidence_dir,
        )
        promotion_issues = validate_promotion_result_artifact_sync(
            promotion,
            metrics_by_strategy,
        )
        if promotion_issues:
            logger.error("promotion_result 재계산 동기화 오류: {}", "; ".join(promotion_issues))
            return None
        return promotion
    except Exception as e:
        logger.error("Artifact 로드 실패: {}", e)
        return None


def _number_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_number(left, right, *, tolerance: float = 1e-9) -> bool:
    left_number = _number_or_none(left)
    right_number = _number_or_none(right)
    if left_number is None or right_number is None:
        return left == right
    return abs(left_number - right_number) <= tolerance


def validate_metrics_source_artifact_sync(
    metrics_raw: dict,
    wf_raw: dict,
    benchmark_raw: dict,
) -> list[str]:
    """metrics_summary의 파생 값이 WF/benchmark 원천 artifact와 일치하는지 검사."""
    issues: list[str] = []
    if not isinstance(metrics_raw, dict):
        return ["metrics_summary.json top-level JSON is not an object"]
    if not isinstance(wf_raw, dict):
        return ["walk_forward_summary.json top-level JSON is not an object"]
    if not isinstance(benchmark_raw, dict):
        return ["benchmark_comparison.json top-level JSON is not an object"]

    excess_return = benchmark_raw.get("strategy_excess_return_pct")
    excess_sharpe = benchmark_raw.get("strategy_excess_sharpe")
    if not isinstance(excess_return, dict):
        issues.append("benchmark_comparison.json strategy_excess_return_pct 누락")
        excess_return = {}
    if not isinstance(excess_sharpe, dict):
        issues.append("benchmark_comparison.json strategy_excess_sharpe 누락")
        excess_sharpe = {}

    metric_names = set(metrics_raw)
    wf_names = set(wf_raw)
    return_names = set(excess_return)
    sharpe_names = set(excess_sharpe)
    if metric_names != wf_names:
        issues.append(
            "walk_forward_summary 전략 목록 불일치: "
            f"missing={sorted(metric_names - wf_names)[:8]}, "
            f"extra={sorted(wf_names - metric_names)[:8]}"
        )
    if metric_names != return_names:
        issues.append(
            "benchmark_comparison.strategy_excess_return_pct 전략 목록 불일치: "
            f"missing={sorted(metric_names - return_names)[:8]}, "
            f"extra={sorted(return_names - metric_names)[:8]}"
        )
    if metric_names != sharpe_names:
        issues.append(
            "benchmark_comparison.strategy_excess_sharpe 전략 목록 불일치: "
            f"missing={sorted(metric_names - sharpe_names)[:8]}, "
            f"extra={sorted(sharpe_names - metric_names)[:8]}"
        )

    for name in sorted(metric_names):
        metrics = metrics_raw.get(name)
        if not isinstance(metrics, dict):
            issues.append(f"metrics_summary.{name} 형식 오류")
            continue

        wf = wf_raw.get(name)
        if isinstance(wf, dict):
            for metric_key, source_key in (
                ("wf_windows", "windows"),
                ("wf_total_trades", "total_trades"),
            ):
                if not _same_number(metrics.get(metric_key), wf.get(source_key)):
                    issues.append(
                        f"{name}.{metric_key} 불일치: "
                        f"metrics_summary={metrics.get(metric_key)!r}, "
                        f"walk_forward_summary.{source_key}={wf.get(source_key)!r}"
                    )

            windows = _number_or_none(wf.get("windows"))
            positive = _number_or_none(wf.get("positive"))
            sharpe_pos = _number_or_none(wf.get("sharpe_pos"))
            if windows is None:
                issues.append(f"walk_forward_summary.{name}.windows 누락 또는 숫자 아님")
            if positive is None:
                issues.append(f"walk_forward_summary.{name}.positive 누락 또는 숫자 아님")
            if sharpe_pos is None:
                issues.append(f"walk_forward_summary.{name}.sharpe_pos 누락 또는 숫자 아님")
            if windows is not None and positive is not None:
                expected = round(positive / max(windows, 1.0), 3)
                if not _same_number(metrics.get("wf_positive_rate"), expected):
                    issues.append(
                        f"{name}.wf_positive_rate 불일치: "
                        f"metrics_summary={metrics.get('wf_positive_rate')!r}, "
                        f"walk_forward_summary={expected!r}"
                    )
            if windows is not None and sharpe_pos is not None:
                expected = round(sharpe_pos / max(windows, 1.0), 3)
                if not _same_number(metrics.get("wf_sharpe_positive_rate"), expected):
                    issues.append(
                        f"{name}.wf_sharpe_positive_rate 불일치: "
                        f"metrics_summary={metrics.get('wf_sharpe_positive_rate')!r}, "
                        f"walk_forward_summary={expected!r}"
                    )
        elif name in wf_raw:
            issues.append(f"walk_forward_summary.{name} 형식 오류")

        if name in excess_return and not _same_number(
            metrics.get("benchmark_excess_return"),
            excess_return.get(name),
        ):
            issues.append(
                f"{name}.benchmark_excess_return 불일치: "
                f"metrics_summary={metrics.get('benchmark_excess_return')!r}, "
                f"benchmark_comparison={excess_return.get(name)!r}"
            )
        if name in excess_sharpe and not _same_number(
            metrics.get("benchmark_excess_sharpe"),
            excess_sharpe.get(name),
        ):
            issues.append(
                f"{name}.benchmark_excess_sharpe 불일치: "
                f"metrics_summary={metrics.get('benchmark_excess_sharpe')!r}, "
                f"benchmark_comparison={excess_sharpe.get(name)!r}"
            )
    return issues


def validate_promotion_result_artifact_sync(
    promotion_raw: dict,
    metrics_by_strategy: dict[str, "StrategyMetrics"],
) -> list[str]:
    """저장된 promotion_result가 현재 metrics/evidence 재계산 결과와 일치하는지 검사."""
    if not isinstance(promotion_raw, dict):
        return ["promotion_result.json top-level JSON is not an object"]
    if not metrics_by_strategy:
        return ["promotion_result 재계산 metrics 없음"]

    issues: list[str] = []
    current_names = set(promotion_raw)
    expected_names = set(metrics_by_strategy)
    missing = sorted(expected_names - current_names)
    extra = sorted(current_names - expected_names)
    if missing:
        issues.append(f"promotion_result 누락 전략: {missing[:8]}")
    if extra:
        issues.append(f"promotion_result 불필요 전략: {extra[:8]}")

    for name in sorted(current_names & expected_names):
        current = promotion_raw.get(name)
        if not isinstance(current, dict):
            issues.append(f"promotion_result.{name} 형식 오류")
            continue
        expected = promote(metrics_by_strategy[name])
        if current.get("status") != expected.status:
            issues.append(
                f"promotion_result {name}.status 재계산 결과 불일치: "
                f"stored={current.get('status')!r}, expected={expected.status!r}"
            )
        if current.get("allowed_modes") != expected.allowed_modes:
            issues.append(
                f"promotion_result {name}.allowed_modes 재계산 결과 불일치: "
                f"stored={current.get('allowed_modes')!r}, expected={expected.allowed_modes!r}"
            )
        if current.get("reason") != expected.reason:
            issues.append(
                f"promotion_result {name}.reason 재계산 결과 불일치"
            )
    return issues


def load_metrics_from_artifact(
    artifact_dir: str = ARTIFACT_DIR,
    evidence_dir: str = PAPER_EVIDENCE_DIR,
) -> dict[str, "StrategyMetrics"]:
    """metrics_summary.json + walk_forward_summary.json에서 StrategyMetrics 구성."""
    import json
    from pathlib import Path

    base = Path(artifact_dir)
    try:
        metrics_raw = json.loads((base / "metrics_summary.json").read_text(encoding="utf-8"))
        wf_raw = json.loads((base / "walk_forward_summary.json").read_text(encoding="utf-8"))
        benchmark_raw = json.loads((base / "benchmark_comparison.json").read_text(encoding="utf-8"))
        metadata = json.loads((base / "run_metadata.json").read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Artifact 로드 실패: {}", e)
        return {}
    if metadata.get("schema_version") != LIVE_GATE_SCHEMA_VERSION:
        logger.error("run_metadata.json schema_version 오류: {}", metadata.get("schema_version"))
        return {}
    if metadata.get("artifact_type") != LIVE_GATE_ARTIFACT_TYPE:
        logger.error("run_metadata.json artifact_type 오류: {}", metadata.get("artifact_type"))
        return {}
    metadata_issues = validate_canonical_metadata_integrity(metadata)
    if metadata_issues:
        logger.error("run_metadata.json canonical integrity 오류: {}", "; ".join(metadata_issues))
        return {}
    source_issues = validate_metrics_source_artifact_sync(
        metrics_raw,
        wf_raw,
        benchmark_raw,
    )
    if source_issues:
        logger.error("promotion source artifact 동기화 오류: {}", "; ".join(source_issues))
        return {}

    result = {}
    canonical_params_hashes = canonical_params_hashes_from_metadata(metadata)
    paper_reference_date = metadata.get("generated_at")
    excess_return = benchmark_raw.get("strategy_excess_return_pct", {})
    excess_sharpe = benchmark_raw.get("strategy_excess_sharpe", {})
    for name, m in metrics_raw.items():
        wf = wf_raw.get(name, {})
        paper_metrics = paper_evidence_metrics_from_package(
            load_paper_evidence_package(name, evidence_dir),
            reference_date=paper_reference_date,
        )
        paper_metrics = attach_target_weight_canonical_hash_check(
            name,
            paper_metrics,
            canonical_params_hashes,
        )
        result[name] = attach_paper_evidence_metrics(StrategyMetrics(
            name=name,
            total_return=m.get("total_return", 0),
            profit_factor=m.get("profit_factor", 0),
            mdd=m.get("mdd", 0),
            wf_positive_rate=m.get("wf_positive_rate", 0),
            wf_sharpe_positive_rate=m.get("wf_sharpe_positive_rate", 0),
            wf_windows=m.get("wf_windows", 0),
            wf_total_trades=m.get("wf_total_trades", wf.get("total_trades", 0)),
            sharpe=m.get("sharpe", 0),
            benchmark_excess_return=m.get("benchmark_excess_return", excess_return.get(name)),
            benchmark_excess_sharpe=m.get("benchmark_excess_sharpe", excess_sharpe.get(name)),
            canonical_benchmark_required=True,
            ev_per_trade=m.get("ev_per_trade"),
            cost_adjusted_cagr=m.get("cost_adjusted_cagr"),
            turnover_per_year=m.get("turnover_per_year"),
        ), paper_metrics)
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
