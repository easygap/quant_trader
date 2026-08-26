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
      take_profit_pct: 0         # 0/미지정이면 익절 없음 (buy&hold 기본)
      trailing_stop_pct: 0       # 0/미지정이면 트레일링 없음
"""

from __future__ import annotations

from typing import Any

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

    return {
        "stop_loss_price": _level(cfg["stop_loss_pct"], -1),
        "take_profit_price": _level(cfg["take_profit_pct"], +1),
        "trailing_stop_price": _level(cfg["trailing_stop_pct"], -1),
    }


def evaluate_basket_stops(
    basket_cfg: dict[str, Any],
    positions: list[Any],
    prices: dict[str, float],
) -> list[dict[str, Any]]:
    """보유 포지션을 바스켓 정책에 비춰 평가하고 청산 대상을 반환한다.

    포지션에 저장된 stop_loss_price 컬럼을 읽지 않고 avg_price와 정책 비율로 매번
    계산한다 — 정책이 단일 진실이어야 과거에 다른 기준으로 기록된 값(전역 -3% 등)이
    남아 있어도 판단이 오염되지 않는다.

    트레일링은 포지션의 highest_price(고점)를 기준으로 한다. 고점 정보가 없으면
    진입가를 고점으로 보아 손절과 같아지므로, 그 경우 트레일링은 건너뛴다.

    반환: [{symbol, action, price, quantity, avg_price, level, reason}] — action은
    "STOP_LOSS" | "TAKE_PROFIT" | "TRAILING_STOP".
    """
    cfg = basket_risk_config(basket_cfg)
    if not any(v is not None for v in cfg.values()):
        return []

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
        if sl_pct is not None:
            level = avg_price * (1 - sl_pct)
            if price <= level:
                hits.append({
                    "symbol": symbol, "action": "STOP_LOSS", "price": float(price),
                    "quantity": quantity, "avg_price": avg_price, "level": round(level, 0),
                    "reason": (
                        f"손절: 현재가 {price:,.0f} ≤ 손절선 {level:,.0f} "
                        f"(진입가 {avg_price:,.0f} -{sl_pct:.0%})"
                    ),
                })

    return hits
