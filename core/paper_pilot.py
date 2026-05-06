"""
Paper Pilot Authorization

blocked_insufficient_evidence 상태에서도 명시적 수동 승인 + 엄격한 리스크 캡 아래에서
제한된 real paper pilot을 돌릴 수 있게 한다.

pilot에서 생성된 evidence는 execution_backed=True, evidence_mode="pilot_paper"로 기록되어
promotable evidence로 카운트된다.

pilot authorization은 promotion/live 승격과 별개인 운영 override다.
canonical promotion bundle / live eligibility는 절대 자동 변경하지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

RUNTIME_DIR = Path("reports/paper_runtime")
PILOT_AUTH_FILE = RUNTIME_DIR / "pilot_authorizations.jsonl"

# pilot 사전 조건: shadow clean days 최소 기준 (no real evidence 전략용)
PILOT_MIN_SHADOW_CLEAN_DAYS = 3
# pilot entry guard: evidence freshness (영업일 기준 최대 stale 허용 일수)
PILOT_MAX_EVIDENCE_STALE_DAYS = 5
# pilot entry guard: 최근 5일 benchmark final 비율 최소 기준
PILOT_MIN_BENCHMARK_FINAL_RATIO = 0.4
PILOT_ELIGIBLE_STATUSES = ("provisional_paper_candidate", "approved", "live_candidate")


def _coerce_date(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.strptime(value, "%Y-%m-%d")


@dataclass
class PilotAuthorization:
    strategy: str
    enabled: bool
    valid_from: str             # YYYY-MM-DD
    valid_to: str               # YYYY-MM-DD
    max_orders_per_day: int     # 1일 최대 주문 수
    max_concurrent_positions: int  # 동시 최대 포지션 수
    max_notional_per_trade: int    # 1건 최대 금액 (원)
    max_gross_exposure: int        # 총 최대 투자 금액 (원)
    operator_reason: str = ""
    created_at: str = ""
    created_by: str = "cli"
    override_scope: str = "entry_only"  # entry_only | full_pilot


@dataclass
class PilotCheckResult:
    allowed: bool
    reason: str
    auth: Optional[dict] = None
    remaining_orders: Optional[int] = None
    remaining_exposure: Optional[int] = None
    caps_snapshot: Optional[dict] = None


# ═══════════════════════════════════════════════════════════════
# Authorization CRUD
# ═══════════════════════════════════════════════════════════════

def _append_auth(record: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with open(PILOT_AUTH_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_auths(strategy: str | None = None) -> list[dict]:
    if not PILOT_AUTH_FILE.exists():
        return []
    results = []
    with open(PILOT_AUTH_FILE, "r", encoding="utf-8") as f:
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


def get_active_pilot(strategy: str, date: str | None = None) -> PilotAuthorization | None:
    """현재 유효한 pilot authorization 반환. 없으면 None."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    auths = _read_auths(strategy)
    # 최신 것부터 역순 탐색
    for a in reversed(auths):
        if not a.get("enabled", False):
            continue
        if a.get("valid_from", "") <= date <= a.get("valid_to", ""):
            return PilotAuthorization(**{k: v for k, v in a.items()
                                         if k in PilotAuthorization.__dataclass_fields__})
    return None


def enable_pilot(strategy: str, valid_from: str, valid_to: str,
                 max_orders: int = 2, max_positions: int = 2,
                 max_notional: int = 1_000_000, max_exposure: int = 3_000_000,
                 reason: str = "", operator: str = "cli") -> PilotAuthorization:
    """pilot authorization 생성."""
    # eligibility check
    _check_pilot_eligibility(strategy)

    auth = PilotAuthorization(
        strategy=strategy, enabled=True,
        valid_from=valid_from, valid_to=valid_to,
        max_orders_per_day=max_orders,
        max_concurrent_positions=max_positions,
        max_notional_per_trade=max_notional,
        max_gross_exposure=max_exposure,
        operator_reason=reason,
        created_at=datetime.now().isoformat(),
        created_by=operator,
    )
    _append_auth(asdict(auth))
    logger.info("Pilot enabled: {} ({} ~ {})", strategy, valid_from, valid_to)
    return auth


