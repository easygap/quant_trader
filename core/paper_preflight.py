"""
Paper Preflight — 첫 실제 paper 세션 전 운영 준비 상태 점검

체크 항목:
  - runtime state / allowed_actions / block reason
  - evidence freshness / benchmark readiness
  - legacy quarantine 상태
  - DB read/write health
  - pending/open order cleanup 필요 여부
  - stale anomaly / phantom / repeated reject
  - notifier health (Discord webhook)
  - market session timing

결과: fail-closed (critical fail → entry 금지, exit/cleanup은 기존 정책대로 유지)
정책 변경 없음. runtime state/allowed_actions semantics 유지.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

RUNTIME_DIR = Path("reports/paper_runtime")


@dataclass
class PreflightCheck:
    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str = ""


@dataclass
class PreflightResult:
    strategy: str
    date: str
    overall: str = "pass"  # "pass" | "warn" | "fail"
    entry_allowed: bool = True
    exit_allowed: bool = True
    checks: list = field(default_factory=list)
    runtime_state: str = ""
    allowed_actions: list = field(default_factory=list)
    block_reasons: list = field(default_factory=list)
    evidence_date: Optional[str] = None
    evidence_freshness: str = ""
    benchmark_final_ratio: Optional[float] = None
    excess_non_null_ratio: Optional[float] = None
    eligible_records: int = 0
    quarantined_records: int = 0
    has_real_evidence: bool = False
    real_paper_days: int = 0
    shadow_days: int = 0
    shadow_bootstrap_available: bool = False
    pilot_authorized: bool = False
    pilot_remaining_orders: Optional[int] = None
    pilot_remaining_exposure: Optional[int] = None
    notifier_health: str = ""
    open_positions: int = 0
    pending_orders: int = 0
    operator_actions: list = field(default_factory=list)
    evaluated_at: str = ""
    # launch readiness (pilot launch 전제조건 요약)
    launch_ready: bool = False
    infra_ready: bool = False
    clean_final_days: int = 0
    remaining_clean_days: int = 0
    blocking_requirements: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Core: Preflight 실행
# ═══════════════════════════════════════════════════════════════

def run_preflight(strategy: str, date: str | None = None,
                  send_test_notification: bool = False) -> PreflightResult:
    """전략별 preflight 점검 실행."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    checks: list[PreflightCheck] = []
    operator_actions: list[str] = []

    result = PreflightResult(
        strategy=strategy, date=date,
        evaluated_at=datetime.now().isoformat(),
    )

    # ── 1. Runtime state ──
    _check_runtime_state(strategy, date, result, checks, operator_actions)

    # ── 2. Evidence freshness / legacy ──
    _check_evidence(strategy, date, result, checks, operator_actions)

    # ── 3. DB health ──
    _check_db_health(checks)

    # ── 4. Pending orders / open positions ──
    _check_positions_orders(strategy, result, checks, operator_actions)

    # ── 5. Stale anomalies ──
    _check_stale_anomalies(strategy, checks, operator_actions)

    # ── 6. Notifier health ──
    _check_notifier_health(result, checks, operator_actions, send_test_notification)

    # ── 7. Market session ──
    _check_market_session(date, checks)

    # ── Overall 판정 ──
    result.checks = [asdict(c) for c in checks]
    result.operator_actions = operator_actions

    has_fail = any(c.status == "fail" for c in checks)
    has_warn = any(c.status == "warn" for c in checks)

    if has_fail:
        result.overall = "fail"
        result.entry_allowed = False
    elif has_warn:
        result.overall = "warn"
    else:
        result.overall = "pass"

    # entry 판정: runtime state 기반 + preflight critical
    if "entry" not in result.allowed_actions:
        result.entry_allowed = False
    # exit는 항상 허용 (exit-safe invariant)
    result.exit_allowed = "exit" in result.allowed_actions

    # ── Launch readiness ──
    try:
        from core.paper_pilot import compute_launch_readiness
        lr = compute_launch_readiness(strategy, as_of_date=date)
        result.launch_ready = lr["launch_ready"]
        result.infra_ready = lr["infra_ready"]
        result.clean_final_days = lr["clean_final_days_current"]
        result.remaining_clean_days = lr["remaining_clean_days"]
        result.blocking_requirements = lr["blocking_requirements"]
    except Exception:
        pass

    # ── 저장 ──
    _save_preflight(result)

    return result


