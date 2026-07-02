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
        "snapshot_days": snapshot_days,
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
        # 성과 귀속(있을 때만): 벤치마크 격차를 실행 격차/구성 격차로 분해.
        if m.get("design_return_pct") is not None:
            exe = m.get("execution_gap_pct")
            comp = m.get("composition_gap_pct")
            lines.append(f"  귀속 분해: 설계 NAV {m['design_return_pct']:+.2f}%")
            if exe is not None:
                lines.append(f"    - 실행 격차 {exe:+.2f}%p (통제 가능: 미체결 슬롯·진입 타이밍·비용)")
            if comp is not None:
                lines.append(f"    - 구성 격차 {comp:+.2f}%p (설계 수용: 균등가중 vs 시총지수)")
    if result["issues"]:
        lines.append("  이슈:")
        for issue in result["issues"]:
            lines.append(f"    - {issue}")
    if result["verdict"] == "PASS_CANDIDATE":
        lines.append("  다음: 운영자 최종 승인 + live gate(basket_rebalance:<name>) 통과 후 live 전환 가능")
    lines.append("=" * 60)
    return "\n".join(lines)


def build_daily_report_extras(
    *,
    eval_result: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
    nav_return_pct: float | None = None,
) -> dict[str, str]:
    """일일 리포트 v2의 부가 필드(문자열)를 만든다 — 순수 함수(테스트 용이).

    한 달 운영 리뷰(docs/PAPER_MONTH1_REVIEW_AND_PLAN.md)의 결론: 기존 리포트는
    절대 수익만 있어 "시장 대비/설계 대비/일정 대비" 판단이 불가능했다. 이 함수는
    그 세 축을 한눈에 보이게 한다.

      - 시장 대비: NAV vs KS11 격차 (benchmark_gap)
      - 설계 대비: 주식 배치율, 미체결 슬롯 (deployment, slot_warning)
      - 일정 대비: 진행률·커버리지·잔여 결측 예산 (progress)
      - 실행 품질: 누적 비용 (cost)

    nav_return_pct를 명시하면 그 값으로 NAV 격차를 계산한다 — 호출부(리포트 카드)의
    '누적 수익률'과 같은 소스를 쓰게 해, 스냅샷 결측일에 카드의 '누적 수익률'(오늘 시가
    기준)과 평가의 nav(직전 스냅샷 기준)가 갈려 같은 카드에 📊 수치 두 개가 어긋나는 것을
    막는다(미지정 시 평가결과의 nav 사용).

    데이터 부재 시 해당 키를 생략한다(리포트가 조용히 축소 — 표시할 게 없으면 안 낸다).
    notifier.send_daily_report가 이 키들을 선택 필드로 렌더링한다.
    """
    extras: dict[str, str] = {}

    if eval_result:
        m = eval_result.get("metrics") or {}

        # 시장 대비 — NAV vs KS11 격차 (베타 전략이라 격차가 '판정'은 아니지만 가시화 대상)
        nav = nav_return_pct if nav_return_pct is not None else m.get("nav_return_pct")
        bench = m.get("benchmark_return_pct")
        if nav is not None:
            if bench is not None:
                extras["benchmark_gap"] = (
                    f"NAV {nav:+.2f}% vs KS11 {bench:+.2f}% (격차 {nav - bench:+.2f}%p)"
                )
            else:
                extras["benchmark_gap"] = f"NAV {nav:+.2f}% (KS11 조회 불가)"

        # 일정 대비 — 진행률·커버리지·잔여 결측 예산
        progress_days = int(eval_result.get("progress_days", 0) or 0)
        min_days_raw = eval_result.get("min_trading_days")
        min_days = int(min_days_raw) if min_days_raw not in (None, "") else 60
        snapshot_days = int(eval_result.get("snapshot_days", 0) or 0)
        coverage = float(eval_result.get("snapshot_coverage", 0.0) or 0.0)
        pct = float(eval_result.get("progress_pct", 0.0) or 0.0)
        # 최종 커버리지 95%를 지키며 앞으로 더 놓쳐도 되는 영업일 수.
        # 허용 결측은 실제 게이트와 같은 기준(운영일수의 5%)이라 기간을 넘겨 운영하면
        # 분모가 늘어난다 — max(min_days, progress_days)로 게이트와 일치시킨다.
        denom_days = max(min_days, progress_days)
        max_allowed_miss = int(denom_days * 0.05)
        already_missed = max(0, progress_days - snapshot_days)
        budget = max(0, max_allowed_miss - already_missed)
        # 표시 분모는 목표 기간(min_days) — 기간 진척을 보여준다. 0(무의미 설정)이면
        # 운영일수로 폴백해 '5/0일' 같은 문자열을 피한다.
        disp_denom = min_days if min_days > 0 else progress_days
        extras["progress"] = (
            f"{progress_days}/{disp_denom}일 ({pct:.0%}) · 커버리지 {coverage:.0%} · 결측예산 {budget}일"
        )

        # 실행 품질 — 누적 비용 (연환산은 기간 미충족 시 과장되므로 라벨로 구분)
        cum = eval_result.get("cost_drag_cum")
        ann = eval_result.get("cost_drag_annualized")
        if cum is not None:
            cost = f"누적 {cum:.3%}"
            if ann is not None:
                period_complete = progress_days >= min_days
                cost += f" · 연환산 {ann:.2%}" + ("" if period_complete else " (참고)")
            extras["cost"] = cost

    if deployment:
        actual = float(deployment.get("deployment_ratio", 0.0) or 0.0)
        design = float(deployment.get("design_fraction", 0.0) or 0.0)
        extras["deployment"] = (
            f"주식 {actual:.0%} / 설계 {design:.0%} ({(actual - design) * 100:+.1f}%p)"
        )
        slots = deployment.get("unfilled_slots") or []
        if slots:
            # Discord 필드값 1024자 한도 — 최대 3개만 명시하고 나머지는 요약.
            shown = slots[:3]
            parts = [
                f"{s.get('symbol')} 1주 {float(s.get('price', 0)):,.0f}원 > 슬롯 "
                f"{float(s.get('slot_amount', 0)):,.0f}원"
                for s in shown
            ]
            more = f" 외 {len(slots) - len(shown)}개" if len(slots) > len(shown) else ""
            extras["slot_warning"] = (
                f"미체결 {len(slots)}개: " + "; ".join(parts) + more + " — 자본 결정 대기(#422)"
            )

    return extras


