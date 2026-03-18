"""
퀀트 트레이더 시스템 - 메인 실행 파일

사용법:
    # 백테스팅 (과거 데이터로 전략 검증)
    python main.py --mode backtest --strategy scoring --symbol 005930

    # 백테스트 리포트 저장 경로 지정
    python main.py --mode backtest --strategy scoring --symbol 005930 --output-dir reports

    # 페이퍼 트레이딩 (모의 매매)
    python main.py --mode paper --strategy scoring

    # 실전 (ENABLE_LIVE_TRADING=true + --confirm-live 필요)
    python main.py --mode live --strategy scoring --confirm-live

    # 특정 기간 백테스팅
    python main.py --mode backtest --strategy scoring --symbol 005930 --start 2023-01-01 --end 2025-12-31
"""

import os
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
    from backtest.report_generator import ReportGenerator

    logger.info("=" * 50)
    logger.info("📊 백테스팅 모드 시작")
    logger.info("=" * 50)

    # 데이터 수집
    collector = DataCollector()
    symbol = args.symbol or "005930"

    logger.info("종목 {} 데이터 수집 중...", symbol)
    df = collector.fetch_korean_stock(symbol, args.start, args.end)

    if df.empty:
        logger.error(
            "데이터를 수집할 수 없습니다. 백테스트를 위해 다음 중 하나를 설정하세요:\n"
            "  1) pip install FinanceDataReader   # 한국 주식 권장\n"
            "  2) pip install yfinance            # 한국 종목 005930.KS 지원\n"
            "  3) KIS API 환경변수 설정 시 일봉 조회로 자동 대체\n"
            "지표: pandas-ta는 Python 3.11~3.12 범위에서 사용하는 것을 권장하며, "
            "패키지 정책도 >=3.11,<3.13 기준입니다."
        )
        return

    logger.info("수집 완료: {}건 ({} ~ {})", len(df), df.index[0], df.index[-1])

    # 백테스팅 실행
    backtester = Backtester()
    result = backtester.run(
        df,
        strategy_name=args.strategy,
        strict_lookahead=args.strict_lookahead,
    )

    if not result:
        logger.error("백테스트 결과가 비어 있습니다.")
        return

    # 결과 출력
    backtester.print_report(result)

    report_generator = ReportGenerator(output_dir=args.output_dir)
    report_paths = report_generator.generate_all(result)
    if report_paths:
        logger.info(
            "백테스트 리포트 저장 완료 | txt={} | html={}",
            report_paths.get("text_path", ""),
            report_paths.get("html_path", ""),
        )


def run_paper_trading(args):
    """페이퍼 트레이딩 모드 실행"""
    from core.data_collector import DataCollector
    from core.order_executor import OrderExecutor
    from core.portfolio_manager import PortfolioManager
    from monitoring.discord_bot import DiscordBot
    from database.repositories import get_position

    config = Config.get()

    logger.info("=" * 50)
    logger.info("📄 페이퍼 트레이딩 모드 시작")
    logger.info("=" * 50)

    collector = DataCollector()
    portfolio = PortfolioManager(config)
    discord = DiscordBot(config)
    executor = OrderExecutor(config)

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

            if signal_info["signal"] != "HOLD":
                discord.send_signal_alert(symbol, signal_info)

            if signal_info["signal"] == "BUY" and not get_position(symbol):
                capital_summary = portfolio.get_portfolio_summary()
                order_result = executor.execute_buy(
                    symbol=symbol,
                    price=signal_info["close"],
                    capital=capital_summary["total_value"],
                    available_cash=capital_summary["cash"],
                    atr=signal_info.get("atr"),
                    signal_score=signal_info["score"],
                    reason="paper auto-entry",
                    strategy=args.strategy,
                )
                if order_result.get("success"):
                    discord.send_trade_alert(order_result)
            elif signal_info["signal"] == "SELL" and get_position(symbol):
                order_result = executor.execute_sell(
                    symbol=symbol,
                    price=signal_info["close"],
                    signal_score=signal_info["score"],
                    reason="paper signal exit",
                    strategy=args.strategy,
                )
                if order_result.get("success"):
                    discord.send_trade_alert(order_result)

        except Exception as e:
            logger.error("종목 {} 분석 실패: {}", symbol, e)
            continue

    # 포트폴리오 요약
    summary = portfolio.get_portfolio_summary()
    logger.info("\n📊 포트폴리오 현황:")
    logger.info("  총 평가금: {:,.0f}원", summary["total_value"])
    logger.info("  수익률: {:.2f}%", summary["total_return"])
    logger.info("  보유 종목: {}개", summary["position_count"])


