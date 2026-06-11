"""바스켓 paper 운영 평가 — "높은 수익을 안정적으로"의 구체 기준과 자동 판정.

수익성 검증의 정착된 결론(docs/PROFITABILITY_FINDINGS.md): broad KOSPI 대형주에서
단순 알파는 EW buy&hold를 이기지 못한다. 따라서 바스켓(베타 전략)의 성공 기준은
"시장을 이겼는가"가 아니라 **"벤치마크(베타)를 비용 최소로, 운영 사고 없이,
안정적으로 추종했는가"**다. 시장 수익률 자체는 통제 변수가 아니다.

paper → live 승격 판정 기준 (자세한 근거: docs/BASKET_PAPER_EVALUATION.md):
  D. 데이터 충분성: 운영 영업일 ≥ min_trading_days(기본 60). 미달이면 WAIT.
  A. 운영 무결성:
     - NAV 스냅샷 커버리지 ≥ 95% (운영 기간 KRX 영업일 대비 — 일일 사이클 생존 증명)
     - 미해결 실패 주문(dead-letter pending) 0건
  B. 실행 품질: 누적 거래비용(수수료+세금+슬리피지) 연환산 ≤ 1.0% (초기자본 대비)
     — 초기 매입 비용은 일회성이라 연환산은 기간 충족 시점에만 판정한다.
  C. 성과 기록(판정 기준 아님, 참고): NAV 누적수익률 / 같은 기간 KS11 수익률.

verdict:
  WAIT           — 기간 미충족(진행률·중간 이슈 표기). 이슈가 있어도 기간 전엔 WAIT.
  PASS_CANDIDATE — 기간 충족 + A·B 모두 충족. live 전환은 운영자 최종 승인 +
                   별도 live gate(basket_rebalance:<name>)를 다시 통과해야 한다.
  FAIL_REVIEW    — 기간 충족했으나 A 또는 B 미충족. 원인 검토 후 재운영.

판정(evaluate_basket_paper_operation)은 순수 함수로 외부 상태를 읽지 않는다
(테스트 용이). DB·설정에서 입력을 모으는 수집(collect_basket_paper_evaluation)은
별도 함수로 분리돼 있고, CLI(tools/basket_paper_evaluation.py)와 바스켓 live gate
(core/live_readiness.py)가 같은 수집·판정 경로를 공유한다.
"""

from __future__ import annotations

from typing import Any

TRADING_DAYS_PER_YEAR = 252


def evaluate_basket_paper_operation(
    *,
    operation_start: Any,
    today: Any,
    trading_days_total: int,
    snapshot_days: int,
    pending_failed_orders: int,
    total_costs: float,
    initial_capital: float,
    nav_return_pct: float | None = None,
    benchmark_return_pct: float | None = None,
    min_trading_days: int = 60,
    min_snapshot_coverage: float = 0.95,
    max_annual_cost_drag: float = 0.01,
) -> dict[str, Any]:
    """바스켓 paper 운영 데이터를 승격 판정으로 환원한다. 반환 dict 키:

    verdict, progress_days, progress_pct, snapshot_coverage, cost_drag_cum,
    cost_drag_annualized, issues(list), metrics(dict — 참고 지표)
    """
    trading_days_total = max(int(trading_days_total), 0)
    snapshot_days = max(int(snapshot_days), 0)
    issues: list[str] = []

    # A. 운영 무결성
    coverage = (snapshot_days / trading_days_total) if trading_days_total > 0 else 0.0
    if trading_days_total > 0 and coverage < min_snapshot_coverage:
        issues.append(
            f"스냅샷 커버리지 {coverage:.0%} < {min_snapshot_coverage:.0%} "
            f"({snapshot_days}/{trading_days_total} 영업일) — 일일 사이클 누락"
        )
    if pending_failed_orders > 0:
        issues.append(f"미해결 실패 주문 {pending_failed_orders}건 (dead-letter)")

    # B. 실행 품질 (연환산은 기간 충족 시점에만 판정 — 초기 매입 비용 왜곡 방지)
    cost_cum = (total_costs / initial_capital) if initial_capital > 0 else 0.0
    cost_annualized = (
        cost_cum * (TRADING_DAYS_PER_YEAR / trading_days_total)
        if trading_days_total > 0 else 0.0
    )
    period_complete = trading_days_total >= min_trading_days
    if period_complete and cost_annualized > max_annual_cost_drag:
        issues.append(
            f"비용 드래그 연환산 {cost_annualized:.2%} > {max_annual_cost_drag:.2%} — 회전/슬리피지 검토"
        )

    if not period_complete:
        verdict = "WAIT"
    elif issues:
        verdict = "FAIL_REVIEW"
    else:
        verdict = "PASS_CANDIDATE"

    return {
        "verdict": verdict,
        "operation_start": str(operation_start),
        "today": str(today),
        "progress_days": trading_days_total,
        "min_trading_days": min_trading_days,
        "progress_pct": min(1.0, trading_days_total / min_trading_days) if min_trading_days > 0 else 1.0,
        "snapshot_coverage": round(coverage, 4),
        "cost_drag_cum": round(cost_cum, 6),
        "cost_drag_annualized": round(cost_annualized, 6),
        "issues": issues,
        "metrics": {
            "nav_return_pct": nav_return_pct,
            "benchmark_return_pct": benchmark_return_pct,
            "pending_failed_orders": pending_failed_orders,
            "total_costs": round(float(total_costs), 2),
            "initial_capital": initial_capital,
        },
    }


