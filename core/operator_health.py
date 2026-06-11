"""운영자 아침 점검용 통합 헬스 요약.

기존에는 운영자가 시스템 상태를 파악하려면 여러 도구를 따로 돌려야 했다:
  - paper_runtime_status.py --all       (전략별 runtime state)
  - evaluate_and_promote.py --check-only (artifact 동기화/freshness)
  - current_blockers.json 직접 확인       (go_live, hard_blockers)

이 모듈은 그 신호들을 하나로 모아 단일 verdict(OK / ATTENTION / BLOCKED)와
사람이 읽는 요약을 만든다. 순수 함수라 외부 상태를 직접 읽지 않고 주입받은
데이터로만 판정하므로 단위 테스트가 쉽다.

verdict 규칙(보수적 — 의심스러우면 주의 이상). '운영 건강(장애)'과 'live 승격
게이트(진행 단계)'를 구분한다 — 승격 NO-GO(live 후보 없음)는 장애가 아니라 상태이고,
알파 없음이 정착 결론인 체제에서는 상시 NO-GO가 정상이므로 전체 verdict에 합산하지
않는다(헤드라인의 게이트 라벨로 보고; 매일 빨강이면 진짜 장애를 못 알아본다):
  - BLOCKED  : frozen 전략 존재, 또는 바스켓 운영 차단 수준 이상.
  - ATTENTION: degraded/blocked_insufficient_evidence 전략, manual freeze, 최근 anomaly,
               바스켓 스냅샷 끊김, 또는 게이트 artifact의 '장애성' 신호(부재/stale/표기 불일치).
  - OK       : 위 어느 것에도 안 걸림. (게이트 NO-GO 여부와 무관)
"""

from __future__ import annotations

from typing import Any

# verdict 우선순위 (높을수록 심각)
_VERDICT_RANK = {"OK": 0, "ATTENTION": 1, "BLOCKED": 2}

# runtime state별 분류
_BLOCKING_STATES = {"frozen"}
_ATTENTION_STATES = {"degraded", "blocked_insufficient_evidence"}


def _worst(*verdicts: str) -> str:
    """주어진 verdict 중 가장 심각한 것을 반환."""
    worst = "OK"
    for v in verdicts:
        if _VERDICT_RANK.get(v, 0) > _VERDICT_RANK[worst]:
            worst = v
    return worst


def summarize_runtime_state(state: Any) -> dict[str, Any]:
    """단일 RuntimeState(또는 동등 객체)를 verdict + 요약으로 환원한다.

    state는 .state / .strategy / .manual_freeze / .last_anomalies 속성을 가진 객체.
    """
    name = getattr(state, "strategy", "?")
    s = getattr(state, "state", "unknown")
    manual_freeze = bool(getattr(state, "manual_freeze", False))
    anomalies = list(getattr(state, "last_anomalies", []) or [])

    if s in _BLOCKING_STATES:
        verdict = "BLOCKED"
    elif s in _ATTENTION_STATES or manual_freeze or anomalies:
        verdict = "ATTENTION"
    else:
        verdict = "OK"

    notes = []
    if s in _BLOCKING_STATES:
        notes.append(f"state={s}")
    elif s in _ATTENTION_STATES:
        notes.append(f"state={s}")
    if manual_freeze:
        notes.append("manual_freeze")
    if anomalies:
        notes.append(f"anomalies={len(anomalies)}")

    return {
        "strategy": name,
        "state": s,
        "verdict": verdict,
        "manual_freeze": manual_freeze,
        "anomaly_count": len(anomalies),
        "notes": notes,
    }