def disable_pilot(strategy: str, reason: str = "", operator: str = "cli") -> None:
    """pilot authorization 비활성화."""
    _append_auth({
        "strategy": strategy, "enabled": False,
        "valid_from": "", "valid_to": "",
        "max_orders_per_day": 0, "max_concurrent_positions": 0,
        "max_notional_per_trade": 0, "max_gross_exposure": 0,
        "operator_reason": reason,
        "created_at": datetime.now().isoformat(),
        "created_by": operator,
        "override_scope": "entry_only",
    })
    logger.info("Pilot disabled: {} — {}", strategy, reason)


def _artifact_promotion_record(strategy: str) -> dict | None:
    """Return canonical artifact promotion record for adapter-only candidates."""
    try:
        from core.promotion_engine import load_promotion_artifact

        promotions = load_promotion_artifact()
        if not promotions:
            return None
        record = promotions.get(strategy)
        return record if isinstance(record, dict) else None
    except Exception:
        return None


def _check_pilot_eligibility(strategy: str) -> None:
    """pilot 사전 조건 확인. 미충족 시 ValueError.

    Normal strategies are checked through STRATEGY_STATUS. Portfolio-level
    adapter candidates may be eligible through canonical promotion artifacts
    without being registered in the per-symbol scheduler registry.
    """
    from core.strategy_universe import is_paper_eligible
    from strategies import get_strategy_status

    if is_paper_eligible(strategy):
        status = get_strategy_status(strategy)
        if status.get("status") not in PILOT_ELIGIBLE_STATUSES:
            raise ValueError(
                f"{strategy} status={status.get('status')} — "
                "pilot requires provisional_paper_candidate or approved"
            )
        return

    artifact_record = _artifact_promotion_record(strategy)
    if artifact_record and "paper" in artifact_record.get("allowed_modes", []):
        status = artifact_record.get("status")
        if status in PILOT_ELIGIBLE_STATUSES:
            return
        raise ValueError(
            f"{strategy} artifact status={status} — "
            "pilot requires provisional_paper_candidate or approved"
        )

    raise ValueError(
        f"{strategy} is not paper-eligible "
        "(disabled/backtest-only and not eligible in canonical promotion artifact)"
    )


# ═══════════════════════════════════════════════════════════════
# Pilot Entry Check (scheduler에서 호출)
# ═══════════════════════════════════════════════════════════════

