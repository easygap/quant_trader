"""
콘솔 대시보드 모듈
- 터미널에 실시간 포트폴리오 현황 표시
- Grafana 연동 없이 즉시 사용 가능한 경량 대시보드
"""

from datetime import datetime
from loguru import logger

from config.config_loader import Config
from core.portfolio_manager import PortfolioManager
from database.repositories import get_portfolio_snapshots


class Dashboard:
    """
    콘솔 기반 포트폴리오 대시보드

    사용법:
        dash = Dashboard()
        dash.show_portfolio(current_prices={"005930": 75000})
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.initial_capital = self.config.risk_params.get(
            "position_sizing", {}
        ).get("initial_capital", 10000000)
        self.portfolio_manager = PortfolioManager(self.config)
        logger.info("Dashboard 초기화 완료")

    def show_portfolio(self, current_prices: dict = None):
        """
        포트폴리오 현황을 콘솔에 출력

        Args:
            current_prices: {종목코드: 현재가} 딕셔너리
        """
        summary = self.portfolio_manager.get_portfolio_summary(current_prices or {})
        positions = summary["positions"]

        print("\n" + "=" * 70)
        print(f"  📊 포트폴리오 대시보드  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 70)

        if positions:
            print(f"\n  {'종목':^8} {'수량':>6} {'평균가':>10} {'현재가':>10} {'평가액':>12} {'수익률':>8}")
            print("  " + "-" * 62)

            for pos in positions:
                emoji = "📈" if pos["pnl_rate"] >= 0 else "📉"
                print(
                    f"  {pos['symbol']:^8} {pos['quantity']:>6d} "
                    f"{pos['avg_price']:>10,.0f} {pos['current_price']:>10,.0f} "
                    f"{pos['current_value']:>12,.0f} {emoji}{pos['pnl_rate']:>6.2f}%"
                )
        else:
            print("\n  보유 종목 없음")

        print("\n  " + "-" * 62)
        print(f"  {'초기 자본':>12}: {self.initial_capital:>14,.0f}원")
        print(f"  {'투자 금액':>12}: {summary['invested']:>14,.0f}원")
        print(f"  {'현금 잔고':>12}: {summary['cash']:>14,.0f}원")
        print(f"  {'총 평가금':>12}: {summary['total_value']:>14,.0f}원")
        print(f"  {'실현 손익':>12}: {summary['realized_pnl']:>14,.0f}원")
        print(f"  {'미실현 손익':>12}: {summary['unrealized_pnl']:>14,.0f}원")

        return_emoji = "📈" if summary["total_return"] >= 0 else "📉"
        print(f"  {'총 수익률':>12}: {return_emoji} {summary['total_return']:>13.2f}%")
        print(f"  {'보유 종목':>12}: {summary['position_count']:>14d}개")
        print("=" * 70 + "\n")

    def show_recent_snapshots(self, days: int = 7):
        """
        최근 N일간 포트폴리오 스냅샷 추이 출력

        Args:
            days: 조회 기간 (일)
        """
        snapshots = get_portfolio_snapshots(days)

        if snapshots.empty:
            print("  스냅샷 데이터 없음")
            return

        print(f"\n  📈 최근 {days}일 수익률 추이")
        print(f"  {'날짜':^12} {'총 평가금':>14} {'수익률':>8} {'MDD':>8} {'종목수':>6}")
        print("  " + "-" * 52)

        for _, row in snapshots.iterrows():
            date_str = row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])[:10]
            print(
                f"  {date_str:^12} {row['total_value']:>14,.0f} "
                f"{row.get('cumulative_return', 0):>7.2f}% "
                f"{row.get('mdd', 0):>7.2f}% "
                f"{row.get('position_count', 0):>6d}"
            )
        print()

    def show_summary_line(self, current_prices: dict = None) -> str:
        """한 줄 요약 (로그용)"""
        summary = self.portfolio_manager.get_portfolio_summary(current_prices or {})

        return (
            f"포트폴리오: {summary['total_value']:,.0f}원 | "
            f"수익률: {summary['total_return']:.2f}% | "
            f"보유: {summary['position_count']}종목 | "
            f"현금: {summary['cash']:,.0f}원"
        )