# ═══════════════════════════════════════════════════════════════
# 개별 Check 함수들
# ═══════════════════════════════════════════════════════════════

def _check_runtime_state(strategy, date, result, checks, actions):
    try:
        from core.paper_runtime import get_paper_runtime_state
        state = get_paper_runtime_state(strategy, as_of_date=date)
        result.runtime_state = state.state
        result.allowed_actions = state.allowed_actions
        result.block_reasons = state.reasons

        m = state.metrics
        result.benchmark_final_ratio = m.get("recent_final_ratio")
        result.excess_non_null_ratio = m.get("excess_non_null_ratio")
        result.eligible_records = m.get("eligible_records", 0)
        result.quarantined_records = m.get("quarantined_records", 0)
        result.real_paper_days = m.get("real_paper_days", 0)
        result.shadow_days = m.get("shadow_days", 0)
        result.evidence_date = state.evidence_date

        result.shadow_bootstrap_available = "shadow_collect" in state.allowed_actions

        # pilot authorization check
        try:
            from core.paper_pilot import check_pilot_entry
            pilot_check = check_pilot_entry(strategy, as_of_date=date)
            result.pilot_authorized = pilot_check.allowed
            result.pilot_remaining_orders = pilot_check.remaining_orders
            result.pilot_remaining_exposure = pilot_check.remaining_exposure
            if pilot_check.allowed:
                result.entry_allowed = True  # pilot override
        except Exception:
            pass

        if state.state == "normal":
            checks.append(PreflightCheck("runtime_state", "pass", "normal"))
        elif state.state in ("degraded", "blocked_insufficient_evidence"):
            checks.append(PreflightCheck("runtime_state", "warn",
                          f"{state.state}: {'; '.join(state.reasons)}"))
            if state.state == "blocked_insufficient_evidence":
                actions.append("shadow bootstrap으로 evidence 수집 가능 (paper_bootstrap.py --mode shadow)")
        elif state.state == "frozen":
            checks.append(PreflightCheck("runtime_state", "fail",
                          f"frozen: {'; '.join(state.reasons)}"))
            actions.append("freeze 원인 해소 후 manual_unfreeze 필요")
        elif state.state == "research_disabled":
            checks.append(PreflightCheck("runtime_state", "fail",
                          "strategy disabled in registry"))
            actions.append("strategy registry에서 전략 상태 재검토 필요")
    except Exception as e:
        checks.append(PreflightCheck("runtime_state", "fail", f"exception: {e}"))


def _check_evidence(strategy, date, result, checks, actions):
    try:
        from core.paper_evidence import get_canonical_records
        from core.paper_runtime import filter_runtime_eligible, classify_evidence_schema

        all_records = get_canonical_records(strategy)
        eligible, quarantined = filter_runtime_eligible(all_records)

        if not all_records:
            checks.append(PreflightCheck("evidence", "fail", "no evidence records at all"))
            result.has_real_evidence = False
            actions.append("최소 1일 이상 collect_daily_evidence 실행 필요")
            return

        result.has_real_evidence = len(eligible) > 0

        # freshness: latest evidence date vs today
        if eligible:
            latest_date = eligible[-1].get("date", "")
            result.evidence_date = latest_date
            days_stale = (datetime.strptime(date, "%Y-%m-%d") -
                         datetime.strptime(latest_date, "%Y-%m-%d")).days
            if days_stale <= 1:
                result.evidence_freshness = "fresh"
                checks.append(PreflightCheck("evidence_freshness", "pass",
                              f"latest={latest_date} (today or yesterday)"))
            elif days_stale <= 5:
                result.evidence_freshness = "stale"
                checks.append(PreflightCheck("evidence_freshness", "warn",
                              f"latest={latest_date} ({days_stale}d ago)"))
            else:
                result.evidence_freshness = "very_stale"
                checks.append(PreflightCheck("evidence_freshness", "warn",
                              f"latest={latest_date} ({days_stale}d ago — very stale)"))
                actions.append(f"최근 evidence가 {days_stale}일 전 — backfill 또는 새 수집 필요")
        else:
            result.evidence_freshness = "no_eligible"
            checks.append(PreflightCheck("evidence_freshness", "warn",
                          "no eligible (v2+) evidence — legacy only"))
            actions.append("v2 evidence 수집 필요 (기존 v1은 quarantine됨)")

        # quarantine 경고
        if quarantined:
            checks.append(PreflightCheck("legacy_quarantine", "warn",
                          f"{len(quarantined)} legacy records quarantined (excluded from runtime)"))
    except Exception as e:
        checks.append(PreflightCheck("evidence", "fail", f"exception: {e}"))


