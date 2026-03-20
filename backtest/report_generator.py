"""
백테스팅 결과 리포트 생성 모듈
- 텍스트 리포트 및 HTML 리포트 생성
- 거래 내역, 성과 지표, 자본 곡선 시각화
- 코스피(KS11) 월 수익률 기준 시장 국면별(상승/하락/횡보) 성과 분해
"""

import base64
import html
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

_TRADES_WITH_PNL = frozenset(
    (
        "SELL",
        "STOP_LOSS",
        "TAKE_PROFIT",
        "TAKE_PROFIT_PARTIAL",
        "TRAILING_STOP",
        "MAX_HOLD",
    )
)


def _overtrading_charts_png_base64(trades: list, equity: pd.DataFrame) -> str:
    """월별 거래 횟수(막대) + 누적 수수료·누적 실현손익(선). matplotlib Agg → PNG base64."""
    if not trades or equity is None or equity.empty:
        return ""

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib 미설치 — 과매매 차트 생략")
        return ""

    tdf = pd.DataFrame(trades)
    if tdf.empty or "date" not in tdf.columns:
        return ""

    if "commission" not in tdf.columns:
        tdf["commission"] = 0.0
    if "pnl" not in tdf.columns:
        tdf["pnl"] = 0.0

    tdf["d"] = pd.to_datetime(tdf["date"])
    tdf["ym"] = tdf["d"].dt.to_period("M")
    monthly_counts = tdf.groupby("ym", sort=True).size()
    if monthly_counts.empty:
        return ""

    eq0 = equity.copy()
    eq0["d"] = pd.to_datetime(eq0["date"]).dt.normalize()
    d0, d1 = eq0["d"].min(), eq0["d"].max()
    idx = pd.date_range(d0, d1, freq="D")
    tdf["dn"] = tdf["d"].dt.normalize()
    comm_day = tdf.groupby("dn")["commission"].sum()
    tdf_p = tdf.loc[tdf["action"].isin(_TRADES_WITH_PNL)]
    pnl_day = tdf_p.groupby(tdf_p["dn"])["pnl"].sum() if not tdf_p.empty else pd.Series(dtype=float)
    cum_c = comm_day.reindex(idx, fill_value=0).cumsum()
    cum_p = pnl_day.reindex(idx, fill_value=0).cumsum()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), dpi=110)
    fig.patch.set_facecolor("#0f172a")
    for ax in (ax1, ax2):
        ax.set_facecolor("#1e293b")
        ax.tick_params(colors="#94a3b8")
        ax.spines["bottom"].set_color("#334155")
        ax.spines["top"].set_color("#334155")
        ax.spines["left"].set_color("#334155")
        ax.spines["right"].set_color("#334155")
        ax.yaxis.label.set_color("#e2e8f0")
        ax.xaxis.label.set_color("#e2e8f0")
        ax.title.set_color("#e2e8f0")

    x_m = [p.to_timestamp() for p in monthly_counts.index]
    ax1.bar(x_m, monthly_counts.values, width=20, color="#4f9ef8", edgecolor="#1e40af", linewidth=0.5)
    ax1.set_title("월별 거래 횟수 (전체 체결)")
    ax1.set_ylabel("건수")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=35, ha="right")

    ax2.plot(idx, cum_c.values, label="누적 수수료", color="#f97316", linewidth=1.8)
    ax2.plot(idx, cum_p.values, label="누적 실현 손익", color="#22c55e", linewidth=1.8)
    ax2.set_title("누적 수수료 vs 누적 실현 손익")
    ax2.set_ylabel("원")
    ax2.legend(facecolor="#1e293b", edgecolor="#334155", labelcolor="#e2e8f0")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=35, ha="right")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")

# 월간 코스피 수익률 기준 국면 (일별→월별 집계 후 분류)
REGIME_BULL_THRESHOLD = 0.02
REGIME_BEAR_THRESHOLD = -0.02

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_SIDEWAYS = "sideways"

REGIME_LABELS = {
    REGIME_BULL: "상승장(+2%)",
    REGIME_BEAR: "하락장(-2%)",
    REGIME_SIDEWAYS: "횡보장",
}


def _strategy_monthly_returns(equity: pd.DataFrame) -> pd.Series:
    """자본 곡선에서 월말 기준 월간 수익률 (당월 첫일 자산 대비 말일)."""
    if equity.empty or "date" not in equity.columns or "value" not in equity.columns:
        return pd.Series(dtype=float)
    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.sort_values("date")
    eq["period"] = eq["date"].dt.to_period("M")
    g = eq.groupby("period", sort=True)["value"].agg(["first", "last"])
    ret = g["last"] / g["first"] - 1.0
    return ret


