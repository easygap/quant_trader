"""성과를 '한 숫자'가 아니라 국면·리스크로 나눠 보는 렌즈. 전부 순수 함수.

왜 필요한가(2026-08-26 점검에서 드러난 사각):
방어적 포지션(주식 60% + 현금 40%)은 하락장에서 **항상** 좋아 보인다. 전체 구간
수익률 하나로 보고하면 상승장 미스가 통째로 숨는다. 실측이 그랬다 —

    전체 (6/10~8/26)   KOSPI -12.43% vs NAV -4.61%   → +7.81%p  (좋아 보임)
    반등 (8/07~8/26)   KOSPI  +8.17% vs NAV -0.80%   → -8.97%p  (숨어 있던 미스)

같은 포트폴리오, 같은 3개월이다. 두 숫자를 나란히 보고해야 '방어의 대가'가 보인다.
또 하나: daily_return이 8/10에야 복구돼서 그전엔 변동성·샤프를 아예 계산할 수 없었다
(스냅샷 61행이 전부 0.0). 이제 계산 가능해졌으니 표면화한다.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

_TRADING_DAYS = 252


def _finite(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def daily_returns_from_nav(
    nav_points: Sequence[tuple[Any, Any]],
    flows: dict | None = None,
) -> list[tuple[Any, float]]:
    """(날짜, NAV) 시계열에서 일간 수익률(%)을 계산한다. [(날짜, 수익률%), ...]

    스냅샷의 `daily_return` 열을 쓰지 않는 이유: 그 열은 2026-08-10 이전 전 구간이
    0.0이다(save_daily_snapshot이 값을 넘기지 않던 버그). 그대로 쓰면 변동성이
    0으로 깔려 '없는 안정성'을 주장하게 된다. NAV 시계열은 처음부터 온전하므로
    거기서 직접 뽑는 편이 항상 정직하다.

    flows: {날짜: 그날 유입액} — 입금은 수익이 아니므로 분모에서 중화한다.
    """
    flows = flows or {}
    out: list[tuple[Any, float]] = []
    prev_val = None
    for day, value in nav_points or []:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v) or v <= 0:
            continue
        if prev_val is not None and prev_val > 0:
            key = day.date() if hasattr(day, "date") else day
            flow = float(flows.get(key, 0) or 0)
            # 저장소 표준과 동일: r = v_now / (v_prev + flow) - 1
            # (core.portfolio_manager.twr_period_return — 입금은 구간 시작 유입으로 보고
            #  분모에서만 중화한다. 분자에서도 빼면 이중 차감이 된다.)
            denom = prev_val + flow
            if denom > 0:
                out.append((day, (v / denom - 1) * 100.0))
        prev_val = v
    return out


def aligned_returns(
    nav_points: Sequence[tuple[Any, Any]],
    bench_closes: dict,
    flows: dict | None = None,
) -> list[tuple[Any, float, float]]:
    """NAV와 벤치마크를 **같은 구간**으로 맞춰 [(날짜, 내수익률%, 벤치수익률%)]를 낸다.

    스냅샷이 하루 빠지면(휴장·PC 미가동) NAV 수익률은 이틀치 구간이 되는데 벤치마크를
    하루치로 짝지으면 기간이 어긋나 국면 분해가 통째로 왜곡된다 — 실제로 1차 구현에서
    상승 국면 벤치마크가 +149%로 나와 전체 수익률과 아귀가 안 맞았다.
    그래서 일간 수익률이 아니라 **종가 레벨**을 받아 NAV와 동일한 (이전 스냅샷 → 이번
    스냅샷) 구간으로 벤치마크 수익률을 다시 계산한다.

    양쪽 종가가 다 있는 구간만 반환한다(한쪽이 없으면 비교 불가 — 버린다).
    """
    flows = flows or {}

    def _key(d: Any):
        return d.date() if hasattr(d, "date") else d

    out: list[tuple[Any, float, float]] = []
    prev_day = prev_val = None
    for day, value in nav_points or []:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v) or v <= 0:
            continue
        if prev_val is not None and prev_val > 0:
            b_prev = bench_closes.get(_key(prev_day))
            b_cur = bench_closes.get(_key(day))
            if b_prev and b_cur and float(b_prev) > 0:
                flow = float(flows.get(_key(day), 0) or 0)
                denom = prev_val + flow
                if denom > 0:
                    mine = (v / denom - 1) * 100.0
                    bench = (float(b_cur) / float(b_prev) - 1) * 100.0
                    out.append((day, mine, bench))
        prev_day, prev_val = day, v
    return out


def risk_metrics(daily_returns_pct: Sequence[Any]) -> dict[str, Any]:
    """일간 수익률(%) 시계열에서 변동성·샤프·하락일 비율을 낸다.

    표본이 2개 미만이면 계산 가능한 항목만 채우고 나머지는 None으로 둔다 — 없는 값을
    0으로 채우면 '변동성 0'처럼 읽혀 없는 안정성을 주장하게 된다.

    무위험수익률은 0으로 둔다(연 단위 환산만 하는 상대 지표로 쓴다).
    """
    rs = _finite(daily_returns_pct)
    n = len(rs)
    base: dict[str, Any] = {
        "samples": n,
        "mean_daily_pct": None,
        "vol_daily_pct": None,
        "vol_annual_pct": None,
        "sharpe_annual": None,
        "down_day_ratio": None,
        "worst_day_pct": None,
        "best_day_pct": None,
    }
    if n == 0:
        return base

    base["mean_daily_pct"] = sum(rs) / n
    base["worst_day_pct"] = min(rs)
    base["best_day_pct"] = max(rs)
    base["down_day_ratio"] = sum(1 for r in rs if r < 0) / n
    if n < 2:
        return base

    mean = base["mean_daily_pct"]
    var = sum((r - mean) ** 2 for r in rs) / (n - 1)
    sd = math.sqrt(var)
    base["vol_daily_pct"] = sd
    ann_vol = sd * math.sqrt(_TRADING_DAYS)
    base["vol_annual_pct"] = ann_vol
    if ann_vol > 0:
        base["sharpe_annual"] = (mean * _TRADING_DAYS) / ann_vol
    return base


def split_by_regime(
    pairs: Sequence[tuple[Any, Any]],
) -> dict[str, dict[str, Any]]:
    """(내 일간수익률%, 벤치마크 일간수익률%) 쌍을 벤치마크 부호로 갈라 집계한다.

    상승 국면(벤치 > 0)과 하락 국면(벤치 < 0)에서 각각 얼마나 따라갔는지를 본다.
    벤치가 정확히 0인 날은 어느 쪽도 아니므로 제외한다.

    각 국면 반환:
      days           그 국면 일수
      mine_pct       그 국면 구간들만 이어 붙인 복리 수익률
      bench_pct      같은 구간 벤치마크 복리 수익률
      gap_pct        mine - bench
      capture        벤치 대비 포착률(bench_pct가 0이 아닐 때). 상승 1.0=완전 추종,
                     하락 1.0=완전 노출(낮을수록 방어). 방향 해석은 호출부가 한다.
    """
    up_mine: list[float] = []
    up_bench: list[float] = []
    down_mine: list[float] = []
    down_bench: list[float] = []

    for mine, bench in pairs or []:
        try:
            m = float(mine)
            b = float(bench)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(m) and math.isfinite(b)) or b == 0:
            continue
        if b > 0:
            up_mine.append(m)
            up_bench.append(b)
        else:
            down_mine.append(m)
            down_bench.append(b)

    def _compound(rs: list[float]) -> float:
        """일간 수익률(%)을 복리로 누적한다.

        단순 합산하면 안 된다 — 변동성이 큰 시장에서 31일을 더하면 +94% 같은 숫자가
        나온다(2026-08-26 1차 구현의 실제 오류). 국면 수익률은 그 국면의 날들만
        이어 붙인 복리 수익률로 정의한다.
        """
        acc = 1.0
        for r in rs:
            acc *= 1 + r / 100.0
        return (acc - 1) * 100.0

    def _agg(mine: list[float], bench: list[float]) -> dict[str, Any]:
        if not bench:
            return {
                "days": 0, "mine_pct": None, "bench_pct": None,
                "gap_pct": None, "capture": None,
            }
        ms, bs = _compound(mine), _compound(bench)
        return {
            "days": len(bench),
            "mine_pct": ms,
            "bench_pct": bs,
            "gap_pct": ms - bs,
            "capture": (ms / bs) if bs != 0 else None,
        }

    return {"up": _agg(up_mine, up_bench), "down": _agg(down_mine, down_bench)}


def format_regime_line(regime: dict[str, dict[str, Any]]) -> str:
    """국면 분해를 한 줄 요약으로. 값이 없으면 그 국면은 생략한다."""
    parts: list[str] = []
    # 포착률을 앞세운다 — 이게 행동을 바꾸는 숫자다. 상승 포착이 낮으면 '방어의 대가'가
    # 크다는 뜻이고, 하락 포착이 높으면 방어가 실제로 작동하지 않는다는 뜻이다.
    for key, label, verb in (
        ("up", "상승", "따라감"),
        ("down", "하락", "맞음"),
    ):
        r = regime.get(key) or {}
        if not r.get("days"):
            continue
        cap = r.get("capture")
        if cap is None:
            parts.append(f"{label} {r['days']}일: 지수 무변동")
            continue
        parts.append(
            f"{label} {r['days']}일 포착 {cap * 100:.0f}% "
            f"(지수 {r['bench_pct']:+.1f}% 중 {r['mine_pct']:+.1f}%만 {verb})"
        )
    return " · ".join(parts) if parts else "국면 분해 불가(표본 부족)"


def format_risk_line(metrics: dict[str, Any]) -> str:
    """리스크 지표를 한 줄 요약으로. 표본 부족이면 그렇다고 말한다."""
    n = metrics.get("samples") or 0
    if n < 2:
        return f"표본 {n}일 — 변동성 산출 불가"
    parts = [f"연변동성 {metrics['vol_annual_pct']:.1f}%"]
    if metrics.get("sharpe_annual") is not None:
        parts.append(f"샤프 {metrics['sharpe_annual']:+.2f}")
    if metrics.get("down_day_ratio") is not None:
        parts.append(f"하락일 {metrics['down_day_ratio'] * 100:.0f}%")
    if metrics.get("worst_day_pct") is not None:
        parts.append(f"최악일 {metrics['worst_day_pct']:+.2f}%")
    return " · ".join(parts) + f" (표본 {n}일)"