def summarize_blockers(blockers: dict[str, Any] | None) -> dict[str, Any]:
    """current_blockers.json 페이로드를 verdict + 요약으로 환원한다."""
    if not blockers:
        return {
            "verdict": "ATTENTION",
            "go_live": False,
            "hard_blocker_count": 0,
            "notes": ["current_blockers 없음/로드 실패"],
            "freshness_stale": True,
            "gate_health_issue": True,
        }

    hard = list(blockers.get("hard_blockers") or [])
    go_live = bool(blockers.get("go_live", False))
    live_candidates = list(blockers.get("live_candidates") or [])
    freshness = blockers.get("promotion_artifact_freshness") or {}
    # freshness가 dict면 stale 여부를 본다(없으면 보수적으로 미상=stale 취급하지 않음).
    stale = False
    if isinstance(freshness, dict):
        stale = bool(freshness.get("stale", False)) or freshness.get("status") in ("stale", "expired")

    notes = []
    verdict = "OK"
    gate_health_issue = False  # '장애성' 신호(artifact 부재/stale/표기 불일치) — NO-GO 자체와 구분
    if hard:
        verdict = "BLOCKED"
        notes.append(f"hard_blockers={len(hard)}")
    if stale:
        verdict = _worst(verdict, "BLOCKED")
        notes.append("artifact_stale")
        gate_health_issue = True
    # go_live=false인데 live_candidates가 비어있지 않으면 표기 불일치(주의).
    if not go_live and live_candidates:
        verdict = _worst(verdict, "ATTENTION")
        notes.append("go_live=false_but_live_candidates_present")
        gate_health_issue = True

    return {
        "verdict": verdict,
        "go_live": go_live,
        "live_candidates": live_candidates,
        "hard_blocker_count": len(hard),
        "hard_blockers": hard,
        "freshness_stale": stale,
        "gate_health_issue": gate_health_issue,
        "notes": notes,
    }


def summarize_basket_operation(
    enabled_baskets: list[str],
    last_snapshot_date: Any,
    position_count: int,
    today: Any,
    max_stale_calendar_days: int = 4,
) -> dict[str, Any]:
    """바스켓 paper 운영(트랙레코드 축적) 상태를 verdict + 요약으로 환원한다.

    배포 수익경로(바스켓 buy&hold)는 일일 NAV 스냅샷 시계열이 생명이다 — 일일
    사이클(스케줄 작업 또는 --mode rebalance)이 조용히 멈추면 60영업일 평가에
    구멍이 난다. 여기서 그 끊김을 운영자 아침 점검에 노출한다.

    규칙(보수적):
      - enabled 바스켓 없음 → OK (운영 안 함은 문제 아님), note만 남김.
      - enabled 있는데 스냅샷이 전혀 없음 → ATTENTION (운영 시작 직후이거나 사이클 미실행).
      - 마지막 스냅샷이 max_stale_calendar_days(기본 4 — 주말+월 휴장 커버) 초과
        경과 → ATTENTION (일일 사이클 중단 의심). 긴 연휴엔 오탐 가능 — 주의 수준이므로 수용.

    last_snapshot_date / today 는 date 또는 datetime(섞여도 됨) — 날짜로 정규화해 비교.
    """
    def _as_date(v: Any):
        return v.date() if hasattr(v, "date") and callable(getattr(v, "date")) else v

    notes: list[str] = []
    if not enabled_baskets:
        return {
            "verdict": "OK",
            "enabled_baskets": [],
            "last_snapshot_date": None,
            "position_count": int(position_count or 0),
            "stale_days": None,
            "notes": ["enabled 바스켓 없음(운영 안 함)"],
        }

    verdict = "OK"
    stale_days = None
    if last_snapshot_date is None:
        verdict = "ATTENTION"
        notes.append("트랙레코드 스냅샷 없음 — 일일 리밸런싱 사이클 미실행 의심")
    else:
        delta = _as_date(today) - _as_date(last_snapshot_date)
        stale_days = int(delta.days)
        if stale_days > max_stale_calendar_days:
            verdict = "ATTENTION"
            notes.append(
                f"스냅샷 끊김 {stale_days}일(마지막 {_as_date(last_snapshot_date)}) — "
                "일일 사이클 중단 의심"
            )

    return {
        "verdict": verdict,
        "enabled_baskets": list(enabled_baskets),
        "last_snapshot_date": _as_date(last_snapshot_date) if last_snapshot_date is not None else None,
        "position_count": int(position_count or 0),
        "stale_days": stale_days,
        "notes": notes,
    }