def decompose_return_gap(
    nav_return_pct: float | None,
    design_return_pct: float | None,
    benchmark_return_pct: float | None,
) -> dict[str, float | None]:
    """실제 NAV 수익을 설계·벤치마크 대비로 분해한다(순수 함수).

    한 달 운영 리뷰(docs/PAPER_MONTH1_REVIEW_AND_PLAN.md §2)의 핵심 분석을 자동화한다.
      execution_gap = 실제 NAV - 설계 NAV
        → 통제 가능한 몫(미체결 슬롯·진입 타이밍·비용). 0에 가까워야 정상.
      composition_gap = 설계 NAV - 벤치마크
        → 설계가 수용한 변동(균등가중 vs 시총 편중 지수). 전략 성격이지 사고가 아님.
      total_gap = 실제 NAV - 벤치마크 (= execution + composition)
    입력 중 None이 있으면 해당 격차는 None.
    """
    def _sub(a: float | None, b: float | None) -> float | None:
        return (a - b) if (a is not None and b is not None) else None

    return {
        "execution_gap_pct": _sub(nav_return_pct, design_return_pct),
        "composition_gap_pct": _sub(design_return_pct, benchmark_return_pct),
        "total_gap_pct": _sub(nav_return_pct, benchmark_return_pct),
    }