def check_pilot_entry(
    strategy: str,
    candidate_notional: float = 0,
    as_of_date: str | datetime | None = None,
) -> PilotCheckResult:
    """pilot authorization + risk caps 체크. scheduler entry gating에서 사용."""
    today_dt = _coerce_date(as_of_date)
    today = today_dt.strftime("%Y-%m-%d")
    auth = get_active_pilot(strategy, today)

    if auth is None:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason="no active pilot authorization",
        )

    # Every branch returns through _pilot_check_result so blocked outcomes are audited too.

    caps = {
        "max_orders_per_day": auth.max_orders_per_day,
        "max_concurrent_positions": auth.max_concurrent_positions,
        "max_notional_per_trade": auth.max_notional_per_trade,
        "max_gross_exposure": auth.max_gross_exposure,
    }

    # ── 1. Critical anomaly check ──
    try:
        from core.paper_runtime import get_paper_runtime_state
        rt = get_paper_runtime_state(strategy, as_of_date=today)
        if rt.state == "frozen":
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason="frozen state — pilot entry blocked",
                auth=auth,
                caps=caps,
            )
        if rt.metrics.get("recent_anomaly_count", 0) > 0:
            latest_anomalies = rt.last_anomalies
            if any(a.get("severity") == "critical" for a in latest_anomalies):
                return _pilot_check_result(
                    strategy,
                    allowed=False,
                    reason="critical anomaly active — pilot entry blocked",
                    auth=auth,
                    caps=caps,
                )
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"runtime guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )

    # ── 1b. Evidence freshness + benchmark finalization guard ──
    try:
        from core.paper_evidence import get_canonical_records
        from core.paper_runtime import filter_runtime_eligible
        all_recs = get_canonical_records(strategy)
        eligible, _ = filter_runtime_eligible(all_recs)
        if not eligible:
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason="no eligible evidence — collect shadow bootstrap first",
                auth=auth,
                caps=caps,
            )

        latest_date = eligible[-1].get("date", "")
        days_stale = (datetime.strptime(today, "%Y-%m-%d") -
                      datetime.strptime(latest_date, "%Y-%m-%d")).days if latest_date else 999
        if days_stale > PILOT_MAX_EVIDENCE_STALE_DAYS:
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason=f"evidence stale ({days_stale}d > {PILOT_MAX_EVIDENCE_STALE_DAYS}d) — collect evidence first",
                auth=auth,
                caps=caps,
            )
        # benchmark finalization: 최근 기록 중 final 비율
        recent = eligible[-5:]
        final_count = sum(1 for r in recent if r.get("benchmark_status") == "final")
        final_ratio = final_count / len(recent) if recent else 0
        if final_ratio < PILOT_MIN_BENCHMARK_FINAL_RATIO:
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason=f"benchmark final ratio {final_ratio:.0%} < {PILOT_MIN_BENCHMARK_FINAL_RATIO:.0%} — finalize benchmarks first",
                auth=auth,
                caps=caps,
            )
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"evidence guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )

    # ── 2. Notifier health ──
    try:
        notifier_path = RUNTIME_DIR / "notifier_health.json"
        if not notifier_path.exists():
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason="notifier health missing — pilot requires discord webhook",
                auth=auth,
                caps=caps,
            )
        nh = json.loads(notifier_path.read_text(encoding="utf-8"))
        if not nh.get("discord_configured", False):
            return _pilot_check_result(
                strategy,
                allowed=False,
                reason="notifier unhealthy — pilot requires discord webhook",
                auth=auth,
                caps=caps,
            )
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"notifier guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )

    # ── 3. Orders per day ──
    try:
        orders_today = _count_orders_today(strategy, today_dt)
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"order-count guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )
    remaining_orders = auth.max_orders_per_day - orders_today
    if remaining_orders <= 0:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"max_orders_per_day={auth.max_orders_per_day} reached (today={orders_today})",
            auth=auth,
            caps=caps,
            remaining_orders=0,
        )

    # ── 4. Concurrent positions ──
    try:
        current_positions = _count_positions(strategy)
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"position-count guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )
    if current_positions >= auth.max_concurrent_positions:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"max_concurrent_positions={auth.max_concurrent_positions} reached (current={current_positions})",
            auth=auth,
            caps=caps,
        )

    # ── 5. Notional per trade ──
    if candidate_notional > 0 and candidate_notional > auth.max_notional_per_trade:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"max_notional_per_trade={auth.max_notional_per_trade:,} exceeded ({candidate_notional:,.0f})",
            auth=auth,
            caps=caps,
        )

    # ── 6. Gross exposure ──
    try:
        current_exposure = _get_gross_exposure(strategy)
    except Exception as exc:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"gross-exposure guard failed — pilot entry blocked: {exc}",
            auth=auth,
            caps=caps,
        )
    remaining_exposure = auth.max_gross_exposure - current_exposure
    if remaining_exposure <= 0:
        return _pilot_check_result(
            strategy,
            allowed=False,
            reason=f"max_gross_exposure={auth.max_gross_exposure:,} reached (current={current_exposure:,.0f})",
            auth=auth,
            caps=caps,
            remaining_exposure=0,
        )

    return _pilot_check_result(
        strategy,
        allowed=True,
        reason="pilot authorized",
        auth=auth,
        caps=caps,
        remaining_orders=remaining_orders,
        remaining_exposure=int(remaining_exposure),
    )


def _pilot_check_result(
    strategy: str,
    *,
    allowed: bool,
    reason: str,
    auth: PilotAuthorization | dict | None = None,
    caps: dict | None = None,
    remaining_orders: int | None = None,
    remaining_exposure: int | None = None,
) -> PilotCheckResult:
    """Build and audit a pilot check result."""
    decision = "allowed" if allowed else "blocked"
    _audit_pilot_check(strategy, decision, reason, caps)
    if isinstance(auth, PilotAuthorization):
        auth_payload = asdict(auth)
    else:
        auth_payload = auth
    return PilotCheckResult(
        allowed=allowed,
        reason=reason,
        auth=auth_payload,
        remaining_orders=remaining_orders,
        remaining_exposure=remaining_exposure,
        caps_snapshot=caps,
    )