def _check_db_health(checks):
    try:
        from database.models import get_session, OperationEvent
        session = get_session()
        # read test
        session.query(OperationEvent).first()
        # write test
        test_ev = OperationEvent(
            event_type="PREFLIGHT_PROBE", severity="info",
            mode="paper", message="preflight DB health check",
        )
        session.add(test_ev)
        session.commit()
        session.delete(test_ev)
        session.commit()
        session.close()
        checks.append(PreflightCheck("db_health", "pass", "read/write OK"))
    except Exception as e:
        checks.append(PreflightCheck("db_health", "fail", f"DB error: {e}"))


def _check_positions_orders(strategy, result, checks, actions):
    try:
        from database.repositories import get_all_positions, get_pending_failed_orders
        positions = get_all_positions(account_key=strategy)
        result.open_positions = len(positions) if positions else 0
        pending = get_pending_failed_orders()
        result.pending_orders = len(pending) if pending else 0

        if result.open_positions > 0:
            checks.append(PreflightCheck("open_positions", "warn",
                          f"{result.open_positions} open positions — exit/cleanup 필요 확인"))
        else:
            checks.append(PreflightCheck("open_positions", "pass", "no open positions"))

        if result.pending_orders > 0:
            checks.append(PreflightCheck("pending_orders", "warn",
                          f"{result.pending_orders} pending/failed orders — cleanup 필요"))
            actions.append(f"pending 주문 {result.pending_orders}건 확인/정리 필요")
        else:
            checks.append(PreflightCheck("pending_orders", "pass", "no pending orders"))
    except Exception as e:
        checks.append(PreflightCheck("positions_orders", "fail", f"exception: {e}"))


def _check_stale_anomalies(strategy, checks, actions):
    try:
        from core.paper_evidence import get_canonical_records
        records = get_canonical_records(strategy)
        if not records:
            return

        latest = records[-1]
        anomalies = latest.get("anomalies", [])
        if anomalies:
            types = [a.get("type", "?") for a in anomalies]
            checks.append(PreflightCheck("latest_anomalies", "warn",
                          f"latest day has anomalies: {', '.join(types)}"))
            if any(a.get("severity") == "critical" for a in anomalies):
                actions.append("critical anomaly 해소 필요 (phantom_position / deep_drawdown)")
        else:
            checks.append(PreflightCheck("latest_anomalies", "pass", "no anomalies on latest day"))

        if latest.get("phantom_position_count", 0) > 0:
            checks.append(PreflightCheck("phantom_position", "fail",
                          f"phantom_position_count={latest['phantom_position_count']}"))
    except Exception as e:
        checks.append(PreflightCheck("anomalies", "fail", f"exception: {e}"))