def _kospi_monthly_returns_from_ohlc(ks11: pd.DataFrame) -> pd.Series:
    """KS11 일봉( DatetimeIndex + close ) → 월간 수익률."""
    if ks11 is None or ks11.empty or "close" not in ks11.columns:
        return pd.Series(dtype=float)
    d = ks11.copy()
    if not isinstance(d.index, pd.DatetimeIndex):
        d.index = pd.to_datetime(d.index)
    d = d.sort_index()
    d["period"] = d.index.to_period("M")
    g = d.groupby("period", sort=True)["close"].agg(["first", "last"])
    return g["last"] / g["first"] - 1.0


def _classify_regime(kospi_monthly_ret: float) -> str:
    if kospi_monthly_ret > REGIME_BULL_THRESHOLD:
        return REGIME_BULL
    if kospi_monthly_ret < REGIME_BEAR_THRESHOLD:
        return REGIME_BEAR
    return REGIME_SIDEWAYS


def _monthly_sharpe(monthly_rets: List[float], risk_free_annual: float = 0.03) -> float:
    arr = np.array(monthly_rets, dtype=float)
    if len(arr) < 2:
        return 0.0
    rf_m = risk_free_annual / 12.0
    xs = arr - rf_m
    std = float(np.std(xs, ddof=1))
    if std <= 0:
        return 0.0
    return float(np.mean(xs) / std * np.sqrt(12))


def _mdd_from_month_end_equity(equity: pd.DataFrame, periods: List) -> float:
    """지정된 월(Period)들에 대해 월말 자산만 이어 MDD(%)."""
    if equity.empty or not periods:
        return 0.0
    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    eq["period"] = eq["date"].dt.to_period("M")
    me = eq.groupby("period", sort=True)["value"].last()
    sub = me[me.index.isin(periods)].sort_index()
    if len(sub) < 2:
        return 0.0
    peak = sub.cummax()
    dd = (sub - peak) / peak
    return float(dd.min() * 100)


