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

    # 긴급 전체 청산 (수동 개입·블랙스완 외 상황에서 즉시 전 종목 매도)
    python main.py --mode liquidate
"""

import os
import sys
import argparse
from pathlib import Path

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(Path(__file__).parent))

from config.config_loader import Config
from database.models import init_database
from monitoring.logger import setup_logger
from loguru import logger
from core.watchlist_manager import WatchlistManager


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

    # 백테스팅 실행: strict-lookahead 기본 True. 해제 시에만 아래 명시적 경고 출력
    if not args.strict_lookahead:
        import sys
        border = "=" * 60
        msg = (
            f"\n{border}\n"
            "  ⚠️  [경고] Look-Ahead Bias 방지가 해제되었습니다 (--allow-lookahead)\n"
            f"{border}\n"
            "  • 미래 데이터가 신호 생성에 섞여 들어갈 수 있습니다.\n"
            "  • 백테스트 수익률이 실제보다 훨씬 좋게 나올 수 있습니다.\n"
            "  • 이 결과를 믿고 실전 투입 시 큰 손실이 날 수 있습니다.\n"
            "  • 실전 투입 판단 근거로 사용하지 마세요.\n"
            "  • 기본 동작은 strict-lookahead=True(권장)입니다.\n"
            f"{border}\n"
        )
        logger.warning("Look-Ahead Bias 방지 해제됨 (--allow-lookahead). 실전 판단 근거로 사용 금지.")
        print(msg, file=sys.stderr)
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


def run_param_optimize(args):
    """전략 파라미터 자동 최적화 (Grid Search 또는 Bayesian). 오버피팅 주의."""
    from core.data_collector import DataCollector
    from backtest.param_optimizer import grid_search, bayesian_optimize

    logger.info("=" * 50)
    logger.info("🔧 전략 파라미터 최적화 ({}), 전략: {}", getattr(args, "optimizer", "grid"), args.strategy)
    logger.info("=" * 50)

    collector = DataCollector()
    symbol = args.symbol or "005930"
    df = collector.fetch_korean_stock(symbol, args.start, args.end)
    if df.empty or len(df) < 252:
        logger.error("데이터가 없거나 1년 미만입니다. --start, --end 및 데이터 소스를 확인하세요.")
        return

    optimizer = getattr(args, "optimizer", "grid")
    metric = getattr(args, "optimize_metric", "sharpe_ratio")
    train_ratio = getattr(args, "train_ratio", 0.7)

    if optimizer == "bayesian":
        result = bayesian_optimize(
            df,
            strategy_name=args.strategy,
            metric=metric,
            train_ratio=train_ratio,
            strict_lookahead=True,
            n_calls=getattr(args, "optimize_calls", 30),
        )
    else:
        result = grid_search(
            df,
            strategy_name=args.strategy,
            search_space=None,
            metric=metric,
            train_ratio=train_ratio,
            strict_lookahead=True,
        )

    if result.get("message"):
        logger.warning(result["message"])
        return

    logger.info("최적 파라미터: {}", result["best_params"])
    logger.info("학습 구간 {} ({}): {:.2f}", result["metric"], result["train_period"], result["best_score"])
    if result.get("oos_metrics"):
        logger.info(
            "OOS 구간 {} ({}): {} (오버피팅 확인용)",
            result["metric"], result.get("oos_period", ""), result["oos_metrics"].get(result["metric"]),
        )
    if result.get("all_results"):
        logger.info("상위 5개: {}", [r["params"] for r in result["all_results"][:5]])


def run_strategy_validation(args):
    """전략 검증 모드 실행."""
    from backtest.strategy_validator import StrategyValidator

    validator = StrategyValidator(output_dir=args.output_dir)
    result = validator.run(
        symbol=args.symbol or "005930",
        strategy_name=args.strategy,
        start_date=args.start,
        end_date=args.end,
        benchmark_symbol=args.benchmark_symbol,
        validation_years=args.validation_years,
        split_ratio=args.split_ratio,
        min_sharpe=args.min_sharpe,
        max_mdd=args.max_mdd,
    )
    validator.print_report(result)
    logger.info("전략 검증 리포트 저장 완료: {}", result["report_path"])


def run_paper_trading(args):
    """페이퍼 트레이딩 모드 실행"""
    from datetime import datetime
    from core.data_collector import DataCollector
    from core.order_executor import OrderExecutor
    from core.portfolio_manager import PortfolioManager
    from monitoring.discord_bot import DiscordBot
    from database.repositories import get_position, get_all_positions

    config = Config.get()

    logger.info("=" * 50)
    logger.info("📄 페이퍼 트레이딩 모드 시작")
    logger.info("=" * 50)

    collector = DataCollector()
    account_key = args.strategy or ""
    portfolio = PortfolioManager(config, account_key=account_key)
    discord = DiscordBot(config)
    executor = OrderExecutor(config, account_key=account_key)

    watchlist = WatchlistManager(config).resolve()

    logger.info("관심 종목: {} (계좌/전략: {})", watchlist, account_key or "default")

    # 최대 보유 기간 초과 포지션 강제 정리 (물리는 상황 방지)
    max_holding_days = (config.risk_params.get("position_limits", {}) or {}).get("max_holding_days", 0)
    if max_holding_days > 0:
        today = datetime.now().date()
        for pos in get_all_positions(account_key=account_key if account_key else None):
            bought_at = getattr(pos, "bought_at", None)
            if not bought_at:
                continue
            bought_date = bought_at.date() if hasattr(bought_at, "date") else bought_at
            holding_days = (today - bought_date).days
            if holding_days >= max_holding_days:
                try:
                    df = collector.fetch_korean_stock(pos.symbol)
                    price = float(df["close"].iloc[-1]) if not df.empty and "close" in df.columns else pos.avg_price
                    result = executor.execute_sell(
                        pos.symbol, price,
                        reason=f"최대 보유 기간({max_holding_days}일) 도달 강제 정리 (보유 {holding_days}일)",
                        strategy=args.strategy,
                    )
                    if result.get("success"):
                        discord.send_trade_alert(result)
                except Exception as e:
                    logger.warning("종목 {} 최대 보유 기간 정리 실패: {}", pos.symbol, e)

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

            if signal_info["signal"] == "BUY" and not get_position(symbol, account_key=account_key):
                capital_summary = portfolio.get_portfolio_summary()
                avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1]) if "volume" in df.columns and not df["volume"].empty else None
                order_result = executor.execute_buy(
                    symbol=symbol,
                    price=signal_info["close"],
                    capital=capital_summary["total_value"],
                    available_cash=capital_summary["cash"],
                    current_invested=capital_summary["current_value"],
                    atr=signal_info.get("atr"),
                    signal_score=signal_info["score"],
                    reason="paper auto-entry",
                    strategy=args.strategy,
                    avg_daily_volume=avg_vol,
                )
                if order_result.get("success"):
                    discord.send_trade_alert(order_result)
            elif signal_info["signal"] == "SELL" and get_position(symbol, account_key=account_key):
                avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1]) if "volume" in df.columns and not df["volume"].empty else None
                order_result = executor.execute_sell(
                    symbol=symbol,
                    price=signal_info["close"],
                    signal_score=signal_info["score"],
                    reason="paper signal exit",
                    strategy=args.strategy,
                    avg_daily_volume=avg_vol,
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
        portfolio = PortfolioManager(config, account_key=args.strategy or "")
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


def run_compare_paper_backtest(args):
    """모의투자 결과 vs 백테스트 결과 자동 비교. 차이가 크면 구현/데이터 문제 신호로 경고."""
    from datetime import datetime, timedelta
    from backtest.paper_compare import run_compare

    start_s = args.start
    end_s = args.end
    if not start_s or not end_s:
        end_d = datetime.now()
        start_d = end_d - timedelta(days=30)
        start_s = start_d.strftime("%Y-%m-%d")
        end_s = end_d.strftime("%Y-%m-%d")
        logger.info("비교 기간 미지정 — 최근 30일 사용: {} ~ {}", start_s, end_s)
    try:
        start_date = datetime.strptime(start_s, "%Y-%m-%d")
        end_date = datetime.strptime(end_s, "%Y-%m-%d")
    except ValueError:
        logger.error("날짜 형식은 YYYY-MM-DD 여야 합니다. 예: --start 2025-01-01 --end 2025-03-18")
        return
    if start_date >= end_date:
        logger.error("시작일이 종료일보다 이전이어야 합니다.")
        return

    logger.info("=" * 50)
    logger.info("📊 모의투자 vs 백테스트 비교 ({} ~ {})", start_s, end_s)
    logger.info("=" * 50)

    result = run_compare(
        start_date=start_date,
        end_date=end_date,
        strategy_name=args.strategy,
        symbol=getattr(args, "compare_symbol", None),
    )
    if result.get("paper_metrics"):
        pm = result["paper_metrics"]
        logger.info(
            "모의투자: 수익률 {:.2f}% | 승률 {:.1f}% | 매도 {}건",
            pm["total_return_pct"], pm["win_rate"], pm["total_trades"],
        )
    if result.get("backtest_metrics"):
        bm = result["backtest_metrics"]
        logger.info(
            "백테스트: 수익률 {:.2f}% | 승률 {:.1f}% | 매도 {}건 (종목: {})",
            bm["total_return"], bm["win_rate"], bm["total_trades"], result.get("symbol", ""),
        )
    if result.get("return_diff_pct") is not None:
        logger.info(
            "차이: 수익률 {:.2f}%p, 승률 {:.2f}%p | divergence={}",
            result["return_diff_pct"], result["win_rate_diff_pct"], result["divergence"],
        )
    logger.info("결과: {}", result["message"])


def run_emergency_liquidate(args):
    """긴급 전 종목 매도 (CLI: --mode liquidate). 블랙스완 감지 외에도 수동 개입이 필요할 때 즉시 전 종목 매도."""
    from database.repositories import get_all_positions
    from core.order_executor import OrderExecutor

    logger.info("=" * 50)
    logger.info("🚨 긴급 전 종목 매도 모드")
    logger.info("=" * 50)

    positions = get_all_positions()  # 긴급 청산은 모든 계좌 포지션 대상
    if not positions:
        logger.info("보유 포지션이 없습니다.")
        return

    config = Config.get()
    # 청산 시에는 계좌(전략)별로 executor 사용 (live 시 해당 계좌로 매도)
    account_executors = {}
    mode = config.trading.get("mode", "paper")

    for pos in positions:
        try:
            ak = getattr(pos, "account_key", None) or ""
            if ak not in account_executors:
                account_executors[ak] = OrderExecutor(config, account_key=ak)
            executor = account_executors[ak]
            price = pos.avg_price
            if mode == "live":
                from api.kis_api import KISApi
                account_no = config.get_account_no(ak)
                kis = KISApi(account_no=account_no)
                price_info = kis.get_current_price(pos.symbol)
                if price_info and price_info.get("price"):
                    price = float(price_info["price"])
            result = executor.execute_sell(
                pos.symbol,
                price,
                quantity=None,
                reason="긴급 전량 청산 (--mode liquidate)",
                strategy="emergency_liquidate",
            )
            if result.get("success"):
                logger.info("청산 완료: {} @ {:,.0f}원", pos.symbol, price)
            else:
                logger.error("청산 실패 {}: {}", pos.symbol, result.get("reason", ""))
        except Exception as e:
            logger.exception("종목 {} 청산 중 오류: {}", pos.symbol, e)

    logger.info("긴급 청산 처리 완료 ({}건)", len(positions))


def run_dashboard(args):
    """실시간 웹 대시보드 서버 실행 (포트폴리오·포지션·스냅샷 추이 표시)."""
    from monitoring.web_dashboard import run_web_dashboard

    logger.info("=" * 50)
    logger.info("📊 웹 대시보드 모드")
    logger.info("=" * 50)

    port = getattr(args, "dashboard_port", None)
    run_web_dashboard(port=port)


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
  python main.py --mode backtest --strategy scoring --symbol 005930
  python main.py --mode backtest --strategy scoring --symbol 005930 --allow-lookahead  # 위험: 미래 데이터 혼입 허용
  python main.py --mode validate --strategy scoring --symbol 005930 --validation-years 5
  python main.py --mode paper --strategy scoring
  python main.py --mode live --strategy scoring --confirm-live  # 실전 (ENABLE_LIVE_TRADING=true 필요)
  python main.py --mode liquidate  # 긴급 전 종목 매도 (실전/모의 모두 DB 기준)
  python main.py --mode compare --start 2025-01-01 --end 2025-03-18 --strategy scoring  # 모의투자 vs 백테스트 비교
  python main.py --update-holidays  # 휴장일 파일(pykrx+fallback) 자동 갱신
  python main.py --mode optimize --strategy scoring  # 전략 파라미터 Grid Search (오버피팅 주의)
  python main.py --mode optimize --strategy scoring --optimizer bayesian  # Bayesian 최적화 (scikit-optimize)
  python main.py --mode dashboard  # 실시간 웹 대시보드 (기본 http://127.0.0.1:8080)
        """,
    )

    parser.add_argument(
        "--optimizer", type=str, default="grid", choices=["grid", "bayesian"],
        help="[optimize 모드] grid 또는 bayesian (bayesian 시 pip install scikit-optimize)",
    )
    parser.add_argument(
        "--optimize-metric", type=str, default="sharpe_ratio",
        help="[optimize 모드] 최대화할 지표 (sharpe_ratio, total_return, calmar_ratio 등)",
    )
    parser.add_argument(
        "--train-ratio", type=float, default=0.7,
        help="[optimize 모드] 학습 구간 비율 (0~1). 나머지는 OOS 검증용",
    )
    parser.add_argument(
        "--optimize-calls", type=int, default=30,
        help="[optimize 모드, bayesian] 목적 함수 호출 횟수",
    )
    parser.add_argument(
        "--update-holidays",
        action="store_true",
        help="config/holidays.yaml을 pykrx(또는 fallback)로 자동 갱신 후 종료",
    )
    parser.add_argument(
        "--mode", type=str, default="backtest",
        choices=["backtest", "validate", "paper", "live", "liquidate", "compare", "optimize", "dashboard"],
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
        "--allow-lookahead", action="store_true",
        help="[위험] Look-Ahead Bias 방지(strict-lookahead)를 해제합니다. 기본값은 strict-lookahead=True이며, 이 옵션 사용 시에만 해제됩니다. 수익률 과대평가·실전 손실 위험.",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="실전 모드 진입 시 필수. 미지정 시 live 모드 진입 거부.",
    )
    parser.add_argument(
        "--output-dir", type=str, default="reports",
        help="백테스트 리포트 저장 디렉토리 (기본: reports)",
    )
    parser.add_argument(
        "--benchmark-symbol", type=str, default="KS11",
        help="전략 검증 벤치마크 종목/지수 코드 (기본: KS11, 코스피 지수)",
    )
    parser.add_argument(
        "--validation-years", type=int, default=5,
        help="전략 검증 시 조회 연수. 최소 3년 권장, 기본 5년 (오버피팅 방지·통계적 신뢰)",
    )
    parser.add_argument(
        "--split-ratio", type=float, default=0.7,
        help="전략 검증 시 in-sample 비율 (기본: 0.7). out-of-sample과 분리해 오버피팅 검증",
    )
    parser.add_argument(
        "--min-sharpe", type=float, default=1.0,
        help="전략 검증 통과 기준: 샤프 비율 최소값 (기본: 1.0). 미달 시 실전 투입 비권장",
    )
    parser.add_argument(
        "--max-mdd", type=float, default=-20.0,
        help="전략 검증 최대 허용 MDD(%, 음수값, 기본: -20)",
    )
    parser.add_argument(
        "--compare-symbol", type=str, default=None,
        help="[compare 모드] 백테스트 대상 종목. 미지정 시 기간 내 모의투자 거래 최다 종목 사용",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=None,
        help="[dashboard 모드] 웹 대시보드 포트 (기본: config dashboard.port 또는 8080)",
    )

    args = parser.parse_args()
    # Look-Ahead Bias 방지: 기본값 True. --allow-lookahead 사용 시에만 False(해제 시 명시적 경고 출력)
    args.strict_lookahead = True
    if args.allow_lookahead:
        args.strict_lookahead = False

    # 휴장일 파일만 갱신 후 종료
    if getattr(args, "update_holidays", False):
        setup_logger()
        from core.holidays_updater import update_holidays_yaml
        update_holidays_yaml()
        logger.info("휴장일 파일 갱신 완료. config/holidays.yaml 확인.")
        return

    # 시스템 초기화
    setup_logger()
    init_database()

    logger.info("🚀 퀀트 트레이더 시작 (모드: {}, 전략: {})", args.mode, args.strategy)

    try:
        if args.mode == "backtest":
            run_backtest(args)
        elif args.mode == "validate":
            run_strategy_validation(args)
        elif args.mode == "paper":
            run_paper_trading(args)
        elif args.mode == "live":
            run_live_trading(args)
        elif args.mode == "liquidate":
            run_emergency_liquidate(args)
        elif args.mode == "compare":
            run_compare_paper_backtest(args)
        elif args.mode == "optimize":
            run_param_optimize(args)
        elif args.mode == "dashboard":
            run_dashboard(args)
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