# ═══════════════════════════════════════════════════════════════
# Audit Trail
# ═══════════════════════════════════════════════════════════════

PILOT_AUDIT_FILE = RUNTIME_DIR / "pilot_audit.jsonl"


def _audit_pilot_check(strategy: str, decision: str, reason: str,
                        caps: dict | None = None) -> None:
    """pilot check 결과를 audit trail에 기록."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "strategy": strategy,
        "decision": decision,
        "reason": reason,
        "caps": caps,
        "at": datetime.now().isoformat(),
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with open(PILOT_AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _count_orders_today(strategy: str, as_of_date: str | datetime | None = None) -> int:
    from database.models import get_session, TradeHistory

    session = get_session()
    try:
        today_start = _coerce_date(as_of_date).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_end = today_start.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        count = session.query(TradeHistory).filter(
            TradeHistory.account_key == strategy,
            TradeHistory.mode == "paper",
            TradeHistory.executed_at >= today_start,
            TradeHistory.executed_at <= today_end,
        ).count()
        return count
    finally:
        session.close()


def _count_positions(strategy: str) -> int:
    from database.repositories import get_all_positions

    positions = get_all_positions(account_key=strategy)
    return len(positions) if positions else 0


def _get_gross_exposure(strategy: str) -> float:
    from database.repositories import get_all_positions

    positions = get_all_positions(account_key=strategy)
    if not positions:
        return 0
    return sum((p.avg_price or 0) * (p.quantity or 0) for p in positions)


def pilot_session_artifact_path(strategy: str, date: str) -> Path:
    return RUNTIME_DIR / f"pilot_session_{strategy}_{date}.json"


def load_pilot_session_artifact(strategy: str, date: str) -> dict | None:
    path = pilot_session_artifact_path(strategy, date)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_pilot_session_artifact(strategy: str, date: str,
                                 pilot_session: dict) -> Path:
    """pilot 세션 종료 후 세션 artifact 저장. operator 확인용."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # runtime state snapshot
    rt_state = None
    try:
        from core.paper_runtime import get_paper_runtime_state
        rt = get_paper_runtime_state(strategy)
        rt_state = {
            "state": rt.state,
            "allowed_actions": rt.allowed_actions,
            "reasons": rt.reasons,
            "metrics": rt.metrics,
        }
    except Exception:
        pass

    # pilot check snapshot
    pilot_check_snap = None
    try:
        result = check_pilot_entry(strategy)
        pilot_check_snap = {
            "allowed": result.allowed,
            "reason": result.reason,
            "remaining_orders": result.remaining_orders,
            "remaining_exposure": result.remaining_exposure,
            "caps_snapshot": result.caps_snapshot,
        }
    except Exception:
        pass

    # evidence count
    evidence_snap = None
    try:
        from core.paper_evidence import get_canonical_records
        records = get_canonical_records(strategy)
        evidence_snap = {
            "total_records": len(records),
            "real_paper_days": sum(1 for r in records if r.get("execution_backed", True)),
            "pilot_real_paper_days": sum(
                1 for r in records
                if r.get("execution_backed", True)
                and (r.get("evidence_mode") == "pilot_paper"
                     or r.get("session_mode") == "pilot_paper")
            ),
            "shadow_days": sum(1 for r in records if not r.get("execution_backed", True)),
        }
    except Exception:
        pass

    artifact = {
        "strategy": strategy,
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "pilot_session": pilot_session,
        "runtime_state": rt_state,
        "pilot_check": pilot_check_snap,
        "evidence_snapshot": evidence_snap,
    }

    # JSON
    json_path = pilot_session_artifact_path(strategy, date)
    json_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Markdown
    md_lines = [
        f"# Pilot Session: {strategy} ({date})",
        f"Generated: {artifact['generated_at'][:19]}",
        "",
        "## Session Context",
        f"- active: {pilot_session.get('active')}",
        f"- session_mode: {pilot_session.get('session_mode')}",
        f"- evidence_mode: {pilot_session.get('evidence_mode')}",
        f"- pilot_authorized: {pilot_session.get('pilot_authorized')}",
    ]
    caps = pilot_session.get("pilot_caps_snapshot", {})
    if caps:
        md_lines.append("- caps: " + json.dumps(caps, ensure_ascii=False))

    if rt_state:
        md_lines.extend([
            "",
            "## Runtime State",
            f"- state: {rt_state['state']}",
            f"- allowed_actions: {', '.join(rt_state['allowed_actions'])}",
            f"- reasons: {'; '.join(rt_state['reasons']) or 'none'}",
        ])

    if pilot_check_snap:
        md_lines.extend([
            "",
            "## Pilot Check",
            f"- entry allowed: {'YES' if pilot_check_snap['allowed'] else 'NO'}",
            f"- reason: {pilot_check_snap['reason']}",
            f"- remaining orders: {pilot_check_snap['remaining_orders']}",
            f"- remaining exposure: {pilot_check_snap['remaining_exposure']}",
        ])

    if evidence_snap:
        md_lines.extend([
            "",
            "## Evidence Snapshot",
            f"- total records: {evidence_snap['total_records']}",
            f"- real_paper_days (total): {evidence_snap['real_paper_days']}",
            f"- pilot_real_paper_days: {evidence_snap['pilot_real_paper_days']}",
            f"- shadow_days: {evidence_snap['shadow_days']}",
        ])

    md_lines.extend(["", "---",
                      "canonical promotion bundle / live eligibility는 이 도구로 자동 수정되지 않습니다."])

    md_path = RUNTIME_DIR / f"pilot_session_{strategy}_{date}.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    logger.info("Pilot session artifact 저장: {}", json_path)
    return json_path


