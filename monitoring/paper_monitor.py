"""
Paper Trading 모니터링 모듈
- 주간 리포트 생성
- 실거래 전환 체크리스트 자동 판정
- 운영 이벤트 로그 기록
- 3개월 paper trading 운영의 핵심 도구
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from config.config_loader import Config
from database.models import (
    TradeHistory, PortfolioSnapshot, OperationEvent,
    get_session,
)


# ═══════════════════════════════════════════════════════════
# 1. 운영 이벤트 로그 기록
# ═══════════════════════════════════════════════════════════

def log_event(
    event_type: str,
    message: str,
    severity: str = "info",
    symbol: str = None,
    strategy: str = None,
    detail: dict = None,
    mode: str = "paper",
) -> None:
    """운영 이벤트를 DB에 기록합니다.

    event_type 예시:
    - SIGNAL: 매수/매도 신호 발생
    - API_FAILURE: KIS API 호출 실패
    - API_RETRY: API 재시도
    - DUPLICATE_BLOCKED: 중복 주문 차단
    - BLACKSWAN: 블랙스완 감지/쿨다운
    - SL_TP: 손절/익절/트레일링 발동
    - WARNING: 일반 경고
    - MDD_HALT: MDD 한도 매매 중단
    - CONFIG_ERROR: 설정 오류
    """
    session = get_session()
    try:
        event = OperationEvent(
            event_type=event_type,
            severity=severity,
            symbol=symbol,
            strategy=strategy,
            message=message,
            detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
            mode=mode,
        )
        session.add(event)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.debug("운영 이벤트 DB 저장 실패 (로그만 남김): {}", e)
    finally:
        session.close()

    # 로그에도 출력
    log_fn = {"info": logger.info, "warning": logger.warning, "error": logger.error, "critical": logger.critical}
    log_fn.get(severity, logger.info)("[{}] {}: {}", event_type, symbol or "-", message)


# ═══════════════════════════════════════════════════════════
# 2. 주간 리포트 생성
# ═══════════════════════════════════════════════════════════

class WeeklyReportGenerator:
    """Paper trading 주간 리포트를 생성합니다."""

    def __init__(self, config: Config = None, account_key: str = ""):
        self.config = config or Config.get()
        self.account_key = account_key or ""
        self.mode = "paper"

    def generate(self, weeks_back: int = 1) -> dict:
        """최근 N주간 리포트 데이터를 수집합니다."""
        end = datetime.now()
        start = end - timedelta(weeks=weeks_back)

        trades = self._get_trades(start, end)
        snapshots = self._get_snapshots(start, end)
        events = self._get_events(start, end)

        # 거래 분석
        sell_trades = [t for t in trades if t.action != "BUY"]
        buy_trades = [t for t in trades if t.action == "BUY"]
        winning = [t for t in sell_trades if self._trade_pnl(t) > 0]
        losing = [t for t in sell_trades if self._trade_pnl(t) <= 0]

        total_pnl = sum(self._trade_pnl(t) for t in sell_trades)
        total_cost = sum((t.commission or 0) + (t.tax or 0) + (t.slippage or 0) for t in trades)

        # 슬리피지 분석 (expected_price vs price)
        price_gaps = [t.price_gap for t in trades if t.price_gap is not None]
        avg_price_gap = sum(price_gaps) / len(price_gaps) if price_gaps else 0

        # 스냅샷 분석
        if snapshots:
            start_value = snapshots[0].total_value
            end_value = snapshots[-1].total_value
            weekly_return = ((end_value / start_value) - 1) * 100 if start_value > 0 else 0
            peak = max(s.total_value for s in snapshots)
            trough = min(s.total_value for s in snapshots)
            week_mdd = ((trough - peak) / peak) * 100 if peak > 0 else 0
        else:
            start_value = end_value = 0
            weekly_return = week_mdd = 0

        # 이벤트 집계
        event_counts = {}
        for e in events:
            event_counts[e.event_type] = event_counts.get(e.event_type, 0) + 1

        # 전략별 분리
        by_strategy = {}
        for t in sell_trades:
            strat = t.strategy or "unknown"
            if strat not in by_strategy:
                by_strategy[strat] = {"trades": 0, "pnl": 0, "wins": 0}
            by_strategy[strat]["trades"] += 1
            pnl = self._trade_pnl(t)
            by_strategy[strat]["pnl"] += pnl
            if pnl > 0:
                by_strategy[strat]["wins"] += 1

        # 일별 PnL
        daily_pnl = {}
        for t in sell_trades:
            day = t.executed_at.strftime("%Y-%m-%d") if t.executed_at else "unknown"
            daily_pnl[day] = daily_pnl.get(day, 0) + self._trade_pnl(t)

        report = {
            "period": f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}",
            "portfolio": {
                "start_value": round(start_value, 0),
                "end_value": round(end_value, 0),
                "weekly_return_pct": round(weekly_return, 2),
                "week_mdd_pct": round(week_mdd, 2),
            },
            "trades": {
                "total": len(trades),
                "buys": len(buy_trades),
                "sells": len(sell_trades),
                "wins": len(winning),
                "losses": len(losing),
                "win_rate": round(len(winning) / len(sell_trades) * 100, 1) if sell_trades else 0,
                "total_pnl": round(total_pnl, 0),
                "total_cost": round(total_cost, 0),
                "avg_price_gap": round(avg_price_gap, 1),
            },
            "by_strategy": by_strategy,
            "daily_pnl": daily_pnl,
            "events": event_counts,
            "critical_events": [
                {"type": e.event_type, "message": e.message, "time": str(e.created_at)}
                for e in events if e.severity in ("error", "critical")
            ],
        }

        return report

    def render_text(self, report: dict) -> str:
        """주간 리포트를 텍스트로 렌더링합니다."""
        p = report["portfolio"]
        t = report["trades"]
        lines = [
            "=" * 70,
            f"📊 Paper Trading 주간 리포트 | {report['period']}",
            "=" * 70,
            "",
            f"💰 포트폴리오: {p['start_value']:,.0f} → {p['end_value']:,.0f}원 ({p['weekly_return_pct']:+.2f}%)",
            f"📉 주간 MDD: {p['week_mdd_pct']:.2f}%",
            "",
            f"📋 거래: {t['total']}건 (매수 {t['buys']}, 매도 {t['sells']})",
            f"🎯 승률: {t['win_rate']:.1f}% ({t['wins']}승 {t['losses']}패)",
            f"💵 실현 PnL: {t['total_pnl']:+,.0f}원",
            f"💸 총 비용: {t['total_cost']:,.0f}원",
            f"📏 평균 체결가 차이: {t['avg_price_gap']:+.1f}원",
        ]

        if report["by_strategy"]:
            lines.extend(["", "전략별 성과:"])
            for strat, s in report["by_strategy"].items():
                wr = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
                lines.append(f"  [{strat}] {s['trades']}건 | 승률 {wr:.1f}% | PnL {s['pnl']:+,.0f}원")

        if report["daily_pnl"]:
            lines.extend(["", "일별 PnL:"])
            for day, pnl in sorted(report["daily_pnl"].items()):
                bar = "+" * max(0, int(pnl / 10000)) if pnl > 0 else "-" * max(0, int(-pnl / 10000))
                lines.append(f"  {day}: {pnl:+,.0f}원 {bar}")

        if report["events"]:
            lines.extend(["", "이벤트 집계:"])
            for etype, count in sorted(report["events"].items(), key=lambda x: -x[1]):
                lines.append(f"  {etype}: {count}건")

        if report["critical_events"]:
            lines.extend(["", "⚠️ 심각 이벤트:"])
            for ce in report["critical_events"][:10]:
                lines.append(f"  [{ce['time']}] {ce['type']}: {ce['message']}")

        return "\n".join(lines) + "\n"

    def save(self, report: dict, output_dir: str = "reports") -> Path:
        """리포트를 파일로 저장합니다."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        txt_path = out / f"paper_weekly_{ts}.txt"
        txt_path.write_text(self.render_text(report), encoding="utf-8")
        json_path = out / f"paper_weekly_{ts}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        logger.info("주간 리포트 저장: {}", txt_path)
        return txt_path

    def _get_trades(self, start: datetime, end: datetime) -> list:
        session = get_session()
        try:
            q = session.query(TradeHistory).filter(
                TradeHistory.mode == self.mode,
                TradeHistory.executed_at >= start,
                TradeHistory.executed_at <= end,
            )
            if self.account_key:
                q = q.filter(TradeHistory.account_key == self.account_key)
            return q.order_by(TradeHistory.executed_at).all()
        finally:
            session.close()

    def _get_snapshots(self, start: datetime, end: datetime) -> list:
        session = get_session()
        try:
            q = session.query(PortfolioSnapshot).filter(
                PortfolioSnapshot.date >= start,
                PortfolioSnapshot.date <= end,
            )
            if self.account_key:
                q = q.filter(PortfolioSnapshot.account_key == self.account_key)
            return q.order_by(PortfolioSnapshot.date).all()
        finally:
            session.close()

    def _get_events(self, start: datetime, end: datetime) -> list:
        session = get_session()
        try:
            return session.query(OperationEvent).filter(
                OperationEvent.mode == self.mode,
                OperationEvent.created_at >= start,
                OperationEvent.created_at <= end,
            ).order_by(OperationEvent.created_at).all()
        finally:
            session.close()

    @staticmethod
    def _trade_pnl(trade: TradeHistory) -> float:
        """trade reason에서 PnL 파싱 또는 0."""
        import re
        reason = trade.reason or ""
        match = re.search(r"PnL:\s*([\-0-9,]+)원", reason)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass
        return 0.0


