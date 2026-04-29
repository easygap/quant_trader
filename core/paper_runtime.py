"""
Paper Runtime State Machine

paper 전략의 runtime state를 evidence + promotion + anomaly 기반으로 결정하고,
scheduler가 주문 허용/중지를 집행할 수 있도록 한다.

States:
  - research_disabled: 전략이 registry에 없거나 비활성
  - normal: paper 실행 허용
  - degraded: 신규 진입 금지, exit-only + evidence/finalize/reporting 허용
  - frozen: 신규 진입 금지, exit/cleanup + evidence/finalize/reporting 허용
  - blocked_insufficient_evidence: 신규 진입 금지, exit/cleanup + evidence/finalize/reporting 허용

Runtime Invariant:
  "증거 부족/품질 저하 때문에 리스크 축소(exit/cancel/reconcile)가 막히지 않는다."
  → 모든 state에서 exit, cancel, reconcile, finalize, evidence, reporting은 허용.
  → entry만 state에 따라 차단.

Legacy Evidence:
  schema_version이 없거나 < CURRENT_SCHEMA_VERSION인 record는
  runtime state 계산의 분모에서 제외(quarantine)한다.

canonical promotion bundle / live eligibility는 절대 자동 변경하지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

# ─── 상수 ───────────────────────────────────────────────────
RUNTIME_DIR = Path("reports/paper_runtime")
CLEAN_DAYS_FOR_UNFREEZE = 3  # 자동 unfreeze에 필요한 연속 clean final days
CURRENT_SCHEMA_VERSION = 2   # 현재 DailyEvidence schema 버전
PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS = 1

# Freeze triggers
FREEZE_PHANTOM_THRESHOLD = 0  # > 0 이면 freeze
FREEZE_REPEATED_REJECT = 3
FREEZE_STALE_PENDING = 0      # > 0 이면 degraded trigger
FREEZE_DUPLICATE_FLOOD = 5
FREEZE_BENCHMARK_FINAL_RATIO_MIN = 0.5  # 최근 10일 기준

# exit-safe actions: 모든 state에서 허용되는 운영 안전 작업
_EXIT_SAFE_ACTIONS = ["exit", "cancel", "reconcile", "finalize", "evidence", "reporting"]

# shadow_collect: 실제 주문 제출 없이 signal/benchmark/evidence만 수집
# entry_submit이 막힌 상태에서도 증거를 축적하여 bootstrap paradox를 해소
_SHADOW_ACTIONS = ["shadow_collect"]

# ─── 데이터 구조 ────────────────────────────────────────────

VALID_STATES = ("research_disabled", "normal", "degraded", "frozen", "blocked_insufficient_evidence")

ALLOWED_ACTIONS = {
    # research_disabled: exit/cleanup만. shadow도 불허 (paper 대상 아님)
    "research_disabled": list(_EXIT_SAFE_ACTIONS),
    "normal": ["run", "entry"] + list(_EXIT_SAFE_ACTIONS) + list(_SHADOW_ACTIONS),
    "degraded": list(_EXIT_SAFE_ACTIONS) + list(_SHADOW_ACTIONS),
    "frozen": list(_EXIT_SAFE_ACTIONS) + list(_SHADOW_ACTIONS),
    # blocked_insufficient: shadow_collect로 증거 축적 가능 (bootstrap paradox 해소)
    "blocked_insufficient_evidence": list(_EXIT_SAFE_ACTIONS) + list(_SHADOW_ACTIONS),
}


@dataclass
class RuntimeState:
    strategy: str
    state: str  # one of VALID_STATES
    reasons: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    evidence_date: Optional[str] = None
    last_final_benchmark_date: Optional[str] = None
    last_anomalies: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    evaluated_at: str = ""
    manual_freeze: bool = False
    manual_freeze_reason: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# Legacy Evidence Normalization
# ═══════════════════════════════════════════════════════════════

def classify_evidence_schema(record: dict) -> int:
    """evidence record의 schema version을 판별.
    - schema_version 필드가 있으면 그 값
    - record_version + benchmark_status 있으면 v2
    - portfolio_value (v1 전용 필드) 있으면 v1
    - 그 외 v0 (unknown)
    """
    if "schema_version" in record:
        return record["schema_version"]
    if "record_version" in record and "benchmark_status" in record:
        return 2
    if "portfolio_value" in record:
        return 1
    return 0


def normalize_v1_record(record: dict) -> dict:
    """V1 (evidence_collector 포맷) → V2 호환 구조로 변환.
    원본은 수정하지 않고 새 dict를 반환한다."""
    out = dict(record)
    # V1 필드 매핑
    if "portfolio_value" in out and "total_value" not in out:
        out["total_value"] = out["portfolio_value"]
    if "n_positions" in out and "position_count" not in out:
        out["position_count"] = out["n_positions"]
    if "drawdown" in out and "mdd" not in out:
        out["mdd"] = out["drawdown"]
    if "absolute_return" in out and "daily_return" not in out:
        out["daily_return"] = out["absolute_return"]

    # 누락 필드 기본값
    out.setdefault("benchmark_status", "unknown")
    out.setdefault("record_version", 1)
    out.setdefault("status", "normal")
    out.setdefault("anomalies", [])
    out.setdefault("cross_validation_warnings", [])
    out.setdefault("phantom_position_count", 0)
    out.setdefault("stale_pending_count", 0)
    out.setdefault("duplicate_blocked_count", 0)
    out.setdefault("total_trades", 0)

    out["schema_version"] = 1
    out["_legacy_normalized"] = True
    return out


def filter_runtime_eligible(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """canonical records를 runtime-eligible(v2+)과 quarantined(v1/v0)으로 분리.
    v1은 normalize 후 quarantine에 보존하되, runtime 계산에는 사용하지 않는다."""
    eligible = []
    quarantined = []
    for r in records:
        sv = classify_evidence_schema(r)
        if sv >= CURRENT_SCHEMA_VERSION:
            eligible.append(r)
        else:
            quarantined.append(normalize_v1_record(r) if sv == 1 else r)
    return eligible, quarantined


def _coerce_date(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.strptime(value, "%Y-%m-%d")


def _trading_days_between(start_date: str | datetime, end_date: str | datetime | None = None) -> int:
    """start 다음 날부터 end까지의 평일 수. 같은 날이면 0."""
    start = _coerce_date(start_date).date()
    end = _coerce_date(end_date).date()
    if end <= start:
        return 0
    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


# ═══════════════════════════════════════════════════════════════
# JSONL I/O for decisions and audit
# ═══════════════════════════════════════════════════════════════

def _append_decision(record: dict) -> None:
    path = RUNTIME_DIR / "runtime_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_decisions(strategy: str | None = None) -> list[dict]:
    path = RUNTIME_DIR / "runtime_decisions.jsonl"
    if not path.exists():
        return []
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if strategy is None or rec.get("strategy") == strategy:
                    results.append(rec)
            except json.JSONDecodeError:
                continue
    return results


def _get_manual_freeze_state(strategy: str) -> tuple[bool, str | None]:
    """최신 manual freeze/unfreeze 이벤트를 탐색."""
    decisions = _read_decisions(strategy)
    for d in reversed(decisions):
        if d.get("action") == "manual_freeze":
            return True, d.get("reason", "manual freeze")
        if d.get("action") == "manual_unfreeze":
            return False, None
    return False, None


# ═══════════════════════════════════════════════════════════════
# Core: Runtime State 결정
# ═══════════════════════════════════════════════════════════════

def get_paper_runtime_state(strategy: str, as_of_date: str | datetime | None = None) -> RuntimeState:
    """
    paper 전략의 현재 runtime state를 결정한다.

    입력:
      1. latest canonical daily evidence (legacy 제외)
      2. recent anomalies
      3. evidence quality metrics
    """
    from core.paper_evidence import (
        get_canonical_records,
        PROMOTION_MIN_EXCESS_DAYS,
    )

    as_of = _coerce_date(as_of_date)
    now_str = as_of.isoformat()
    reasons = []

    # ── 1. registry check ──
    if not _is_strategy_registered(strategy):
        state = RuntimeState(
            strategy=strategy,
            state="research_disabled",
            reasons=["strategy not registered or disabled"],
            allowed_actions=ALLOWED_ACTIONS["research_disabled"],
            evaluated_at=now_str,
        )
        _save_status(state)
        _append_decision({"strategy": strategy, "state": "research_disabled",
                          "action": "evaluate", "at": now_str, "reasons": state.reasons})
        return state

    # ── 2. canonical records + legacy 분리 ──
    all_records = get_canonical_records(strategy)
    eligible, quarantined = filter_runtime_eligible(all_records)

    if not eligible:
        q_info = ""
        if quarantined:
            q_info = " (%d legacy records quarantined)" % len(quarantined)
        state = RuntimeState(
            strategy=strategy,
            state="blocked_insufficient_evidence",
            reasons=["no eligible (v2+) evidence records" + q_info],
            allowed_actions=ALLOWED_ACTIONS["blocked_insufficient_evidence"],
            evaluated_at=now_str,
            metrics={
                "total_records": len(all_records),
                "eligible_records": 0,
                "quarantined_records": len(quarantined),
            },
        )
        _save_status(state)
        _append_decision({"strategy": strategy, "state": "blocked_insufficient_evidence",
                          "action": "evaluate", "at": now_str, "reasons": state.reasons})
        return state

    # ── 3. manual freeze check ──
    manual_frozen, manual_reason = _get_manual_freeze_state(strategy)

    # ── 4. latest evidence 분석 (eligible만) ──
    latest = eligible[-1]
    evidence_date = latest.get("date")
    stale_trading_days = (
        _trading_days_between(evidence_date, as_of) if evidence_date else 999
    )
    evidence_stale = stale_trading_days > PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS

    recent = eligible[-10:]
    recent_anomalies = []
    for r in recent:
        for a in r.get("anomalies", []):
            recent_anomalies.append({"date": r["date"], **a})

    last_final_date = None
    for r in reversed(eligible):
        if r.get("benchmark_status") == "final":
            last_final_date = r["date"]
            break

    # ── 5. freeze trigger 평가 ──
    freeze_triggers = []
    latest_anomalies = latest.get("anomalies", [])
    latest_types = [a.get("type") for a in latest_anomalies]

    if latest.get("phantom_position_count", 0) > FREEZE_PHANTOM_THRESHOLD:
        freeze_triggers.append("phantom_position=%d" % latest["phantom_position_count"])

    if "deep_drawdown" in latest_types:
        freeze_triggers.append("deep_drawdown (critical anomaly)")

    if manual_frozen:
        freeze_triggers.append("manual_freeze: %s" % (manual_reason or "operator"))

    # degraded triggers
    degraded_triggers = []
    if latest.get("reject_count", 0) > FREEZE_REPEATED_REJECT:
        degraded_triggers.append("repeated_reject=%d" % latest["reject_count"])

    if latest.get("stale_pending_count", 0) > FREEZE_STALE_PENDING:
        degraded_triggers.append("stale_pending=%d" % latest["stale_pending_count"])

    if latest.get("duplicate_blocked_count", 0) > FREEZE_DUPLICATE_FLOOD:
        degraded_triggers.append("duplicate_flood=%d" % latest["duplicate_blocked_count"])

    if any(a.get("type") == "cross_validation_mismatch" for a in latest_anomalies):
        degraded_triggers.append("cross_validation_mismatch")

    # ── 6. evidence sufficiency (eligible만 분모, provenance 분리) ──
    total = len(eligible)
    real_paper = [r for r in eligible if r.get("execution_backed", True)]
    shadow_only = [r for r in eligible if not r.get("execution_backed", True)]
    real_paper_count = len(real_paper)
    shadow_count = len(shadow_only)

    excess_non_null = sum(1 for r in eligible if r.get("same_universe_excess") is not None)
    excess_ratio = excess_non_null / total if total > 0 else 0

    recent_final = sum(1 for r in recent if r.get("benchmark_status") == "final")
    recent_final_ratio = recent_final / len(recent) if recent else 0

    insufficient = False
    if excess_ratio < PROMOTION_MIN_EXCESS_DAYS:
        insufficient = True
        detail = "excess_non_null=%d/%d (%.0f%% < %.0f%%)" % (
            excess_non_null, total, excess_ratio * 100, PROMOTION_MIN_EXCESS_DAYS * 100)
        if shadow_count > 0 and real_paper_count == 0:
            detail += " [shadow_only=%d, real_paper=0]" % shadow_count
        reasons.append("insufficient_evidence: " + detail)

    if recent_final_ratio < FREEZE_BENCHMARK_FINAL_RATIO_MIN:
        degraded_triggers.append("benchmark_final_ratio=%.0f%% < %.0f%%" %
                                 (recent_final_ratio * 100, FREEZE_BENCHMARK_FINAL_RATIO_MIN * 100))

    if evidence_stale:
        insufficient = True
        reasons.append(
            "stale_evidence: latest=%s, stale_trading_days=%d > %d"
            % (
                evidence_date or "N/A",
                stale_trading_days,
                PAPER_RUNTIME_MAX_EVIDENCE_STALE_TRADING_DAYS,
            )
        )

    # ── 7. auto-unfreeze check ──
    auto_unfrozen = False
    if freeze_triggers and not manual_frozen:
        clean_count = 0
        for r in reversed(eligible):
            if (r.get("status") == "normal" and
                r.get("benchmark_status") == "final" and
                not r.get("anomalies")):
                clean_count += 1
            else:
                break
        if clean_count >= CLEAN_DAYS_FOR_UNFREEZE:
            auto_unfrozen = True
            freeze_triggers = []
            _append_decision({
                "strategy": strategy, "action": "auto_unfreeze",
                "at": now_str,
                "reason": "consecutive_clean_final_days=%d >= %d" % (clean_count, CLEAN_DAYS_FOR_UNFREEZE),
            })

    # ── 8. state 결정 ──
    if freeze_triggers:
        state_name = "frozen"
        reasons.extend(["freeze: " + t for t in freeze_triggers])
    elif insufficient:
        state_name = "blocked_insufficient_evidence"
    elif degraded_triggers:
        state_name = "degraded"
        reasons.extend(["degraded: " + t for t in degraded_triggers])
    else:
        state_name = "normal"

    metrics = {
        "total_records": len(all_records),
        "eligible_records": total,
        "real_paper_days": real_paper_count,
        "shadow_days": shadow_count,
        "quarantined_records": len(quarantined),
        "excess_non_null_ratio": round(excess_ratio, 4),
        "recent_final_ratio": round(recent_final_ratio, 4),
        "evidence_stale_trading_days": stale_trading_days,
        "evidence_fresh": not evidence_stale,
        "recent_anomaly_count": len(recent_anomalies),
        "freeze_triggers": freeze_triggers,
        "degraded_triggers": degraded_triggers,
    }

    state = RuntimeState(
        strategy=strategy,
        state=state_name,
        reasons=reasons,
        allowed_actions=ALLOWED_ACTIONS[state_name],
        evidence_date=evidence_date,
        last_final_benchmark_date=last_final_date,
        last_anomalies=recent_anomalies[-5:],
        metrics=metrics,
        evaluated_at=now_str,
        manual_freeze=manual_frozen and not auto_unfrozen,
        manual_freeze_reason=manual_reason if manual_frozen and not auto_unfrozen else None,
    )

    _save_status(state)
    _append_decision({
        "strategy": strategy, "state": state_name,
        "action": "evaluate", "at": now_str,
        "reasons": reasons, "metrics": metrics,
    })
    return state


# ═══════════════════════════════════════════════════════════════
# Public API (scheduler가 사용)
# ═══════════════════════════════════════════════════════════════

def is_paper_frozen(strategy: str) -> tuple[bool, str]:
    """scheduler._execute_entry_candidates()에서 호출. (frozen, reason) 반환."""
    state = get_paper_runtime_state(strategy)
    if state.state == "frozen":
        return True, "; ".join(state.reasons) or "frozen"
    return False, ""


def is_paper_trade_allowed(strategy: str, action: str = "entry") -> bool:
    """주어진 action이 현재 state에서 허용되는지."""
    state = get_paper_runtime_state(strategy)
    return action in state.allowed_actions


def explain_paper_block_reason(strategy: str) -> str:
    """현재 block/freeze 이유를 구조화된 문자열로 반환."""
    state = get_paper_runtime_state(strategy)
    if state.state == "normal":
        return "not blocked"
    m = state.metrics
    parts = [
        f"state={state.state}",
        f"strategy={strategy}",
        f"evidence_date={state.evidence_date}",
        f"benchmark_final_ratio={m.get('recent_final_ratio', 'N/A')}",
        f"anomaly_count={m.get('recent_anomaly_count', 0)}",
        f"allowed_actions={','.join(state.allowed_actions)}",
    ]
    if state.reasons:
        parts.append("reasons=" + "; ".join(state.reasons))
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════
# Manual freeze / unfreeze
# ═══════════════════════════════════════════════════════════════

def manual_freeze(strategy: str, reason: str, operator: str = "cli") -> RuntimeState:
    """수동 freeze. audit trail 기록."""
    _append_decision({
        "strategy": strategy, "action": "manual_freeze",
        "at": datetime.now().isoformat(),
        "reason": reason, "operator": operator,
    })
    logger.warning("Manual freeze: {} — {}", strategy, reason)
    return get_paper_runtime_state(strategy)


def manual_unfreeze(strategy: str, reason: str, operator: str = "cli") -> RuntimeState:
    """수동 unfreeze. audit trail 기록."""
    _append_decision({
        "strategy": strategy, "action": "manual_unfreeze",
        "at": datetime.now().isoformat(),
        "reason": reason, "operator": operator,
    })
    logger.info("Manual unfreeze: {} — {}", strategy, reason)
    return get_paper_runtime_state(strategy)


# ═══════════════════════════════════════════════════════════════
# Status persistence + audit
# ═══════════════════════════════════════════════════════════════

def _save_status(state: RuntimeState) -> Path:
    """runtime_status_{strategy}.json 저장."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / f"runtime_status_{state.strategy}.json"
    path.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return path


