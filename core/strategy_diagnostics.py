"""
전략 성과 진단 (장마감 / validate 공통 기준).
- 최근 N건 매도 기준 승률·평균이익/평균손실 비율
- 일자·종목별 당일 왕복(완전 청산) 횟수
- 수수료 대 실현손익 비율
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

MIN_WIN_RATE_PCT = 35.0
MIN_AVG_WIN_LOSS_RATIO = 1.0
MAX_ROUND_TRIPS_PER_SYMBOL_DAY = 1
MAX_COMMISSION_TO_PNL_RATIO = 0.5
RECENT_SELLS_LIMIT = 20
MIN_SELLS_FOR_RATE_STATS = 3


@dataclass
class DiagnosticLine:
    ok: bool
    text: str

    def formatted(self) -> str:
        emoji = "✅" if self.ok else "⚠️"
        return f"{emoji} {self.text}"


def _extract_pnl_from_reason(reason: str) -> float:
    import re

    if not reason:
        return 0.0
    match = re.search(r"PnL:\s*([\-0-9,]+)원", reason)
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def fifo_round_trips_chronological(
    items: Sequence[Tuple[str, int]],
) -> int:
    """
    (action, qty) 시계열. BUY lot을 deque로 쌓고, SELL 시 앞 lot부터 소진.
    한 BUY lot이 완전히 매도되면 왕복 1회.
    action: BUY 또는 SELL(계열 통일 upper).
    """
    lots: deque[int] = deque()
    trips = 0
    for action, qty in items:
        a = (action or "").upper()
        q = int(qty or 0)
        if q <= 0:
            continue
        if a == "BUY":
            lots.append(q)
            continue
        if (
            "SELL" in a
            or a in ("STOP_LOSS", "TAKE_PROFIT", "TAKE_PROFIT_PARTIAL", "TRAILING_STOP", "MAX_HOLD")
        ):
            rem = q
            while rem > 0 and lots:
                top = lots[0]
                if top <= rem:
                    rem -= top
                    lots.popleft()
                    trips += 1
                else:
                    lots[0] = top - rem
                    rem = 0
    return trips


def round_trips_per_symbol_today_db(trades: Sequence[Any]) -> Dict[str, int]:
    """TradeHistory 등 executed_at·symbol·action·quantity."""
    by_sym: Dict[str, List[Tuple[str, int, Any]]] = defaultdict(list)
    for t in trades:
        sym = getattr(t, "symbol", "") or ""
        ts = getattr(t, "executed_at", None) or getattr(t, "created_at", None)
        by_sym[sym].append((getattr(t, "action", "") or "", int(getattr(t, "quantity", 0) or 0), ts))
    out: Dict[str, int] = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: x[2] or datetime.min)
        seq = [(a, q) for a, q, _ in rows]
        out[sym] = fifo_round_trips_chronological(seq)
    return out


def round_trips_per_symbol_day_backtest(trades: list) -> List[Tuple[str, str, int]]:
    """[(symbol, date_key, trips), ...] 각 (종목, 거래일) 그룹별 왕복 수."""
    groups: Dict[Tuple[str, str], List[Tuple[str, int, Any]]] = defaultdict(list)
    for t in trades:
        sym = str(t.get("symbol") or "")
        d = t.get("date")
        if hasattr(d, "date"):
            dk = str(d.date())
        else:
            dk = str(d)[:10]
        a = t.get("action") or ""
        q = int(t.get("quantity") or 0)
        groups[(sym, dk)].append((a, q, d))
    triples: List[Tuple[str, str, int]] = []
    for (sym, dk), rows in groups.items():
        rows.sort(key=lambda x: x[2])
        seq = [(a, q) for a, q, _ in rows]
        trips = fifo_round_trips_chronological(seq)
        triples.append((sym, dk, trips))
    return triples


def diagnose_live_post_market(
    mode: str,
    account_key: str,
    today: Optional[datetime] = None,
) -> List[DiagnosticLine]:
    """DB 기반 장마감 진단 라인."""
    from database.repositories import (
        get_daily_trade_summary,
        get_recent_sell_trades,
        get_trade_history,
    )

    dt = today or datetime.now()
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)

    lines: List[DiagnosticLine] = []
    sells = get_recent_sell_trades(
        limit=RECENT_SELLS_LIMIT, mode=mode, account_key=account_key,
    )
    pnls = [_extract_pnl_from_reason(t.reason or "") for t in sells]

    if len(pnls) < MIN_SELLS_FOR_RATE_STATS:
        lines.append(DiagnosticLine(
            ok=True,
            text=f"최근 매도 {len(pnls)}건 — 승률·손익비 진단은 {MIN_SELLS_FOR_RATE_STATS}건 이상에서 수행",
        ))
    else:
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) * 100.0
        if win_rate < MIN_WIN_RATE_PCT:
            lines.append(DiagnosticLine(
                ok=False,
                text=f"최근 {len(pnls)}건 승률 {win_rate:.1f}% < {MIN_WIN_RATE_PCT:.0f}%",
            ))
        else:
            lines.append(DiagnosticLine(
                ok=True,
                text=f"최근 {len(pnls)}건 승률 {win_rate:.1f}% ≥ {MIN_WIN_RATE_PCT:.0f}%",
            ))

        w_vals = [p for p in pnls if p > 0]
        l_vals = [p for p in pnls if p < 0]
        avg_win = sum(w_vals) / len(w_vals) if w_vals else 0.0
        avg_loss = abs(sum(l_vals) / len(l_vals)) if l_vals else 0.0
        if not l_vals:
            lines.append(DiagnosticLine(
                ok=True,
                text="최근 매도 중 손실 거래 없음 — 평균 손익비 진단 생략",
            ))
        elif not w_vals:
            lines.append(DiagnosticLine(
                ok=False,
                text=f"최근 {len(pnls)}건 전부 손실 — 평균수익/평균손실 구조 불량",
            ))
        else:
            ratio = avg_win / avg_loss
            if ratio < MIN_AVG_WIN_LOSS_RATIO:
                lines.append(DiagnosticLine(
                    ok=False,
                    text=f"최근 {len(pnls)}건 평균수익/평균손실 {ratio:.2f} < {MIN_AVG_WIN_LOSS_RATIO:.1f} (순손실 구조)",
                ))
            else:
                lines.append(DiagnosticLine(
                    ok=True,
                    text=f"최근 {len(pnls)}건 평균수익/평균손실 {ratio:.2f} ≥ {MIN_AVG_WIN_LOSS_RATIO:.1f}",
                ))

    day_trades = get_trade_history(
        mode=mode, start_date=start, end_date=end, account_key=account_key,
    )
    rt_map = round_trips_per_symbol_today_db(day_trades)
    bad_symbols = [s for s, n in rt_map.items() if n > MAX_ROUND_TRIPS_PER_SYMBOL_DAY]
    if bad_symbols:
        lines.append(DiagnosticLine(
            ok=False,
            text=f"당일 종목별 왕복 초과(>{MAX_ROUND_TRIPS_PER_SYMBOL_DAY}회): {', '.join(bad_symbols)}",
        ))
    else:
        lines.append(DiagnosticLine(
            ok=True,
            text=f"당일 종목별 왕복 — 모두 종목당 {MAX_ROUND_TRIPS_PER_SYMBOL_DAY}회 이하",
        ))

    ts = get_daily_trade_summary(date=dt, mode=mode, account_key=account_key)
    realized = float(ts.get("realized_pnl") or 0)
    comm = float(ts.get("total_commission") or 0)
    if realized > 0 and comm > MAX_COMMISSION_TO_PNL_RATIO * realized:
        lines.append(DiagnosticLine(
            ok=False,
            text=f"당일 수수료 {comm:,.0f}원 > 실현손익 {realized:,.0f}원의 {MAX_COMMISSION_TO_PNL_RATIO:.0%}",
        ))
    elif realized > 0:
        lines.append(DiagnosticLine(
            ok=True,
            text=f"당일 수수료/실현손익 = {comm / realized:.1%} (기준 {MAX_COMMISSION_TO_PNL_RATIO:.0%} 이하)",
        ))
    else:
        lines.append(DiagnosticLine(
            ok=True,
            text=f"당일 실현손익 {realized:,.0f}원 — 수수료 비율 진단 생략(손익≤0)",
        ))

    for line in lines:
        if not line.ok:
            logger.warning("[전략진단] {}", line.text)

    return lines


def append_backtest_diagnostic_warnings(
    validation: Dict[str, Any],
    out_sample_result: Dict[str, Any],
) -> None:
    """
    validate run()용. 손익비(profit_factor)는 _check_profit_factor_warnings에 위임 — 여기서는 승률·왕복·수수료만.
    """
    warnings: List[str] = validation.setdefault("warnings", [])
    oos = out_sample_result.get("metrics") or {}
    oos_trades = out_sample_result.get("trades") or []

    n_sell = int(oos.get("total_trades") or 0)
    wr = float(oos.get("win_rate") or 0)
    if n_sell >= MIN_SELLS_FOR_RATE_STATS and wr < MIN_WIN_RATE_PCT:
        msg = (
            f"WARN: OOS 승률 {wr:.1f}% < {MIN_WIN_RATE_PCT:.0f}% "
            f"(매도 {n_sell}건)"
        )
        warnings.append(msg)
        logger.warning(msg)

    triples = round_trips_per_symbol_day_backtest(oos_trades)
    offenders = [(s, d, n) for s, d, n in triples if n > MAX_ROUND_TRIPS_PER_SYMBOL_DAY]
    if offenders:
        sample = offenders[:5]
        detail = ", ".join(f"{s}@{d}({n}회)" for s, d, n in sample)
        msg = (
            f"WARN: OOS 일자·종목별 왕복 {MAX_ROUND_TRIPS_PER_SYMBOL_DAY}회 초과 구간 존재 — {detail}"
            + (" …" if len(offenders) > 5 else "")
        )
        warnings.append(msg)
        logger.warning(msg)

    cpr = oos.get("commission_to_profit_ratio")
    if cpr is not None and float(cpr) > MAX_COMMISSION_TO_PNL_RATIO:
        msg = (
            f"WARN: OOS 수수료/총이익 비율 {float(cpr) * 100:.1f}% "
            f"> {MAX_COMMISSION_TO_PNL_RATIO:.0%}"
        )
        warnings.append(msg)
        logger.warning(msg)


def append_walk_forward_metrics_diagnostic_warnings(windows: List[dict], wf_warnings: List[str]) -> None:
    """
    워크포워드 창별 metrics만 사용 (trades 미보관 → 일별 왕복 검사 생략).
    손익비 경고는 기존 루프와 중복하지 않음.
    """
    for w in windows:
        m = w.get("metrics")
        if not m:
            continue
        wid = w.get("window")
        tp = w.get("test_period", "")
        n = int(m.get("total_trades") or 0)
        wr = float(m.get("win_rate") or 0)
        if n >= MIN_SELLS_FOR_RATE_STATS and wr < MIN_WIN_RATE_PCT:
            msg = (
                f"WARN: 창 {wid} ({tp}) 승률 {wr:.1f}% < {MIN_WIN_RATE_PCT:.0f}% "
                f"(매도 {n}건)"
            )
            wf_warnings.append(msg)
            logger.warning(msg)
        cpr = m.get("commission_to_profit_ratio")
        if cpr is not None and float(cpr) > MAX_COMMISSION_TO_PNL_RATIO:
            msg = (
                f"WARN: 창 {wid} ({tp}) 수수료/총이익 비율 {float(cpr) * 100:.1f}% "
                f"> {MAX_COMMISSION_TO_PNL_RATIO:.0%}"
            )
            wf_warnings.append(msg)
            logger.warning(msg)