def check_pilot_prerequisites(strategy: str) -> tuple[bool, str]:
    """pilot 사전 조건 확인 (no-real-evidence 전략용).
    shadow clean days >= N, anomaly 없음, benchmark availability 양호."""
    try:
        from core.paper_evidence import get_canonical_records
        from core.paper_runtime import filter_runtime_eligible

        all_records = get_canonical_records(strategy)
        eligible, _ = filter_runtime_eligible(all_records)

        if not eligible:
            return False, "no eligible evidence — shadow bootstrap 필요"

        # shadow clean days
        clean_count = 0
        for r in reversed(eligible):
            if (r.get("status") == "normal" and
                r.get("benchmark_status") == "final" and
                not r.get("anomalies")):
                clean_count += 1
            else:
                break

        if clean_count < PILOT_MIN_SHADOW_CLEAN_DAYS:
            return False, (
                f"clean final days={clean_count} < {PILOT_MIN_SHADOW_CLEAN_DAYS} required — "
                "shadow bootstrap으로 clean evidence 축적 필요"
            )

        return True, "prerequisites met"
    except Exception as e:
        return False, f"check failed: {e}"


# ═══════════════════════════════════════════════════════════════
# Launch Readiness
# ═══════════════════════════════════════════════════════════════

def _count_trailing_clean_final(eligible: list[dict]) -> int:
    """eligible records 끝에서 연속 clean final day 수."""
    count = 0
    for r in reversed(eligible):
        if (r.get("status") == "normal"
                and r.get("benchmark_status") == "final"
                and not r.get("anomalies")):
            count += 1
        else:
            break
    return count


