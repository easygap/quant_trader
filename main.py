"""
퀀트 트레이더 시스템 - 메인 실행 파일

사용법:
    # 백테스팅 (과거 데이터로 전략 검증)
    python main.py --mode backtest --strategy scoring --symbol 005930

    # 백테스트 리포트 저장 경로 지정
    python main.py --mode backtest --strategy scoring --symbol 005930 --output-dir reports

    # 페이퍼 트레이딩 (모의 매매, 워치리스트 1회 순회 후 종료)
    python main.py --mode paper --strategy scoring

    # 모의 24시간 스케줄 루프 (systemd 상시 구동용, config trading.mode=paper 권장)
    python main.py --mode schedule --strategy scoring

    # 실전 (ENABLE_LIVE_TRADING=true + --confirm-live 필요)
    python main.py --mode live --strategy scoring --confirm-live

    # 특정 기간 백테스팅
    python main.py --mode backtest --strategy scoring --symbol 005930 --start 2023-01-01 --end 2025-12-31

    # 긴급 전체 청산 (수동 개입·블랙스완 외 상황에서 즉시 전 종목 매도)
    python main.py --mode liquidate

    # 바스켓 리밸런싱 (목표 비중 대비 드리프트 체크 → 주문)
    python main.py --mode rebalance --basket kr_blue_chip
    python main.py --mode rebalance --basket kr_blue_chip --dry-run
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

    config = Config.get()
    universe = (config.risk_params or {}).get("backtest_universe") or {}
    u_mode = (universe.get("mode") or "current").strip().lower()
    if u_mode == "current":
        import sys
        logger.warning(
            "backtest_universe.mode=current — 현재 상장 종목 기준. "
            "생존자 편향으로 수익률이 과대평가될 수 있습니다. "
            "mode: historical 또는 kospi200 권장 (설계서 §8.2.1)."
        )
        print(
            "\n" + "=" * 60 + "\n"
            "  ⚠️  [경고] 생존자 편향 (Survivorship Bias)\n"
            + "=" * 60 + "\n"
            "  backtest_universe.mode = current (기본)\n"
            "  → 현재 상장 종목만 사용하므로 상장폐지 종목이 제외됩니다.\n"
            "  → 백테스트 수익률이 실전보다 과대평가될 수 있습니다.\n"
            "  → config/risk_params.yaml에서 mode: historical 로 변경 권장.\n"
            + "=" * 60 + "\n",
            file=sys.stderr,
        )

    collector = DataCollector()
    symbol = args.symbol or "005930"

    logger.info("종목 {} 데이터 수집 중...", symbol)
    df = collector.fetch_stock(symbol, args.start, args.end)

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
        notify_overtrading=True,
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


def _run_auto_correlation(df, config, threshold=0.7):
    """상관 분석 실행 후 자동 비활성화 대상 가중치 키 반환."""
    from core.indicator_correlation import (
        run_indicator_correlation_check,
        suggest_disable_weights,
    )

    result = run_indicator_correlation_check(
        df,
        threshold=threshold,
        config=config,
        save_report=False,
    )
    disable = suggest_disable_weights(result.get("high_correlation_pairs", []))
    if result.get("high_correlation_pairs"):
        logger.info(
            "[auto-correlation] 고상관 쌍 {}개 발견: {}",
            len(result["high_correlation_pairs"]),
            [(p[0], p[1], f"r={p[2]:.2f}") for p in result["high_correlation_pairs"]],
        )
    if disable:
        logger.info("[auto-correlation] 자동 비활성화: {}", disable)
    else:
        logger.info("[auto-correlation] 고상관 쌍 없음. 모든 지표 유지.")
    return disable


def run_param_optimize(args):
    """전략 파라미터 자동 최적화 (Grid Search / Bayesian / 가중치 포함).

    --include-weights: 스코어링 전략일 때 가중치도 탐색 (대칭 Grid, OOS 샤프 게이트 포함).
    --auto-correlation: 최적화 전 상관 분석 자동 실행, 고상관 지표 비활성화 후 최적화.
    권장 파이프라인: check_correlation → optimize --include-weights → validate --walk-forward.
    """
    from core.data_collector import DataCollector
    from backtest.param_optimizer import grid_search, bayesian_optimize, grid_search_scoring_weights

    logger.info("=" * 50)
    logger.info("전략 파라미터 최적화 ({}), 전략: {}", getattr(args, "optimizer", "grid"), args.strategy)
    logger.info("=" * 50)

    config = Config.get()
    collector = DataCollector()
    symbol = args.symbol or "005930"
    df = collector.fetch_stock(symbol, args.start, args.end)
    if df.empty or len(df) < 252:
        logger.error("데이터가 없거나 1년 미만입니다. --start, --end 및 데이터 소스를 확인하세요.")
        return

    optimizer = getattr(args, "optimizer", "grid")
    metric = getattr(args, "optimize_metric", "sharpe_ratio")
    train_ratio = getattr(args, "train_ratio", 0.7)
    include_weights = getattr(args, "include_weights", False)

    # 스코어링 전략 + --include-weights → 가중치 포함 Grid Search
    if args.strategy == "scoring" and include_weights:
        disabled = [s.strip() for s in (getattr(args, "disable_weights", "") or "").split(",") if s.strip()]

        auto_corr = getattr(args, "auto_correlation", False)
        if auto_corr:
            corr_threshold = getattr(args, "correlation_threshold", 0.7)
            auto_disabled = _run_auto_correlation(df, config, threshold=corr_threshold)
            merged = sorted(set(disabled) | set(auto_disabled))
            if merged != sorted(disabled):
                logger.info(
                    "[auto-correlation] 비활성화 병합: {} → {}",
                    disabled or "(없음)", merged,
                )
            disabled = merged

        result = grid_search_scoring_weights(
            df,
            metric=metric,
            train_ratio=train_ratio,
            strict_lookahead=True,
            disabled_weights=disabled or None,
        )
        if result is None:
            return
        if result.get("message"):
            logger.warning(result["message"])
            return

        logger.info("=" * 60)
        logger.info("[가중치 최적화 결과]")
        logger.info("평가 조합 수: {}", result["total_evaluated"])
        logger.info("최적 가중치 (대칭): {}", result["best_weight_combo"])
        logger.info("최적 임계값: buy={}, sell={}", *result["best_threshold"])
        logger.info("학습 구간 {} ({}): {:.2f}", metric, result["train_period"], result["best_score"])
        if result.get("disabled_weights"):
            logger.info("비활성화 지표(0 고정): {}", result["disabled_weights"])

        if result.get("oos_metrics"):
            oos = result["oos_metrics"]
            logger.info(
                "OOS 구간 {} ({}): sharpe={}, return={:.1f}%",
                metric, result.get("oos_period", ""),
                oos.get("sharpe_ratio"), oos.get("total_return", 0),
            )
            logger.info(
                "OOS 샤프 게이트 통과 (≥ {:.1f}). 채택 스니펫은 위 stdout에 출력되었습니다.",
                result["oos_sharpe_gate"],
            )
        if result.get("all_results"):
            logger.info("상위 5개 조합:")
            for r in result["all_results"][:5]:
                logger.info("  {} | threshold={} | {}={:.2f}", r["weight_combo"], r["threshold"], metric, r["score"])

        logger.info("=" * 60)
        validate_cmd = (
            f"python main.py --mode validate --walk-forward --strategy scoring "
            f"--symbol {symbol} --validation-years 5"
        )
        logger.info("다음 단계 (워크포워드 검증):\n  {}", validate_cmd)
        if result["oos_passed"]:
            logger.info(
                "YAML 스니펫을 strategies.yaml에 반영한 뒤 위 명령을 실행하세요.\n"
                "워크포워드에서도 안정적이면 --mode paper 로 모의투자를 시작합니다."
            )
        return

    # 기존: 임계값만 탐색
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


def run_check_indicator_correlation(args):
    """스코어링 지표 독립성 검증: 지표 간 상관계수 계산, 0.7 이상 쌍에 대해 제거/가중치 축소 권고."""
    from datetime import datetime, timedelta

    from core.data_collector import DataCollector
    from core.indicator_correlation import run_indicator_correlation_check, render_correlation_report

    symbol = args.symbol or "005930"
    years = getattr(args, "validation_years", 5)
    end_date = args.end
    start_date = args.start
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    collector = DataCollector()
    df = collector.fetch_stock(symbol, start_date, end_date)
    if df.empty or len(df) < 120:
        logger.error("지표 상관 검증용 데이터 부족: {} ({}~{}, {}일)", symbol, start_date, end_date, len(df))
        return

    threshold = args.correlation_threshold
    result = run_indicator_correlation_check(
        df,
        threshold=threshold,
        symbol=symbol,
        config=Config.get(),
        output_dir=getattr(args, "output_dir", "reports"),
        save_report=True,
        start=start_date,
        end=end_date,
    )
    report_text = render_correlation_report(result, symbol=symbol, start=start_date, end=end_date)
    print(report_text)
    logger.info("지표 독립성 검증 리포트 저장: {}", result.get("report_path"))

    disable_weights = result.get("disable_weights", [])
    if disable_weights:
        logger.info(
            "고상관 지표 자동 비활성화 대상: {}. "
            "optimize --include-weights --disable-weights {} 로 전달하거나 "
            "--auto-correlation 플래그를 사용하세요.",
            disable_weights, ",".join(disable_weights),
        )


def run_check_ensemble_correlation(args):
    """앙상블 구성 전략(technical, momentum_factor, volatility_condition) 신호 간 상관계수 검증.
    BUY=1, SELL=-1, HOLD=0 로 수치화한 일별 시리즈 상관계수. |r| >= 0.6 이면 conservative 모드 또는 전략 재구성 권고."""
    from datetime import datetime, timedelta
    from pathlib import Path

    from core.data_collector import DataCollector
    from core.ensemble_correlation import run_ensemble_signal_correlation_check, render_ensemble_correlation_report

    symbol = args.symbol or "005930"
    years = getattr(args, "validation_years", 5)
    end_date = args.end
    start_date = args.start
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    collector = DataCollector()
    df = collector.fetch_stock(symbol, start_date, end_date)
    if df.empty or len(df) < 60:
        logger.error("앙상블 신호 상관 검증용 데이터 부족: {} ({}~{}, {}일)", symbol, start_date, end_date, len(df))
        return

    threshold = getattr(args, "ensemble_correlation_threshold", 0.6)
    result = run_ensemble_signal_correlation_check(df, threshold=threshold)
    report_text = render_ensemble_correlation_report(result)

    out_dir = Path(getattr(args, "output_dir", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"ensemble_correlation_{symbol}_{timestamp}.txt"
    report_path.write_text(report_text, encoding="utf-8")

    print(report_text)
    logger.info("앙상블 신호 독립성 검증 리포트 저장: {}", report_path)


def run_strategy_validation(args):
    """전략 검증 모드 실행. --walk-forward 시 슬라이딩 윈도우 워크포워드 검증."""
    from backtest.strategy_validator import StrategyValidator
    from core.notifier import Notifier

    config = Config.get()
    universe = (config.risk_params or {}).get("backtest_universe") or {}
    u_mode = (universe.get("mode") or "current").strip().lower()
    if u_mode == "current":
        logger.warning(
            "backtest_universe.mode=current — 벤치마크·유니버스가 현재 종목 기준. "
            "생존자 편향 위험. mode: historical 또는 kospi200 권장 (§8.2.1)."
        )
    else:
        logger.info("backtest_universe.mode={} — 생존자 편향 완화 적용", u_mode)

    validator = StrategyValidator(config=config, output_dir=args.output_dir)
    discord = Notifier(config)

    if getattr(args, "walk_forward", False):
        result = validator.run_walk_forward(
            symbol=args.symbol or "005930",
            strategy_name=args.strategy,
            start_date=args.start,
            end_date=args.end,
            benchmark_symbol=args.benchmark_symbol,
            validation_years=args.validation_years,
            train_days=504,
            test_days=252,
            step_days=252,
            min_sharpe=args.min_sharpe,
            max_mdd=args.max_mdd,
        )
        print(validator._render_walk_forward_report(result))
        logger.info("워크포워드 검증 리포트 저장 완료: {}", result["report_path"])
        warnings = result.get("warnings", [])
    else:
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
            use_benchmark_top50=not getattr(args, "no_benchmark_top50", False),
        )
        validator.print_report(result)
        logger.info("전략 검증 리포트 저장 완료: {}", result["report_path"])
        warnings = result.get("validation", {}).get("warnings", [])

    if warnings:
        warn_text = "\n".join([f"⚠️ {w}" for w in warnings])
        discord.send_message(
            f"📊 전략 검증 경고 ({args.strategy} | {args.symbol or '005930'})\n{warn_text}"
        )

    # 앙상블 전략일 때 독립성 검증 자동 실행
    if args.strategy == "ensemble":
        _run_ensemble_independence_check_in_validate(args, config, discord)


def _run_ensemble_independence_check_in_validate(args, config, notifier):
    """validate 모드에서 앙상블 전략의 신호 독립성을 자동 검증한다."""
    from datetime import datetime, timedelta
    from core.data_collector import DataCollector
    from core.ensemble_correlation import (
        run_ensemble_signal_correlation_check,
        render_ensemble_correlation_report,
    )

    symbol = args.symbol or "005930"
    end_date = args.end or datetime.now().strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")

    collector = DataCollector(config)
    df = collector.fetch_stock(symbol, start_date, end_date)
    if df.empty or len(df) < 60:
        logger.warning("앙상블 독립성 검증 스킵: 데이터 부족 ({}일)", len(df) if df is not None else 0)
        return

    threshold = getattr(args, "ensemble_correlation_threshold", 0.6)
    result = run_ensemble_signal_correlation_check(df, threshold=threshold, config=config)
    report = render_ensemble_correlation_report(result)

    logger.info("\n{}", report)

    if result.get("should_force_conservative"):
        warn_msg = (
            f"앙상블 독립성 검증 경고 ({symbol}): "
            f"고상관 쌍 {result['n_high']}개 감지 (|r|>={threshold}). "
            "majority_vote 모드에서 다수결 의미 퇴색 위험. "
            "conservative 전환 또는 전략 재구성 권장."
        )
        notifier.send_message(warn_msg)
    else:
        logger.info("앙상블 독립성 검증 통과: 모든 전략 쌍 |r| < {:.1f}", threshold)


# ──────────────────────────────────────────────────────────────
# 바스켓 리밸런싱 모드
# ──────────────────────────────────────────────────────────────
def run_rebalance(args):
    """바스켓 포트폴리오 리밸런싱 모드."""
    from core.basket_rebalancer import BasketRebalancer
    from core.notifier import Notifier

    config = Config.get()
    notifier = Notifier(config)
    basket_name = getattr(args, "basket", None)
    dry_run = getattr(args, "dry_run", False)

    if basket_name:
        basket_names = [basket_name]
    else:
        basket_names = BasketRebalancer.get_enabled_baskets()
        if not basket_names:
            logger.warning("enabled=true인 바스켓이 없습니다. --basket으로 지정하거나 baskets.yaml에서 enabled를 true로 설정하세요.")
            return

    logger.info("=" * 50)
    logger.info("🔄 바스켓 리밸런싱 시작 (바스켓: {}, dry_run: {})", basket_names, dry_run)
    logger.info("=" * 50)

    for name in basket_names:
        try:
            rebalancer = BasketRebalancer(basket_name=name, config=config)

            report = rebalancer.get_status_report()
            logger.info("\n{}", report)

            should, reason = rebalancer.should_rebalance()
            if not should and not dry_run:
                logger.info("바스켓 '{}' 리밸런싱 불필요: {}", name, reason)
                continue

            orders = rebalancer.plan_rebalance()
            if not orders:
                logger.info("바스켓 '{}' 리밸런싱 주문 없음", name)
                continue

            result = rebalancer.execute(orders, dry_run=dry_run)

            summary = (
                f"바스켓 '{name}' 리밸런싱 {'(DRY RUN) ' if dry_run else ''}"
                f"완료: 실행 {result['executed']}건, 스킵 {result['skipped']}건, "
                f"실패 {result['failed']}건"
            )
            logger.info(summary)
            notifier.send_message(summary)

        except Exception as e:
            logger.error("바스켓 '{}' 리밸런싱 실패: {}", name, e)
            notifier.send_message(f"바스켓 '{name}' 리밸런싱 오류: {e}")


def run_paper_trading(args):
    """페이퍼 트레이딩 모드 실행"""
    from datetime import datetime
    from core.data_collector import DataCollector
    from core.order_executor import OrderExecutor
    from core.portfolio_manager import PortfolioManager
    from core.notifier import Notifier
    from database.repositories import get_position, get_all_positions

    config = Config.get()

    logger.info("=" * 50)
    logger.info("📄 페이퍼 트레이딩 모드 시작")
    logger.info("=" * 50)

    collector = DataCollector()
    account_key = args.strategy or ""
    portfolio = PortfolioManager(config, account_key=account_key)
    discord = Notifier(config)
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
                    df = collector.fetch_stock(pos.symbol)
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
            df = collector.fetch_stock(symbol)

            if df.empty or len(df) < 30:
                logger.warning("종목 {} 데이터 부족 — 스킵", symbol)
                continue

            # 신호 생성 (평균회귀 시 symbol 전달 → 펀더멘털 필터 사용)
            signal_info = strategy.generate_signal(df, symbol=symbol)

            logger.info(
                "종목 {} | 신호: {} | 점수: {} | 상세: {}",
                symbol, signal_info["signal"], signal_info["score"], signal_info["details"],
            )

            if signal_info["signal"] != "HOLD":
                discord.send_signal_alert(symbol, signal_info)

            if signal_info["signal"] == "BUY" and not get_position(symbol, account_key=account_key):
                from core.market_regime import check_market_regime
                regime_result = check_market_regime(config, collector)
                if not regime_result["allow_buys"]:
                    logger.info("시장 국면 [bearish] — 200일선+단기모멘텀 하락 → {} 매수 스킵", symbol)
                    continue
                regime_scale = regime_result["position_scale"]
                capital_summary = portfolio.get_portfolio_summary()
                adjusted_capital = capital_summary["total_value"] * regime_scale
                adjusted_cash = capital_summary["cash"] * regime_scale
                if regime_scale < 1.0:
                    logger.info("시장 국면 [caution] — {} 포지션 사이징 {:.0f}%로 축소", symbol, regime_scale * 100)
                avg_vol = float(df["volume"].rolling(20, min_periods=1).mean().iloc[-1]) if "volume" in df.columns and not df["volume"].empty else None
                order_result = executor.execute_buy(
                    symbol=symbol,
                    price=signal_info["close"],
                    capital=adjusted_capital,
                    available_cash=adjusted_cash,
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


def _check_live_readiness_gate(config, strategy_name: str) -> list[str]:
    """
    라이브 전 필수 검증 게이트.
    실패 항목 리스트를 반환 (빈 리스트 = 통과).
    """
    issues = []

    # 1. 데이터 소스 일관성: KIS 폴백 허용 여부
    ds = config.get("data_source", {})
    if ds.get("allow_kis_fallback", True):
        issues.append(
            "data_source.allow_kis_fallback=true — KIS 비수정주가 폴백 가능. "
            "false로 설정하여 백테스트/실전 소스 일치 필요."
        )

    # 2. 페이퍼 트레이딩 최소 거래 기록 확인
    try:
        from database.repositories import get_recent_sell_trades
        recent_sells = get_recent_sell_trades(limit=50, mode="paper", account_key=strategy_name)
        if len(recent_sells) < 20:
            issues.append(
                f"페이퍼 트레이딩 매도 기록 {len(recent_sells)}건 (최소 20건 필요). "
                "충분한 페이퍼 트레이딩 후 진행하세요."
            )
    except Exception:
        issues.append("페이퍼 트레이딩 기록 조회 실패 — DB 확인 필요.")

    # 3. 스코어링 가중치 최적화 여부 (strategies.yaml 직관값 경고 확인)
    scoring_cfg = config.get("scoring", {})
    weights = scoring_cfg.get("weights", {})
    # 기본 직관값 그대로인지 확인 (rsi_oversold=2, macd_golden_cross=2)
    if weights.get("rsi_oversold") == 2 and weights.get("macd_golden_cross") == 2:
        issues.append(
            "스코어링 가중치가 기본 직관값 상태입니다. "
            "python main.py --mode optimize --include-weights 실행 후 진행 권장."
        )

    return issues


def run_live_trading(args):
    """
    실전 매매 모드 실행.
    이중 확인: ENABLE_LIVE_TRADING=true 환경변수 + --confirm-live 플래그 필수.
    삼중 확인: 라이브 검증 게이트 (--force-live로 강제 가능).
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

    # ── 라이브 전 필수 검증 게이트 ──
    force_live = getattr(args, "force_live", False)
    if not force_live:
        gate_issues = _check_live_readiness_gate(config, args.strategy or "scoring")
        if gate_issues:
            logger.error("=" * 50)
            logger.error("🚫 실전 전환 검증 실패 — 아래 항목 확인 후 재시도하세요:")
            for issue in gate_issues:
                logger.error("  - {}", issue)
            logger.error("강제 진행: --force-live 플래그 추가")
            logger.error("=" * 50)
            sys.exit(1)
        logger.info("✅ 라이브 전 검증 게이트 통과")

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