def run_live_trading(args):
    """
    실전 매매 모드 실행.
    이중 확인: ENABLE_LIVE_TRADING=true 환경변수 + --confirm-live 플래그 필수.
    """
    if os.environ.get("ENABLE_LIVE_TRADING", "").lower() != "true":
        logger.error(
            "실전 모드 진입 거부: 환경변수 ENABLE_LIVE_TRADING=true 가 필요합니다. "
            "실수로 실주문이 나가지 않도록 설정 후 다시 실행하세요."
        )
        sys.exit(1)

    if not getattr(args, "confirm_live", False):
        logger.error(
            "실전 모드 진입 거부: --confirm-live 플래그가 필요합니다. "
            "예: python main.py --mode live --strategy scoring --confirm-live"
        )
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("🔴 실전 매매 모드 시작 (이중 확인 완료)")
    logger.info("=" * 50)

    config = Config.get()
    old_mode = config.trading.get("mode", "paper")
    config._settings.setdefault("trading", {})["mode"] = "live"

    try:
        # 토큰 사전 발급 (필수 환경변수 미설정 시 명확히 종료)
        from api.kis_api import KISApi
        kis = KISApi()
        if not kis.authenticate():
            logger.error(
                "KIS API 인증 실패. 실전 모드를 사용하려면 "
                "KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 환경변수를 설정하세요."
            )
            sys.exit(1)
        logger.info("KIS API 토큰 사전 발급 완료")
        if not kis.verify_connection():
            logger.warning(
                "KIS 잔고 조회 실패 — 실환경 연결 검증 실패. "
                "계좌/권한을 확인한 뒤 재시도하세요. 스케줄러는 계속 진행합니다."
            )

        # 블랙스완 상태 확인 (로그만)
        from core.blackswan_detector import BlackSwanDetector
        bs = BlackSwanDetector(config)
        if bs.is_on_cooldown():
            logger.warning("블랙스완 쿨다운 중 — 신규 매수만 차단되며, 손절/익절은 정상 동작합니다.")

        # 장 시작 전 KIS 잔고와 DB 포지션 동기화
        from core.portfolio_manager import PortfolioManager
        portfolio = PortfolioManager(config)
        sync_result = portfolio.sync_with_broker()
        if not sync_result["ok"]:
            logger.warning("포지션 동기화 불일치 — 확인 후 진행: {}", sync_result["message"])

        # 실전 스케줄러 실행 (OrderExecutor는 config에서 mode=live 읽음)
        from core.scheduler import Scheduler
        scheduler = Scheduler(strategy_name=args.strategy, config=config)
        scheduler.run()
    finally:
        config._settings["trading"]["mode"] = old_mode
        logger.info("실전 모드 설정 복원됨")


def _get_strategy(name: str):
    """전략 인스턴스 반환"""
    from config.config_loader import Config
    config = Config.get()
    if name == "mean_reversion":
        from strategies.mean_reversion import MeanReversionStrategy
        return MeanReversionStrategy(config)
    elif name == "trend_following":
        from strategies.trend_following import TrendFollowingStrategy
        return TrendFollowingStrategy(config)
    elif name == "ensemble":
        from core.strategy_ensemble import StrategyEnsemble
        return StrategyEnsemble(config)
    else:
        from strategies.scoring_strategy import ScoringStrategy
        return ScoringStrategy(config)


def main():
    """메인 진입점"""
    parser = argparse.ArgumentParser(
        description="퀀트 트레이더 - 자동 주식 매매 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py --mode backtest --strategy scoring --symbol 005930
  python main.py --mode backtest --strategy mean_reversion --symbol 000660 --start 2023-01-01
  python main.py --mode backtest --strategy scoring --symbol 005930 --strict-lookahead  # Look-Ahead Bias 방어
  python main.py --mode paper --strategy scoring
  python main.py --mode live --strategy scoring --confirm-live  # 실전 (ENABLE_LIVE_TRADING=true 필요)
        """,
    )

    parser.add_argument(
        "--mode", type=str, default="backtest",
        choices=["backtest", "paper", "live"],
        help="실행 모드 (기본: backtest)",
    )
    parser.add_argument(
        "--strategy", type=str, default="scoring",
        choices=["scoring", "mean_reversion", "trend_following", "ensemble"],
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
    parser.add_argument(
        "--strict-lookahead", action="store_true",
        help="백테스트 시 Look-Ahead Bias 완전 방어 (매 시점 T에서 df[:T+1]만 사용, 느림)",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="실전 모드 진입 시 필수. 미지정 시 live 모드 진입 거부.",
    )
    parser.add_argument(
        "--output-dir", type=str, default="reports",
        help="백테스트 리포트 저장 디렉토리 (기본: reports)",
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
            run_live_trading(args)
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