def _check_notifier_health(result, checks, actions, send_test: bool):
    health = {"discord_configured": False, "discord_reachable": None,
              "test_send_id": None, "test_send_status": None}
    try:
        from config.config_loader import Config
        config = Config.get()
        dc = config.discord
        webhook_url = dc.get("webhook_url", "")
        enabled = dc.get("enabled", False)

        health["discord_configured"] = bool(enabled and webhook_url)

        if not health["discord_configured"]:
            checks.append(PreflightCheck("notifier_discord", "warn",
                          "Discord webhook not configured — 알림 미발송"))
            actions.append("Discord webhook 설정 필요 (settings.yaml discord.webhook_url)")
            result.notifier_health = "unconfigured"
        else:
            if send_test:
                # 실제 test notification 발송
                correlation_id = str(uuid.uuid4())[:8]
                test_msg = (
                    f"🧪 **Preflight Test** ({result.strategy})\n"
                    f"correlation_id={correlation_id}\n"
                    f"date={result.date}\n"
                    f"runtime_state={result.runtime_state}\n"
                    f"This is an automated preflight check. No action required."
                )
                try:
                    import requests as req
                    payload = {"content": test_msg, "username": dc.get("username", "Preflight")}
                    resp = req.post(webhook_url, json=payload, timeout=10)
                    health["discord_reachable"] = resp.status_code in (200, 204)
                    health["test_send_id"] = correlation_id
                    health["test_send_status"] = resp.status_code
                    if health["discord_reachable"]:
                        checks.append(PreflightCheck("notifier_discord", "pass",
                                      f"test send OK (status={resp.status_code}, id={correlation_id})"))
                        result.notifier_health = "healthy"
                    else:
                        checks.append(PreflightCheck("notifier_discord", "warn",
                                      f"test send failed (status={resp.status_code})"))
                        result.notifier_health = "degraded"
                except Exception as e:
                    health["discord_reachable"] = False
                    health["test_send_status"] = str(e)
                    checks.append(PreflightCheck("notifier_discord", "warn",
                                  f"test send exception: {e}"))
                    result.notifier_health = "degraded"
            else:
                checks.append(PreflightCheck("notifier_discord", "pass",
                              "webhook configured (use --send-test-notification to verify)"))
                result.notifier_health = "configured"

    except Exception as e:
        checks.append(PreflightCheck("notifier_discord", "warn", f"exception: {e}"))
        result.notifier_health = "error"

    # notifier health artifact
    _save_notifier_health(health)


def _check_market_session(date, checks):
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        if d.weekday() >= 5:
            checks.append(PreflightCheck("market_session", "warn",
                          f"{date} is weekend — no trading session"))
        else:
            checks.append(PreflightCheck("market_session", "pass",
                          f"{date} is weekday — trading session available"))
    except Exception as e:
        checks.append(PreflightCheck("market_session", "fail", f"exception: {e}"))


# ═══════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════

def _save_preflight(result: PreflightResult) -> tuple[Path, Path]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = RUNTIME_DIR / f"preflight_status_{result.strategy}.json"
    json_path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False, default=str),
                         encoding="utf-8")

    # Markdown
    md_path = RUNTIME_DIR / f"preflight_status_{result.strategy}.md"
    lines = _format_preflight_md(result)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