def run_scheduler_loop(args):
    """
    모의 매매용 무한 스케줄러 (장전/장중/장마감 루프).
    systemd 상시 구동 시 `--mode paper` 한 사이클 종료와 달리 프로세스를 유지한다.
    config.trading.mode 가 live 이면 거부 (--mode live --confirm-live 사용).
    """
    config = Config.get()
    if str(config.trading.get("mode", "paper")).lower() == "live":
        logger.error(
            "config.trading.mode 가 live 입니다. 실전 루프는 "
            "ENABLE_LIVE_TRADING=true 및 --mode live --confirm-live 를 사용하세요. "
            "schedule 모드는 모의(paper) 전용입니다."
        )
        sys.exit(1)

    from core.runtime_lock import scheduler_lock
    from core.scheduler import Scheduler

    root = Path(__file__).resolve().parent
    lock_file = root / "data" / ".scheduler.lock"

    with scheduler_lock(lock_file) as acquired:
        if not acquired:
            sys.exit(1)
        logger.info("=" * 50)
        logger.info(
            "🗓️ 스케줄러 모드 시작 (무한 루프, trading.mode={})",
            config.trading.get("mode", "paper"),
        )
        logger.info("=" * 50)
        scheduler = Scheduler(strategy_name=args.strategy, config=config)
        scheduler.run()


