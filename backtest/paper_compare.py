"""
모의투자(paper) 결과 vs 백테스트 결과 자동 비교.

두 결과가 크게 다르면 구현 버그 또는 데이터 문제 신호로 간주하고,
설정한 임계값을 초과할 경우 divergence로 판정하여 로그/디스코드 알림을 보낸다.
"""

from datetime import datetime
from typing import Optional

from loguru import logger

from config.config_loader import Config
from database.repositories import (
    get_paper_performance_metrics,
    get_trade_history,
)
from core.data_collector import DataCollector
from backtest.backtester import Backtester


def _most_traded_symbol(
    start_date: datetime, end_date: datetime, mode: str = "paper", account_key: Optional[str] = None
) -> Optional[str]:
    """기간 내 모의투자 거래가 가장 많은 종목 코드 반환."""
    trades = get_trade_history(
        mode=mode, start_date=start_date, end_date=end_date, account_key=account_key
    )
    if not trades:
        return None
    from collections import Counter
    counts = Counter(t.symbol for t in trades if t.symbol)
    return counts.most_common(1)[0][0] if counts else None


def run_compare(
    start_date: datetime,
    end_date: datetime,
    strategy_name: str = "scoring",
    symbol: Optional[str] = None,
    config=None,
) -> dict:
    """
    모의투자 기간 성과와 동일 기간·동일 전략 백테스트 결과를 비교한다.

    Args:
        start_date: 비교 구간 시작일
        end_date: 비교 구간 종료일
        strategy_name: 전략명 (백테스트에 사용)
        symbol: 백테스트 대상 종목. None이면 기간 내 paper 거래 최다 종목 사용
        config: Config 인스턴스 (None이면 Config.get())

    Returns:
        {
            "paper_metrics": {...},
            "backtest_metrics": {...},
            "return_diff_pct": float,
            "win_rate_diff_pct": float,
            "divergence": bool,
            "message": str,
        }
    """
    cfg = config or Config.get()
    risk = cfg.risk_params
    compare_cfg = risk.get("paper_backtest_compare", {})
    return_threshold = compare_cfg.get("return_diff_warn_pct", 15)
    win_rate_threshold = compare_cfg.get("win_rate_diff_warn_pct", 20)
    initial_capital = risk.get("position_sizing", {}).get("initial_capital", 10_000_000)

    account_key = strategy_name or ""
    paper_metrics = get_paper_performance_metrics(
        start_date, end_date, mode="paper", initial_capital=initial_capital,
        account_key=account_key if account_key else None,
    )
    if not paper_metrics:
        return {
            "paper_metrics": None,
            "backtest_metrics": None,
            "return_diff_pct": None,
            "win_rate_diff_pct": None,
            "divergence": False,
            "message": "모의투자 기간 데이터가 없어 비교할 수 없습니다. 스냅샷 또는 거래 기록을 확인하세요.",
        }
    if symbol is None:
        symbol = _most_traded_symbol(start_date, end_date, account_key=account_key if account_key else None)
    if not symbol:
        return {
            "paper_metrics": paper_metrics,
            "backtest_metrics": None,
            "return_diff_pct": None,
            "win_rate_diff_pct": None,
            "divergence": False,
            "message": "기간 내 모의투자 거래가 없어 백테스트 비교 대상 종목을 정할 수 없습니다.",
        }

    collector = DataCollector()
    df = collector.fetch_korean_stock(symbol, start_date, end_date)
    if df.empty or len(df) < 10:
        return {
            "paper_metrics": paper_metrics,
            "backtest_metrics": None,
            "return_diff_pct": None,
            "win_rate_diff_pct": None,
            "divergence": False,
            "message": f"종목 {symbol} 해당 기간 주가 데이터가 없거나 부족해 백테스트를 수행할 수 없습니다.",
        }

    backtester = Backtester(config=cfg)
    result = backtester.run(
        df,
        strategy_name=strategy_name,
        initial_capital=initial_capital,
        strict_lookahead=True,
    )
    if not result or "metrics" not in result:
        return {
            "paper_metrics": paper_metrics,
            "backtest_metrics": None,
            "return_diff_pct": None,
            "win_rate_diff_pct": None,
            "divergence": False,
            "message": f"종목 {symbol} 백테스트 결과가 비어 있습니다.",
        }

    bt_metrics = result["metrics"]
    return_diff = abs(paper_metrics["total_return_pct"] - bt_metrics["total_return"])
    win_rate_diff = abs(paper_metrics["win_rate"] - bt_metrics["win_rate"])
    divergence = return_diff > return_threshold or win_rate_diff > win_rate_threshold

    out = {
        "paper_metrics": paper_metrics,
        "backtest_metrics": bt_metrics,
        "return_diff_pct": round(return_diff, 2),
        "win_rate_diff_pct": round(win_rate_diff, 2),
        "divergence": divergence,
        "symbol": symbol,
        "message": "",
    }
    if divergence:
        out["message"] = (
            f"모의투자 vs 백테스트 차이 임계값 초과: "
            f"수익률 차이 {return_diff:.1f}% (기준 {return_threshold}%), "
            f"승률 차이 {win_rate_diff:.1f}% (기준 {win_rate_threshold}%). "
            "구현 버그 또는 데이터 문제 가능성을 점검하세요."
        )
        logger.warning(out["message"])
        if compare_cfg.get("notify_on_divergence"):
            try:
                from monitoring.discord_bot import DiscordBot
                bot = DiscordBot(cfg)
                bot.send_embed(
                    "⚠️ 모의투자 vs 백테스트 차이 경고",
                    out["message"],
                    color=0xE74C3C,
                )
            except Exception as e:
                logger.warning("디스코드 divergence 알림 전송 실패: {}", e)
    else:
        out["message"] = (
            f"모의투자와 백테스트 결과가 설정 임계값 이내입니다 "
            f"(수익률 차이 {return_diff:.1f}%, 승률 차이 {win_rate_diff:.1f}%)."
        )
        logger.info(out["message"])
    return out