def generate_runtime_audit(strategy: str) -> Path | None:
    """runtime_audit_{strategy}.md — freeze/unfreeze/decision history."""
    decisions = _read_decisions(strategy)
    if not decisions:
        return None

    lines = [
        f"# Runtime Audit: {strategy}",
        f"Generated: {datetime.now().isoformat()}",
        "",
        "## Decision History",
        "| Time | Action | State | Reasons |",
        "|------|--------|-------|---------|",
    ]
    for d in decisions[-50:]:
        at = d.get("at", "")[:19]
        action = d.get("action", "")
        st = d.get("state", "")
        reasons = "; ".join(d.get("reasons", [])) or d.get("reason", "")
        lines.append(f"| {at} | {action} | {st} | {reasons} |")

    lines.extend(["", "---", "canonical promotion bundle / live eligibility는 이 도구로 자동 수정되지 않습니다."])

    path = RUNTIME_DIR / f"runtime_audit_{strategy}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_runtime_status(strategy: str) -> dict | None:
    """저장된 runtime_status_{strategy}.json 로드."""
    path = RUNTIME_DIR / f"runtime_status_{strategy}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════
# Rebuild: 날짜별 runtime state 재계산
# ═══════════════════════════════════════════════════════════════

def rebuild_runtime_history(strategy: str, from_date: str | None = None,
                            to_date: str | None = None) -> list[dict]:
    """기존 evidence를 날짜별로 순회하며 runtime state를 재계산한다.
    legacy record 식별 + quarantine 포함."""
    from core.paper_evidence import get_canonical_records

    all_records = get_canonical_records(strategy)
    if not all_records:
        return []

    results = []
    for i, r in enumerate(all_records):
        date_str = r.get("date", "")
        if from_date and date_str < from_date:
            continue
        if to_date and date_str > to_date:
            continue

        sv = classify_evidence_schema(r)
        is_legacy = sv < CURRENT_SCHEMA_VERSION

        # 이 날짜까지의 eligible records로 incremental state 추정
        eligible_so_far = [
            rec for rec in all_records[:i+1]
            if classify_evidence_schema(rec) >= CURRENT_SCHEMA_VERSION
        ]
        n_eligible = len(eligible_so_far)
        n_final = sum(1 for rec in eligible_so_far if rec.get("benchmark_status") == "final")
        n_excess = sum(1 for rec in eligible_so_far if rec.get("same_universe_excess") is not None)
        final_ratio = n_final / n_eligible if n_eligible > 0 else 0
        excess_ratio = n_excess / n_eligible if n_eligible > 0 else 0

        # 단순화된 state 판정
        if is_legacy:
            est_state = "quarantined_legacy"
            reason = "schema_version=%d < %d" % (sv, CURRENT_SCHEMA_VERSION)
        elif r.get("status") == "frozen" or r.get("phantom_position_count", 0) > 0:
            est_state = "frozen"
            reason = "anomaly: " + r.get("status", "")
        elif any(a.get("severity") == "critical" for a in r.get("anomalies", [])):
            est_state = "frozen"
            reason = "critical anomaly"
        elif r.get("status") == "degraded":
            est_state = "degraded"
            reason = "degraded status in evidence"
        elif n_eligible == 0:
            est_state = "blocked_insufficient_evidence"
            reason = "no eligible records yet"
        else:
            est_state = "normal"
            reason = ""

        results.append({
            "date": date_str,
            "schema_version": sv,
            "is_legacy": is_legacy,
            "benchmark_status": r.get("benchmark_status", "unknown"),
            "same_universe_excess": r.get("same_universe_excess"),
            "eligible_so_far": n_eligible,
            "final_ratio_so_far": round(final_ratio, 4),
            "excess_ratio_so_far": round(excess_ratio, 4),
            "runtime_state": est_state,
            "reason": reason,
        })

    return results


