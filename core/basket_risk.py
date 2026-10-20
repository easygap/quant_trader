"""바스켓 트랙별 리스크 정책 — 손절/익절 기준을 트랙 성격에 맞게 분리한다.

배경(2026-08-07 점검):
전역 risk_params는 단타 신호 전략 기준이다 — 진입가 -3% 손절, +8% 익절, 고점 -5%
트레일링. 이 값이 저회전 buy&hold 바스켓 포지션에도 그대로 기록돼 왔는데,

  1) 일일 리밸런싱 사이클은 손절/익절을 평가하지 않는다(평가는 core/scheduler.py의
     장중 루프에만 있다). 그래서 9개 중 6개 포지션이 손절선을 뚫은 채 방치됐다.
  2) 그렇다고 그 -3% 손절을 그대로 켰다면 대형주 정상 변동에도 전 종목이 털려
     하락을 손실로 확정했을 것이다. 애초에 이 트랙에 맞는 숫자가 아니다.

즉 문제는 '손절이 꺼져 있다'가 아니라 '트랙에 맞지 않는 숫자가 장부에만 적혀 있다'
였다. 이 모듈은 바스켓별로 의도한 기준을 명시(baskets.yaml의 `risk:` 블록)하게 하고,
그 기준으로 진입 레벨을 기록하고 일일 사이클이 실제로 평가하게 만든다.

baskets.yaml 예:
    risk:
      stop_loss_pct: 0.20        # 진입가 대비 -20%에서 청산 (0/미지정이면 손절 없음)
      stop_basis: relative       # absolute(기본) | relative — 같은 기간 시장 대비로 잴지
      stop_benchmark: "069500"   # relative일 때 비교할 종목 (기본 KODEX 200)
      take_profit_pct: 0         # 0/미지정이면 익절 없음 (buy&hold 기본)
      trailing_stop_pct: 0       # 0/미지정이면 트레일링 없음
      account_drawdown_guard: delegate  # block(기본) | delegate — 계좌 낙폭 매수 차단을 맡길지

상대 손절(2026-10-20): 평균 매입가 대비 -25% 손절은 시장 전체가 빠질 때 연달아 걸린다.
10/15 삼성전자·현대차가 그렇게 팔렸는데 그 기간 KODEX 200도 -30% 가까이 빠져 있었다.
'한 종목이 포트폴리오를 망가뜨리는 것을 막는다'는 원래 목적에 맞게 같은 보유 기간의
시장 수익률로 나눠서 잰다. 2014-07~2026-10 비교는 docs/HOLD_RISK_REVIEW_20261020.md.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable

from loguru import logger


def _pct(risk_cfg: dict[str, Any], key: str) -> float | None:
    """비율 파라미터를 (0, 1) 범위로 읽는다. 미지정·0·범위 밖이면 None(장치 없음)."""
    raw = (risk_cfg or {}).get(key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("바스켓 리스크 설정 무시 — {}={!r}는 숫자가 아님", key, raw)
        return None
    if not (0 < value < 1):
        if value != 0:
            logger.warning(
                "바스켓 리스크 설정 무시 — {}={}는 (0,1) 범위를 벗어남", key, value,
            )
        return None
    return value


# 리스크 청산 사유에 새겨 넣는 표지. 재진입 차단이 이 표지로 청산 이력을 찾는다.
RISK_EXIT_TAG = "RISK_EXIT"
_REENTRY_BLOCKING_ACTIONS = ("STOP_LOSS", "TRAILING_STOP")


def reentry_cooldown_days(basket_cfg: dict[str, Any]) -> int:
    """손절/트레일링으로 청산한 종목의 재매수 차단 일수. 0이면 차단 없음.

    이 값이 0이면 손절이 무의미해진다: 같은 사이클의 리밸런싱이 방금 비운 슬롯을
    '비중 0% → 목표 부족'으로 보고 즉시 되사기 때문이다. 실측 사고(2026-08-07 10:07):
    현대차를 -25% 손절로 394,500원에 판 4초 뒤 395,500원에 되샀다 — 손실 -207,349원만
    확정하고 노출·비중은 그대로인 왕복매매였다.
    """
    raw = ((basket_cfg or {}).get("risk", {}) or {}).get("reentry_cooldown_days")
    if raw is None:
        return 0
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning("reentry_cooldown_days={!r}는 정수가 아님 — 차단 없음으로 처리", raw)
        return 0
    return max(0, days)


def symbols_in_reentry_cooldown(
    basket_cfg: dict[str, Any],
    account_key: str,
    mode: str,
    now=None,
) -> dict[str, str]:
    """재매수가 차단된 종목 → 사유 문자열.

    최근 reentry_cooldown_days 안에 손절/트레일링으로 청산된 종목을 반환한다.
    조회 실패 시 빈 dict(차단 없음)로 폴백한다 — 관측 실패가 리밸런싱 전체를
    막지는 않게 하되, 경고는 남긴다.
    """
    days = reentry_cooldown_days(basket_cfg)
    if days <= 0:
        return {}

    from datetime import datetime, timedelta

    from database.repositories import get_trade_history

    current = now or datetime.now()
    since = current - timedelta(days=days)
    try:
        trades = get_trade_history(
            mode=mode, start_date=since, account_key=account_key,
        )
    except Exception as exc:
        logger.warning("재진입 차단 조회 실패 — 차단 없이 진행: {}", exc)
        return {}

    blocked: dict[str, str] = {}
    for t in trades:
        if str(getattr(t, "action", "")).upper() != "SELL":
            continue
        reason = str(getattr(t, "reason", "") or "")
        if not any(tag in reason for tag in _REENTRY_BLOCKING_ACTIONS):
            continue
        symbol = str(getattr(t, "symbol", "") or "")
        if not symbol or symbol in blocked:
            continue
        executed_at = getattr(t, "executed_at", None)
        until = (
            (executed_at + timedelta(days=days)).strftime("%Y-%m-%d")
            if executed_at else "?"
        )
        blocked[symbol] = f"{days}일 재진입 차단 (~{until}) — 직전 리스크 청산"
    return blocked


_STOP_BASES = ("absolute", "relative")
DEFAULT_STOP_BENCHMARK = "069500"
_ACCOUNT_GUARD_MODES = ("block", "delegate")


def stop_basis(basket_cfg: dict[str, Any]) -> tuple[str, str | None]:
    """손절을 무엇에 비춰 재는가 — (기준, 비교 종목).

    absolute(기본): 평균 매입가 대비 하락률.
    relative: 같은 보유 기간 비교 종목(기본 069500) 수익률 대비 초과 하락률. 시장 전체가
      내릴 때는 걸리지 않고 시장보다 손절 비율만큼 더 빠진 종목만 판다.
    모르는 값은 기존 동작(absolute)으로 처리하고 경고를 남긴다.
    """
    risk_cfg = (basket_cfg or {}).get("risk") or {}
    raw = str(risk_cfg.get("stop_basis") or "absolute").strip().lower()
    if raw not in _STOP_BASES:
        logger.warning("바스켓 손절 기준 {!r}을 알 수 없어 absolute로 처리", raw)
        raw = "absolute"
    if raw != "relative":
        return "absolute", None
    bench = str(risk_cfg.get("stop_benchmark") or DEFAULT_STOP_BENCHMARK).strip()
    return "relative", bench or DEFAULT_STOP_BENCHMARK


def delegates_account_drawdown_guard(basket_cfg: dict[str, Any]) -> bool:
    """계좌 단위 낙폭·일일 손실 매수 차단을 이 바스켓의 규칙에 맡기는가.

    - overlays.drawdown_guard가 켜져 있으면 낙폭이 커질 때 목표 주식 비중 자체가 줄어든다
      (kr_pocket). 그 바스켓이 낙폭 정책을 갖고 있으니 계좌 가드를 맡긴다.
    - risk.account_drawdown_guard: delegate는 낙폭과 상관없이 선언한 주식 비중까지만
      되사겠다는 결정이다. 주문 단계의 노출 상한(설계 비중 + 밴드)은 그대로 검사한다.
      전역 가드에 막히면 손절·비중 초과 매도로 생긴 현금이 돌아가지 못해 비중이 한쪽으로만
      줄어든다(2026-10: 설계 60%가 45%로, 2014~2026 비교에서 연 0.3~1.0%p 손해).
    둘 다 아니면 전역 가드가 매수를 막는다(기존 동작).
    """
    cfg = basket_cfg or {}
    overlay = ((cfg.get("overlays") or {}).get("drawdown_guard") or {})
    if bool(overlay.get("enabled")):
        return True
    raw = str(((cfg.get("risk") or {}).get("account_drawdown_guard") or "block")).strip().lower()
    if raw not in _ACCOUNT_GUARD_MODES:
        logger.warning("account_drawdown_guard={!r}를 알 수 없어 block으로 처리", raw)
        return False
    return raw == "delegate"


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def relative_stop_basis(
    trades: Iterable[Any],
    benchmark_closes: dict[Any, float],
    *,
    max_gap_days: int = 7,
) -> dict[str, dict[str, Any]]:
    """현재 보유 구간의 매수마다 그날 비교 종목 종가를 수량 가중 평균한다.

    평균 매입가와 같은 규칙(매도는 평균을 바꾸지 않고, 수량이 0이 되면 새 보유 구간)을
    써야 '종목 수익률 ÷ 같은 기간 시장 수익률'이 평균 매입가 기준 손익과 짝이 맞는다.
    매수일 종가가 없으면 그 전 max_gap_days 안의 마지막 종가를 쓰고, 그것도 없으면
    basis를 None으로 둔다(호출부는 절대 기준으로 대신 판단한다).

    반환 {symbol: {"basis": float | None, "quantity": int, "missing": [날짜 문자열]}}
    """
    closes: dict[date, float] = {}
    for key, value in (benchmark_closes or {}).items():
        day = _as_date(key)
        try:
            close = float(value)
        except (TypeError, ValueError):
            continue
        if day is not None and math.isfinite(close) and close > 0:
            closes[day] = close
    ordered_days = sorted(closes)

    def close_on(day: date) -> float | None:
        if day in closes:
            return closes[day]
        earlier = [d for d in ordered_days if d < day and (day - d).days <= max_gap_days]
        return closes[earlier[-1]] if earlier else None

    def _when(trade):
        return getattr(trade, "executed_at", None) or datetime.min

    state: dict[str, dict[str, Any]] = {}
    for trade in sorted(list(trades or []), key=_when):
        symbol = str(getattr(trade, "symbol", "") or "")
        action = str(getattr(trade, "action", "") or "").upper()
        try:
            qty = int(getattr(trade, "quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        if not symbol or qty <= 0 or action not in ("BUY", "SELL"):
            continue
        entry = state.setdefault(symbol, {"basis": 0.0, "quantity": 0, "missing": []})
        if action == "SELL":
            entry["quantity"] -= qty
            if entry["quantity"] <= 0:
                state[symbol] = {"basis": 0.0, "quantity": 0, "missing": []}
            continue
        day = _as_date(_when(trade))
        close = close_on(day) if day else None
        if close is None:
            entry["missing"].append(str(day))
        elif entry["basis"] is not None:
            held = entry["quantity"]
            entry["basis"] = (entry["basis"] * held + close * qty) / (held + qty)
        entry["quantity"] += qty
        if entry["missing"]:
            entry["basis"] = None
    return {s: e for s, e in state.items() if e["quantity"] > 0}


def basket_risk_config(basket_cfg: dict[str, Any]) -> dict[str, float | None]:
    """바스켓의 리스크 정책을 정규화해 반환한다.

    반환: {"stop_loss_pct", "take_profit_pct", "trailing_stop_pct"} — 각 값은
    비율(float) 또는 None(해당 장치 없음).
    """
    risk_cfg = (basket_cfg or {}).get("risk", {}) or {}
    return {
        "stop_loss_pct": _pct(risk_cfg, "stop_loss_pct"),
        "take_profit_pct": _pct(risk_cfg, "take_profit_pct"),
        "trailing_stop_pct": _pct(risk_cfg, "trailing_stop_pct"),
    }


def has_risk_policy(basket_cfg: dict[str, Any]) -> bool:
    """바스켓이 `risk:` 블록을 명시했는지 여부.

    값이 전부 0이어도 True다 — '손절 없음'은 누락이 아니라 결정이며, 그 결정과
    '아무 말도 안 했으니 전역 단타 기본값(-3%)을 쓴다'는 정반대 결과를 낳는다.
    """
    return isinstance((basket_cfg or {}).get("risk"), dict)


def basket_risk_levels(
    basket_cfg: dict[str, Any], entry_price: float,
) -> dict[str, float | None] | None:
    """진입가 기준 손절/익절/트레일링 가격. `risk:` 블록이 없으면 None.

    None을 반환하면 호출부는 '이 바스켓은 리스크 레벨을 지정하지 않음'으로 보고
    전역 risk_params 기본값을 쓴다(기존 동작 유지). 블록이 있으면 dict를 반환하며,
    그 안의 None은 '그 장치는 이 트랙에 없음'을 뜻한다.
    """
    if not has_risk_policy(basket_cfg):
        return None
    cfg = basket_risk_config(basket_cfg)

    # 진입가가 유효하지 않으면 레벨을 계산할 수 없다. 이때 None을 반환해 전역
    # 기본값으로 되돌아가면 정책이 '손절 없음'인 트랙에 단타 손절이 다시 적힌다 —
    # 정책은 유지한 채 레벨만 비운다(기록 없음).
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        entry = 0.0
    if not entry > 0:
        return {
            "stop_loss_price": None,
            "take_profit_price": None,
            "trailing_stop_price": None,
        }

    def _level(pct: float | None, direction: int) -> float | None:
        if pct is None:
            return None
        return round(entry * (1 + direction * pct), 0)

    # 상대 손절은 시장이 움직이면 손절 가격도 움직인다. 고정 가격을 적어 두면 장부에는
    # 절대 기준처럼 보이므로 비워 둔다(판단은 evaluate_basket_stops가 매번 다시 한다).
    relative = stop_basis(basket_cfg)[0] == "relative"
    return {
        "stop_loss_price": None if relative else _level(cfg["stop_loss_pct"], -1),
        "take_profit_price": _level(cfg["take_profit_pct"], +1),
        "trailing_stop_price": _level(cfg["trailing_stop_pct"], -1),
    }


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def evaluate_basket_stops(
    basket_cfg: dict[str, Any],
    positions: list[Any],
    prices: dict[str, float],
    relative_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """보유 포지션을 바스켓 정책에 비춰 평가하고 청산 대상을 반환한다.

    포지션에 저장된 stop_loss_price 컬럼을 읽지 않고 avg_price와 정책 비율로 매번
    계산한다 — 정책이 단일 진실이어야 과거에 다른 기준으로 기록된 값(전역 -3% 등)이
    남아 있어도 판단이 오염되지 않는다.

    트레일링은 포지션의 highest_price(고점)를 기준으로 한다. 고점 정보가 없으면
    진입가를 고점으로 보아 손절과 같아지므로, 그 경우 트레일링은 건너뛴다.

    relative_context: stop_basis가 relative일 때 {"benchmark", "price"(지금 비교 종목
    가격), "basis"({종목: 매입 시점 비교 종목 평균})}. 비교 자료가 없는 종목은 절대
    기준으로 대신 판단한다 — 자료 문제로 큰 개별 손실 방어까지 꺼지지 않게 한다.

    반환: [{symbol, action, price, quantity, avg_price, level, reason}] — action은
    "STOP_LOSS" | "TAKE_PROFIT" | "TRAILING_STOP".
    """
    cfg = basket_risk_config(basket_cfg)
    if not any(v is not None for v in cfg.values()):
        return []
    basis_kind, benchmark = stop_basis(basket_cfg)
    ctx = relative_context or {}
    bench_now = _positive(ctx.get("price"))
    bench_basis = ctx.get("basis") or {}

    hits: list[dict[str, Any]] = []
    for pos in positions or []:
        symbol = str(getattr(pos, "symbol", "") or "")
        price = prices.get(symbol)
        avg_price = float(getattr(pos, "avg_price", 0) or 0)
        quantity = int(getattr(pos, "quantity", 0) or 0)
        if not symbol or not price or price <= 0 or avg_price <= 0 or quantity <= 0:
            continue

        # 익절 → 트레일링 → 손절 순. 같은 가격이 여러 조건에 걸리면 이익 실현을
        # 우선한다(core/order_executor.check_stop_loss_take_profit와 같은 순서).
        tp_pct = cfg["take_profit_pct"]
        if tp_pct is not None:
            level = avg_price * (1 + tp_pct)
            if price >= level:
                hits.append({
                    "symbol": symbol, "action": "TAKE_PROFIT", "price": float(price),
                    "quantity": quantity, "avg_price": avg_price, "level": round(level, 0),
                    "reason": f"익절 도달: 현재가 {price:,.0f} ≥ 목표가 {level:,.0f} (+{tp_pct:.0%})",
                })
                continue

        ts_pct = cfg["trailing_stop_pct"]
        highest = float(getattr(pos, "highest_price", 0) or 0)
        if ts_pct is not None and highest > avg_price:
            level = highest * (1 - ts_pct)
            if price <= level:
                hits.append({
                    "symbol": symbol, "action": "TRAILING_STOP", "price": float(price),
                    "quantity": quantity, "avg_price": avg_price, "level": round(level, 0),
                    "reason": (
                        f"트레일링 스탑: 현재가 {price:,.0f} ≤ 고점 {highest:,.0f} "
                        f"대비 -{ts_pct:.0%} ({level:,.0f})"
                    ),
                })
                continue

        sl_pct = cfg["stop_loss_pct"]
        if sl_pct is None:
            continue
        note = ""
        if basis_kind == "relative":
            if bench_now is None:
                # 지금 시장 수준을 모르면 상대 손절을 판단할 수 없다. 절대 기준으로 대신
                # 팔면 시장 전체 하락에 손절이 몰리는 바로 그 일을 자료 장애가 만든다 —
                # 오늘은 판단을 보류하고(다음 사이클에 다시 본다) 호출부가 사유를 남긴다.
                continue
            start = _positive(bench_basis.get(symbol))
            if start is not None:
                own = price / avg_price - 1.0
                market = bench_now / start - 1.0
                relative = (1.0 + own) / (1.0 + market) - 1.0
                if relative <= -sl_pct:
                    # 지금 시장 수준에서 상대 손절이 걸리는 가격(참고용)
                    level = avg_price * (1 - sl_pct) * (bench_now / start)
                    hits.append({
                        "symbol": symbol, "action": "STOP_LOSS", "price": float(price),
                        "quantity": quantity, "avg_price": avg_price, "level": round(level, 0),
                        "basis": "relative", "relative_return": round(relative, 4),
                        "reason": (
                            f"시장 대비 손절: 종목 {own:+.1%} · {benchmark} {market:+.1%} "
                            f"→ 상대 {relative:+.1%} ≤ -{sl_pct:.0%} (진입가 {avg_price:,.0f})"
                        ),
                    })
                continue
            # 매수 기록과 보유 수량이 맞지 않는 등 이 종목의 매입 시점 시장 수준만 모르는
            # 경우다. 종목 하나의 문제라 절대 기준으로 대신 판단한다.
            note = f" — 매입 시점 {benchmark} 자료가 없어 절대 기준으로 판단"
            logger.warning("바스켓 상대 손절: {} 매입 시점 비교 자료 없음 — 절대 기준으로 판단", symbol)
        level = avg_price * (1 - sl_pct)
        if price <= level:
            hits.append({
                "symbol": symbol, "action": "STOP_LOSS", "price": float(price),
                "quantity": quantity, "avg_price": avg_price, "level": round(level, 0),
                "basis": "absolute",
                "reason": (
                    f"손절: 현재가 {price:,.0f} ≤ 손절선 {level:,.0f} "
                    f"(진입가 {avg_price:,.0f} -{sl_pct:.0%}){note}"
                ),
            })

    return hits