def _save_notifier_health(health: dict):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / "notifier_health.json"
    path.write_text(json.dumps(health, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _save_session_bootstrap(date: str, strategies: list[PreflightResult]):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Session Bootstrap: {date}",
        f"Generated: {datetime.now().isoformat()}",
        "",
    ]
    for r in strategies:
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(r.overall, "❓")
        entry = "YES" if r.entry_allowed else "NO"
        lines.extend([
            f"## {icon} {r.strategy}",
            f"- **Overall**: {r.overall.upper()}",
            f"- **Entry Allowed**: {entry}",
            f"- **Runtime State**: {r.runtime_state}",
            f"- **Evidence Date**: {r.evidence_date or 'N/A'}",
            f"- **Notifier**: {r.notifier_health}",
            f"- **Block Reasons**: {'; '.join(r.block_reasons) or 'none'}",
            "",
        ])
        if r.operator_actions:
            lines.append("**Operator Actions:**")
            for a in r.operator_actions:
                lines.append(f"  - {a}")
            lines.append("")
    lines.append("---")
    lines.append("canonical promotion bundle / live eligibility는 이 도구로 자동 수정되지 않습니다.")

    path = RUNTIME_DIR / f"session_bootstrap_{date}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _format_preflight_md(result: PreflightResult) -> list[str]:
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(result.overall, "❓")
    entry = "YES ✅" if result.entry_allowed else "NO ❌"

    lines = [
        f"# Preflight: {result.strategy}",
        f"Date: {result.date} | Evaluated: {result.evaluated_at[:19]}",
        "",
        f"## {icon} Overall: {result.overall.upper()}",
        f"- **Entry Allowed**: {entry}",
        f"- **Exit Allowed**: {'YES' if result.exit_allowed else 'NO'}",
        f"- **Runtime State**: {result.runtime_state}",
        f"- **Allowed Actions**: {', '.join(result.allowed_actions) or 'NONE'}",
        "",
        "## Evidence",
        f"- Latest Date: {result.evidence_date or 'N/A'}",
        f"- Freshness: {result.evidence_freshness}",
        f"- Eligible Records: {result.eligible_records}",
        f"- Quarantined (legacy): {result.quarantined_records}",
        f"- Has Real Evidence: {'YES' if result.has_real_evidence else 'NO'}",
        f"- Real Paper Days: {result.real_paper_days}",
        f"- Shadow Days: {result.shadow_days}",
        f"- Shadow Bootstrap: {'AVAILABLE' if result.shadow_bootstrap_available else 'N/A'}",
        f"- Pilot Authorized: {'YES' if result.pilot_authorized else 'NO'}",
        f"- Pilot Remaining Orders: {result.pilot_remaining_orders if result.pilot_remaining_orders is not None else 'N/A'}",
        f"- Pilot Remaining Exposure: {f'{result.pilot_remaining_exposure:,}' if result.pilot_remaining_exposure is not None else 'N/A'}",
        f"- Benchmark Final Ratio: {result.benchmark_final_ratio}",
        f"- Excess Non-Null Ratio: {result.excess_non_null_ratio}",
        "",
        "## Operations",
        f"- Open Positions: {result.open_positions}",
        f"- Pending Orders: {result.pending_orders}",
        f"- Notifier Health: {result.notifier_health}",
        "",
        "## Checks",
        "| Check | Status | Detail |",
        "|-------|--------|--------|",
    ]
    for c in result.checks:
        s_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(c["status"], "?")
        lines.append(f"| {c['name']} | {s_icon} {c['status']} | {c['detail']} |")

    if result.block_reasons:
        lines.extend(["", "## Block Reasons"])
        for r in result.block_reasons:
            lines.append(f"- {r}")

    if result.operator_actions:
        lines.extend(["", "## Operator Actions Required"])
        for a in result.operator_actions:
            lines.append(f"- [ ] {a}")

    # Launch readiness section
    lr_icon = "✅" if result.launch_ready else "❌"
    infra_icon = "✅" if result.infra_ready else "⏳"
    lines.extend([
        "",
        "## Launch Readiness",
        f"- {lr_icon} **Launch Ready**: {'YES' if result.launch_ready else 'NO'}",
        f"- {infra_icon} **Infra Ready**: {'YES' if result.infra_ready else 'NO'}",
        f"- Clean Final Days: {result.clean_final_days} (remaining: {result.remaining_clean_days})",
    ])
    if result.blocking_requirements:
        for b in result.blocking_requirements:
            lines.append(f"  - ❌ {b}")

    # Legacy evidence note
    if result.quarantined_records > 0:
        lines.extend([
            "",
            "## Legacy Evidence Note",
            f"> {result.quarantined_records} legacy (v1) record(s) quarantined.",
            "> runtime/promotion 계산에 미반영. 향후 제거 예정 (운영상 무해).",
        ])

    return lines


# ═══════════════════════════════════════════════════════════════
# Scheduler 연동
# ═══════════════════════════════════════════════════════════════

def load_preflight_status(strategy: str) -> PreflightResult | None:
    """저장된 preflight 결과 로드. scheduler 시작 시 사용."""
    path = RUNTIME_DIR / f"preflight_status_{strategy}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PreflightResult(**{k: v for k, v in data.items()
                                  if k in PreflightResult.__dataclass_fields__})
    except Exception:
        return None
