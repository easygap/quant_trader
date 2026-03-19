"""
모의투자(paper) 결과 vs 백테스트 결과 자동 비교.

두 결과가 크게 다르면 구현 버그 또는 데이터 문제 신호로 간주하고,
설정한 임계값을 초과할 경우 divergence로 판정하여 로그/디스코드 알림을 보낸다.

또한 paper → live 전환 기준을 자동 평가하는 check_live_readiness()를 제공한다.
방향성 일치율, 누적 수익률 차이, 최소 거래일수를 기준으로 "실전 전환 준비 완료" 신호를 판별한다.
"""

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.config_loader import Config
from database.repositories import (
    get_paper_performance_metrics,
    get_portfolio_snapshots_between,
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
                from core.notifier import Notifier
                notifier = Notifier(cfg)
                notifier.send_embed(
                    "⚠️ 모의투자 vs 백테스트 차이 경고",
                    out["message"],
                    color=0xE74C3C,
                    critical=True,
                )
            except Exception as e:
                logger.warning("divergence 알림 전송 실패: {}", e)
    else:
        out["message"] = (
            f"모의투자와 백테스트 결과가 설정 임계값 이내입니다 "
            f"(수익률 차이 {return_diff:.1f}%, 승률 차이 {win_rate_diff:.1f}%)."
        )
        logger.info(out["message"])
    return out


def _compute_direction_agreement(
    paper_returns: pd.Series, backtest_returns: pd.Series
) -> float:
    """
    두 일별 수익률 시리즈의 방향성 일치율(%) 계산.
    같은 날짜에 둘 다 양(+) 또는 둘 다 음(-)이면 일치.
    0이면 일치로 간주(변동 없는 날).
    """
    aligned = pd.concat(
        [paper_returns.rename("paper"), backtest_returns.rename("bt")],
        axis=1, join="inner",
    ).dropna()
    if aligned.empty:
        return 0.0
    same_dir = (
        (aligned["paper"] >= 0) & (aligned["bt"] >= 0)
    ) | (
        (aligned["paper"] < 0) & (aligned["bt"] < 0)
    )
    return round(same_dir.mean() * 100, 1)


def check_live_readiness(
    start_date: datetime,
    end_date: datetime,
    strategy_name: str = "scoring",
    symbol: Optional[str] = None,
    config=None,
) -> dict:
    """
    paper 모의투자 결과가 백테스트와 충분히 일치하는지 평가하여
    '실전 전환 준비 완료' 여부를 반환한다.

    기준 (risk_params.yaml → paper_backtest_compare.live_readiness):
      - min_direction_agreement_pct: 방향성 일치율 하한 (기본 70%)
      - max_return_diff_pct: 누적 수익률 차이 상한 (기본 5%)
      - min_trading_days: 최소 평가 거래일수 (기본 20)
      - min_trades: 최소 매도 거래 건수 (기본 5)

    Returns:
        {
            "ready": bool,
            "direction_agreement_pct": float | None,
            "return_diff_pct": float | None,
            "trading_days": int,
            "total_trades": int,
            "criteria": {...},
            "reasons": [str],
            "message": str,
        }
    """
    cfg = config or Config.get()
    risk = cfg.risk_params
    compare_cfg = risk.get("paper_backtest_compare", {})
    readiness_cfg = compare_cfg.get("live_readiness", {})

    min_dir_agree = readiness_cfg.get("min_direction_agreement_pct", 70)
    max_ret_diff = readiness_cfg.get("max_return_diff_pct", 5)
    min_trading_days = readiness_cfg.get("min_trading_days", 20)
    min_trades = readiness_cfg.get("min_trades", 5)
    initial_capital = risk.get("position_sizing", {}).get("initial_capital", 10_000_000)

    criteria = {
        "min_direction_agreement_pct": min_dir_agree,
        "max_return_diff_pct": max_ret_diff,
        "min_trading_days": min_trading_days,
        "min_trades": min_trades,
    }
    base = {
        "ready": False,
        "direction_agreement_pct": None,
        "return_diff_pct": None,
        "trading_days": 0,
        "total_trades": 0,
        "criteria": criteria,
        "reasons": [],
        "message": "",
    }

    account_key = strategy_name or ""
    snapshots = get_portfolio_snapshots_between(
        start_date, end_date, account_key=account_key if account_key else None,
    )
    if len(snapshots) < 2:
        base["message"] = "포트폴리오 스냅샷이 부족하여 실전 전환 평가 불가 (최소 2일 필요)."
        base["reasons"].append(base["message"])
        logger.warning(base["message"])
        return base

    paper_df = pd.DataFrame(snapshots)
    paper_df["date"] = pd.to_datetime(paper_df["date"])
    paper_df = paper_df.sort_values("date").set_index("date")
    paper_equity = paper_df["total_value"].astype(float)
    paper_returns = paper_equity.pct_change().dropna()
    trading_days = len(paper_returns)
    base["trading_days"] = trading_days

    paper_metrics = get_paper_performance_metrics(
        start_date, end_date, mode="paper", initial_capital=initial_capital,
        account_key=account_key if account_key else None,
    )
    total_trades = paper_metrics["total_trades"] if paper_metrics else 0
    base["total_trades"] = total_trades

    if symbol is None:
        symbol = _most_traded_symbol(start_date, end_date, account_key=account_key if account_key else None)
    if not symbol:
        base["message"] = "기간 내 모의투자 거래가 없어 평가 대상 종목을 정할 수 없습니다."
        base["reasons"].append(base["message"])
        logger.warning(base["message"])
        return base

    collector = DataCollector()
    df = collector.fetch_korean_stock(symbol, start_date, end_date)
    if df.empty or len(df) < 10:
        base["message"] = f"종목 {symbol} 주가 데이터 부족으로 백테스트 비교 불가."
        base["reasons"].append(base["message"])
        logger.warning(base["message"])
        return base

    backtester = Backtester(config=cfg)
    bt_result = backtester.run(
        df, strategy_name=strategy_name,
        initial_capital=initial_capital, strict_lookahead=True,
    )
    if not bt_result or "equity_curve" not in bt_result:
        base["message"] = f"종목 {symbol} 백테스트 결과가 비어 있습니다."
        base["reasons"].append(base["message"])
        logger.warning(base["message"])
        return base

    bt_equity_df = bt_result["equity_curve"]
    bt_equity_df["date"] = pd.to_datetime(bt_equity_df["date"])
    bt_equity = bt_equity_df.set_index("date")["equity"].astype(float)
    bt_returns = bt_equity.pct_change().dropna()

    dir_agree = _compute_direction_agreement(paper_returns, bt_returns)
    base["direction_agreement_pct"] = dir_agree

    paper_total_ret = paper_metrics["total_return_pct"] if paper_metrics else 0
    bt_total_ret = bt_result["metrics"].get("total_return", 0)
    ret_diff = abs(paper_total_ret - bt_total_ret)
    base["return_diff_pct"] = round(ret_diff, 2)

    reasons = []
    passed_all = True

    if trading_days < min_trading_days:
        reasons.append(f"거래일수 {trading_days}일 < 기준 {min_trading_days}일")
        passed_all = False

    if total_trades < min_trades:
        reasons.append(f"총 매도 거래 {total_trades}건 < 기준 {min_trades}건")
        passed_all = False

    if dir_agree < min_dir_agree:
        reasons.append(f"방향성 일치율 {dir_agree:.1f}% < 기준 {min_dir_agree}%")
        passed_all = False

    if ret_diff > max_ret_diff:
        reasons.append(f"수익률 차이 {ret_diff:.1f}% > 기준 {max_ret_diff}%")
        passed_all = False

    base["ready"] = passed_all
    base["reasons"] = reasons

    if passed_all:
        base["message"] = (
            f"실전 전환 준비 완료: 방향성 일치율 {dir_agree:.1f}% (≥{min_dir_agree}%), "
            f"수익률 차이 {ret_diff:.1f}% (≤{max_ret_diff}%), "
            f"거래일 {trading_days}일, 매도 {total_trades}건"
        )
        logger.info("✅ {}", base["message"])
    else:
        base["message"] = (
            f"실전 전환 기준 미달: {'; '.join(reasons)}. "
            f"(방향성 {dir_agree:.1f}%, 수익률 차이 {ret_diff:.1f}%, "
            f"거래일 {trading_days}일, 매도 {total_trades}건)"
        )
        logger.info("⏳ {}", base["message"])

    return base