def run_compare_paper_backtest(args):
    """모의투자 결과 vs 백테스트 결과 자동 비교 + 실전 전환 준비 평가."""
    from datetime import datetime, timedelta
    from backtest.paper_compare import run_compare, check_live_readiness
    from core.notifier import Notifier

    config = Config.get()

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

    compare_symbol = getattr(args, "compare_symbol", None)
    result = run_compare(
        start_date=start_date,
        end_date=end_date,
        strategy_name=args.strategy,
        symbol=compare_symbol,
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

    # 실전 전환 준비 평가
    logger.info("-" * 50)
    logger.info("🔄 실전 전환 준비 평가")
    readiness = check_live_readiness(
        start_date=start_date,
        end_date=end_date,
        strategy_name=args.strategy,
        symbol=compare_symbol or result.get("symbol"),
        config=config,
    )
    cr = readiness["criteria"]
    logger.info(
        "기준: 방향성 ≥{}%, 수익률차 ≤{}%, 거래일 ≥{}일, 매도 ≥{}건",
        cr["min_direction_agreement_pct"], cr["max_return_diff_pct"],
        cr["min_trading_days"], cr["min_trades"],
    )
    if readiness.get("direction_agreement_pct") is not None:
        logger.info(
            "결과: 방향성 {:.1f}% | 수익률차 {:.1f}% | 거래일 {}일 | 매도 {}건",
            readiness["direction_agreement_pct"], readiness.get("return_diff_pct", 0),
            readiness["trading_days"], readiness["total_trades"],
        )
    status = "✅ 준비 완료" if readiness["ready"] else "⏳ 미달"
    logger.info("{}: {}", status, readiness["message"])

    discord = Notifier(config)
    risk = config.risk_params
    readiness_cfg = risk.get("paper_backtest_compare", {}).get("live_readiness", {})
    if readiness["ready"] and readiness_cfg.get("notify_on_ready", True):
        discord.send_embed(
            "✅ 실전 전환 준비 완료",
            readiness["message"],
            color=0x2ECC71,
            fields=[
                {"name": "방향성 일치율", "value": f"{readiness['direction_agreement_pct']:.1f}%", "inline": True},
                {"name": "수익률 차이", "value": f"{readiness.get('return_diff_pct', 0):.1f}%", "inline": True},
                {"name": "거래일수", "value": f"{readiness['trading_days']}일", "inline": True},
            ],
        )


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
    """전략 인스턴스 반환 (레지스트리 기반)"""
    from strategies import create_strategy
    return create_strategy(name)


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
  python main.py --mode schedule --strategy scoring  # 모의 스케줄 무한 루프 (서버 상시 구동)
  python main.py --mode live --strategy scoring --confirm-live  # 실전 (ENABLE_LIVE_TRADING=true 필요)
  python main.py --mode liquidate  # 긴급 전 종목 매도 (실전/모의 모두 DB 기준)
  python main.py --mode compare --start 2025-01-01 --end 2025-03-18 --strategy scoring  # 모의투자 vs 백테스트 비교
  python main.py --update-holidays  # 휴장일 파일(pykrx+fallback) 자동 갱신
  python main.py --mode optimize --strategy scoring  # 임계값만 Grid Search (오버피팅 주의)
  python main.py --mode optimize --strategy scoring --include-weights  # 가중치+임계값 동시 최적화 (OOS 샤프≥1.0 게이트)
  python main.py --mode optimize --strategy scoring --include-weights --auto-correlation  # 상관분석+비활성화+최적화 원스텝
  python main.py --mode optimize --strategy scoring --include-weights --disable-weights w_rsi,w_ma  # 수동으로 RSI·MA 제외
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
        "--include-weights", action="store_true",
        help="[optimize 모드, scoring] 스코어링 가중치(weights)도 탐색 (대칭 Grid + OOS 샤프 게이트)",
    )
    parser.add_argument(
        "--disable-weights", type=str, default="",
        help="[optimize 모드, --include-weights] 탐색에서 0으로 고정할 가중치 키 (쉼표 구분, 예: w_rsi,w_ma)",
    )
    parser.add_argument(
        "--auto-correlation", action="store_true",
        help="[optimize 모드, --include-weights] 최적화 전 상관 분석 자동 실행, 고상관 지표 자동 비활성화 후 최적화. "
        "check_correlation + optimize를 한 번에 수행.",
    )
    parser.add_argument(
        "--update-holidays",
        action="store_true",
        help="config/holidays.yaml을 pykrx(또는 fallback)로 자동 갱신 후 종료",
    )
    parser.add_argument(
        "--mode", type=str, default="backtest",
        choices=["backtest", "validate", "paper", "schedule", "live", "liquidate", "compare", "optimize", "dashboard", "check_correlation", "check_ensemble_correlation", "rebalance"],
        help="실행 모드. paper: 워치리스트 1회. schedule: 모의 스케줄 무한 루프(상시 서버). rebalance: 바스켓 리밸런싱.",
    )
    from strategies import get_strategy_names
    parser.add_argument(
        "--strategy", type=str, default="scoring",
        choices=get_strategy_names(),
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
        "--force-live", action="store_true",
        help="라이브 검증 게이트 강제 통과. 검증 미완료 상태에서 실전 진입 시 사용.",
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
        "--walk-forward", action="store_true",
        help="[validate 모드] 슬라이딩 윈도우 워크포워드 검증 (train 2년→test 1년, 1년 스텝 반복). 미지정 시 1회 train/test 분할",
    )
    parser.add_argument(
        "--no-benchmark-top50", action="store_true",
        help="[validate 모드] 코스피 상위 50종목 동일비중 벤치마크 비활성화 (기본: 사용)",
    )
    parser.add_argument(
        "--compare-symbol", type=str, default=None,
        help="[compare 모드] 백테스트 대상 종목. 미지정 시 기간 내 모의투자 거래 최다 종목 사용",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=None,
        help="[dashboard 모드] 웹 대시보드 포트 (기본: config dashboard.port 또는 8080)",
    )
    parser.add_argument(
        "--correlation-threshold", type=float, default=0.7,
        help="[check_correlation 모드] 고상관 판단 기준 (기본: 0.7). 이 값 이상이면 제거/가중치 축소 권고",
    )
    parser.add_argument(
        "--ensemble-correlation-threshold", type=float, default=0.6,
        help="[check_ensemble_correlation 모드] 앙상블 신호 고상관 기준 (기본: 0.6). 이 값 이상이면 conservative 또는 전략 재구성 권고",
    )
    parser.add_argument(
        "--basket", type=str, default=None,
        help="[rebalance 모드] 리밸런싱 대상 바스켓 이름 (미지정 시 enabled=true인 모든 바스켓)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="[rebalance 모드] 실제 주문 없이 계획만 출력",
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
        elif args.mode == "schedule":
            run_scheduler_loop(args)
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
        elif args.mode == "check_correlation":
            run_check_indicator_correlation(args)
        elif args.mode == "check_ensemble_correlation":
            run_check_ensemble_correlation(args)
        elif args.mode == "rebalance":
            run_rebalance(args)
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
