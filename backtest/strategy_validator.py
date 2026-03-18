"""
전략 검증 도구.
- strict look-ahead 기본 검증
- in-sample / out-of-sample 분리
- 코스피 벤치마크 비교
"""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from backtest.backtester import Backtester
from config.config_loader import Config
from core.data_collector import DataCollector


class StrategyValidator:
    """전략 검증 리포트를 생성한다."""

    def __init__(self, config: Config = None, output_dir: str = "reports"):
        self.config = config or Config.get()
        self.collector = DataCollector()
        self.backtester = Backtester(self.config)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        symbol: str,
        strategy_name: str,
        start_date: str = None,
        end_date: str = None,
        benchmark_symbol: str = "KS11",
        validation_years: int = 5,
        split_ratio: float = 0.7,
        min_sharpe: float = 1.0,
        max_mdd: float = -20.0,
    ) -> dict:
        # 오버피팅 방지·통계적 신뢰를 위해 최소 3년 데이터 사용
        if validation_years < 3:
            logger.warning("검증 연수는 최소 3년 권장. {}년 → 3년으로 적용합니다.", validation_years)
            validation_years = 3
        start_date, end_date = self._resolve_dates(start_date, end_date, validation_years)

        strategy_df = self.collector.fetch_korean_stock(symbol, start_date, end_date)
        if strategy_df.empty or len(strategy_df) < 120:
            raise ValueError(f"전략 검증용 데이터가 부족합니다: {symbol}")

        benchmark_df = self.collector.fetch_korean_stock(benchmark_symbol, start_date, end_date)
        if benchmark_df.empty:
            logger.warning("벤치마크 데이터 조회 실패: {}", benchmark_symbol)

        split_idx = max(60, int(len(strategy_df) * split_ratio))
        split_idx = min(split_idx, len(strategy_df) - 30)

        full_result = self.backtester.run(
            strategy_df.copy(),
            strategy_name=strategy_name,
            strict_lookahead=True,
        )
        relaxed_result = self.backtester.run(
            strategy_df.copy(),
            strategy_name=strategy_name,
            strict_lookahead=False,
        )
        in_sample_result = self.backtester.run(
            strategy_df.iloc[:split_idx].copy(),
            strategy_name=strategy_name,
            strict_lookahead=True,
        )
        out_sample_result = self.backtester.run(
            strategy_df.iloc[split_idx:].copy(),
            strategy_name=strategy_name,
            strict_lookahead=True,
        )

        benchmark = {
            "full": self._buy_and_hold_metrics(benchmark_df.copy(), full_result["initial_capital"]),
            "in_sample": self._buy_and_hold_metrics(
                benchmark_df.loc[in_sample_result["equity_curve"]["date"].min():in_sample_result["equity_curve"]["date"].max()].copy(),
                in_sample_result["initial_capital"],
            ) if benchmark_df is not None and not benchmark_df.empty else {},
            "out_sample": self._buy_and_hold_metrics(
                benchmark_df.loc[out_sample_result["equity_curve"]["date"].min():out_sample_result["equity_curve"]["date"].max()].copy(),
                out_sample_result["initial_capital"],
            ) if benchmark_df is not None and not benchmark_df.empty else {},
        }

        validation = {
            "min_sharpe": min_sharpe,
            "max_mdd": max_mdd,
            "full_passed": self._passes(full_result["metrics"], min_sharpe, max_mdd),
            "out_sample_passed": self._passes(out_sample_result["metrics"], min_sharpe, max_mdd),
            "lookahead_return_gap": round(
                relaxed_result["metrics"]["total_return"] - full_result["metrics"]["total_return"],
                2,
            ),
            "benchmark_outperformance": round(
                out_sample_result["metrics"]["total_return"] - benchmark.get("out_sample", {}).get("total_return", 0),
                2,
            ),
        }

        result = {
            "symbol": symbol,
            "strategy": strategy_name,
            "benchmark_symbol": benchmark_symbol,
            "period": f"{strategy_df.index[0].date()} ~ {strategy_df.index[-1].date()}",
            "full": full_result,
            "relaxed": relaxed_result,
            "in_sample": in_sample_result,
            "out_sample": out_sample_result,
            "benchmark": benchmark,
            "validation": validation,
        }
        result["report_path"] = str(self.save_report(result))
        return result

    def save_report(self, result: dict) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"validation_{result['strategy']}_{result['symbol']}_{timestamp}.txt"
        path.write_text(self.render_text_report(result), encoding="utf-8")
        logger.info("전략 검증 리포트 저장: {}", path)
        return path

    def render_text_report(self, result: dict) -> str:
        validation = result["validation"]
        lines = [
            "=" * 70,
            f"전략 검증 리포트 | {result['strategy']} | {result['symbol']}",
            f"기간: {result['period']}",
            f"벤치마크: {result['benchmark_symbol']}",
            "=" * 70,
            self._format_section("FULL", result["full"]["metrics"], result["benchmark"].get("full", {})),
            self._format_section("IN_SAMPLE", result["in_sample"]["metrics"], result["benchmark"].get("in_sample", {})),
            self._format_section("OUT_OF_SAMPLE", result["out_sample"]["metrics"], result["benchmark"].get("out_sample", {})),
            "-" * 70,
            f"look-ahead 완화 모드 수익률 차이: {validation['lookahead_return_gap']:.2f}%",
            f"out-of-sample 벤치마크 초과수익: {validation['benchmark_outperformance']:.2f}%",
            f"샤프 기준({validation['min_sharpe']:.2f}) 충족: {validation['full_passed']}",
            f"Out-of-sample 기준 통과: {validation['out_sample_passed']}",
        ]
        return "\n".join(lines) + "\n"

    def print_report(self, result: dict) -> None:
        print(self.render_text_report(result))

    @staticmethod
    def _resolve_dates(start_date: str, end_date: str, validation_years: int) -> tuple[str, str]:
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * validation_years)).strftime("%Y-%m-%d")
        return start_date, end_date

    @staticmethod
    def _passes(metrics: dict, min_sharpe: float, max_mdd: float) -> bool:
        return (
            metrics.get("sharpe_ratio", 0) >= min_sharpe
            and metrics.get("max_drawdown", 0) >= max_mdd
        )

    def _buy_and_hold_metrics(self, df: pd.DataFrame, initial_capital: float) -> dict:
        if df is None or df.empty or len(df) < 2:
            return {}

        closes = df["close"].astype(float).dropna()
        if closes.empty:
            return {}

        shares = initial_capital / closes.iloc[0]
        equity = shares * closes
        daily_returns = equity.pct_change().dropna()

        if len(daily_returns) > 0 and daily_returns.std() > 0:
            annual_return = daily_returns.mean() * 252
            annual_std = daily_returns.std() * np.sqrt(252)
            sharpe = (annual_return - 0.03) / annual_std
        else:
            sharpe = 0

        peak = equity.cummax()
        drawdown = ((equity - peak) / peak) * 100
        years = len(equity) / 252 if len(equity) > 0 else 1
        total_return = ((equity.iloc[-1] / initial_capital) - 1) * 100
        annual_return_pct = total_return / years if years > 0 else 0

        return {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(drawdown.min(), 2),
            "final_value": round(equity.iloc[-1], 0),
            "initial_capital": initial_capital,
        }

    @staticmethod
    def _format_section(title: str, metrics: dict, benchmark: dict) -> str:
        benchmark_return = benchmark.get("total_return", 0)
        benchmark_sharpe = benchmark.get("sharpe_ratio", 0)
        benchmark_mdd = benchmark.get("max_drawdown", 0)
        return "\n".join([
            f"[{title}]",
            f"전략 수익률 {metrics.get('total_return', 0):>8.2f}% | 샤프 {metrics.get('sharpe_ratio', 0):>5.2f} | MDD {metrics.get('max_drawdown', 0):>6.2f}%",
            f"벤치 수익률 {benchmark_return:>8.2f}% | 샤프 {benchmark_sharpe:>5.2f} | MDD {benchmark_mdd:>6.2f}%",
        ])
