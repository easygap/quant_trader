"""
콘솔 대시보드 모듈
- 터미널에 실시간 포트폴리오 현황 표시
- Grafana 연동 없이 즉시 사용 가능한 경량 대시보드
"""

from datetime import datetime
from loguru import logger

from config.config_loader import Config
from database.repositories import get_all_positions, get_portfolio_snapshots


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
        logger.info("Dashboard 초기화 완료")

    def show_portfolio(self, current_prices: dict = None):
        """
        포트폴리오 현황을 콘솔에 출력

        Args:
            current_prices: {종목코드: 현재가} 딕셔너리
        """
        current_prices = current_prices or {}
        positions = get_all_positions()

        print("\n" + "=" * 70)
        print(f"  📊 포트폴리오 대시보드  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        print("=" * 70)

        total_invested = 0
        total_current = 0

        if positions:
            print(f"\n  {'종목':^8} {'수량':>6} {'평균가':>10} {'현재가':>10} {'평가액':>12} {'수익률':>8}")
            print("  " + "-" * 62)

            for pos in positions:
                price = current_prices.get(pos.symbol, pos.avg_price)
                value = price * pos.quantity
                invested = pos.avg_price * pos.quantity
                pnl_rate = ((price / pos.avg_price) - 1) * 100 if pos.avg_price > 0 else 0

                total_invested += invested
                total_current += value

                emoji = "📈" if pnl_rate >= 0 else "📉"
                print(
                    f"  {pos.symbol:^8} {pos.quantity:>6d} "
                    f"{pos.avg_price:>10,.0f} {price:>10,.0f} "
                    f"{value:>12,.0f} {emoji}{pnl_rate:>6.2f}%"
                )
        else:
            print("\n  보유 종목 없음")

        # 요약
        cash = self.initial_capital - total_invested
        total_value = cash + total_current
        total_return = ((total_value / self.initial_capital) - 1) * 100

        print("\n  " + "-" * 62)
        print(f"  {'초기 자본':>12}: {self.initial_capital:>14,.0f}원")
        print(f"  {'투자 금액':>12}: {total_invested:>14,.0f}원")
        print(f"  {'현금 잔고':>12}: {cash:>14,.0f}원")
        print(f"  {'총 평가금':>12}: {total_value:>14,.0f}원")

        return_emoji = "📈" if total_return >= 0 else "📉"
        print(f"  {'총 수익률':>12}: {return_emoji} {total_return:>13.2f}%")
        print(f"  {'보유 종목':>12}: {len(positions):>14d}개")
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
        current_prices = current_prices or {}
        positions = get_all_positions()

        total_invested = sum(p.avg_price * p.quantity for p in positions)
        total_current = sum(
            current_prices.get(p.symbol, p.avg_price) * p.quantity
            for p in positions
        )
        cash = self.initial_capital - total_invested
        total_value = cash + total_current
        total_return = ((total_value / self.initial_capital) - 1) * 100

        summary = (
            f"포트폴리오: {total_value:,.0f}원 | "
            f"수익률: {total_return:.2f}% | "
            f"보유: {len(positions)}종목 | "
            f"현금: {cash:,.0f}원"
        )
        return summary
