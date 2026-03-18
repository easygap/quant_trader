"""
백테스팅 결과 리포트 생성 모듈
- 텍스트 리포트 및 HTML 리포트 생성
- 거래 내역, 성과 지표, 자본 곡선 시각화
"""

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger


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

        # 매도 거래만 추출 (수익 계산용)
        sell_trades = [t for t in trades if t["action"] in ("SELL", "STOP_LOSS", "TAKE_PROFIT")]

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
            "[ 매매 성과 ]",
            f"  총 매매 횟수  : {m['total_trades']:>13d}회",
            f"  승률          : {m['win_rate']:>13.1f}%",
            f"  수익 거래     : {m['winning_trades']:>13d}회",
            f"  손실 거래     : {m['losing_trades']:>13d}회",
            f"  손익비        : {m['profit_factor']:>13.2f}",
            f"  평균 수익     : {m['avg_win']:>14,.0f}원",
            f"  평균 손실     : {m['avg_loss']:>14,.0f}원",
            "",
        ]

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
    </div>

    {f'<div class="chart"><h3 style="margin-bottom:12px;font-size:14px;">📈 자본 곡선</h3>{chart_svg}</div>' if chart_svg else ''}

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
        sell_trades = [t for t in trades if t["action"] in ("SELL", "STOP_LOSS", "TAKE_PROFIT")]

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