def compute_market_regime_breakdown(
    result: dict,
    warn_bear_underperformance: bool = True,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    코스피(KS11) 월 수익률로 국면을 나누고, 전략의 월별 성과를 집계한다.

    Returns:
        { "bull": {n_months, avg_strat_pct, avg_kospi_pct, excess_pct, sharpe, mdd_pct, periods}, ... }
        데이터 부족 시 None.

    warn_bear_underperformance:
        True이고 하락장 평균 초과수익이 음이면 logger.warning 1회.
    """
    equity = result.get("equity_curve")
    if equity is None or equity.empty:
        return None

    eq_start = pd.to_datetime(equity["date"]).min()
    eq_end = pd.to_datetime(equity["date"]).max()
    start_s = eq_start.strftime("%Y-%m-%d")
    end_s = eq_end.strftime("%Y-%m-%d")

    try:
        from core.data_collector import DataCollector

        ks11 = DataCollector().fetch_korean_stock("KS11", start_date=start_s, end_date=end_s)
    except Exception as e:
        logger.warning("시장 국면 분석: KS11 수집 실패 — {}", e)
        return None

    if ks11 is None or ks11.empty:
        logger.warning("시장 국면 분석: KS11 데이터 없음")
        return None

    kospi_m = _kospi_monthly_returns_from_ohlc(ks11)
    strat_m = _strategy_monthly_returns(equity)
    common = strat_m.index.intersection(kospi_m.index)
    if len(common) == 0:
        logger.warning("시장 국면 분석: 전략·코스피 공통 월 없음")
        return None

    buckets: Dict[str, Dict[str, List]] = {
        REGIME_BULL: {"periods": [], "strat": [], "kospi": []},
        REGIME_BEAR: {"periods": [], "strat": [], "kospi": []},
        REGIME_SIDEWAYS: {"periods": [], "strat": [], "kospi": []},
    }

    for p in common:
        rk = float(kospi_m.loc[p])
        rs = float(strat_m.loc[p])
        reg = _classify_regime(rk)
        buckets[reg]["periods"].append(p)
        buckets[reg]["strat"].append(rs)
        buckets[reg]["kospi"].append(rk)

    out: Dict[str, Dict[str, Any]] = {}
    for key in (REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS):
        b = buckets[key]
        n = len(b["periods"])
        if n == 0:
            out[key] = {
                "n_months": 0,
                "avg_strat_pct": 0.0,
                "avg_kospi_pct": 0.0,
                "excess_pct": 0.0,
                "sharpe": 0.0,
                "mdd_pct": 0.0,
                "periods": [],
            }
            continue
        avg_s = float(np.mean(b["strat"]) * 100)
        avg_k = float(np.mean(b["kospi"]) * 100)
        out[key] = {
            "n_months": n,
            "avg_strat_pct": round(avg_s, 2),
            "avg_kospi_pct": round(avg_k, 2),
            "excess_pct": round(avg_s - avg_k, 2),
            "sharpe": round(_monthly_sharpe(b["strat"]), 2),
            "mdd_pct": round(_mdd_from_month_end_equity(equity, b["periods"]), 2),
            "periods": b["periods"],
        }

    if warn_bear_underperformance:
        bear_excess = out[REGIME_BEAR]["excess_pct"]
        if out[REGIME_BEAR]["n_months"] > 0 and bear_excess < 0:
            logger.warning(
                "[리포트] 하락장에서 벤치마크 대비 성과 부진. 시장 국면 필터 강화 검토 권장."
            )

    return out


def _format_regime_text_table(breakdown: Dict[str, Dict[str, Any]]) -> List[str]:
    lines = [
        "",
        "=== 시장 국면별 성과 ===",
        "국면          | 기간(개월) | 전략 평균 월수익률 | 코스피 월수익률 | 초과 수익",
        "-" * 72,
    ]
    for key in (REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS):
        r = breakdown[key]
        label = REGIME_LABELS[key]
        lines.append(
            f"{label:14} | {r['n_months']:>8}개월 | {r['avg_strat_pct']:>+14.1f}% | "
            f"{r['avg_kospi_pct']:>+12.1f}% | {r['excess_pct']:>+7.1f}%"
        )
    lines.append("-" * 72)
    lines.append("[ 국면별 위험지표 — 월간 수익 기준 샤프, 월말 자산 기준 MDD ]")
    for key in (REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS):
        r = breakdown[key]
        if r["n_months"] == 0:
            continue
        lines.append(
            f"  {REGIME_LABELS[key]}: 샤프 {r['sharpe']:.2f} | MDD {r['mdd_pct']:.2f}%"
        )
    return lines


def _format_regime_html_table(breakdown: Dict[str, Dict[str, Any]]) -> str:
    rows = ""
    for key in (REGIME_BULL, REGIME_BEAR, REGIME_SIDEWAYS):
        r = breakdown[key]
        rows += f"""<tr>
            <td>{REGIME_LABELS[key]}</td>
            <td style="text-align:right;">{r['n_months']}개월</td>
            <td style="text-align:right;" class="{'positive' if r['avg_strat_pct'] >= 0 else 'negative'}">{r['avg_strat_pct']:+.1f}%</td>
            <td style="text-align:right;" class="{'positive' if r['avg_kospi_pct'] >= 0 else 'negative'}">{r['avg_kospi_pct']:+.1f}%</td>
            <td style="text-align:right;" class="{'positive' if r['excess_pct'] >= 0 else 'negative'}">{r['excess_pct']:+.1f}%</td>
            <td style="text-align:right;">{r['sharpe']:.2f}</td>
            <td style="text-align:right;" class="negative">{r['mdd_pct']:.2f}%</td>
        </tr>"""
    return f"""
    <div class="card" style="margin-top:24px;">
        <h3 style="margin-bottom:12px;font-size:14px;">📉 시장 국면별 성과 (KS11 월 수익률 기준)</h3>
        <p style="color:#64748b;font-size:12px;margin-bottom:12px;">
            상승장: 월 &gt; +2% · 하락장: 월 &lt; -2% · 횡보장: 그 외. 샤프는 월간 수익 연율화, MDD는 해당 월들의 월말 자산만으로 산출.
        </p>
        <table>
            <thead><tr>
                <th>국면</th><th>기간(개월)</th><th>전략 평균 월수익률</th><th>코스피 월수익률</th><th>초과 수익</th><th>샤프</th><th>MDD</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


class ReportGenerator:
    """
    백테스팅 결과 리포트 생성기

    사용법:
        rg = ReportGenerator()
        rg.generate_text_report(result)
        rg.generate_html_report(result, "report.html")
    """

    def __init__(self, output_dir: str = "reports"):
        project_root = Path(__file__).parent.parent
        self.output_dir = project_root / output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("ReportGenerator 초기화 (출력: {})", self.output_dir)

    def generate_text_report(self, result: dict) -> str:
        """
        텍스트 형식 백테스팅 리포트 생성

        Args:
            result: Backtester.run() 의 반환값

        Returns:
            리포트 텍스트
        """
        if not result or "metrics" not in result:
            return "결과 없음"

        m = result["metrics"]
        trades = result.get("trades", [])

        # 매도·청산 거래만 추출 (수익 계산용)
        sell_trades = [t for t in trades if t["action"] in _TRADES_WITH_PNL]

        lines = [
            "=" * 60,
            f"  📊 백테스팅 결과 리포트",
            f"  전략: {result.get('strategy', 'N/A')}",
            f"  기간: {result.get('period', 'N/A')}",
            f"  생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
            "[ 자본 현황 ]",
            f"  초기 자본     : {m['initial_capital']:>14,.0f}원",
            f"  최종 자본     : {m['final_value']:>14,.0f}원",
            f"  순 수익       : {m['final_value'] - m['initial_capital']:>14,.0f}원",
            "",
            "[ 수익률 지표 ]",
            f"  총 수익률     : {m['total_return']:>13.2f}%",
            f"  연간 수익률   : {m['annual_return']:>13.2f}%",
            "",
            "[ 위험 지표 ]",
            f"  샤프 지수     : {m['sharpe_ratio']:>13.2f}",
            f"  최대 낙폭     : {m['max_drawdown']:>13.2f}%",
            f"  칼마 비율     : {m['calmar_ratio']:>13.2f}",
            "",
        ]

        regime_bd = compute_market_regime_breakdown(result, warn_bear_underperformance=True)
        if regime_bd:
            result["_report_regime_breakdown"] = regime_bd
            lines.extend(_format_regime_text_table(regime_bd))

        lines.extend([
            "[ 매매 성과 ]",
            f"  총 매매 횟수  : {m['total_trades']:>13d}회",
            f"  승률          : {m['win_rate']:>13.1f}%",
            f"  수익 거래     : {m['winning_trades']:>13d}회",
            f"  손실 거래     : {m['losing_trades']:>13d}회",
            f"  손익비        : {m['profit_factor']:>13.2f}",
            f"  평균 수익     : {m['avg_win']:>14,.0f}원",
            f"  평균 손실     : {m['avg_loss']:>14,.0f}원",
            "",
            "[ 과매매 분석 ]",
            f"  평균 보유 기간 : {m.get('avg_holding_days', 0):>11.1f}일",
            f"  총 수수료     : {m.get('total_commission', 0):>14,.0f}원 (총 거래 {m['total_trades']}회)",
        ])
        cpr = m.get("commission_to_profit_ratio")
        cpr_line = (
            f"  수수료/총이익 : {cpr * 100:>12.2f}%"
            if cpr is not None
            else "  수수료/총이익 :          N/A (총 이익 0)"
        )
        lines.append(cpr_line)
        lines.append(
            f"  종목당 월 왕복 : {m.get('monthly_roundtrips_per_symbol', 0):>10.2f}회 | "
            f"연간 왕복 {m.get('annual_roundtrips_total', 0):.2f}회"
        )
        ow = result.get("overtrading_warnings") or []
        if ow:
            lines.append("  [ 자동 경고 ]")
            for w in ow:
                lines.append(f"    {w}")
        lines.append("")

        # 최근 거래 요약 (최대 10건)
        if sell_trades:
            lines.append("[ 최근 매도 거래 (최대 10건) ]")
            lines.append(f"  {'날짜':^12} {'유형':^12} {'가격':>10} {'수량':>6} {'수익률':>8}")
            lines.append("  " + "-" * 52)
            for t in sell_trades[-10:]:
                date_str = t["date"].strftime("%Y-%m-%d") if hasattr(t["date"], "strftime") else str(t["date"])[:10]
                lines.append(
                    f"  {date_str:^12} {t['action']:^12} "
                    f"{t['price']:>10,.0f} {t['quantity']:>6d} "
                    f"{t['pnl_rate']:>7.2f}%"
                )

        lines.append("")
        lines.append("=" * 60)

        report_text = "\n".join(lines)

        # 파일 저장
        filename = f"backtest_{result.get('strategy', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = self.output_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report_text)

        logger.info("텍스트 리포트 저장: {}", filepath)
        return report_text

    def generate_all(self, result: dict) -> dict:
        """텍스트/HTML 리포트를 한 번에 생성하고 경로를 반환."""
        if not result or "metrics" not in result:
            return {}

        self.generate_text_report(result)
        html_path = self.generate_html_report(result)

        txt_files = sorted(
            self.output_dir.glob(f"backtest_{result.get('strategy', 'unknown')}_*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        latest_txt = str(txt_files[0]) if txt_files else ""

        return {
            "text_path": latest_txt,
            "html_path": html_path,
        }

    def generate_html_report(self, result: dict, filename: str = None) -> str:
        """
        HTML 형식 백테스팅 리포트 생성

        Args:
            result: Backtester.run() 의 반환값
            filename: 출력 파일명 (None이면 자동 생성)

        Returns:
            HTML 파일 경로
        """
        if not result or "metrics" not in result:
            return ""

        m = result["metrics"]
        equity = result.get("equity_curve", pd.DataFrame())
        trades = result.get("trades", [])

        if filename is None:
            filename = f"backtest_{result.get('strategy', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

        # 자본 곡선 데이터 (SVG 차트)
        chart_svg = self._generate_equity_chart_svg(equity) if not equity.empty else ""

        # 거래 테이블 HTML
        trades_html = self._generate_trades_table(trades)

        regime_bd = result.get("_report_regime_breakdown")
        if regime_bd is None:
            regime_bd = compute_market_regime_breakdown(result, warn_bear_underperformance=False)
        regime_html = _format_regime_html_table(regime_bd) if regime_bd else ""

        mcpr = m.get("commission_to_profit_ratio")
        cpr_disp = f"{mcpr * 100:.2f}%" if mcpr is not None else "N/A"
        ow = result.get("overtrading_warnings") or []
        if ow:
            ow_items = "".join(
                f'<li style="margin:6px 0;color:#fbbf24;">{html.escape(w)}</li>' for w in ow
            )
            overtrading_warn_html = f"""
    <div class="card" style="margin-bottom:24px;border:1px solid rgba(251,191,36,0.25);">
        <h3 style="margin-bottom:12px;font-size:14px;color:#fbbf24;">⚠️ 과매매 자동 경고</h3>
        <ul style="padding-left:20px;font-size:13px;">{ow_items}</ul>
    </div>"""
        else:
            overtrading_warn_html = ""

        ot_b64 = _overtrading_charts_png_base64(trades, equity)
        if ot_b64:
            overtrading_chart_html = f"""
    <div class="chart">
        <h3 style="margin-bottom:12px;font-size:14px;">📉 과매매 분석 차트</h3>
        <p style="color:#64748b;font-size:12px;margin-bottom:12px;">
            월별 전체 체결 건수 · 일별 누적 수수료 vs 누적 실현 손익
        </p>
        <img src="data:image/png;base64,{ot_b64}" alt="과매매 분석" style="max-width:100%;height:auto;border-radius:8px;" />
    </div>"""
        else:
            overtrading_chart_html = ""

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>백테스팅 리포트 — {result.get('strategy', '')}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 32px; }}
        .container {{ max-width: 960px; margin: 0 auto; }}
        h1 {{ font-size: 24px; margin-bottom: 8px; color: #4f9ef8; }}
        .subtitle {{ color: #64748b; font-size: 13px; margin-bottom: 24px; }}
        .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
        .card {{ background: rgba(30,41,59,0.8); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 16px; }}
        .card .label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; }}
        .card .value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
        .positive {{ color: #27ae60; }}
        .negative {{ color: #e74c3c; }}
        .chart {{ background: rgba(30,41,59,0.8); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 20px; margin-bottom: 24px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ text-align: left; padding: 10px 12px; background: rgba(79,158,248,0.1); color: #4f9ef8; font-size: 11px; text-transform: uppercase; }}
        td {{ padding: 8px 12px; border-bottom: 1px solid rgba(255,255,255,0.05); }}
    </style>
</head>
<body>
<div class="container">
    <h1>📊 백테스팅 리포트</h1>
    <p class="subtitle">전략: {result.get('strategy', '')} | 기간: {result.get('period', '')} | 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

    <div class="grid">
        <div class="card">
            <div class="label">총 수익률</div>
            <div class="value {'positive' if m['total_return'] >= 0 else 'negative'}">{m['total_return']:.2f}%</div>
        </div>
        <div class="card">
            <div class="label">샤프 지수</div>
            <div class="value">{m['sharpe_ratio']:.2f}</div>
        </div>
        <div class="card">
            <div class="label">최대 낙폭</div>
            <div class="value negative">{m['max_drawdown']:.2f}%</div>
        </div>
        <div class="card">
            <div class="label">승률</div>
            <div class="value">{m['win_rate']:.1f}%</div>
        </div>
        <div class="card">
            <div class="label">초기 자본</div>
            <div class="value">{m['initial_capital']:,.0f}원</div>
        </div>
        <div class="card">
            <div class="label">최종 자본</div>
            <div class="value {'positive' if m['total_return'] >= 0 else 'negative'}">{m['final_value']:,.0f}원</div>
        </div>
        <div class="card">
            <div class="label">총 매매</div>
            <div class="value">{m['total_trades']}회</div>
        </div>
        <div class="card">
            <div class="label">손익비</div>
            <div class="value">{m['profit_factor']:.2f}</div>
        </div>
        <div class="card">
            <div class="label">평균 보유 기간</div>
            <div class="value">{m.get('avg_holding_days', 0):.1f}일</div>
        </div>
        <div class="card">
            <div class="label">총 수수료 (총 거래 {m['total_trades']}회)</div>
            <div class="value">{m.get('total_commission', 0):,.0f}원</div>
        </div>
        <div class="card">
            <div class="label">수수료 / 총 이익</div>
            <div class="value">{cpr_disp}</div>
        </div>
        <div class="card">
            <div class="label">종목당 월 평균 왕복</div>
            <div class="value">{m.get('monthly_roundtrips_per_symbol', 0):.2f}회</div>
        </div>
        <div class="card">
            <div class="label">연간 왕복 횟수</div>
            <div class="value">{m.get('annual_roundtrips_total', 0):.2f}회</div>
        </div>
    </div>

    {overtrading_warn_html}

    {f'<div class="chart"><h3 style="margin-bottom:12px;font-size:14px;">📈 자본 곡선</h3>{chart_svg}</div>' if chart_svg else ''}

    {overtrading_chart_html}

    {regime_html}

    <div class="card">
        <h3 style="margin-bottom:12px;font-size:14px;">📋 거래 내역</h3>
        {trades_html}
    </div>
</div>
</body>
</html>"""

        filepath = self.output_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info("HTML 리포트 저장: {}", filepath)
        return str(filepath)

    def _generate_equity_chart_svg(self, equity: pd.DataFrame) -> str:
        """간단한 SVG 자본 곡선 차트 생성"""
        if equity.empty or "value" not in equity.columns:
            return ""

        values = equity["value"].tolist()
        if not values:
            return ""

        # SVG viewbox
        width = 900
        height = 200
        padding = 10

        min_val = min(values)
        max_val = max(values)
        val_range = max_val - min_val if max_val != min_val else 1

        # 좌표 계산
        points = []
        for i, v in enumerate(values):
            x = padding + (i / max(len(values) - 1, 1)) * (width - 2 * padding)
            y = padding + (1 - (v - min_val) / val_range) * (height - 2 * padding)
            points.append(f"{x:.1f},{y:.1f}")

        polyline = " ".join(points)
        color = "#27ae60" if values[-1] >= values[0] else "#e74c3c"

        svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>
  <text x="{padding}" y="{height - 2}" font-size="10" fill="#64748b">{min_val:,.0f}</text>
  <text x="{padding}" y="{padding + 10}" font-size="10" fill="#64748b">{max_val:,.0f}</text>
</svg>"""
        return svg

    @staticmethod
    def _generate_trades_table(trades: list) -> str:
        """거래 내역 HTML 테이블 생성"""
        sell_trades = [t for t in trades if t["action"] in _TRADES_WITH_PNL]

        if not sell_trades:
            return "<p style='color:#64748b;'>거래 내역 없음</p>"

        rows = ""
        for t in sell_trades[-20:]:  # 최근 20건
            date_str = t["date"].strftime("%Y-%m-%d") if hasattr(t["date"], "strftime") else str(t["date"])[:10]
            pnl_class = "positive" if t["pnl"] >= 0 else "negative"
            rows += f"""<tr>
                <td>{date_str}</td>
                <td>{t['action']}</td>
                <td style="text-align:right;">{t['price']:,.0f}원</td>
                <td style="text-align:right;">{t['quantity']}주</td>
                <td class="{pnl_class}" style="text-align:right;">{t['pnl']:,.0f}원</td>
                <td class="{pnl_class}" style="text-align:right;">{t['pnl_rate']:.2f}%</td>
            </tr>"""

        return f"""<table>
            <thead><tr><th>날짜</th><th>유형</th><th>가격</th><th>수량</th><th>수익</th><th>수익률</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>"""