# ═══════════════════════════════════════════════════════════
# 3. 실거래 전환 체크리스트 자동 판정
# ═══════════════════════════════════════════════════════════

class GoLiveChecker:
    """3개월 paper trading 후 실거래 전환 가능 여부를 자동 판정합니다."""

    # 전환 조건 (모두 충족 시 GO)
    CRITERIA = {
        "min_paper_days":      60,       # 최소 60 영업일 paper 운영
        "min_trades":          20,       # 최소 20건 매도 거래
        "min_win_rate":        40.0,     # 승률 40% 이상
        "max_mdd":            -20.0,     # MDD -20% 이내
        "min_sharpe_approx":    0.3,     # 근사 샤프 0.3 이상
        "max_api_failure_rate": 5.0,     # API 실패율 5% 이하
        "min_positive_months":  2,       # 3개월 중 2개월 이상 수익
        "max_cost_drag_pct":    3.0,     # 비용 드래그 3% 이하
    }

    # 전환 금지 조건 (하나라도 해당 시 BLOCK)
    BLOCKERS = {
        "cumulative_loss":    "누적 수익률 < 0%",
        "recent_mdd_spike":   "최근 2주 MDD < -15%",
        "api_failure_streak": "연속 API 실패 3회 이상",
        "zero_trades_week":   "거래 0건인 주가 2주 이상",
        "blackswan_3plus":    "블랙스완 발동 3회 이상",
    }

    def __init__(self, config: Config = None, account_key: str = ""):
        self.config = config or Config.get()
        self.account_key = account_key or ""

    def check(self) -> dict:
        """전환 체크리스트를 실행하고 결과를 반환합니다."""
        session = get_session()
        try:
            # 스냅샷 기간 확인
            snapshots = session.query(PortfolioSnapshot).filter(
                PortfolioSnapshot.account_key == (self.account_key or ""),
            ).order_by(PortfolioSnapshot.date).all()

            trades = session.query(TradeHistory).filter(
                TradeHistory.mode == "paper",
                TradeHistory.account_key == (self.account_key or ""),
            ).order_by(TradeHistory.executed_at).all()

            events = session.query(OperationEvent).filter(
                OperationEvent.mode == "paper",
            ).all()

            sell_trades = [t for t in trades if t.action != "BUY"]
            winning = [t for t in sell_trades if WeeklyReportGenerator._trade_pnl(t) > 0]

            # 기간 계산
            paper_days = len(snapshots)
            initial_cap = self.config.risk_params.get("position_sizing", {}).get("initial_capital", 10_000_000)

            # MDD 계산
            if snapshots:
                values = [s.total_value for s in snapshots]
                peak = values[0]
                max_dd = 0
                for v in values:
                    peak = max(peak, v)
                    dd = (v - peak) / peak * 100 if peak > 0 else 0
                    max_dd = min(max_dd, dd)
                cumulative_return = ((values[-1] / initial_cap) - 1) * 100 if initial_cap > 0 else 0
            else:
                max_dd = 0
                cumulative_return = 0

            # 근사 샤프
            if snapshots and len(snapshots) > 5:
                vals = pd.Series([s.total_value for s in snapshots])
                daily_ret = vals.pct_change().dropna()
                if len(daily_ret) > 0 and daily_ret.std() > 0:
                    sharpe_approx = (daily_ret.mean() * 252 - 0.03) / (daily_ret.std() * (252 ** 0.5))
                else:
                    sharpe_approx = 0
            else:
                sharpe_approx = 0

            # API 실패율
            api_failures = sum(1 for e in events if e.event_type == "API_FAILURE")
            api_retries = sum(1 for e in events if e.event_type == "API_RETRY")
            total_api_calls = len(trades) + api_failures + api_retries
            api_failure_rate = (api_failures / total_api_calls * 100) if total_api_calls > 0 else 0

            # 월별 수익
            monthly_pnl = {}
            for t in sell_trades:
                month = t.executed_at.strftime("%Y-%m") if t.executed_at else "unknown"
                monthly_pnl[month] = monthly_pnl.get(month, 0) + WeeklyReportGenerator._trade_pnl(t)
            positive_months = sum(1 for v in monthly_pnl.values() if v > 0)

            # 비용 드래그
            total_cost = sum((t.commission or 0) + (t.tax or 0) + (t.slippage or 0) for t in trades)
            cost_drag = (total_cost / initial_cap * 100) if initial_cap > 0 else 0

            # 블랙스완 발동 횟수
            blackswan_count = sum(1 for e in events if e.event_type == "BLACKSWAN")

            # ── 체크리스트 판정 ──
            criteria = self.CRITERIA
            results = {
                "paper_days":       {"value": paper_days, "required": f"≥ {criteria['min_paper_days']}", "pass": paper_days >= criteria["min_paper_days"]},
                "total_trades":     {"value": len(sell_trades), "required": f"≥ {criteria['min_trades']}", "pass": len(sell_trades) >= criteria["min_trades"]},
                "win_rate":         {"value": round(len(winning) / len(sell_trades) * 100, 1) if sell_trades else 0, "required": f"≥ {criteria['min_win_rate']}%", "pass": (len(winning) / len(sell_trades) * 100 if sell_trades else 0) >= criteria["min_win_rate"]},
                "max_mdd":          {"value": round(max_dd, 2), "required": f"≥ {criteria['max_mdd']}%", "pass": max_dd >= criteria["max_mdd"]},
                "sharpe_approx":    {"value": round(sharpe_approx, 2), "required": f"≥ {criteria['min_sharpe_approx']}", "pass": sharpe_approx >= criteria["min_sharpe_approx"]},
                "api_failure_rate": {"value": round(api_failure_rate, 1), "required": f"≤ {criteria['max_api_failure_rate']}%", "pass": api_failure_rate <= criteria["max_api_failure_rate"]},
                "positive_months":  {"value": positive_months, "required": f"≥ {criteria['min_positive_months']}", "pass": positive_months >= criteria["min_positive_months"]},
                "cost_drag":        {"value": round(cost_drag, 2), "required": f"≤ {criteria['max_cost_drag_pct']}%", "pass": cost_drag <= criteria["max_cost_drag_pct"]},
            }

            # ── 금지 조건 판정 ──
            blockers = []
            if cumulative_return < 0:
                blockers.append(f"🛑 {self.BLOCKERS['cumulative_loss']}: {cumulative_return:+.2f}%")
            if snapshots and len(snapshots) >= 10:
                recent_vals = [s.total_value for s in snapshots[-10:]]
                rpeak = max(recent_vals)
                rtrough = min(recent_vals)
                recent_mdd = ((rtrough - rpeak) / rpeak * 100) if rpeak > 0 else 0
                if recent_mdd < -15:
                    blockers.append(f"🛑 {self.BLOCKERS['recent_mdd_spike']}: {recent_mdd:.1f}%")
            if blackswan_count >= 3:
                blockers.append(f"🛑 {self.BLOCKERS['blackswan_3plus']}: {blackswan_count}회")

            all_pass = all(r["pass"] for r in results.values())
            no_blockers = len(blockers) == 0
            go_live = all_pass and no_blockers

            return {
                "go_live": go_live,
                "verdict": "GO — 실거래 전환 가능" if go_live else "NO-GO — 조건 미충족",
                "checklist": results,
                "blockers": blockers,
                "summary": {
                    "paper_days": paper_days,
                    "cumulative_return": round(cumulative_return, 2),
                    "max_mdd": round(max_dd, 2),
                    "sharpe": round(sharpe_approx, 2),
                    "total_trades": len(sell_trades),
                    "blackswan_events": blackswan_count,
                },
            }
        finally:
            session.close()

    def render_text(self, result: dict) -> str:
        """체크리스트를 텍스트로 렌더링합니다."""
        lines = [
            "=" * 70,
            f"🔍 실거래 전환 체크리스트 | 판정: {result['verdict']}",
            "=" * 70,
            "",
            "체크리스트:",
        ]
        for name, r in result["checklist"].items():
            icon = "✅" if r["pass"] else "❌"
            lines.append(f"  {icon} {name}: {r['value']} (기준: {r['required']})")

        if result["blockers"]:
            lines.extend(["", "전환 금지 조건:"])
            for b in result["blockers"]:
                lines.append(f"  {b}")
        else:
            lines.append("\n✅ 전환 금지 조건 해당 없음")

        s = result["summary"]
        lines.extend([
            "",
            "요약:",
            f"  운영 기간: {s['paper_days']}일",
            f"  누적 수익률: {s['cumulative_return']:+.2f}%",
            f"  최대 MDD: {s['max_mdd']:.2f}%",
            f"  근사 샤프: {s['sharpe']:.2f}",
            f"  총 거래: {s['total_trades']}건",
            f"  블랙스완: {s['blackswan_events']}회",
        ])

        return "\n".join(lines) + "\n"
