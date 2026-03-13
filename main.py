"""
퀀트 트레이더 시스템 - 메인 실행 파일

사용법:
    # 백테스팅 (과거 데이터로 전략 검증)
    python main.py --mode backtest --strategy scoring --symbol 005930

    # 페이퍼 트레이딩 (모의 매매)
    python main.py --mode paper --strategy scoring

    # 특정 기간 백테스팅
    python main.py --mode backtest --strategy scoring --symbol 005930 --start 2023-01-01 --end 2025-12-31
"""

import sys
import argparse
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(Path(__file__).parent))

from config.config_loader import Config
from database.models import init_database
from monitoring.logger import setup_logger
from loguru import logger


def run_backtest(args):
    """백테스팅 모드 실행"""
    from core.data_collector import DataCollector
    from backtest.backtester import Backtester

    logger.info("=" * 50)
    logger.info("📊 백테스팅 모드 시작")
    logger.info("=" * 50)

    # 데이터 수집
    collector = DataCollector()
    symbol = args.symbol or "005930"

    logger.info("종목 {} 데이터 수집 중...", symbol)
    df = collector.fetch_korean_stock(symbol, args.start, args.end)

    if df.empty:
        logger.error("데이터를 수집할 수 없습니다.")
        return

    logger.info("수집 완료: {}건 ({} ~ {})", len(df), df.index[0], df.index[-1])

    # 백테스팅 실행
    backtester = Backtester()
    result = backtester.run(df, strategy_name=args.strategy)

    # 결과 출력
    backtester.print_report(result)


def run_paper_trading(args):
    """페이퍼 트레이딩 모드 실행"""
    from core.data_collector import DataCollector
    from core.indicator_engine import IndicatorEngine
    from core.signal_generator import SignalGenerator
    from core.risk_manager import RiskManager
    from core.portfolio_manager import PortfolioManager
    from monitoring.discord_bot import DiscordBot

    config = Config.get()

    logger.info("=" * 50)
    logger.info("📄 페이퍼 트레이딩 모드 시작")
    logger.info("=" * 50)

    collector = DataCollector()
    portfolio = PortfolioManager()
    discord = DiscordBot()

    watchlist = config.watchlist
    if not watchlist:
        watchlist = ["005930"]  # 기본: 삼성전자

    logger.info("관심 종목: {}", watchlist)

    # 전략 선택
    strategy = _get_strategy(args.strategy)

    for symbol in watchlist:
        logger.info("\n--- 종목 {} 분석 시작 ---", symbol)

        try:
            # 최근 데이터 수집
            df = collector.fetch_korean_stock(symbol)

            if df.empty or len(df) < 30:
                logger.warning("종목 {} 데이터 부족 — 스킵", symbol)
                continue

            # 신호 생성
            signal_info = strategy.generate_signal(df)

            logger.info(
                "종목 {} | 신호: {} | 점수: {} | 상세: {}",
                symbol, signal_info["signal"], signal_info["score"], signal_info["details"],
            )

            # 신호에 따른 알림
            if signal_info["signal"] != "HOLD":
                discord.send_signal_alert(symbol, signal_info)

        except Exception as e:
            logger.error("종목 {} 분석 실패: {}", symbol, e)
            continue

    # 포트폴리오 요약
    summary = portfolio.get_portfolio_summary()
    logger.info("\n📊 포트폴리오 현황:")
    logger.info("  총 평가금: {:,.0f}원", summary["total_value"])
    logger.info("  수익률: {:.2f}%", summary["total_return"])
    logger.info("  보유 종목: {}개", summary["position_count"])


def _get_strategy(name: str):
    """전략 인스턴스 반환"""
    if name == "mean_reversion":
        from strategies.mean_reversion import MeanReversionStrategy
        return MeanReversionStrategy()
    elif name == "trend_following":
        from strategies.trend_following import TrendFollowingStrategy
        return TrendFollowingStrategy()
    else:
        from strategies.scoring_strategy import ScoringStrategy
        return ScoringStrategy()


def main():
    """메인 진입점"""
    parser = argparse.ArgumentParser(
        description="퀀트 트레이더 - 자동 주식 매매 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py --mode backtest --strategy scoring --symbol 005930
  python main.py --mode backtest --strategy mean_reversion --symbol 000660 --start 2023-01-01
  python main.py --mode paper --strategy scoring
        """,
    )

    parser.add_argument(
        "--mode", type=str, default="backtest",
        choices=["backtest", "paper", "live"],
        help="실행 모드 (기본: backtest)",
    )
    parser.add_argument(
        "--strategy", type=str, default="scoring",
        choices=["scoring", "mean_reversion", "trend_following"],
        help="매매 전략 (기본: scoring)",
    )
    parser.add_argument(
        "--symbol", type=str, default="005930",
        help="종목 코드 (기본: 005930 삼성전자)",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="시작일 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="종료일 (YYYY-MM-DD)",
    )

    args = parser.parse_args()

    # 시스템 초기화
    setup_logger()
    init_database()

    logger.info("🚀 퀀트 트레이더 시작 (모드: {}, 전략: {})", args.mode, args.strategy)

    try:
        if args.mode == "backtest":
            run_backtest(args)
        elif args.mode == "paper":
            run_paper_trading(args)
        elif args.mode == "live":
            logger.warning("⚠️ 실전 모드는 충분한 검증 후 사용하세요!")
            run_paper_trading(args)
        else:
            logger.error("알 수 없는 모드: {}", args.mode)
    except KeyboardInterrupt:
        logger.info("사용자에 의해 중단됨")
    except Exception as e:
        logger.exception("시스템 오류 발생: {}", e)
    finally:
        logger.info("퀀트 트레이더 종료")


if __name__ == "__main__":
    main()
