"""
Transaction cost impact helpers for backtest reports.

The summary is an explicit-cost estimate: it adds recorded commission, tax,
and slippage diagnostics back to final equity to show how much reported net
performance depends on trading friction.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _sum_trade_field(trades: list[dict[str, Any]] | None, field: str) -> float:
    if not trades:
        return 0.0
    return sum(_num(t.get(field)) for t in trades)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def summarize_cost_impact(
    metrics: dict[str, Any],
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a standard before/after cost summary from metrics and trades."""
    initial_capital = _num(metrics.get("initial_capital"))
    net_return_pct = _num(metrics.get("total_return"))
    final_value = _num(metrics.get("final_value"))
    if final_value <= 0 and initial_capital > 0:
        final_value = initial_capital * (1 + net_return_pct / 100)

    total_commission = _num(
        metrics.get("total_commission"),
        _sum_trade_field(trades, "commission"),
    )
    total_tax = _num(
        metrics.get("total_tax"),
        _sum_trade_field(trades, "tax"),
    )
    total_slippage_cost = _num(
        metrics.get("total_slippage_cost"),
        _sum_trade_field(trades, "slippage_cost"),
    )
    total_transaction_cost = _num(
        metrics.get("total_transaction_cost"),
        total_commission + total_tax + total_slippage_cost,
    )

    gross_final_value_estimate = final_value + total_transaction_cost
    gross_return_estimate_pct = None
    cost_drag_pct = None
    cost_to_initial_capital_pct = None
    if initial_capital > 0:
        gross_return_estimate_pct = (gross_final_value_estimate / initial_capital - 1) * 100
        cost_drag_pct = gross_return_estimate_pct - net_return_pct
        cost_to_initial_capital_pct = total_transaction_cost / initial_capital * 100

    net_profit = final_value - initial_capital
    cost_to_net_profit_pct = None
    if abs(net_profit) > 1e-6:
        cost_to_net_profit_pct = total_transaction_cost / abs(net_profit) * 100

    if total_transaction_cost > 0:
        commission_share_pct = total_commission / total_transaction_cost * 100
        tax_share_pct = total_tax / total_transaction_cost * 100
        slippage_share_pct = total_slippage_cost / total_transaction_cost * 100
    else:
        commission_share_pct = 0.0
        tax_share_pct = 0.0
        slippage_share_pct = 0.0

    issues: list[str] = []
    status = "pass"
    if total_transaction_cost > 0:
        if (
            gross_return_estimate_pct is not None
            and gross_return_estimate_pct > 0
            and net_return_pct <= 0
        ):
            status = "fail"
            issues.append("costs_flip_profitable_gross_to_net_loss")
        elif (
            cost_to_net_profit_pct is not None
            and net_profit > 0
            and cost_to_net_profit_pct >= 100
        ):
            status = "fail"
            issues.append("costs_exceed_net_profit")
        elif (
            cost_drag_pct is not None
            and cost_drag_pct >= 1.0
        ) or (
            cost_to_net_profit_pct is not None
            and net_profit > 0
            and cost_to_net_profit_pct >= 50
        ):
            status = "warn"
            issues.append("material_cost_drag")

    return {
        "status": status,
        "issues": issues,
        "initial_capital": _round(initial_capital, 0),
        "net_final_value": _round(final_value, 0),
        "net_return_pct": _round(net_return_pct),
        "gross_final_value_estimate": _round(gross_final_value_estimate, 0),
        "gross_return_estimate_pct": _round(gross_return_estimate_pct),
        "cost_drag_pct": _round(cost_drag_pct),
        "cost_drag_bps": _round(cost_drag_pct * 100 if cost_drag_pct is not None else None, 1),
        "cost_to_initial_capital_pct": _round(cost_to_initial_capital_pct, 3),
        "cost_to_net_profit_pct": _round(cost_to_net_profit_pct, 1),
        "total_commission": _round(total_commission, 0),
        "total_tax": _round(total_tax, 0),
        "total_slippage_cost": _round(total_slippage_cost, 0),
        "total_transaction_cost": _round(total_transaction_cost, 0),
        "commission_share_pct": _round(commission_share_pct, 1),
        "tax_share_pct": _round(tax_share_pct, 1),
        "slippage_share_pct": _round(slippage_share_pct, 1),
    }


def cost_impact_metric_fields(summary: dict[str, Any]) -> dict[str, Any]:
    """Flatten selected cost summary values into a metrics dict."""
    return {
        "gross_return": summary.get("gross_return_estimate_pct"),
        "cost_drag_pct": summary.get("cost_drag_pct"),
        "cost_drag_bps": summary.get("cost_drag_bps"),
        "cost_to_initial_capital_pct": summary.get("cost_to_initial_capital_pct"),
        "cost_to_net_profit_pct": summary.get("cost_to_net_profit_pct"),
        "cost_impact_status": summary.get("status"),
        "cost_impact_issues": summary.get("issues", []),
    }


def render_cost_impact_text(summary: dict[str, Any], indent: str = "  ") -> list[str]:
    """Render a compact text block for console and txt reports."""
    cnp = summary.get("cost_to_net_profit_pct")
    cnp_text = "N/A" if cnp is None else f"{cnp:.1f}%"
    return [
        f"{indent}비용 차감 전 추정 수익률 : {summary.get('gross_return_estimate_pct', 0):>8.2f}%",
        f"{indent}비용 차감 후 수익률      : {summary.get('net_return_pct', 0):>8.2f}%",
        f"{indent}비용 드래그             : {summary.get('cost_drag_pct', 0):>8.2f}% ({summary.get('cost_drag_bps', 0):.1f}bp)",
        f"{indent}총 거래비용             : {summary.get('total_transaction_cost', 0):>12,.0f}원 "
        f"(수수료 {summary.get('total_commission', 0):,.0f} / "
        f"세금 {summary.get('total_tax', 0):,.0f} / "
        f"슬리피지 {summary.get('total_slippage_cost', 0):,.0f})",
        f"{indent}비용/순손익             : {cnp_text} | 상태: {summary.get('status', 'pass')}",
    ]
