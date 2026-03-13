"""
로깅 시스템 모듈
- loguru 기반 구조화된 로깅
- 파일 로테이션, 콘솔 출력 지원
"""

import sys
from pathlib import Path
from loguru import logger

from config.config_loader import Config


def setup_logger():
    """
    로깅 시스템 초기화
    - 콘솔 출력 + 파일 출력 설정
    - 설정 파일(settings.yaml)의 logging 섹션 참조
    """
    config = Config.get()
    log_config = config.logging_config

    log_level = log_config.get("level", "INFO")
    log_dir = log_config.get("log_dir", "logs")
    rotation = log_config.get("rotation", "10 MB")
    retention = log_config.get("retention", "30 days")
    console_output = log_config.get("console_output", True)

    # 기존 핸들러 제거
    logger.remove()

    # 콘솔 출력 핸들러
    if console_output:
        logger.add(
            sys.stderr,
            level=log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level:^8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            colorize=True,
        )

    # 로그 디렉토리 생성
    project_root = Path(__file__).parent.parent
    log_path = project_root / log_dir
    log_path.mkdir(parents=True, exist_ok=True)

    # 전체 로그 파일 (모든 레벨)
    logger.add(
        log_path / "quant_trader_{time:YYYY-MM-DD}.log",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:^8} | {name}:{function}:{line} | {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    # 에러 전용 로그 파일
    logger.add(
        log_path / "error_{time:YYYY-MM-DD}.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:^8} | {name}:{function}:{line} | {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    # 매매 기록 전용 로그 파일
    logger.add(
        log_path / "trades_{time:YYYY-MM-DD}.log",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        filter=lambda record: "trade" in record["extra"],
    )

    logger.info("로깅 시스템 초기화 완료 (레벨: {}, 디렉토리: {})", log_level, log_path)
    return logger


# 매매 전용 로거 (trades 로그 파일에만 기록)
trade_logger = logger.bind(trade=True)


def log_trade(action: str, symbol: str, price: float, quantity: int, reason: str = ""):
    """
    매매 기록 로깅 편의 함수

    Args:
        action: 매매 유형 (BUY, SELL, STOP_LOSS, TAKE_PROFIT 등)
        symbol: 종목 코드
        price: 체결 가격
        quantity: 체결 수량
        reason: 매매 사유
    """
    trade_logger.info(
        "[{action}] 종목={symbol} | 가격={price:,.0f}원 | 수량={quantity}주 | 사유={reason}",
        action=action,
        symbol=symbol,
        price=price,
        quantity=quantity,
        reason=reason,
    )


def log_signal(symbol: str, signal: str, score: float, details: dict):
    """
    매매 신호 로깅 편의 함수

    Args:
        symbol: 종목 코드
        signal: 신호 (BUY, SELL, HOLD)
        score: 합산 점수
        details: 개별 지표 점수 상세
    """
    logger.info(
        "[신호] 종목={} | 신호={} | 점수={:.1f} | 상세={}",
        symbol, signal, score, details,
    )
