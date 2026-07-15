"""일일 사이클 관측성 — 사이클 이벤트 기록 + 스냅샷 결측(gap) 감지.

한 달 운영 리뷰(docs/PAPER_MONTH1_REVIEW_AND_PLAN.md P0-1)의 배경: 2026-06-26
스냅샷 결측은 operation_events에 사이클 이벤트가 전혀 없어 '왜 빠졌는지' 사후
추적조차 불가능했다. 이 모듈은 두 가지를 제공한다.

  1) record_cycle_event: 사이클 시작/종료/실패·스냅샷 저장/스킵을 남겨, 결측이
     나도 '언제·어디서 멈췄는지' 추적 가능하게 한다(best-effort, 사이클에 영향 없음).
  2) find_snapshot_gaps: 최근 영업일 중 스냅샷이 빠진 날을 찾는 순수 함수 —
     다음 사이클이 이를 당일 경보로 노출한다(결측 당일/익일 인지).

재시도(11:00·14:00) 자체는 스케줄링 영역이다. 사이클은 멱등((account_key, date)
스냅샷 upsert, 드리프트 재평가)하므로 같은 날 재실행이 안전하다 — 결측일 재실행이
그 날 스냅샷을 채운다. 이 모듈은 재시도의 '관측·경보' 절반을 담당한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from loguru import logger


def _as_date(value: Any) -> Any:
    """datetime/date를 date로 정규화(문자열·None은 그대로)."""
    if hasattr(value, "date") and callable(getattr(value, "date")):
        return value.date()
    return value


def find_snapshot_gaps(
    trading_days: Iterable[Any],
    snapshot_dates: Iterable[Any],
) -> list[date]:
    """검사 대상 영업일 중 스냅샷이 없는 날을 정렬해 반환한다(순수 함수).

    trading_days: 검사할 영업일(date/datetime) 목록.
    snapshot_dates: 스냅샷이 존재하는 날(date/datetime) 집합/목록.
    반환: 스냅샷이 빠진 영업일(date) 오름차순 목록.
    """
    snaps = {_as_date(d) for d in snapshot_dates}
    targets = {_as_date(d) for d in trading_days}
    return sorted(d for d in targets if d not in snaps)


def format_gap_alert(basket_name: str, gaps: list[Any], *, today: Any = None) -> str:
    """결측 영업일 목록을 운영자 경보 문구로 만든다(순수 함수).

    당일(today)이 결측 목록에 있으면 '오늘 포함'을 명시해 즉시 조치를 유도한다.
    """
    gap_dates = [_as_date(g) for g in gaps]
    today_d = _as_date(today) if today is not None else None
    includes_today = today_d is not None and today_d in gap_dates
    shown = ", ".join(str(g) for g in gap_dates[-5:])  # 최근 5개만 표기
    more = f" 외 {len(gap_dates) - 5}일" if len(gap_dates) > 5 else ""
    head = f"⚠️ 바스켓 '{basket_name}' NAV 스냅샷 결측 {len(gap_dates)}일"
    tail = (
        " — 오늘 포함, 사이클 재실행 필요(커버리지 게이트 위험)"
        if includes_today
        else " — 일일 사이클 누락 의심, 재실행 권장"
    )
    return f"{head}: {shown}{more}{tail}"


def detect_snapshot_gaps_for_account(
    config: Any,
    account_key: str,
    today: Any,
    *,
    lookback_calendar_days: int = 14,
) -> list[date]:
    """최근 구간의 영업일 중 이 계정 스냅샷이 빠진 날을 반환한다(impure 수집).

    운영 시작 전(첫 스냅샷 이전) 영업일은 결측이 아니므로 제외한다 — 계정에
    스냅샷이 하나도 없으면 아직 운영 전으로 보고 빈 목록을 반환한다.
    스냅샷 저장 시도 '이후'에 호출해야 오늘이 정확히 판정된다(저장됨=정상, 스킵=결측).

    lookback 기본 14일: 명절(추석·설) 연휴+주말 클러스터(최장 ~9-10일)를 넘겨 재개해도
    직전 결측을 놓치지 않게 한다. 그보다 오래된 결측은 이 경보 계층이 아니라 승격
    게이트(전체 기간 커버리지)와 헬스 점검(장기 stale)이 담당한다.
    """
    from datetime import datetime, timedelta

    from core.trading_hours import TradingHours
    from database.models import PortfolioSnapshot, get_session

    today_d = _as_date(today)
    th = TradingHours(config)
    ledger_mode = (
        "live"
        if str(getattr(config, "trading", {}).get("mode", "paper")).lower()
        == "live"
        else "paper"
    )

    session = get_session()
    try:
        snaps = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.mode == ledger_mode,
                PortfolioSnapshot.account_key == account_key,
            )
            .all()
        )
        snap_dates = [_as_date(s.date) for s in snaps]
    finally:
        session.close()

    if not snap_dates:
        return []  # 운영 전 — gap 판정 대상 아님
    earliest = min(snap_dates)

    start = today_d - timedelta(days=lookback_calendar_days)
    trading_days: list[date] = []
    d = start
    while d <= today_d:
        if d >= earliest and th.is_trading_day(datetime(d.year, d.month, d.day)):
            trading_days.append(d)
        d += timedelta(days=1)

    return find_snapshot_gaps(trading_days, snap_dates)


def record_cycle_event(
    event_type: str,
    message: str,
    *,
    severity: str = "info",
    strategy: str | None = None,
    mode: str = "paper",
    detail: str | None = None,
) -> bool:
    """사이클 이벤트를 operation_events에 남긴다(best-effort).

    event_type 규약: CYCLE_START / CYCLE_END / CYCLE_ERROR / SNAPSHOT_SAVED /
    SNAPSHOT_SKIPPED / SNAPSHOT_GAP. 기록 실패는 사이클을 막지 않는다(로그만).
    """
    try:
        from database.models import OperationEvent, get_session

        session = get_session()
        try:
            session.add(OperationEvent(
                event_type=event_type,
                severity=severity,
                strategy=strategy,
                message=message,
                detail=detail,
                mode=mode,
            ))
            session.commit()
        finally:
            session.close()
        return True
    except Exception as e:  # 관측 실패가 운영을 막으면 안 된다
        logger.debug("사이클 이벤트 기록 실패(무시): {} {} — {}", event_type, message, e)
        return False