def compute_design_portfolio_return(
    holdings: dict[str, float],
    design_stock_fraction: float,
    operation_start: Any,
    today: Any,
    *,
    fetch: Any = None,
) -> float | None:
    """설계 포트폴리오(목표비중 buy&hold, 주식 비중 design_stock_fraction)의 구간 수익%(단위: %).

    각 종목의 구간 수익률(%)을 fetch(start, end, symbol=...)로 얻어 목표비중 가중평균한 뒤
    주식 비중을 곱한다(현금 슬리브는 0% 수익). 조회 실패 종목은 제외하고 나머지 비중으로
    재정규화한다 — 실행(실제 NAV)과의 격차 분해에서 '설계 NAV' 기준선으로 쓴다.
    fetch 기본값은 DataCollector.fetch_benchmark_return(임의 종목도 조회 가능). 주입 가능(테스트).
    """
    if not holdings:
        return None
    total_w = sum(float(w) for w in holdings.values())
    if total_w <= 0:
        return None
    if fetch is None:
        from core.data_collector import DataCollector
        fetch = DataCollector.fetch_benchmark_return

    weighted_sum = 0.0
    used_weight = 0.0
    for symbol, weight in holdings.items():
        try:
            r = fetch(str(operation_start), str(today), symbol=str(symbol))
        except Exception:
            r = None
        if r is None:
            continue
        wn = float(weight) / total_w
        weighted_sum += wn * float(r)
        used_weight += wn
    if used_weight <= 0:
        return None
    stock_leg_return = weighted_sum / used_weight  # 조회 성공 종목 재정규화 가중평균(%)
    return stock_leg_return * float(design_stock_fraction)


def collect_basket_paper_evaluation(
    config=None,
    min_days: int | None = None,
    include_benchmark: bool = True,
    basket_name: str | None = None,
    include_attribution: bool = False,
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

    # NAV는 마지막 스냅샷 시점의 값이다 — 벤치마크·설계 조회도 같은 종료일로 맞춰야
    # 세 값을 비교할 때 하루치 시장 변동이 실행 격차로 오귀속되지 않는다(적대적 리뷰 medium).
    # 스냅샷이 없으면 today로 폴백.
    nav_end = _d(snaps[-1].date) if snaps else today

    benchmark_return_pct = None
    if include_benchmark:
        try:
            from core.data_collector import DataCollector
            benchmark_return_pct = DataCollector.fetch_benchmark_return(
                str(operation_start), str(nav_end), symbol="KS11",
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

    # 성과 귀속(실행 격차/구성 격차) — 종목별 조회(네트워크)라 기본 off.
    # 일일 사이클(리포트 부가필드)은 호출하지 않고, CLI 평가 도구에서만 켠다.
    if include_attribution:
        max_stock = 1.0 - (
            config.risk_params.get("diversification", {}).get("min_cash_ratio", 0.20)
        )
        tsw = basket_cfg.get("target_stock_weight")
        design_fraction = max_stock if tsw is None else max(0.0, min(float(tsw), max_stock))
        holdings = basket_cfg.get("holdings") or {}
        design_return_pct = None
        if snaps and holdings:
            try:
                # 벤치마크·NAV와 같은 종료일(nav_end)로 조회 — 창 불일치 방지.
                design_return_pct = compute_design_portfolio_return(
                    holdings, design_fraction, operation_start, nav_end,
                )
            except Exception:
                design_return_pct = None  # 참고 지표 — 실패해도 평가는 진행
        gaps = decompose_return_gap(nav_return_pct, design_return_pct, benchmark_return_pct)
        result["metrics"]["design_return_pct"] = design_return_pct
        result["metrics"]["execution_gap_pct"] = gaps["execution_gap_pct"]
        result["metrics"]["composition_gap_pct"] = gaps["composition_gap_pct"]
        result["metrics"]["total_gap_pct"] = gaps["total_gap_pct"]

    return result, basket_name