def compute_launch_readiness(strategy: str, as_of_date: str | datetime | None = None) -> dict:
    """pilot launch 전 모든 전제조건을 한 번에 계산.

    Clean final day 정의:
      - status == "normal"
      - benchmark_status == "final"
      - anomalies == [] (빈 리스트)
    리셋 조건: 위 3개 중 하나라도 불만족인 날이 끼면 trailing count가 0으로 리셋.

    Returns dict with launch_ready, blocking_requirements[], 각 조건 상세.
    """
    from core.paper_evidence import get_canonical_records
    from core.paper_runtime import filter_runtime_eligible, get_paper_runtime_state

    blockers: list[str] = []
    today_dt = _coerce_date(as_of_date)
    today = today_dt.strftime("%Y-%m-%d")

    # ── 1. evidence & clean days ──
    all_records = get_canonical_records(strategy)
    eligible, quarantined = filter_runtime_eligible(all_records)
    clean_days = _count_trailing_clean_final(eligible) if eligible else 0
    required_clean = PILOT_MIN_SHADOW_CLEAN_DAYS

    if clean_days < required_clean:
        blockers.append(
            f"clean_final_days: {clean_days}/{required_clean} "
            f"(need {required_clean - clean_days} more)")

    # ── 2. evidence freshness ──
    evidence_fresh = False
    evidence_date = None
    days_stale = None
    if eligible:
        evidence_date = eligible[-1].get("date", "")
        try:
            days_stale = (datetime.strptime(today, "%Y-%m-%d") -
                          datetime.strptime(evidence_date, "%Y-%m-%d")).days
            evidence_fresh = days_stale <= PILOT_MAX_EVIDENCE_STALE_DAYS
        except ValueError:
            pass
    if not evidence_fresh:
        detail = f"stale {days_stale}d" if days_stale is not None else "no evidence"
        blockers.append(f"evidence_freshness: {detail}")

    # ── 3. benchmark final ratio (최근 5일) ──
    benchmark_ready = False
    benchmark_final_ratio = None
    if eligible:
        recent = eligible[-5:]
        final_count = sum(1 for r in recent if r.get("benchmark_status") == "final")
        benchmark_final_ratio = final_count / len(recent) if recent else 0
        benchmark_ready = benchmark_final_ratio >= PILOT_MIN_BENCHMARK_FINAL_RATIO
    if not benchmark_ready:
        ratio_str = f"{benchmark_final_ratio:.0%}" if benchmark_final_ratio is not None else "N/A"
        blockers.append(
            f"benchmark_final_ratio: {ratio_str} < {PILOT_MIN_BENCHMARK_FINAL_RATIO:.0%}")

    # ── 4. notifier health ──
    notifier_ready = False
    try:
        nh_path = RUNTIME_DIR / "notifier_health.json"
        if nh_path.exists():
            nh = json.loads(nh_path.read_text(encoding="utf-8"))
            notifier_ready = nh.get("discord_configured", False)
    except Exception:
        pass
    if not notifier_ready:
        blockers.append("notifier: Discord webhook 미설정")

    # ── 5. pilot authorization ──
    auth = get_active_pilot(strategy, today)
    pilot_present = auth is not None

    # ── 6. strategy eligibility ──
    strategy_eligible = True
    try:
        _check_pilot_eligibility(strategy)
    except ValueError as e:
        strategy_eligible = False
        blockers.append(f"eligibility: {e}")

    # ── 7. runtime state ──
    rt = get_paper_runtime_state(strategy, as_of_date=today)
    real_paper_days = sum(1 for r in eligible if r.get("execution_backed", True))
    shadow_days = sum(1 for r in eligible if not r.get("execution_backed", True))

    # launch_ready = 모든 전제조건 충족 (pilot auth 제외 — 마지막 수동 단계)
    infra_ready = (clean_days >= required_clean
                   and evidence_fresh
                   and benchmark_ready
                   and notifier_ready
                   and strategy_eligible)
    launch_ready = infra_ready and pilot_present

    return {
        "strategy": strategy,
        "evaluated_at": datetime.now().isoformat(),
        # clean day tracking
        "clean_final_days_current": clean_days,
        "clean_final_days_required": required_clean,
        "remaining_clean_days": max(0, required_clean - clean_days),
        "clean_day_definition": (
            "status==normal AND benchmark_status==final AND anomalies==[]"
        ),
        "clean_day_reset_condition": (
            "위 3개 중 하나라도 불만족인 날이 끼면 trailing count 리셋"
        ),
        # individual checks
        "evidence_fresh": evidence_fresh,
        "evidence_date": evidence_date,
        "evidence_stale_days": days_stale,
        "benchmark_ready": benchmark_ready,
        "benchmark_final_ratio": benchmark_final_ratio,
        "notifier_ready": notifier_ready,
        "pilot_authorization_present": pilot_present,
        "strategy_eligible": strategy_eligible,
        # runtime
        "runtime_state": rt.state,
        "real_paper_days": real_paper_days,
        "shadow_days": shadow_days,
        "eligible_records": len(eligible),
        "quarantined_records": len(quarantined),
        # verdict
        "infra_ready": infra_ready,
        "launch_ready": launch_ready,
        "blocking_requirements": blockers,
    }