def generate_rebuild_report(strategy: str, history: list[dict]) -> Path:
    """runtime_rebuild_{strategy}.md 생성."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Runtime Rebuild: {strategy}",
        f"Generated: {datetime.now().isoformat()}",
        "",
        "## Per-Day Runtime State",
        "| Date | Schema | Legacy | Bench Status | Excess | Eligible | Final% | State | Reason |",
        "|------|--------|--------|-------------|--------|----------|--------|-------|--------|",
    ]
    for h in history:
        ex = "%.4f" % h["same_universe_excess"] if h["same_universe_excess"] is not None else "null"
        lines.append(
            "| %s | v%d | %s | %s | %s | %d | %.0f%% | %s | %s |" % (
                h["date"], h["schema_version"],
                "Y" if h["is_legacy"] else "N",
                h["benchmark_status"], ex,
                h["eligible_so_far"],
                h["final_ratio_so_far"] * 100,
                h["runtime_state"], h["reason"],
            )
        )

    lines.extend(["", "---", "Legacy records (schema < v%d)는 runtime 분모에서 제외됨." % CURRENT_SCHEMA_VERSION])

    path = RUNTIME_DIR / f"runtime_rebuild_{strategy}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════
# 내부 helpers
# ═══════════════════════════════════════════════════════════════

def _is_strategy_registered(strategy: str) -> bool:
    """legacy approved_strategies.json 또는 config에서 전략 등록 여부 확인.
    없으면 True (paper 모드에서는 기본 허용 — research_disabled는 명시적 비활성만)."""
    try:
        approved_path = Path("reports/approved_strategies.json")
        if approved_path.exists():
            data = json.loads(approved_path.read_text(encoding="utf-8"))
            strategies = data.get("strategies", [])
            for s in strategies:
                if s.get("name") == strategy:
                    return s.get("status") != "disabled"
        return True
    except Exception:
        return True
