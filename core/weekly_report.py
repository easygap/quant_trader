"""주간 요약 리포트 — 판단 주기(주 1회)에 맞춘 다이제스트.

한 달 운영 리뷰(docs/PAPER_MONTH1_REVIEW_AND_PLAN.md P1-7)의 배경: 일일 숫자는
노이즈가 커서 매일 판단하기엔 부적합하다. 주간 요약은 (1) 주간·누적 성과, (2) 성과
귀속(실행 격차/구성 격차 — CLI에만 있던 P1-4 분석을 오너에게 주간 푸시), (3) 일정
대비 진행률·커버리지, (4) 주간 운영 이벤트(결측·스킵·오류)를 한 장으로 모은다.

build_weekly_summary는 순수 함수다 — 입력(평가결과·주간 NAV 변화·이벤트 카운트)을
받아 Discord embed 필드 목록과 텍스트 폴백을 만든다. 데이터 수집은 호출부(main.py
run_weekly_report)가 담당한다.
"""

from __future__ import annotations

from typing import Any


def build_weekly_summary(
    *,
    basket_name: str,
    eval_result: dict[str, Any],
    week_nav_change_pct: float | None = None,
    missing_days: int = 0,
    cycle_errors: int = 0,
) -> dict[str, Any]:
    """주간 다이제스트를 만든다(순수 함수).

    missing_days: 이번 주(최근 영업일) 스냅샷이 빠진 '고유 일수' — SNAPSHOT_GAP 이벤트
      원시 카운트가 아니다(P0-1이 미복구 결측을 매 사이클 재경보하므로 이벤트 수는
      하루 결측을 여러 건으로 부풀린다 → 고유 일수로 집계해야 정확).
    cycle_errors: 이번 주 CYCLE_ERROR 발생 건수.

    반환: {"title": str, "fields": [{"name","value","inline"}...], "text": str}
      - fields: notifier.send_embed용
      - text: 임베드 실패 시 폴백 텍스트
    """
    m = (eval_result or {}).get("metrics") or {}
    verdict = (eval_result or {}).get("verdict", "?")
    icon = {"WAIT": "⏳", "PASS_CANDIDATE": "✅", "FAIL_REVIEW": "❌"}.get(verdict, "❓")

    fields: list[dict[str, Any]] = []

    # 1) 성과 — 주간 변화 + 누적
    nav = m.get("nav_return_pct")
    perf = []
    if week_nav_change_pct is not None:
        perf.append(f"주간 {week_nav_change_pct:+.2f}%")
    if nav is not None:
        perf.append(f"누적 {nav:+.2f}%")
    if perf:
        fields.append({"name": "💰 성과", "value": " · ".join(perf), "inline": False})

    # 2) vs KS11 + 귀속 분해(실행/구성)
    bench = m.get("benchmark_return_pct")
    exe = m.get("execution_gap_pct")
    comp = m.get("composition_gap_pct")
    if bench is not None and nav is not None:
        line = f"NAV {nav:+.2f}% vs KS11 {bench:+.2f}% (격차 {nav - bench:+.2f}%p)"
        fields.append({"name": "📊 vs KS11", "value": line, "inline": False})
    if exe is not None or comp is not None:
        parts = []
        if exe is not None:
            parts.append(f"실행 {exe:+.2f}%p")
        if comp is not None:
            parts.append(f"구성 {comp:+.2f}%p")
        fields.append({
            "name": "🔎 귀속 분해",
            "value": " · ".join(parts) + " (실행=통제가능 / 구성=설계수용)",
            "inline": False,
        })

    # 3) 일정 대비 — 진행률·커버리지
    progress_days = eval_result.get("progress_days") if eval_result else None
    min_days = eval_result.get("min_trading_days") if eval_result else None
    coverage = eval_result.get("snapshot_coverage") if eval_result else None
    if progress_days is not None and min_days:
        cov = f" · 커버리지 {coverage:.0%}" if coverage is not None else ""
        fields.append({
            "name": "📅 진행률",
            "value": f"{progress_days}/{min_days}일 ({progress_days / min_days:.0%}){cov}",
            "inline": False,
        })

    # 4) 주간 운영 이벤트 — 결측은 '고유 일수'(이벤트 재경보로 부풀지 않게), 오류는 건수.
    md = int(missing_days or 0)
    ce = int(cycle_errors or 0)
    ev_line = f"결측 {md}일 · 사이클 오류 {ce}건"
    if md == 0 and ce == 0:
        ev_line += " (무사고)"
    fields.append({"name": "🛠 주간 이벤트", "value": ev_line, "inline": False})

    title = f"{icon} 주간 요약 — {basket_name} ({verdict})"
    text_lines = [title] + [f"{f['name']}: {f['value']}" for f in fields]
    return {"title": title, "fields": fields, "text": "\n".join(text_lines)}