def build_operator_health(
    runtime_states: list[Any],
    blockers: dict[str, Any] | None,
    basket_operation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """전략별 runtime state + current_blockers (+ 바스켓 운영)를 하나의 헬스 요약으로 합친다.

    basket_operation: summarize_basket_operation에 넘길 raw 입력 dict
      {"enabled_baskets": [...], "last_snapshot_date": date|None,
       "position_count": int, "today": date}. None이면 섹션 생략(하위 호환).

    반환:
      {
        "verdict": "OK" | "ATTENTION" | "BLOCKED",
        "strategy_count": N,
        "strategies": [summarize_runtime_state(...), ...],
        "blockers": summarize_blockers(...),
        "basket": summarize_basket_operation(...) | None,
        "headline": 사람이 읽는 한 줄 요약,
        "attention_items": [...],  # 운영자가 봐야 할 항목들
      }
    """
    strat_summaries = [summarize_runtime_state(s) for s in runtime_states]
    blocker_summary = summarize_blockers(blockers)
    basket_summary = (
        summarize_basket_operation(**basket_operation) if basket_operation is not None else None
    )

    # verdict 합산 원칙: '운영 건강(장애)'과 'live 승격 게이트(진행 단계)'를 구분한다.
    # hard_blocker("live 후보 없음" 등)는 장애가 아니라 승격 파이프라인의 상태이며,
    # 알파 없음이 정착 결론인 현 체제에서는 상시 NO-GO가 정상이다 — 이것을 전체
    # BLOCKED로 합산하면 운영자가 매일 빨강을 보다가 진짜 장애를 못 알아본다(알람
    # 피로). 게이트 차원에서는 '장애성' 신호(artifact 부재/stale/표기 불일치)만
    # ATTENTION으로 합산하고, NO-GO 자체는 헤드라인의 게이트 라벨로 보고한다.
    verdict = "OK"
    for s in strat_summaries:
        verdict = _worst(verdict, s["verdict"])
    # 문자열 매칭 대신 구조 플래그 — 노트 문구가 바뀌어도 강등 로직이 깨지지 않는다.
    gate_health_notes = (
        list(blocker_summary["notes"]) if blocker_summary.get("gate_health_issue") else []
    )
    if gate_health_notes:
        verdict = _worst(verdict, "ATTENTION")
    if basket_summary is not None:
        verdict = _worst(verdict, basket_summary["verdict"])

    attention_items: list[str] = []
    for s in strat_summaries:
        if s["verdict"] != "OK":
            attention_items.append(f"{s['strategy']}: {', '.join(s['notes']) or s['state']}")
    if gate_health_notes:
        attention_items.append("blockers: " + ", ".join(gate_health_notes))
    if basket_summary is not None and basket_summary["verdict"] != "OK":
        attention_items.append("basket: " + ", ".join(basket_summary["notes"]))

    if blocker_summary["go_live"]:
        gate_label = "GO"
    elif blocker_summary["hard_blocker_count"]:
        gate_label = (
            f"NO-GO (hard_blockers={blocker_summary['hard_blocker_count']} — "
            "live 후보 없음은 연구 결론상 정상)"
        )
    else:
        gate_label = "NO-GO"

    n = len(strat_summaries)
    n_ok = sum(1 for s in strat_summaries if s["verdict"] == "OK")
    if verdict == "OK":
        headline = f"운영 정상 — 전략 {n}개 OK | live 승격 게이트: {gate_label}"
    elif verdict == "ATTENTION":
        headline = (
            f"주의 필요 — 전략 {n}개 중 {n_ok}개 OK, 확인 항목 {len(attention_items)}건 "
            f"| live 승격 게이트: {gate_label}"
        )
    else:
        headline = f"차단 상태 — 운영 개입 필요, 확인 항목 {len(attention_items)}건"

    return {
        "verdict": verdict,
        "strategy_count": n,
        "strategies": strat_summaries,
        "blockers": blocker_summary,
        "basket": basket_summary,
        "headline": headline,
        "attention_items": attention_items,
    }
