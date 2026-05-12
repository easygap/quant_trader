"""
Paper 체결 품질 리포트.

실행 예:
  python tools/paper_trade_quality_report.py --account-key scoring
  python tools/paper_trade_quality_report.py --start-date 2026-05-01 --end-date 2026-05-12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DEFAULT_OUTPUT_DIR = Path("reports/paper_runtime")


def _parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt + timedelta(days=1) - timedelta(microseconds=1)
    return dt


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _trade_to_row(trade) -> dict[str, Any]:
    action = (getattr(trade, "action", "") or "").upper()
    price = float(getattr(trade, "price", 0) or 0)
    quantity = int(getattr(trade, "quantity", 0) or 0)
    expected_price = getattr(trade, "expected_price", None)
    expected = float(expected_price) if expected_price is not None else None
    price_gap = getattr(trade, "price_gap", None)
    if price_gap is None and expected is not None:
        price_gap = price - expected
    price_gap = float(price_gap) if price_gap is not None else None
    gap_pct = (price_gap / expected * 100) if expected and price_gap is not None else None
    notional = abs(price * quantity)
    if action == "BUY" and price_gap is not None:
        signed_gap_cost = price_gap * quantity
    elif action != "BUY" and price_gap is not None:
        signed_gap_cost = -price_gap * quantity
    else:
        signed_gap_cost = None
    executed_at = getattr(trade, "executed_at", None)
    return {
        "id": getattr(trade, "id", None),
        "date": executed_at.date().isoformat() if executed_at else None,
        "executed_at": executed_at.isoformat() if executed_at else None,
        "account_key": getattr(trade, "account_key", "") or "",
        "strategy": getattr(trade, "strategy", "") or "",
        "symbol": getattr(trade, "symbol", "") or "",
        "action": action,
        "price": price,
        "quantity": quantity,
        "expected_price": expected,
        "price_gap": price_gap,
        "price_gap_pct": gap_pct,
        "signed_gap_cost": signed_gap_cost,
        "notional": notional,
        "slippage_cost": float(getattr(trade, "slippage", 0) or 0),
        "actual_slippage_pct": getattr(trade, "actual_slippage_pct", None),
        "execution_session_id": getattr(trade, "execution_session_id", "") or "",
        "order_id": getattr(trade, "order_id", "") or "",
        "reason": getattr(trade, "reason", "") or "",
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    with_expected = [row for row in rows if row["expected_price"] is not None and row["price_gap"] is not None]
    buy_count = sum(1 for row in rows if row["action"] == "BUY")
    sell_count = total - buy_count
    signed_costs = [float(row["signed_gap_cost"]) for row in with_expected if row["signed_gap_cost"] is not None]
    gap_pcts = [float(row["price_gap_pct"]) for row in with_expected if row["price_gap_pct"] is not None]
    slippage_pcts = [
        float(row["actual_slippage_pct"])
        for row in rows
        if row["actual_slippage_pct"] is not None
    ]
    notional = sum(float(row["notional"] or 0) for row in rows)
    signed_gap_cost = sum(signed_costs)
    adverse_gap_cost = sum(cost for cost in signed_costs if cost > 0)
    favorable_gap_cost = sum(cost for cost in signed_costs if cost < 0)
    missing_expected = total - len(with_expected)
    return {
        "trade_count": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "with_expected_price_count": len(with_expected),
        "missing_expected_price_count": missing_expected,
        "missing_expected_price_ratio": _round_or_none(missing_expected / total if total else None),
        "total_notional": round(notional, 2),
        "signed_gap_cost": round(signed_gap_cost, 2),
        "adverse_gap_cost": round(adverse_gap_cost, 2),
        "favorable_gap_cost": round(favorable_gap_cost, 2),
        "gap_cost_bps_of_notional": _round_or_none(signed_gap_cost / notional * 10000 if notional else None, 2),
        "adverse_gap_bps_of_notional": _round_or_none(adverse_gap_cost / notional * 10000 if notional else None, 2),
        "avg_price_gap_pct": _round_or_none(sum(gap_pcts) / len(gap_pcts) if gap_pcts else None),
        "avg_abs_price_gap_pct": _round_or_none(
            sum(abs(value) for value in gap_pcts) / len(gap_pcts) if gap_pcts else None
        ),
        "max_abs_price_gap_pct": _round_or_none(max((abs(value) for value in gap_pcts), default=None)),
        "adverse_fill_count": sum(1 for cost in signed_costs if cost > 0),
        "total_slippage_cost": round(sum(float(row["slippage_cost"] or 0) for row in rows), 2),
        "avg_actual_slippage_pct": _round_or_none(
            sum(slippage_pcts) / len(slippage_pcts) if slippage_pcts else None
        ),
        "avg_abs_actual_slippage_pct": _round_or_none(
            sum(abs(value) for value in slippage_pcts) / len(slippage_pcts) if slippage_pcts else None
        ),
    }


def _group_summary(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    return {name: _summarize_rows(items) for name, items in sorted(grouped.items())}


def _quality_status(summary: dict[str, Any], *, max_gap_cost_bps: float, max_missing_expected_ratio: float) -> tuple[str, list[str]]:
    issues = []
    if summary["trade_count"] == 0:
        return "no_trades", ["선택한 기간에 paper 체결 기록이 없습니다."]
    missing_ratio = summary.get("missing_expected_price_ratio")
    if missing_ratio is not None and missing_ratio > max_missing_expected_ratio:
        issues.append(
            "expected_price 누락 비율 "
            f"{missing_ratio:.1%} > {max_missing_expected_ratio:.1%}"
        )
    gap_bps = summary.get("adverse_gap_bps_of_notional")
    if gap_bps is not None and gap_bps > max_gap_cost_bps:
        issues.append(f"불리한 체결 갭 {gap_bps:.2f}bp > {max_gap_cost_bps:.2f}bp")
    if issues:
        return "review", issues
    return "ok", []


def build_paper_trade_quality_report(
    *,
    mode: str = "paper",
    account_key: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_gap_cost_bps: float = 50.0,
    max_missing_expected_ratio: float = 0.0,
) -> dict[str, Any]:
    """TradeHistory 기반 paper 체결 품질 요약을 만든다."""
    from database.models import TradeHistory, get_session

    session = get_session()
    try:
        query = session.query(TradeHistory).filter(TradeHistory.mode == mode)
        if account_key is not None:
            query = query.filter(TradeHistory.account_key == (account_key or ""))
        if start_date is not None:
            query = query.filter(TradeHistory.executed_at >= start_date)
        if end_date is not None:
            query = query.filter(TradeHistory.executed_at <= end_date)
        trades = query.order_by(TradeHistory.executed_at.asc(), TradeHistory.id.asc()).all()
        rows = [_trade_to_row(trade) for trade in trades]
    finally:
        session.close()

    summary = _summarize_rows(rows)
    status, issues = _quality_status(
        summary,
        max_gap_cost_bps=max_gap_cost_bps,
        max_missing_expected_ratio=max_missing_expected_ratio,
    )
    return {
        "artifact_type": "paper_trade_quality_report",
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(),
        "mode": mode,
        "account_key": account_key,
        "period": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
        "thresholds": {
            "max_gap_cost_bps": max_gap_cost_bps,
            "max_missing_expected_ratio": max_missing_expected_ratio,
        },
        "quality_status": status,
        "issues": issues,
        "summary": summary,
        "by_date": _group_summary(rows, "date"),
        "by_symbol": _group_summary(rows, "symbol"),
        "trades": rows,
    }


def _report_stem(report: dict[str, Any]) -> str:
    account = report.get("account_key") or "all"
    start = (report.get("period") or {}).get("start")
    end = (report.get("period") or {}).get("end")
    start_part = start[:10] if start else "all"
    end_part = end[:10] if end else "latest"
    safe_account = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(account))
    return f"paper_trade_quality_{safe_account}_{start_part}_{end_part}"


def write_paper_trade_quality_report(report: dict[str, Any], output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _report_stem(report)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    summary = report.get("summary") or {}
    lines = [
        "# Paper Trade Quality Report",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Mode: {report.get('mode')}",
        f"- Account: {report.get('account_key') or 'all'}",
        f"- Status: {report.get('quality_status')}",
        f"- Trades: {summary.get('trade_count', 0)}",
        f"- Adverse gap cost: {summary.get('adverse_gap_cost', 0)}",
        f"- Adverse gap bps: {summary.get('adverse_gap_bps_of_notional')}",
        f"- Missing expected price: {summary.get('missing_expected_price_count', 0)}",
        "",
    ]
    issues = report.get("issues") or []
    if issues:
        lines.extend(["## Issues", ""])
        lines.extend(f"- {issue}" for issue in issues)
        lines.append("")

    lines.extend([
        "## Daily Summary",
        "",
        "| Date | Trades | Adverse Gap Cost | Adverse Gap bp | Avg Abs Gap % | Missing Expected |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for date, item in (report.get("by_date") or {}).items():
        lines.append(
            "| "
            + " | ".join([
                _md_cell(date),
                _md_cell(item.get("trade_count")),
                _md_cell(item.get("adverse_gap_cost")),
                _md_cell(item.get("adverse_gap_bps_of_notional")),
                _md_cell(item.get("avg_abs_price_gap_pct")),
                _md_cell(item.get("missing_expected_price_count")),
            ])
            + " |"
        )

    lines.extend([
        "",
        "## Symbol Summary",
        "",
        "| Symbol | Trades | Adverse Gap Cost | Adverse Gap bp | Avg Abs Gap % | Slippage Cost |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for symbol, item in (report.get("by_symbol") or {}).items():
        lines.append(
            "| "
            + " | ".join([
                _md_cell(symbol),
                _md_cell(item.get("trade_count")),
                _md_cell(item.get("adverse_gap_cost")),
                _md_cell(item.get("adverse_gap_bps_of_notional")),
                _md_cell(item.get("avg_abs_price_gap_pct")),
                _md_cell(item.get("total_slippage_cost")),
            ])
            + " |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper 체결 품질 리포트 생성")
    parser.add_argument("--mode", default="paper", help="조회할 TradeHistory mode")
    parser.add_argument("--account-key", default=None, help="전략/account_key 필터")
    parser.add_argument("--start-date", default=None, help="조회 시작일 또는 ISO datetime")
    parser.add_argument("--end-date", default=None, help="조회 종료일 또는 ISO datetime")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="리포트 출력 디렉터리")
    parser.add_argument("--max-gap-cost-bps", type=float, default=50.0, help="review 기준 불리한 체결 갭 bp")
    parser.add_argument(
        "--max-missing-expected-ratio",
        type=float,
        default=0.0,
        help="review 기준 expected_price 누락 비율",
    )
    args = parser.parse_args()

    report = build_paper_trade_quality_report(
        mode=args.mode,
        account_key=args.account_key,
        start_date=_parse_date(args.start_date),
        end_date=_parse_date(args.end_date, end_of_day=True),
        max_gap_cost_bps=args.max_gap_cost_bps,
        max_missing_expected_ratio=args.max_missing_expected_ratio,
    )
    json_path, md_path = write_paper_trade_quality_report(report, args.output_dir)
    print(f"OK: paper trade quality report 생성 성공\n  {json_path}\n  {md_path}")
    if report.get("quality_status") == "review":
        sys.exit(2)


if __name__ == "__main__":
    main()