def format_evaluation_report(result: dict[str, Any], basket_name: str = "") -> str:
    """평가 결과를 사람이 읽는 텍스트로 포맷한다."""
    icon = {"WAIT": "⏳", "PASS_CANDIDATE": "✅", "FAIL_REVIEW": "❌"}.get(result["verdict"], "❓")
    m = result["metrics"]
    lines = [
        "=" * 60,
        f"  {icon} 바스켓 paper 운영 평가{f' — {basket_name}' if basket_name else ''}",
        "=" * 60,
        f"  판정: {result['verdict']}"
        + (
            f" (진행 {result['progress_days']}/{result['min_trading_days']} 영업일,"
            f" {result['progress_pct']:.0%})" if result["verdict"] == "WAIT" else ""
        ),
        f"  운영 기간: {result['operation_start']} ~ {result['today']}",
        f"  스냅샷 커버리지: {result['snapshot_coverage']:.0%}",
        f"  비용 드래그: 누적 {result['cost_drag_cum']:.4%} | 연환산 {result['cost_drag_annualized']:.4%}"
        + (
            " (기간 미충족 — 초기 매입 일회성 비용이 과장되므로 판정 미적용)"
            if result["verdict"] == "WAIT" else ""
        ),
    ]
    if m.get("nav_return_pct") is not None:
        bench = (
            f" | KS11 {m['benchmark_return_pct']:+.2f}%"
            if m.get("benchmark_return_pct") is not None else " | KS11 조회 불가"
        )
        lines.append(f"  성과(참고): NAV {m['nav_return_pct']:+.2f}%{bench}")
        lines.append("    ※ 베타 전략 — '시장을 이겼는가'는 합격 기준이 아님(비용·무결성·추종이 기준)")
    if result["issues"]:
        lines.append("  이슈:")
        for issue in result["issues"]:
            lines.append(f"    - {issue}")
    if result["verdict"] == "PASS_CANDIDATE":
        lines.append("  다음: 운영자 최종 승인 + live gate(basket_rebalance:<name>) 통과 후 live 전환 가능")
    lines.append("=" * 60)
    return "\n".join(lines)