def generate_launch_readiness_artifact(strategy: str) -> tuple[Path, Path]:
    """launch readiness JSON + MD 생성."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lr = compute_launch_readiness(strategy)

    # JSON
    json_path = RUNTIME_DIR / f"{strategy}_pilot_launch_readiness.json"
    json_path.write_text(
        json.dumps(lr, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")

    # Markdown
    icon = "✅" if lr["launch_ready"] else "❌"
    infra_icon = "✅" if lr["infra_ready"] else "⏳"
    md = [
        f"# Pilot Launch Readiness: {strategy}",
        f"Evaluated: {lr['evaluated_at'][:19]}",
        "",
        f"## {icon} Launch Ready: {'YES' if lr['launch_ready'] else 'NO'}",
        f"## {infra_icon} Infrastructure Ready: {'YES' if lr['infra_ready'] else 'NO'}",
        "",
        "## Checklist",
        _lr_check("Clean final days", lr["clean_final_days_current"],
                   f">= {lr['clean_final_days_required']}",
                   lr["clean_final_days_current"] >= lr["clean_final_days_required"]),
        _lr_check("Evidence fresh", lr["evidence_date"] or "N/A",
                   f"<= {PILOT_MAX_EVIDENCE_STALE_DAYS}d stale",
                   lr["evidence_fresh"]),
        _lr_check("Benchmark final ratio",
                   f"{lr['benchmark_final_ratio']:.0%}" if lr['benchmark_final_ratio'] is not None else "N/A",
                   f">= {PILOT_MIN_BENCHMARK_FINAL_RATIO:.0%}",
                   lr["benchmark_ready"]),
        _lr_check("Discord notifier", "configured" if lr["notifier_ready"] else "missing",
                   "configured", lr["notifier_ready"]),
        _lr_check("Pilot authorization", "present" if lr["pilot_authorization_present"] else "absent",
                   "present (manual)", lr["pilot_authorization_present"]),
        _lr_check("Strategy eligible", lr["strategy"] , "provisional_paper_candidate+",
                   lr["strategy_eligible"]),
        "",
        "## Runtime Context",
        f"- State: {lr['runtime_state']}",
        f"- Real paper days: {lr['real_paper_days']}",
        f"- Shadow days: {lr['shadow_days']}",
        f"- Eligible / Quarantined: {lr['eligible_records']} / {lr['quarantined_records']}",
        "",
        "## Clean Day Definition",
        f"> {lr['clean_day_definition']}",
        f"> Reset: {lr['clean_day_reset_condition']}",
        f"> Current trailing count: **{lr['clean_final_days_current']}** / {lr['clean_final_days_required']} required",
    ]

    if lr["blocking_requirements"]:
        md.extend(["", "## Blocking Requirements"])
        for b in lr["blocking_requirements"]:
            md.append(f"- ❌ {b}")

    if not lr["blocking_requirements"]:
        md.extend(["", "## All pre-conditions met."])

    md.extend(["", "---",
               "canonical promotion bundle / live eligibility는 이 도구로 자동 수정되지 않습니다."])

    md_path = RUNTIME_DIR / f"{strategy}_pilot_launch_readiness.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    return json_path, md_path


def _lr_check(label: str, current, requirement, ok: bool) -> str:
    icon = "✅" if ok else "❌"
    return f"- {icon} **{label}**: {current} (need {requirement})"


def generate_pilot_runbook(strategy: str) -> Path:
    """operator-facing runbook markdown 생성."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lr = compute_launch_readiness(strategy)

    lines = [
        f"# Pilot Runbook: {strategy}",
        f"Generated: {datetime.now().isoformat()[:19]}",
        "",
        "## 1. 현재 상태",
        f"- Runtime: **{lr['runtime_state']}**",
        f"- Launch ready: **{'YES' if lr['launch_ready'] else 'NO'}**",
        f"- Infrastructure ready: **{'YES' if lr['infra_ready'] else 'NO'}**",
        f"- Real paper days: {lr['real_paper_days']}",
        f"- Clean final days: {lr['clean_final_days_current']}/{lr['clean_final_days_required']}",
    ]

    if lr["blocking_requirements"]:
        lines.extend(["", "## 2. 남은 Requirement"])
        for b in lr["blocking_requirements"]:
            lines.append(f"- [ ] {b}")

    lines.extend([
        "",
        "## 3. Pilot Enable 전 필요한 단계",
    ])
    step = 1
    if not lr["notifier_ready"]:
        lines.append(f"{step}. Discord webhook 설정: settings.yaml → discord.webhook_url")
        lines.append(f"   확인: `python tools/paper_preflight.py --strategy {strategy} --with-pilot-check`")
        step += 1
    if lr["remaining_clean_days"] > 0:
        lines.append(f"{step}. Clean evidence {lr['remaining_clean_days']}일 추가 축적:")
        lines.append(f"   `python tools/run_paper_evidence_pipeline.py --strategy {strategy} --date YYYY-MM-DD`")
        lines.append(f"   `python tools/run_paper_evidence_pipeline.py --strategy {strategy} --finalize --date YYYY-MM-DD`")
        step += 1
    if not lr["evidence_fresh"]:
        lines.append(f"{step}. Evidence 수집 (오늘 or 가장 최근 거래일):")
        lines.append(f"   `python tools/run_paper_evidence_pipeline.py --strategy {strategy} --date $(date +%Y-%m-%d)`")
        step += 1
    lines.append(f"{step}. Preflight 확인:")
    lines.append(f"   `python tools/paper_preflight.py --strategy {strategy} --with-pilot-check`")
    step += 1
    lines.append(f"{step}. Launch readiness 확인:")
    lines.append(f"   `python tools/paper_launch_readiness.py --strategy {strategy}`")

    lines.extend([
        "",
        "## 4. Pilot Enable 명령",
        "```bash",
        f"python tools/paper_pilot_control.py --strategy {strategy} --enable \\",
        f"  --from YYYY-MM-DD --to YYYY-MM-DD \\",
        f"  --max-orders 2 --max-positions 2 --max-notional 1000000 --max-exposure 3000000 \\",
        f'  --reason "first {strategy} pilot — collect execution-backed evidence"',
        "```",
        "",
        "## 5. Pilot Disable / Rollback 명령",
        "```bash",
        f'python tools/paper_pilot_control.py --strategy {strategy} --disable --reason "stop pilot"',
        "```",
        "",
        "## 6. Pilot 실행 후 예상 Artifact",
        f"- `reports/paper_runtime/preflight_status_{strategy}.json`",
        f"- `reports/paper_runtime/pilot_session_{strategy}_YYYY-MM-DD.json`",
        f"- `reports/paper_runtime/pilot_session_{strategy}_YYYY-MM-DD.md`",
        f"- `reports/paper_evidence/daily_evidence_{strategy}.jsonl` (pilot_paper record 추가)",
        f"- `reports/paper_evidence/promotion_evidence_{strategy}.json` (pilot_real_paper_days 증가)",
        "",
        "## 7. Success Criteria",
        "- post-market evidence의 evidence_mode == pilot_paper",
        "- execution_backed == true",
        "- session_mode == pilot_paper",
        "- pilot_real_paper_days가 전일 대비 +1",
        "- anomalies == [] (clean day)",
        "- benchmark_status == final",
        "",
        "## 8. Abort Criteria",
        "- phantom_position_count > 0 → 자동 freeze",
        "- deep_drawdown anomaly → 자동 freeze",
        "- cap hit (max_orders / max_exposure) → 추가 entry 차단",
        "- notifier 장애 → pilot entry 차단",
        "- operator 판단 → disable 명령으로 즉시 중단",
        "",
        "---",
        "canonical promotion bundle / live eligibility는 이 도구로 자동 수정되지 않습니다.",
        "pilot_real_paper_days는 promotion 카운트에 포함되지만, live 승격은 별도 수동 승인 필요.",
    ])

    path = RUNTIME_DIR / f"{strategy}_pilot_runbook.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
