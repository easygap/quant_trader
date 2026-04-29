"""
Paper Evidence 런타임 수집 모듈

매일 장마감 후 DailyEvidence를 자동 누적한다:
- reports/paper_evidence/daily_evidence_{strategy}.jsonl  (append-only)
- reports/paper_evidence/anomalies.jsonl                  (append-only)

또한 주간 markdown 요약, 60일 promotion evidence package,
approval_checklist.md 를 생성하는 함수를 제공한다.

Live eligibility(approved_strategies.json)는 절대 자동 수정하지 않는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

# ─── 상수 ───────────────────────────────────────────────────
EVIDENCE_DIR = Path("reports/paper_evidence")
RF_ANNUAL = 0.035  # 한국 국채 근사

# Anomaly thresholds
_REJECT_THRESHOLD = 3
_DUPLICATE_THRESHOLD = 5
_DEEP_DD_MDD = -15.0
_DEEP_DD_DAILY = -5.0

# Benchmark completeness thresholds (per excess type)
# same_universe_excess: 종목 universe 전체 대비이므로 높은 completeness 필요
# exposure_matched / cash_adjusted: 투자비중 가중이므로 같은 기준 적용
# 근거: completeness 50%는 5종목 universe에서 3개만 있어도 final이 됨.
#   50%면 universe 대표성이 편향될 수 있으나, 소형 watchlist(5~10)에서는
#   1~2개 누락이 흔하고, KOSPI fallback 대비 universe 일부가 더 정확하므로 유지.
#   promotion에서 benchmark_final_ratio >= 80%로 장기 데이터 품질을 별도 관리.
BENCHMARK_COMPLETENESS_FINAL = 0.5  # >= 50% → final (유지, 아래 근거 참조)

# Promotion guard: positive evidence thresholds
PROMOTION_MIN_EXCESS_DAYS = 0.6   # non-null excess 비율 >= 60%
PROMOTION_FINAL_RATIO_MIN = 0.8   # benchmark final ratio >= 80%
PROMOTION_MIN_AVG_EXCESS = 0.0    # benchmark 대비 평균 excess는 양수여야 함
PROMOTION_MIN_CUMULATIVE_RETURN = 0.0
PROMOTION_MIN_SELL_TRADES = 5
PROMOTION_MIN_WIN_RATE = 45.0


# ─── 데이터 구조 ────────────────────────────────────────────

@dataclass
class DailyEvidence:
    date: str
    day_number: int
    strategy: str

    # portfolio
    total_value: float = 0.0
    cash: float = 0.0
    invested: float = 0.0
    daily_return: Optional[float] = None
    cumulative_return: Optional[float] = None
    mdd: Optional[float] = None
    position_count: int = 0

    # trades
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0

    # benchmark excess
    same_universe_excess: Optional[float] = None
    exposure_matched_excess: Optional[float] = None
    cash_adjusted_excess: Optional[float] = None
    benchmark_meta: dict = field(default_factory=dict)
    # benchmark finality: "provisional" | "final" | "failed"
    benchmark_status: str = "provisional"

    # execution / ops
    raw_fill_rate: Optional[float] = None
    effective_fill_rate: Optional[float] = None
    turnover: Optional[float] = None
    signal_density: Optional[float] = None
    reconcile_count: int = 0
    stale_pending_count: int = 0
    phantom_position_count: int = 0
    restart_recovery_count: int = 0
    duplicate_blocked_count: int = 0
    reject_count: int = 0

    # diagnostics
    diagnostics: list = field(default_factory=list)

    # cross-validation warnings
    cross_validation_warnings: list = field(default_factory=list)

    # anomalies & status
    anomalies: list = field(default_factory=list)
    status: str = "normal"

    # record version: incremented on finalize
    record_version: int = 1

    # schema version: pipeline epoch 식별 (v1=evidence_collector, v2=paper_evidence)
    schema_version: int = 2

    # provenance: evidence 출처 구분
    # real_paper: 실제 scheduler paper run (주문 제출 가능 세션)
    # shadow_bootstrap: 주문 없이 signal/benchmark/evidence만 수집
    # replay: seeded replay (golden test 등)
    # backfill: 과거 날짜 보충 수집
    # test: 테스트 환경
    evidence_mode: str = "real_paper"
    execution_backed: bool = True  # 실제 주문 제출이 가능한 세션에서 수집됐는지
    order_submit_count: int = 0
    fill_count: int = 0

    # session_mode: 세션 유형 (pilot provenance 분리)
    # normal_paper: 일반 real paper 세션
    # pilot_paper: pilot authorization 하 제한 entry 세션
    # shadow_bootstrap: shadow evidence only 세션
    # replay / test: 비운영 세션
    session_mode: str = "normal_paper"
    pilot_authorized: bool = False
    pilot_caps_snapshot: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# JSONL I/O
# ═══════════════════════════════════════════════════════════════

def _evidence_path(strategy: str) -> Path:
    return EVIDENCE_DIR / f"daily_evidence_{strategy}.jsonl"


def _anomaly_path() -> Path:
    return EVIDENCE_DIR / "anomalies.jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    # append tmp content to target (atomic-ish on all OS)
    with open(path, "a", encoding="utf-8") as dst, open(tmp, "r", encoding="utf-8") as src:
        dst.write(src.read())
    tmp.unlink(missing_ok=True)


def _already_recorded(jsonl_path: Path, date_str: str, *, allow_provisional: bool = False) -> bool:
    """동일 날짜 기록 여부 확인. allow_provisional=True 면 provisional record는 무시(finalize 허용)."""
    if not jsonl_path.exists():
        return False
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-10:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("date") == date_str:
                    if allow_provisional and rec.get("benchmark_status") == "provisional":
                        return False  # provisional이면 finalize 가능
                    return True
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return False


def _compute_day_number(jsonl_path: Path, today_str: str) -> int:
    if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
        return 1
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            first_line = ""
            for raw in f:
                raw = raw.strip()
                if raw:
                    first_line = raw
                    break
            if not first_line:
                return 1
            first_rec = json.loads(first_line)
            first_date = datetime.strptime(first_rec["date"], "%Y-%m-%d").date()
            today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
            # count all existing entries + 1
            f.seek(0)
            count = sum(1 for line in f if line.strip())
            return count + 1
    except Exception:
        return 1


def _read_all_evidence(jsonl_path: Path) -> list[dict]:
    if not jsonl_path.exists():
        return []
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def get_canonical_records(strategy: str) -> list[dict]:
    """
    Canonical view: 같은 date에 여러 record(provisional → final)가 있을 때 최신만 반환.
    JSONL은 append-only이므로 같은 date의 뒤쪽 record가 최신.
    """
    jsonl_path = _evidence_path(strategy)
    all_records = _read_all_evidence(jsonl_path)
    by_date: dict[str, dict] = {}
    for r in all_records:
        by_date[r["date"]] = r  # later entry wins
    return list(by_date.values())


# ═══════════════════════════════════════════════════════════════
# 데이터 수집 함수들
# ═══════════════════════════════════════════════════════════════

def _collect_portfolio_metrics(account_key: str, date: datetime) -> dict:
    """포트폴리오 메트릭 수집.

    당일 PortfolioSnapshot이 없으면 직전 snapshot을 조회하여
    cash-only / no-trade day를 추론한다:
      - 직전 snapshot이 존재하고
      - 그 이후 거래가 없으면 (TradeHistory 0건)
      → 포트폴리오 가치 불변, daily_return=0.0 으로 처리
    이를 통해 blocked 상태에서도 valid evidence를 생성할 수 있다.

    진짜 데이터 부재(snapshot 자체가 한 번도 없음)면 {} 반환.
    """
    from database.models import PortfolioSnapshot, TradeHistory, get_session

    session = get_session()
    try:
        ak = account_key or ""
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        # 1) 당일 snapshot 조회
        snap = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.account_key == ak,
                PortfolioSnapshot.date >= day_start,
                PortfolioSnapshot.date < day_end,
            )
            .order_by(PortfolioSnapshot.date.desc())
            .first()
        )
        if snap:
            return {
                "total_value": snap.total_value or 0,
                "cash": snap.cash or 0,
                "invested": snap.invested or 0,
                "daily_return": snap.daily_return,
                "cumulative_return": snap.cumulative_return,
                "mdd": snap.mdd,
                "position_count": snap.position_count or 0,
            }

        # 2) 당일 snapshot 없음 → 직전 snapshot fallback
        prev_snap = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.account_key == ak,
                PortfolioSnapshot.date < day_start,
            )
            .order_by(PortfolioSnapshot.date.desc())
            .first()
        )
        if not prev_snap:
            return {}  # 진짜 데이터 부재

        # 3) 직전 snapshot 이후 ~ 당일까지 거래 유무 확인
        trades_since = (
            session.query(TradeHistory)
            .filter(
                TradeHistory.account_key == ak,
                TradeHistory.executed_at >= prev_snap.date,
                TradeHistory.executed_at < day_end,
            )
            .count()
        )
        if trades_since > 0:
            # 거래가 있었는데 snapshot이 없음 → 진짜 missing data
            return {}

        # 4) 거래 없음 + 직전 snapshot 존재 → cash-only carry-forward
        #    포트폴리오 가치 불변이므로 daily_return=0.0
        return {
            "total_value": prev_snap.total_value or 0,
            "cash": prev_snap.cash or 0,
            "invested": prev_snap.invested or 0,
            "daily_return": 0.0,  # 가치 불변 = 수익률 0%
            "cumulative_return": prev_snap.cumulative_return,
            "mdd": prev_snap.mdd,
            "position_count": prev_snap.position_count or 0,
            "_inferred_from_previous": True,  # 추론 출처 표시
        }
    finally:
        session.close()


def _collect_trade_metrics(mode: str, account_key: str, date: datetime) -> dict:
    from database.repositories import get_daily_trade_summary
    from core.portfolio_manager import PortfolioManager
    from config.config_loader import Config

    ts = get_daily_trade_summary(date=date, mode=mode, account_key=account_key)
    unrealized = 0.0
    try:
        pm = PortfolioManager(Config.get(), account_key=account_key)
        summary = pm.get_portfolio_summary()
        unrealized = summary.get("unrealized_pnl", 0.0)
    except Exception:
        pass

    return {
        "total_trades": ts.get("total_trades", 0),
        "buy_count": ts.get("buy_count", 0),
        "sell_count": ts.get("sell_count", 0),
        "realized_pnl": ts.get("realized_pnl", 0.0),
        "unrealized_pnl": unrealized,
        "winning_trades": ts.get("winning_trades", 0),
        "losing_trades": ts.get("losing_trades", 0),
    }


def _collect_execution_ops_metrics(
    mode: str,
    account_key: str,
    date: datetime,
    watchlist_size: int,
    total_value: float,
) -> dict:
    from database.models import (
        get_session, TradeHistory, FailedOrder, OperationEvent,
        PendingOrderGuard, Position,
    )

    session = get_session()
    try:
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        # --- fills vs rejects ---
        trades_today = (
            session.query(TradeHistory)
            .filter(
                TradeHistory.mode == mode,
                TradeHistory.account_key == (account_key or ""),
                TradeHistory.executed_at >= day_start,
                TradeHistory.executed_at < day_end,
            )
            .all()
        )
        filled_count = len(trades_today)

        failed_today = (
            session.query(FailedOrder)
            .filter(
                FailedOrder.mode == mode,
                FailedOrder.account_key == (account_key or ""),
                FailedOrder.failed_at >= day_start,
                FailedOrder.failed_at < day_end,
            )
            .all()
        )
        reject_count = len(failed_today)
        attempted = filled_count + reject_count

        raw_fill = filled_count / attempted if attempted > 0 else None
        # effective: exclude legitimate rejections (diversification, time block, etc.)
        legit_reject = sum(
            1 for f in failed_today
            if any(kw in (f.error_detail or "").lower() for kw in ("다양화", "diversif", "차단 시간", "volatility", "차단"))
        )
        effective_attempted = attempted - legit_reject
        effective_fill = filled_count / effective_attempted if effective_attempted > 0 else None

        # --- turnover ---
        buy_value = sum(t.total_amount or 0 for t in trades_today if (t.action or "").upper() == "BUY")
        sell_value = sum(t.total_amount or 0 for t in trades_today if (t.action or "").upper() != "BUY")
        turnover = (buy_value + sell_value) / total_value if total_value > 0 else None

        # --- signal density ---
        signal_events = (
            session.query(OperationEvent)
            .filter(
                OperationEvent.mode == mode,
                OperationEvent.event_type == "SIGNAL",
                OperationEvent.created_at >= day_start,
                OperationEvent.created_at < day_end,
            )
            .count()
        )
        signal_density = signal_events / watchlist_size if watchlist_size > 0 else None

        # --- reconcile count ---
        reconcile_count = (
            session.query(OperationEvent)
            .filter(
                OperationEvent.mode == mode,
                OperationEvent.event_type.in_(["RECONCILE_MISMATCH", "RECONCILE"]),
                OperationEvent.created_at >= day_start,
                OperationEvent.created_at < day_end,
            )
            .count()
        )

        # --- stale pending ---
        now = datetime.now()
        stale_pending_count = (
            session.query(PendingOrderGuard)
            .filter(PendingOrderGuard.expires_at < now)
            .count()
        )

        # --- phantom position ---
        positions = (
            session.query(Position)
            .filter(Position.account_key == (account_key or ""))
            .all()
        )
        lookback = date - timedelta(days=90)
        phantom_count = 0
        for pos in positions:
            has_buy = (
                session.query(TradeHistory)
                .filter(
                    TradeHistory.symbol == pos.symbol,
                    TradeHistory.account_key == (account_key or ""),
                    TradeHistory.action == "BUY",
                    TradeHistory.executed_at >= lookback,
                )
                .first()
            )
            if not has_buy:
                phantom_count += 1

        # --- restart recovery ---
        restart_recovery_count = (
            session.query(OperationEvent)
            .filter(
                OperationEvent.mode == mode,
                OperationEvent.event_type.in_(["RECOVERY", "STARTUP_RECOVERY"]),
                OperationEvent.created_at >= day_start,
                OperationEvent.created_at < day_end,
            )
            .count()
        )

        # --- duplicate blocked ---
        duplicate_blocked_count = (
            session.query(OperationEvent)
            .filter(
                OperationEvent.mode == mode,
                OperationEvent.event_type == "DUPLICATE_BLOCKED",
                OperationEvent.created_at >= day_start,
                OperationEvent.created_at < day_end,
            )
            .count()
        )

        return {
            "raw_fill_rate": round(raw_fill, 4) if raw_fill is not None else None,
            "effective_fill_rate": round(effective_fill, 4) if effective_fill is not None else None,
            "turnover": round(turnover, 6) if turnover is not None else None,
            "signal_density": round(signal_density, 4) if signal_density is not None else None,
            "reconcile_count": reconcile_count,
            "stale_pending_count": stale_pending_count,
            "phantom_position_count": phantom_count,
            "restart_recovery_count": restart_recovery_count,
            "duplicate_blocked_count": duplicate_blocked_count,
            "reject_count": reject_count,
        }
    finally:
        session.close()


def _compute_benchmark_excess(
    date: datetime,
    daily_return: Optional[float],
    cash_ratio: float,
    watchlist_symbols: list[str],
) -> dict:
    """3종 benchmark excess 계산. 데이터 미비 시 null + reason + benchmark_status."""
    asof = datetime.now().isoformat()
    result = {
        "same_universe_excess": None,
        "exposure_matched_excess": None,
        "cash_adjusted_excess": None,
        "benchmark_status": "failed",
        "benchmark_meta": {
            "type": "universe_equal_weight",
            "symbols_count": len(watchlist_symbols),
            "date": date.strftime("%Y-%m-%d"),
            "asof": asof,
            "source": None,
            "completeness": 0.0,
        },
    }

    if daily_return is None:
        result["benchmark_meta"]["warning"] = "daily_return is null"
        return result

    if not watchlist_symbols:
        result["benchmark_meta"]["warning"] = "empty watchlist"
        return result

    try:
        from core.data_collector import DataCollector

        collector = DataCollector()
        end_date = date
        start_date = date - timedelta(days=10)

        returns = []
        missing_symbols = []
        for sym in watchlist_symbols:
            try:
                df = collector.fetch_stock(sym, start_date=start_date.strftime("%Y-%m-%d"),
                                           end_date=end_date.strftime("%Y-%m-%d"))
                if df is None or len(df) < 2:
                    missing_symbols.append(sym)
                    continue
                prev_close = float(df["close"].iloc[-2])
                curr_close = float(df["close"].iloc[-1])
                if prev_close > 0:
                    returns.append((curr_close - prev_close) / prev_close * 100)
            except Exception:
                missing_symbols.append(sym)

        source_used = "universe"
        if not returns:
            try:
                df_idx = collector.fetch_stock("KS11", start_date=start_date.strftime("%Y-%m-%d"),
                                               end_date=end_date.strftime("%Y-%m-%d"))
                if df_idx is not None and len(df_idx) >= 2:
                    pc = float(df_idx["close"].iloc[-2])
                    cc = float(df_idx["close"].iloc[-1])
                    if pc > 0:
                        returns = [(cc - pc) / pc * 100]
                        source_used = "kospi_fallback"
                        result["benchmark_meta"]["type"] = "kospi_fallback"
            except Exception:
                pass

        if not returns:
            result["benchmark_meta"]["warning"] = "all benchmark data missing"
            return result

        total_syms = len(watchlist_symbols)
        available = len(returns)
        completeness = available / total_syms if total_syms > 0 else 0.0

        universe_return = sum(returns) / len(returns)
        invested_ratio = 1.0 - cash_ratio
        rf_daily = RF_ANNUAL / 252

        result["same_universe_excess"] = round(daily_return - universe_return, 4)
        result["exposure_matched_excess"] = round(
            daily_return - universe_return * invested_ratio, 4
        )
        result["cash_adjusted_excess"] = round(
            daily_return - (universe_return * invested_ratio + rf_daily * cash_ratio), 4
        )

        # completeness >= BENCHMARK_COMPLETENESS_FINAL 이면 final, 아니면 provisional
        if completeness >= BENCHMARK_COMPLETENESS_FINAL:
            result["benchmark_status"] = "final"
        else:
            result["benchmark_status"] = "provisional"

        result["benchmark_meta"]["source"] = source_used
        result["benchmark_meta"]["completeness"] = round(completeness, 4)
        result["benchmark_meta"]["universe_return"] = round(universe_return, 4)
        result["benchmark_meta"]["invested_ratio"] = round(invested_ratio, 4)
        result["benchmark_meta"]["available_symbols"] = available
        if missing_symbols:
            result["benchmark_meta"]["missing_symbols"] = missing_symbols[:10]

    except Exception as e:
        result["benchmark_meta"]["error"] = str(e)

    return result


# ═══════════════════════════════════════════════════════════════
# Anomaly Detection
# ═══════════════════════════════════════════════════════════════

def _detect_anomalies(ops: dict, portfolio: dict) -> list[dict]:
    anomalies = []

    if ops.get("reject_count", 0) > _REJECT_THRESHOLD:
        anomalies.append({
            "type": "repeated_reject",
            "severity": "warning",
            "detail": f"reject_count={ops['reject_count']}",
        })

    if ops.get("phantom_position_count", 0) > 0:
        anomalies.append({
            "type": "phantom_position",
            "severity": "critical",
            "detail": f"phantom_position_count={ops['phantom_position_count']}",
        })

    if ops.get("stale_pending_count", 0) > 0:
        anomalies.append({
            "type": "stale_pending",
            "severity": "warning",
            "detail": f"stale_pending_count={ops['stale_pending_count']}",
        })

    if ops.get("duplicate_blocked_count", 0) > _DUPLICATE_THRESHOLD:
        anomalies.append({
            "type": "duplicate_flood",
            "severity": "warning",
            "detail": f"duplicate_blocked_count={ops['duplicate_blocked_count']}",
        })

    if ops.get("reconcile_count", 0) > 0:
        anomalies.append({
            "type": "reconcile_anomaly",
            "severity": "warning",
            "detail": f"reconcile_count={ops['reconcile_count']}",
        })

    mdd = portfolio.get("mdd")
    dr = portfolio.get("daily_return")
    if (mdd is not None and mdd < _DEEP_DD_MDD) or (dr is not None and dr < _DEEP_DD_DAILY):
        anomalies.append({
            "type": "deep_drawdown",
            "severity": "critical",
            "detail": f"mdd={mdd}, daily_return={dr}",
        })

    return anomalies


def _determine_status(anomalies: list[dict]) -> str:
    if any(a["severity"] == "critical" for a in anomalies):
        return "frozen"
    if anomalies:
        return "degraded"
    return "normal"


def _cross_validate(portfolio: dict, trades: dict, account_key: str, date: datetime) -> list[str]:
    """DailyEvidence vs DailyReport/PortfolioSnapshot 교차검증. 불일치 시 warning 목록 반환."""
    warnings = []
    from database.models import DailyReport, PortfolioSnapshot, get_session

    session = get_session()
    try:
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        # DailyReport 교차검증
        dr = (
            session.query(DailyReport)
            .filter(
                DailyReport.account_key == (account_key or ""),
                DailyReport.date >= day_start,
                DailyReport.date < day_end,
            )
            .first()
        )
        if dr:
            if dr.total_trades != trades.get("total_trades", 0):
                warnings.append(
                    "trade_count mismatch: evidence=%d vs DailyReport=%d" %
                    (trades.get("total_trades", 0), dr.total_trades)
                )
            dr_pnl = dr.realized_pnl or 0
            ev_pnl = trades.get("realized_pnl", 0)
            if abs(dr_pnl - ev_pnl) > 1.0:
                warnings.append(
                    "realized_pnl mismatch: evidence=%.0f vs DailyReport=%.0f" % (ev_pnl, dr_pnl)
                )

        # PortfolioSnapshot 교차검증
        snap = (
            session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.account_key == (account_key or ""),
                PortfolioSnapshot.date >= day_start,
                PortfolioSnapshot.date < day_end,
            )
            .first()
        )
        if snap:
            snap_dr = snap.daily_return
            ev_dr = portfolio.get("daily_return")
            if snap_dr is not None and ev_dr is not None and abs(snap_dr - ev_dr) > 0.01:
                warnings.append(
                    "daily_return mismatch: evidence=%.4f vs Snapshot=%.4f" % (ev_dr, snap_dr)
                )
    except Exception as e:
        warnings.append("cross_validation error: " + str(e))
    finally:
        session.close()

    return warnings


def _derive_session_mode(evidence_mode: str, pilot_authorized: bool) -> str:
    """evidence_mode + pilot flag에서 session_mode를 결정."""
    if evidence_mode == "pilot_paper":
        return "pilot_paper"
    if evidence_mode == "shadow_bootstrap":
        return "shadow_bootstrap"
    if evidence_mode in ("replay", "test", "backfill"):
        return evidence_mode
    # real_paper: pilot auth 여부로 분기
    if pilot_authorized:
        return "pilot_paper"
    return "normal_paper"


# ═══════════════════════════════════════════════════════════════
# 메인 진입점
# ═══════════════════════════════════════════════════════════════

def collect_daily_evidence(
    strategy: str,
    mode: str = "paper",
    account_key: str = "",
    date: datetime | None = None,
    watchlist_symbols: list[str] | None = None,
    evidence_mode: str = "real_paper",
    pilot_authorized: bool = False,
    pilot_caps_snapshot: dict | None = None,
) -> DailyEvidence | None:
    """
    장마감 후 호출. DailyEvidence를 수집하여 JSONL에 append한다.
    동일 날짜 final 기록이 있으면 skip. provisional만 있으면 skip (finalize로 승격).
    """
    date = date or datetime.now()
    today_str = date.strftime("%Y-%m-%d")
    jsonl_path = _evidence_path(strategy)

    if _already_recorded(jsonl_path, today_str):
        logger.info("Paper evidence 이미 기록됨: {} {}", strategy, today_str)
        return None

    day_number = _compute_day_number(jsonl_path, today_str)
    portfolio = _collect_portfolio_metrics(account_key, date)
    trades = _collect_trade_metrics(mode, account_key, date)

    total_value = portfolio.get("total_value", 0)
    cash = portfolio.get("cash", 0)
    cash_ratio = cash / total_value if total_value > 0 else 1.0

    ops = _collect_execution_ops_metrics(
        mode=mode,
        account_key=account_key,
        date=date,
        watchlist_size=len(watchlist_symbols) if watchlist_symbols else 0,
        total_value=total_value,
    )

    benchmark = _compute_benchmark_excess(
        date=date,
        daily_return=portfolio.get("daily_return"),
        cash_ratio=cash_ratio,
        watchlist_symbols=watchlist_symbols or [],
    )

    # portfolio fallback 출처를 benchmark_meta에 기록
    if portfolio.get("_inferred_from_previous"):
        benchmark.setdefault("benchmark_meta", {})["portfolio_source"] = "inferred_carry_forward"

    # diagnostics
    diag_list = []
    try:
        from core.strategy_diagnostics import diagnose_live_post_market
        diag_lines = diagnose_live_post_market(mode=mode, account_key=account_key, today=date)
        diag_list = [{"ok": d.ok, "text": d.text} for d in diag_lines]
    except Exception:
        pass

    # cross-validation
    xv_warnings = _cross_validate(portfolio, trades, account_key, date)
    if xv_warnings:
        logger.warning("Cross-validation warnings: {}", xv_warnings)

    anomalies = _detect_anomalies(ops, portfolio)
    # cross-validation mismatch를 anomaly로 추가
    if xv_warnings:
        anomalies.append({
            "type": "cross_validation_mismatch",
            "severity": "warning",
            "detail": "; ".join(xv_warnings),
        })
    status = _determine_status(anomalies)

    bench_status = benchmark.get("benchmark_status", "failed")

    ev = DailyEvidence(
        date=today_str,
        day_number=day_number,
        strategy=strategy,
        # portfolio
        total_value=total_value,
        cash=cash,
        invested=portfolio.get("invested", 0),
        daily_return=portfolio.get("daily_return"),
        cumulative_return=portfolio.get("cumulative_return"),
        mdd=portfolio.get("mdd"),
        position_count=portfolio.get("position_count", 0),
        # trades
        total_trades=trades.get("total_trades", 0),
        buy_count=trades.get("buy_count", 0),
        sell_count=trades.get("sell_count", 0),
        realized_pnl=trades.get("realized_pnl", 0),
        unrealized_pnl=trades.get("unrealized_pnl", 0),
        winning_trades=trades.get("winning_trades", 0),
        losing_trades=trades.get("losing_trades", 0),
        # benchmark
        same_universe_excess=benchmark["same_universe_excess"],
        exposure_matched_excess=benchmark["exposure_matched_excess"],
        cash_adjusted_excess=benchmark["cash_adjusted_excess"],
        benchmark_meta=benchmark["benchmark_meta"],
        benchmark_status=bench_status,
        # ops
        **ops,
        # diagnostics
        diagnostics=diag_list,
        cross_validation_warnings=xv_warnings,
        anomalies=anomalies,
        status=status,
        record_version=1,
        # provenance
        evidence_mode=evidence_mode,
        execution_backed=evidence_mode in ("real_paper", "pilot_paper"),
        order_submit_count=trades.get("buy_count", 0) + trades.get("sell_count", 0),
        fill_count=trades.get("total_trades", 0),
        # pilot provenance
        session_mode=_derive_session_mode(evidence_mode, pilot_authorized),
        pilot_authorized=pilot_authorized,
        pilot_caps_snapshot=pilot_caps_snapshot or {},
    )

    _append_jsonl(jsonl_path, asdict(ev))
    logger.info(
        "Paper evidence 기록: {} day={} status={} bench={} anomalies={}",
        today_str, day_number, status, bench_status, len(anomalies),
    )

    if anomalies:
        anom_path = _anomaly_path()
        for a in anomalies:
            rec = {"date": today_str, "strategy": strategy, **a}
            _append_jsonl(anom_path, rec)
        logger.warning("Anomaly {} 건 기록: {}", len(anomalies), [a["type"] for a in anomalies])

    return ev


def finalize_daily_evidence(
    strategy: str,
    mode: str = "paper",
    account_key: str = "",
    date: datetime | None = None,
    watchlist_symbols: list[str] | None = None,
) -> DailyEvidence | None:
    """
    Provisional evidence를 final로 승격한다.
    - 기존 provisional record가 없으면 새로 수집
    - 있으면 benchmark만 재계산하여 final version을 append
    - 이미 final이면 skip
    """
    date = date or datetime.now()
    today_str = date.strftime("%Y-%m-%d")
    jsonl_path = _evidence_path(strategy)

    # 이미 final이면 skip
    if _already_recorded(jsonl_path, today_str, allow_provisional=False):
        # check if it's truly final
        records = _read_all_evidence(jsonl_path)
        for r in reversed(records):
            if r.get("date") == today_str:
                if r.get("benchmark_status") == "final":
                    logger.info("Evidence already final: {} {}", strategy, today_str)
                    return None
                break

    # provisional이 있는지 확인
    existing = None
    records = _read_all_evidence(jsonl_path)
    for r in reversed(records):
        if r.get("date") == today_str:
            existing = r
            break

    if existing is None:
        # 기존 기록 없음 → 새로 수집
        return collect_daily_evidence(strategy, mode, account_key, date, watchlist_symbols)

    # provisional → final: benchmark만 재계산
    portfolio = _collect_portfolio_metrics(account_key, date)
    total_value = portfolio.get("total_value", 0)
    cash = portfolio.get("cash", 0)
    cash_ratio = cash / total_value if total_value > 0 else 1.0

    benchmark = _compute_benchmark_excess(
        date=date,
        daily_return=portfolio.get("daily_return"),
        cash_ratio=cash_ratio,
        watchlist_symbols=watchlist_symbols or [],
    )

    new_bench_status = benchmark.get("benchmark_status", "failed")
    old_version = existing.get("record_version", 1)

    # 기존 record 복사 후 benchmark 필드 업데이트
    updated = dict(existing)
    updated["same_universe_excess"] = benchmark["same_universe_excess"]
    updated["exposure_matched_excess"] = benchmark["exposure_matched_excess"]
    updated["cash_adjusted_excess"] = benchmark["cash_adjusted_excess"]
    updated["benchmark_meta"] = benchmark["benchmark_meta"]
    updated["benchmark_status"] = new_bench_status
    updated["record_version"] = old_version + 1

    # portfolio fallback: 기존 record에서 daily_return/portfolio가 null이었는데
    # 이제 추론할 수 있으면 portfolio 필드도 갱신 (zero-return semantics 수정)
    if existing.get("daily_return") is None and portfolio.get("daily_return") is not None:
        updated["daily_return"] = portfolio["daily_return"]
        updated["total_value"] = portfolio.get("total_value", 0)
        updated["cash"] = portfolio.get("cash", 0)
        updated["invested"] = portfolio.get("invested", 0)
        updated["cumulative_return"] = portfolio.get("cumulative_return")
        updated["mdd"] = portfolio.get("mdd")
        updated["position_count"] = portfolio.get("position_count", 0)

    _append_jsonl(jsonl_path, updated)
    logger.info(
        "Evidence finalized: {} {} bench={} v{}→v{}",
        strategy, today_str, new_bench_status, old_version, old_version + 1,
    )

    return DailyEvidence(**{k: v for k, v in updated.items() if k in DailyEvidence.__dataclass_fields__})


# ═══════════════════════════════════════════════════════════════
# Weekly Summary
# ═══════════════════════════════════════════════════════════════

def generate_weekly_summary(strategy: str, week_end_date: str | None = None) -> Path | None:
    """최근 5영업일 evidence에서 주간 markdown 요약 생성. Canonical records 사용."""
    records = get_canonical_records(strategy)
    if not records:
        logger.warning("Weekly summary: no evidence data for {}", strategy)
        return None

    if week_end_date:
        end_dt = datetime.strptime(week_end_date, "%Y-%m-%d").date()
    else:
        end_dt = datetime.strptime(records[-1]["date"], "%Y-%m-%d").date()

    start_dt = end_dt - timedelta(days=7)
    week_records = [
        r for r in records
        if start_dt <= datetime.strptime(r["date"], "%Y-%m-%d").date() <= end_dt
    ]

    if not week_records:
        logger.warning("Weekly summary: no records in week ending {}", end_dt)
        return None

    # aggregate
    total_trades = sum(r.get("total_trades", 0) for r in week_records)
    realized_pnl = sum(r.get("realized_pnl", 0) for r in week_records)
    daily_returns = [r.get("daily_return", 0) or 0 for r in week_records]
    avg_return = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    excess_vals = [r.get("same_universe_excess") for r in week_records if r.get("same_universe_excess") is not None]
    avg_excess = sum(excess_vals) / len(excess_vals) if excess_vals else None
    anomaly_count = sum(len(r.get("anomalies", [])) for r in week_records)
    statuses = [r.get("status", "normal") for r in week_records]

    start_val = week_records[0].get("total_value", 0)
    end_val = week_records[-1].get("total_value", 0)
    week_return = ((end_val / start_val) - 1) * 100 if start_val > 0 else 0

    lines = [
        f"# Paper Evidence Weekly Summary: {strategy}",
        f"## Period: {week_records[0]['date']} ~ {week_records[-1]['date']}",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Days | {len(week_records)} |",
        f"| Portfolio Start | {start_val:,.0f} |",
        f"| Portfolio End | {end_val:,.0f} |",
        f"| Week Return | {week_return:+.2f}% |",
        f"| Avg Daily Return | {avg_return:+.4f}% |",
        f"| Total Trades | {total_trades} |",
        f"| Realized PnL | {realized_pnl:+,.0f} |",
        f"| Avg Same-Universe Excess | {f'{avg_excess:+.4f}%' if avg_excess is not None else 'N/A'} |",
        f"| Anomalies | {anomaly_count} |",
        f"| Status History | {', '.join(statuses)} |",
        "",
    ]

    # daily breakdown
    lines.append("## Daily Breakdown")
    lines.append("| Date | Return | Trades | PnL | Excess | Status |")
    lines.append("|------|--------|--------|-----|--------|--------|")
    for r in week_records:
        dr = r.get("daily_return")
        dr_str = f"{dr:+.2f}%" if dr is not None else "N/A"
        ex = r.get("same_universe_excess")
        ex_str = f"{ex:+.4f}" if ex is not None else "N/A"
        lines.append(
            f"| {r['date']} | {dr_str} | {r.get('total_trades', 0)} "
            f"| {r.get('realized_pnl', 0):+,.0f} | {ex_str} | {r.get('status', 'normal')} |"
        )

    # anomalies section
    all_anomalies = []
    for r in week_records:
        for a in r.get("anomalies", []):
            all_anomalies.append({"date": r["date"], **a})
    if all_anomalies:
        lines.extend(["", "## Anomalies"])
        for a in all_anomalies:
            lines.append(f"- **{a['date']}** [{a['severity']}] {a['type']}: {a.get('detail', '')}")

    out_path = EVIDENCE_DIR / f"weekly_summary_{strategy}_{end_dt.strftime('%Y%m%d')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Weekly summary 생성: {}", out_path)
    return out_path


# ═══════════════════════════════════════════════════════════════
# Promotion Package (60일)
# ═══════════════════════════════════════════════════════════════

def generate_promotion_package(strategy: str) -> tuple[Path | None, Path | None]:
    """
    60일 누적 evidence에서 promotion package + approval checklist 생성.
    Returns (package_path, checklist_path) or (None, None).
    approved_strategies.json은 절대 수정하지 않는다.
    """
    all_records = get_canonical_records(strategy)
    if not all_records:
        logger.warning("Promotion package: no evidence for {}", strategy)
        return None, None

    # provenance 분리: execution_backed=True만 승격 카운트
    records = [r for r in all_records if r.get("execution_backed", True)]
    shadow_records = [r for r in all_records if not r.get("execution_backed", True)]
    total_days = len(records)
    shadow_days = len(shadow_records)

    if total_days == 0:
        logger.warning("Promotion package: no execution-backed evidence for {}", strategy)
        # shadow만 있는 경우: blocked package 생성
        records = all_records  # fallback for package generation
        total_days = len(records)

    # aggregate metrics (execution-backed records 기준)
    daily_returns = [r.get("daily_return") for r in records if r.get("daily_return") is not None]
    avg_daily_return = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    cumulative = records[-1].get("cumulative_return", 0)
    mdds = [r.get("mdd") for r in records if r.get("mdd") is not None]
    max_mdd = min(mdds) if mdds else 0

    win = sum(r.get("winning_trades", 0) for r in records)
    lose = sum(r.get("losing_trades", 0) for r in records)
    total_sells = win + lose
    win_rate = (win / total_sells * 100) if total_sells > 0 else 0

    same_excess = [r.get("same_universe_excess") for r in records if r.get("same_universe_excess") is not None]
    avg_same_excess = sum(same_excess) / len(same_excess) if same_excess else None
    exp_excess = [r.get("exposure_matched_excess") for r in records if r.get("exposure_matched_excess") is not None]
    avg_exp_excess = sum(exp_excess) / len(exp_excess) if exp_excess else None
    cash_excess = [r.get("cash_adjusted_excess") for r in records if r.get("cash_adjusted_excess") is not None]
    avg_cash_excess = sum(cash_excess) / len(cash_excess) if cash_excess else None

    # ops averages
    fill_rates = [r.get("raw_fill_rate") for r in records if r.get("raw_fill_rate") is not None]
    avg_fill = sum(fill_rates) / len(fill_rates) if fill_rates else None
    total_rejects = sum(r.get("reject_count", 0) for r in records)
    total_dup_blocked = sum(r.get("duplicate_blocked_count", 0) for r in records)

    # anomaly summary
    degraded_days = sum(1 for r in records if r.get("status") == "degraded")
    frozen_days = sum(1 for r in records if r.get("status") == "frozen")
    anomaly_types: dict[str, int] = {}
    for r in records:
        for a in r.get("anomalies", []):
            t = a.get("type", "unknown")
            anomaly_types[t] = anomaly_types.get(t, 0) + 1

    # benchmark finality check
    benchmark_final_days = sum(1 for r in records if r.get("benchmark_status") == "final")
    benchmark_provisional_days = sum(1 for r in records if r.get("benchmark_status") == "provisional")
    benchmark_failed_days = sum(1 for r in records if r.get("benchmark_status") == "failed")
    benchmark_final_ratio = benchmark_final_days / total_days if total_days > 0 else 0

    # positive evidence 지표
    excess_non_null_days = sum(
        1 for r in records if r.get("same_universe_excess") is not None
    )
    excess_non_null_ratio = excess_non_null_days / total_days if total_days > 0 else 0

    # recommendation
    blocked = False
    block_reasons = []
    if frozen_days > 0:
        blocked = True
        block_reasons.append("frozen_days=%d" % frozen_days)
    if total_days < 60:
        blocked = True
        block_reasons.append("insufficient_days=%d/60" % total_days)
    if max_mdd < -20:
        blocked = True
        block_reasons.append("max_mdd=%.1f%%" % max_mdd)
    if benchmark_final_ratio < PROMOTION_FINAL_RATIO_MIN:
        blocked = True
        block_reasons.append(
            "benchmark_incomplete: final=%d/provisional=%d/failed=%d (%.0f%% < 80%%)" %
            (benchmark_final_days, benchmark_provisional_days, benchmark_failed_days,
             benchmark_final_ratio * 100)
        )
    # insufficient positive evidence: 데이터 없음 vs 성과 부진 구분
    if excess_non_null_ratio < PROMOTION_MIN_EXCESS_DAYS:
        blocked = True
        block_reasons.append(
            "insufficient_evidence: excess_non_null=%d/%d (%.0f%% < %.0f%%)" %
            (excess_non_null_days, total_days,
             excess_non_null_ratio * 100, PROMOTION_MIN_EXCESS_DAYS * 100)
        )
    if avg_same_excess is not None and avg_same_excess <= PROMOTION_MIN_AVG_EXCESS:
        blocked = True
        block_reasons.append("non_positive_same_universe_excess=%.4f" % avg_same_excess)
    if avg_exp_excess is not None and avg_exp_excess <= PROMOTION_MIN_AVG_EXCESS:
        blocked = True
        block_reasons.append("non_positive_exposure_matched_excess=%.4f" % avg_exp_excess)
    if avg_cash_excess is not None and avg_cash_excess <= PROMOTION_MIN_AVG_EXCESS:
        blocked = True
        block_reasons.append("non_positive_cash_adjusted_excess=%.4f" % avg_cash_excess)
    if (cumulative or 0) <= PROMOTION_MIN_CUMULATIVE_RETURN:
        blocked = True
        block_reasons.append("non_positive_cumulative_return=%.2f%%" % (cumulative or 0))

    sell_count = sum(r.get("sell_count", 0) for r in records)
    if sell_count < PROMOTION_MIN_SELL_TRADES:
        blocked = True
        block_reasons.append(
            "insufficient_sell_trades=%d/%d" % (sell_count, PROMOTION_MIN_SELL_TRADES)
        )
    if total_sells > 0 and win_rate < PROMOTION_MIN_WIN_RATE:
        blocked = True
        block_reasons.append(
            "low_win_rate=%.1f%% < %.1f%%" % (win_rate, PROMOTION_MIN_WIN_RATE)
        )

    package = {
        "strategy": strategy,
        "generated_at": datetime.now().isoformat(),
        "period": f"{records[0]['date']} ~ {records[-1]['date']}",
        "total_days": total_days,
        "avg_daily_return": round(avg_daily_return, 4),
        "cumulative_return": round(cumulative, 2) if cumulative else 0,
        "max_mdd": round(max_mdd, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": sum(r.get("total_trades", 0) for r in records),
        "sell_count": sell_count,
        "avg_same_universe_excess": round(avg_same_excess, 4) if avg_same_excess is not None else None,
        "avg_exposure_matched_excess": round(avg_exp_excess, 4) if avg_exp_excess is not None else None,
        "avg_cash_adjusted_excess": round(avg_cash_excess, 4) if avg_cash_excess is not None else None,
        "avg_fill_rate": round(avg_fill, 4) if avg_fill is not None else None,
        "total_rejects": total_rejects,
        "total_duplicate_blocked": total_dup_blocked,
        "degraded_days": degraded_days,
        "frozen_days": frozen_days,
        "anomaly_summary": anomaly_types,
        "benchmark_final_days": benchmark_final_days,
        "benchmark_provisional_days": benchmark_provisional_days,
        "benchmark_failed_days": benchmark_failed_days,
        "benchmark_final_ratio": round(benchmark_final_ratio, 4),
        "excess_non_null_days": excess_non_null_days,
        "excess_non_null_ratio": round(excess_non_null_ratio, 4),
        # provenance 분리 (pilot / non-pilot / shadow)
        "real_paper_days_total": sum(1 for r in all_records if r.get("execution_backed", True)),
        "pilot_real_paper_days": sum(
            1 for r in all_records
            if r.get("execution_backed", True)
            and (r.get("evidence_mode") == "pilot_paper"
                 or r.get("session_mode") == "pilot_paper")
        ),
        "non_pilot_real_paper_days": sum(
            1 for r in all_records
            if r.get("execution_backed", True)
            and r.get("evidence_mode") != "pilot_paper"
            and r.get("session_mode", "normal_paper") != "pilot_paper"
        ),
        "shadow_days": shadow_days,
        # backward compat aliases
        "real_paper_days": sum(1 for r in all_records if r.get("execution_backed", True)),
        "promotable_evidence_days": sum(1 for r in all_records if r.get("execution_backed", True)),
        "non_promotable_shadow_days": shadow_days,
        "recommendation": "BLOCKED" if blocked else "ELIGIBLE",
        "block_reasons": block_reasons if blocked else [],
    }

    pkg_path = EVIDENCE_DIR / f"promotion_evidence_{strategy}.json"
    pkg_path.parent.mkdir(parents=True, exist_ok=True)
    pkg_path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Promotion package 생성: {}", pkg_path)

    # approval checklist
    cl_path = _generate_approval_checklist(strategy, package)

    return pkg_path, cl_path


def _generate_approval_checklist(strategy: str, package: dict) -> Path:
    rec = package["recommendation"]
    lines = [
        f"# Approval Checklist: {strategy}",
        f"Generated: {package['generated_at']}",
        "",
        "## WARNING",
        "> approved_strategies.json은 이 도구로 절대 자동 수정되지 않습니다.",
        "> live 전환은 반드시 수동 승인 후 approved_strategies.json을 직접 업데이트하세요.",
        "",
        f"## Recommendation: **{rec}**",
    ]
    if package.get("block_reasons"):
        reasons_str = ", ".join(package["block_reasons"])
        lines.append(f"Block reasons: {reasons_str}")

    cum_ret = package.get("cumulative_return", 0)
    max_mdd_val = package.get("max_mdd", 0)
    win_rate_val = package.get("win_rate", 0)
    total_trades_val = package.get("total_trades", 0)
    same_univ = package.get("avg_same_universe_excess", "N/A")
    exp_match = package.get("avg_exposure_matched_excess", "N/A")
    cash_adj = package.get("avg_cash_adjusted_excess", "N/A")
    avg_fill = package.get("avg_fill_rate", "N/A")
    total_rej = package.get("total_rejects", 0)
    anom_json = json.dumps(package.get("anomaly_summary", {}), ensure_ascii=False)

    lines.append("")
    lines.append("## Performance Summary")
    lines.append("- Period: " + package["period"])
    lines.append("- Total Days: " + str(package["total_days"]))
    lines.append("- Real Paper Days (total): " + str(package.get("real_paper_days_total", "N/A")))
    lines.append("  - Pilot Real Paper Days: " + str(package.get("pilot_real_paper_days", 0)))
    lines.append("  - Non-Pilot Real Paper Days: " + str(package.get("non_pilot_real_paper_days", 0)))
    lines.append("- Shadow Days: " + str(package.get("shadow_days", 0)))
    lines.append("- Cumulative Return: %+.2f%%" % cum_ret)
    lines.append("- Max MDD: %.2f%%" % max_mdd_val)
    lines.append("- Win Rate: %.1f%%" % win_rate_val)
    lines.append("- Total Trades: " + str(total_trades_val))
    lines.append("")
    lines.append("## Benchmark Excess (Averages)")
    lines.append("- Same Universe: " + str(same_univ))
    lines.append("- Exposure Matched: " + str(exp_match))
    lines.append("- Cash Adjusted: " + str(cash_adj))
    lines.append("")
    lines.append("## Operational Health")
    lines.append("- Avg Fill Rate: " + str(avg_fill))
    lines.append("- Total Rejects: " + str(total_rej))
    lines.append("- Degraded Days: " + str(package["degraded_days"]))
    lines.append("- Frozen Days: " + str(package["frozen_days"]))
    lines.append("- Anomaly Summary: " + anom_json)
    lines.append("")
    lines.append("## Manual Approval Checklist")
    lines.append("- [ ] Walk-Forward validation 통과 확인")
    lines.append("- [ ] Benchmark excess return 양수 확인")
    lines.append("- [ ] 60영업일 paper 운영 기록 확인")
    lines.append("- [ ] Anomaly history 검토 완료")
    lines.append("- [ ] 운영 메트릭 (fill rate, reject rate) 정상 범위 확인")
    lines.append("- [ ] config_hash 변경 여부 확인")
    lines.append("- [ ] approved_strategies.json 수동 업데이트 완료")
    lines.append("- [ ] 승인자 서명: _______________")
    lines.append("")
    lines.append("---")
    lines.append("이 문서는 recommendation만 제공합니다. Live 전환은 별도의 명시적 승인이 필요합니다.")

    cl_path = EVIDENCE_DIR / f"approval_checklist_{strategy}.md"
    cl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Approval checklist 생성: {}", cl_path)
    return cl_path


# ═══════════════════════════════════════════════════════════════
# Evidence Quality Report
# ═══════════════════════════════════════════════════════════════

def generate_evidence_quality_report(strategy: str) -> tuple[dict, Path | None]:
    """
    전략별 evidence 품질 요약 report 생성.
    60일 promotion package의 입력으로 재사용 가능.

    Returns: (report_dict, report_path) or ({}, None)
    """
    records = get_canonical_records(strategy)
    if not records:
        logger.warning("Evidence quality report: no data for {}", strategy)
        return {}, None

    total = len(records)

    # benchmark non-null ratio
    bench_non_null = sum(
        1 for r in records if r.get("same_universe_excess") is not None
    )
    bench_non_null_ratio = bench_non_null / total if total > 0 else 0

    # provisional → final conversion ratio
    final_days = sum(1 for r in records if r.get("benchmark_status") == "final")
    provisional_days = sum(1 for r in records if r.get("benchmark_status") == "provisional")
    failed_days = sum(1 for r in records if r.get("benchmark_status") == "failed")
    final_conversion_ratio = final_days / total if total > 0 else 0

    # final benchmark completeness 분포
    completeness_values = []
    for r in records:
        meta = r.get("benchmark_meta", {})
        c = meta.get("completeness")
        if c is not None and r.get("benchmark_status") == "final":
            completeness_values.append(c)
    completeness_min = min(completeness_values) if completeness_values else None
    completeness_max = max(completeness_values) if completeness_values else None
    completeness_avg = (
        sum(completeness_values) / len(completeness_values) if completeness_values else None
    )

    # cross-validation mismatch count
    xv_mismatch = sum(
        1 for r in records
        if any(a.get("type") == "cross_validation_mismatch" for a in r.get("anomalies", []))
    )

    # restart_recovery_count
    total_recovery = sum(r.get("restart_recovery_count", 0) for r in records)

    # anomaly rate
    days_with_anomaly = sum(1 for r in records if r.get("anomalies"))
    anomaly_rate = days_with_anomaly / total if total > 0 else 0

    # anomaly type breakdown
    anomaly_types: dict[str, int] = {}
    for r in records:
        for a in r.get("anomalies", []):
            t = a.get("type", "unknown")
            anomaly_types[t] = anomaly_types.get(t, 0) + 1

    report = {
        "strategy": strategy,
        "generated_at": datetime.now().isoformat(),
        "total_days": total,
        "period": f"{records[0]['date']} ~ {records[-1]['date']}",
        "benchmark_non_null_days": bench_non_null,
        "benchmark_non_null_ratio": round(bench_non_null_ratio, 4),
        "provisional_to_final_conversion": {
            "final_days": final_days,
            "provisional_days": provisional_days,
            "failed_days": failed_days,
            "conversion_ratio": round(final_conversion_ratio, 4),
        },
        "final_completeness_distribution": {
            "min": round(completeness_min, 4) if completeness_min is not None else None,
            "max": round(completeness_max, 4) if completeness_max is not None else None,
            "avg": round(completeness_avg, 4) if completeness_avg is not None else None,
            "count": len(completeness_values),
        },
        "cross_validation_mismatch_count": xv_mismatch,
        "restart_recovery_count": total_recovery,
        "anomaly_rate": round(anomaly_rate, 4),
        "anomaly_type_breakdown": anomaly_types,
    }

    out_path = EVIDENCE_DIR / f"evidence_quality_{strategy}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Evidence quality report 생성: {}", out_path)
    return report, out_path