def collect_basket_paper_evaluation(
    config=None,
    min_days: int | None = None,
    include_benchmark: bool = True,
    basket_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """DB·설정에서 **특정 바스켓**의 paper 운영 데이터를 수집해 (평가결과, 바스켓 이름) 반환.

    CLI(tools/basket_paper_evaluation.py)와 바스켓 live gate가 공유하는 단일 수집 경로.
    거래·스냅샷·dead-letter 모두 바스켓 전용 키(basket_rebalance:<name>)로 필터한다 —
    바스켓별 귀속 없이 합산하면 A 바스켓의 트랙레코드로 B 바스켓이 승격되는 구멍이 생긴다.
    basket_name 미지정 시 enabled 바스켓이 정확히 1개면 그것을 쓰고, 0개·복수면
    ValueError(어느 기록을 평가하는지 모호 — fail-closed).
    min_days 미지정(None) 시 바스켓 설정 promotion.min_trading_days(기본 60)를
    사용한다 — 기간 해석의 단일 소스. CLI·게이트가 같은 값으로 판정해야
    "CLI가 보여주는 판정 = 게이트 판정" 원칙이 유지된다.
    include_benchmark=False면 KS11 조회(네트워크)를 생략한다(게이트 경로용).
    """
    from datetime import date, datetime, timedelta

    from config.config_loader import Config
    from core.basket_rebalancer import BasketRebalancer, rebalance_live_strategy_id
    from core.trading_hours import TradingHours
    from database.models import get_session, PortfolioSnapshot, TradeHistory, init_database
    from database.repositories import get_pending_failed_orders

    init_database()
    config = config or Config.get()
    if not basket_name:
        enabled = BasketRebalancer.get_enabled_baskets()
        if len(enabled) != 1:
            raise ValueError(
                f"평가 대상 바스켓이 모호합니다 (enabled={enabled}) — basket_name을 명시하세요."
            )
        basket_name = enabled[0]
    basket_key = rebalance_live_strategy_id(basket_name)

    basket_cfg = BasketRebalancer._load_baskets_config().get(basket_name) or {}
    if min_days is None:
        min_days = int((basket_cfg.get("promotion") or {}).get("min_trading_days", 60))

    session = get_session()
    try:
        trades = (
            session.query(TradeHistory)
            .filter(TradeHistory.strategy == basket_key)
            .filter(TradeHistory.mode == "paper")
            .all()
        )
        snaps = (
            session.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.account_key == basket_key)
            .order_by(PortfolioSnapshot.date.asc())
            .all()
        )
    finally:
        session.close()

    def _d(v):
        return v.date() if hasattr(v, "date") and callable(getattr(v, "date")) else v

    today = date.today()
    candidates = [_d(t.executed_at) for t in trades if getattr(t, "executed_at", None)]
    candidates += [_d(s.date) for s in snaps]
    operation_start = min(candidates) if candidates else today

    th = TradingHours(config)
    snapshot_dates = {_d(s.date) for s in snaps}
    trading_days_total = 0
    snapshot_days = 0
    d = operation_start
    while d <= today:
        if th.is_trading_day(datetime(d.year, d.month, d.day)):
            # 오늘은 스냅샷이 이미 찍힌 경우에만 분모에 포함한다 — 장전(스냅샷 저장 전)
            # 게이트 실행에서 분모만 +1 되어 커버리지가 경계에서 오판되는 것을 방지.
            if d == today and d not in snapshot_dates:
                break
            trading_days_total += 1
            if d in snapshot_dates:
                snapshot_days += 1
        d += timedelta(days=1)

    total_costs = sum(
        float(t.commission or 0) + float(t.tax or 0) + float(t.slippage or 0) for t in trades
    )
    # 자본 해석은 운영(BasketRebalancer→PortfolioManager)과 동일해야 한다 — 바스켓별
    # initial_capital(레버)이 설정됐는데 평가가 전역 자본으로 나누면 비용 드래그가
    # 수 배 과대(예: 30M 매입 비용 / 10M)되어 거짓 FAIL_REVIEW, NAV 수익률도 왜곡된다.
    # (자기검토 2라운드 HIGH — 레버와 같은 날 정합 수정)
    basket_capital = basket_cfg.get("initial_capital")
    initial_capital = float(
        basket_capital
        if basket_capital is not None
        else (
            (config.risk_params.get("position_sizing") or {}).get("initial_capital")
            or config.trading.get("initial_capital")
            or 10_000_000
        )
    )

    nav_return_pct = None
    if snaps:
        nav_return_pct = (float(snaps[-1].total_value) / initial_capital - 1.0) * 100

    benchmark_return_pct = None
    if include_benchmark:
        try:
            from core.data_collector import DataCollector
            benchmark_return_pct = DataCollector.fetch_benchmark_return(
                str(operation_start), str(today), symbol="KS11",
            )
        except Exception:
            benchmark_return_pct = None  # 참고 지표 — 실패해도 평가는 진행

    result = evaluate_basket_paper_operation(
        operation_start=operation_start,
        today=today,
        trading_days_total=trading_days_total,
        snapshot_days=snapshot_days,
        # dead-letter도 이 바스켓 계정 것만 집계 — 다른 전략의 잔여 실패 주문이
        # 바스켓 승격을 막는 오판 방지(바스켓 자신의 실패는 여전히 fail-closed).
        pending_failed_orders=len(get_pending_failed_orders(account_key=basket_key) or []),
        total_costs=total_costs,
        initial_capital=initial_capital,
        nav_return_pct=nav_return_pct,
        benchmark_return_pct=benchmark_return_pct,
        min_trading_days=min_days,
    )
    return result, basket_name
