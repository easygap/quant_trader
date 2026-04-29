"""
Live trading readiness gate.

The live gate intentionally trusts only canonical promotion artifacts that match
the current code and resolved config, then cross-checks real paper evidence.
Legacy walk-forward JSON files and hand-edited approval files are not enough to
allow live trading.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


LIVE_GATE_SCHEMA_VERSION = 1
LIVE_GATE_ARTIFACT_TYPE = "canonical_promotion_bundle"
LIVE_GATE_MAX_ARTIFACT_AGE_DAYS = 7

REQUIRED_PROMOTION_ARTIFACTS = (
    "metrics_summary.json",
    "walk_forward_summary.json",
    "benchmark_comparison.json",
    "run_metadata.json",
    "promotion_result.json",
)


def get_current_git_hash() -> str:
    """Return the current short commit hash, or unknown if git is unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "top-level JSON is not an object"
    return data, None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_age_days(generated_at: datetime, now: datetime) -> float:
    if generated_at.tzinfo is not None and now.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=None)
    if generated_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - generated_at).total_seconds() / 86400


def _config_hash(config: Any, attr: str) -> str:
    value = getattr(config, attr, "")
    return value if isinstance(value, str) else ""


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_live_readiness(
    config: Any,
    strategy_name: str,
    *,
    promotion_dir: str | Path = "reports/promotion",
    evidence_dir: str | Path = "reports/paper_evidence",
    now: datetime | None = None,
    current_git_hash: str | None = None,
    max_artifact_age_days: int = LIVE_GATE_MAX_ARTIFACT_AGE_DAYS,
) -> list[str]:
    """Validate the canonical evidence chain required before live trading."""
    issues: list[str] = []
    promotion_base = Path(promotion_dir)
    evidence_base = Path(evidence_dir)
    now = now or datetime.now()
    current_git_hash = current_git_hash or get_current_git_hash()

    missing = [name for name in REQUIRED_PROMOTION_ARTIFACTS if not (promotion_base / name).exists()]
    if missing:
        issues.append(
            "승인 파일 없음: canonical promotion bundle 누락 "
            f"({promotion_base}; missing={missing}). "
            "python tools/evaluate_and_promote.py --canonical 실행 후 재검증하세요."
        )
        return issues

    artifacts: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_PROMOTION_ARTIFACTS:
        path = promotion_base / name
        data, err = _read_json(path)
        if data is None:
            issues.append(f"canonical artifact 파싱 오류: {path} ({err})")
        else:
            artifacts[name] = data
    if issues:
        return issues

    metadata = artifacts["run_metadata.json"]
    if metadata.get("schema_version") != LIVE_GATE_SCHEMA_VERSION:
        issues.append(
            f"promotion artifact schema_version 불일치: "
            f"{metadata.get('schema_version')} != {LIVE_GATE_SCHEMA_VERSION}"
        )
    if metadata.get("artifact_type") != LIVE_GATE_ARTIFACT_TYPE:
        issues.append(
            f"promotion artifact_type 불일치: "
            f"{metadata.get('artifact_type')!r} != {LIVE_GATE_ARTIFACT_TYPE!r}"
        )

    artifact_commit = metadata.get("commit_hash")
    if current_git_hash == "unknown":
        issues.append("현재 git commit hash 확인 실패. live 전환 불가.")
    elif artifact_commit != current_git_hash:
        issues.append(
            f"promotion artifact commit_hash 불일치: artifact={artifact_commit}, current={current_git_hash}. "
            "현재 코드로 canonical 평가를 다시 실행하세요."
        )

    yaml_hash = _config_hash(config, "yaml_hash")
    resolved_hash = _config_hash(config, "resolved_hash")
    if metadata.get("config_yaml_hash") != yaml_hash:
        issues.append(
            "promotion artifact config_yaml_hash 불일치. "
            "설정 파일 변경 후 canonical 평가를 다시 실행하세요."
        )
    if metadata.get("config_resolved_hash") != resolved_hash:
        issues.append(
            "promotion artifact config_resolved_hash 불일치. "
            "환경변수/실행 설정 변경 후 canonical 평가를 다시 실행하세요."
        )

    generated_at = _parse_iso_datetime(metadata.get("generated_at"))
    if generated_at is None:
        issues.append("promotion artifact generated_at 누락 또는 형식 오류.")
    else:
        age_days = _datetime_age_days(generated_at, now)
        if age_days < 0:
            issues.append("promotion artifact generated_at이 현재 시각보다 미래입니다.")
        elif age_days > max_artifact_age_days:
            issues.append(
                f"promotion artifact가 오래됨: {age_days:.1f}일 경과 "
                f"(최대 {max_artifact_age_days}일). canonical 평가를 다시 실행하세요."
            )

    promotions = artifacts["promotion_result.json"]
    strategy_promotion = promotions.get(strategy_name)
    if not isinstance(strategy_promotion, dict):
        issues.append(f"전략 '{strategy_name}'의 promotion_result가 없음.")
    else:
        status = strategy_promotion.get("status")
        allowed_modes = strategy_promotion.get("allowed_modes") or []
        if status != "live_candidate" or "live" not in allowed_modes:
            issues.append(
                f"전략 '{strategy_name}'은 live_candidate가 아님 "
                f"(status={status}, allowed_modes={allowed_modes})."
            )

    metrics = artifacts["metrics_summary.json"].get(strategy_name)
    if not isinstance(metrics, dict):
        issues.append(f"전략 '{strategy_name}'의 metrics_summary가 없음.")
    else:
        total_return = _as_float(metrics.get("total_return"))
        sharpe = _as_float(metrics.get("sharpe"))
        profit_factor = _as_float(metrics.get("profit_factor"))
        if total_return is None or total_return <= 0:
            issues.append(f"전략 '{strategy_name}'의 canonical total_return이 양수가 아님.")
        if sharpe is None or sharpe <= 0:
            issues.append(f"전략 '{strategy_name}'의 canonical Sharpe가 양수가 아님.")
        if profit_factor is None or profit_factor < 1.1:
            issues.append(f"전략 '{strategy_name}'의 canonical profit_factor가 1.1 미만.")

    wf = artifacts["walk_forward_summary.json"].get(strategy_name)
    if not isinstance(wf, dict):
        issues.append(f"전략 '{strategy_name}'의 walk_forward_summary가 없음.")
    else:
        windows = _as_int(wf.get("windows")) or 0
        positive = _as_int(wf.get("positive")) or 0
        sharpe_pos = _as_int(wf.get("sharpe_pos")) or 0
        total_trades = _as_int(wf.get("total_trades")) or 0
        if windows < 3:
            issues.append(f"WF windows {windows}개 < 3개.")
        if windows > 0 and positive / windows < 0.6:
            issues.append(f"WF positive ratio {positive}/{windows} < 60%.")
        if windows > 0 and sharpe_pos / windows < 0.6:
            issues.append(f"WF Sharpe>0 ratio {sharpe_pos}/{windows} < 60%.")
        if total_trades < 30:
            issues.append(f"WF trades {total_trades}개 < 30개.")

    benchmark = artifacts["benchmark_comparison.json"]
    excess_return_map = benchmark.get("strategy_excess_return_pct")
    excess_sharpe_map = benchmark.get("strategy_excess_sharpe")
    if not isinstance(excess_return_map, dict):
        issues.append("benchmark_comparison.strategy_excess_return_pct 누락.")
    else:
        excess_return = excess_return_map.get(strategy_name)
        if excess_return is None:
            issues.append(f"전략 '{strategy_name}'의 benchmark excess return 누락.")
        else:
            excess_return_value = _as_float(excess_return)
            if excess_return_value is None or excess_return_value <= 0:
                issues.append(
                    f"전략 '{strategy_name}' 벤치마크 대비 초과수익 {excess_return!r} <= 0."
                )
    if not isinstance(excess_sharpe_map, dict):
        issues.append("benchmark_comparison.strategy_excess_sharpe 누락.")
    else:
        excess_sharpe = excess_sharpe_map.get(strategy_name)
        if excess_sharpe is None:
            issues.append(f"전략 '{strategy_name}'의 benchmark excess Sharpe 누락.")
        else:
            excess_sharpe_value = _as_float(excess_sharpe)
            if excess_sharpe_value is None or excess_sharpe_value <= 0:
                issues.append(
                    f"전략 '{strategy_name}' 벤치마크 대비 excess Sharpe {excess_sharpe!r} <= 0."
                )

    evidence_path = evidence_base / f"promotion_evidence_{strategy_name}.json"
    evidence, evidence_err = _read_json(evidence_path)
    if evidence is None:
        issues.append(
            f"paper promotion evidence 없음 또는 파싱 오류: {evidence_path} ({evidence_err}). "
            "tools/run_paper_evidence_pipeline.py로 60영업일 증거 패키지를 생성하세요."
        )
    else:
        if evidence.get("strategy") not in (None, strategy_name):
            issues.append(
                f"paper evidence strategy 불일치: {evidence.get('strategy')} != {strategy_name}"
            )
        if evidence.get("recommendation") != "ELIGIBLE":
            issues.append(
                f"paper evidence recommendation={evidence.get('recommendation')} "
                f"block_reasons={evidence.get('block_reasons', [])}"
            )
        promotable_days = _as_int(evidence.get("promotable_evidence_days")) or 0
        benchmark_final_ratio = _as_float(evidence.get("benchmark_final_ratio")) or 0
        same_excess = _as_float(evidence.get("avg_same_universe_excess")) or 0
        cash_excess = _as_float(evidence.get("avg_cash_adjusted_excess")) or 0
        cumulative_return = _as_float(evidence.get("cumulative_return")) or 0
        sell_count = _as_int(evidence.get("sell_count")) or 0
        win_rate = _as_float(evidence.get("win_rate")) or 0
        frozen_days = _as_int(evidence.get("frozen_days")) or 0
        if promotable_days < 60:
            issues.append("paper evidence 60영업일 미달.")
        if benchmark_final_ratio < 0.8:
            issues.append("paper evidence benchmark_final_ratio 80% 미달.")
        if same_excess <= 0:
            issues.append("paper evidence same-universe excess가 양수가 아님.")
        if cash_excess <= 0:
            issues.append("paper evidence cash-adjusted excess가 양수가 아님.")
        if cumulative_return <= 0:
            issues.append("paper evidence cumulative_return이 양수가 아님.")
        if sell_count < 5:
            issues.append("paper evidence sell_count 5건 미달.")
        if win_rate < 45:
            issues.append("paper evidence win_rate 45% 미달.")
        if frozen_days > 0:
            issues.append("paper evidence에 frozen day가 존재함.")

    return issues
