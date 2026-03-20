"""
전략 검증 도구.
- strict look-ahead 기본 검증
- in-sample / out-of-sample 분리
- 코스피 벤치마크 비교

한계: 검증 통과(샤프·MDD·벤치마크)만으로는 실전 수익을 보장하지 않음. 검증 기간이 해당 전략에
유리한 시장 국면이었을 수 있고, 최적화 후 OOS가 같은 시대라 간접 과적합일 수 있음. quant_trader_design.md §8.2 참고.
단일 분할 검증: run(). 워크포워드(슬라이딩 윈도우) 검증: run_walk_forward().
"""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from backtest.backtester import Backtester
from config.config_loader import Config
from core.data_collector import DataCollector
from core.notifier import Notifier


def _get_kospi_top_n_symbols(
    collector: DataCollector,
    top_n: int = 50,
    as_of_date: str = None,
    config: Config = None,
) -> list:
    """
    벤치마크용 종목 코드 리스트 반환.
    config.backtest_universe.mode가 kospi200이면 해당 일자 코스피200 구성종목 사용(생존자 편향 완화).
    그 외에는 코스피 시가총액 상위 top_n 종목.
    """
    cfg = config or Config.get()
    universe = (cfg.risk_params or {}).get("backtest_universe") or {}
    mode = (universe.get("mode") or "current").strip().lower()
    exclude_admin = universe.get("exclude_administrative", True)
    try:
        stocks = DataCollector.get_krx_stock_list(
            as_of_date=as_of_date,
            exclude_administrative=exclude_admin,
            universe_mode=mode,
        )
    except Exception as e:
        logger.warning("벤치마크용 KRX 목록 조회 실패: {}", e)
        return []
    if stocks.empty:
        return []
    df = stocks.copy()
    code_col = next((c for c in ["Code", "Symbol", "code", "symbol"] if c in df.columns), None)
    if not code_col:
        return []
    market_col = next((c for c in ["Market", "market"] if c in df.columns), None)
    if market_col is not None:
        df = df[df[market_col].astype(str).str.upper().str.contains("KOSPI", na=False)]
    marcap_col = next((c for c in ["Marcap", "marcap", "Amount", "amount"] if c in df.columns), None)
    if marcap_col and df[marcap_col].fillna(0).astype(float).gt(0).any():
        df[marcap_col] = pd.to_numeric(df[marcap_col], errors="coerce")
        df = df.dropna(subset=[marcap_col]).sort_values(marcap_col, ascending=False)
    n = len(df) if mode == "kospi200" else min(top_n, len(df))
    symbols = [str(s).strip().zfill(6) for s in df[code_col].head(n).tolist()]
    return symbols


def _portfolio_metrics_from_equity(equity: pd.Series, initial_capital: float) -> dict:
    """일별 equity 시리즈로부터 total_return, sharpe, max_drawdown 등 계산 (_buy_and_hold_metrics와 동일 형식)."""
    if equity is None or equity.empty or len(equity) < 2:
        return {}
    equity = equity.astype(float).dropna()
    if equity.empty or equity.iloc[0] <= 0:
        return {}
    daily_returns = equity.pct_change().dropna()
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        annual_return = daily_returns.mean() * 252
        annual_std = daily_returns.std() * np.sqrt(252)
        sharpe = (annual_return - 0.03) / annual_std
    else:
        sharpe = 0.0
    peak = equity.cummax()
    drawdown = ((equity - peak) / peak) * 100
    years = len(equity) / 252 if len(equity) > 0 else 1
    total_return = ((equity.iloc[-1] / initial_capital) - 1) * 100
    annual_return_pct = total_return / years if years > 0 else 0
    return {
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(float(drawdown.min()), 2),
        "final_value": round(float(equity.iloc[-1]), 0),
        "initial_capital": initial_capital,
    }


def _build_equal_weight_panel(
    collector: DataCollector,
    symbols: list,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """동일 비중 벤치용: (date index, columns=symbol) close 패널. 공통 일자만 join='inner'."""
    if not symbols:
        return pd.DataFrame()
    closes_list = []
    for sym in symbols:
        df = collector.fetch_korean_stock(sym, start_date, end_date)
        if df.empty or "close" not in df.columns:
            continue
        close = df["close"].astype(float)
        close.name = sym
        closes_list.append(close)
    if not closes_list:
        return pd.DataFrame()
    try:
        return pd.concat(closes_list, axis=1, join="inner")
    except Exception:
        return pd.DataFrame()


def _equal_weight_buy_and_hold_metrics(
    collector: DataCollector,
    symbols: list,
    start_date: str,
    end_date: str,
    initial_capital: float,
) -> dict:
    """코스피 상위 N종목 동일 비중 매수·홀딩 수익률 등. symbols가 비면 {} 반환."""
    panel = _build_equal_weight_panel(collector, symbols, start_date, end_date)
    if panel.empty or len(panel) < 2:
        return {}
    equity = (panel / panel.iloc[0]).mean(axis=1) * initial_capital
    return _portfolio_metrics_from_equity(equity, initial_capital)


class StrategyValidator:
    """
    전략 검증 리포트를 생성한다.

    조건: 샤프 ≥ min_sharpe, MDD ≥ max_mdd, 벤치마크 대비 초과 수익, in/out-of-sample 분리.
    통과해도 실전 수익을 보장하지 않음(검증 기간 국면 편향·최적화 후 OOS 과적합 가능성). §8.2 참고.
    단일 분할: run(). 워크포워드(슬라이딩 윈도우): run_walk_forward().
    """

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
        use_benchmark_top50: bool = True,
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

        # 단일 train/test 분할 (워크포워드는 run_walk_forward() 사용)
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

        benchmark_top50 = {}
        if use_benchmark_top50:
            symbols_top50 = _get_kospi_top_n_symbols(
                self.collector, 50, as_of_date=start_date, config=self.config
            )
            if symbols_top50:
                s0, s1 = str(strategy_df.index[0].date()), str(strategy_df.index[-1].date())
                panel = _build_equal_weight_panel(self.collector, symbols_top50, s0, s1)
                if not panel.empty and len(panel) >= 2:
                    cap_full = full_result["initial_capital"]
                    equity_full = (panel / panel.iloc[0]).mean(axis=1) * cap_full
                    benchmark_top50["full"] = _portfolio_metrics_from_equity(equity_full, cap_full)
                    panel_is = panel.loc[strategy_df.index[0] : strategy_df.index[split_idx - 1]].dropna(how="any")
                    if not panel_is.empty and len(panel_is) >= 2:
                        cap_is = in_sample_result["initial_capital"]
                        equity_is = (panel_is / panel_is.iloc[0]).mean(axis=1) * cap_is
                        benchmark_top50["in_sample"] = _portfolio_metrics_from_equity(equity_is, cap_is)
                    else:
                        benchmark_top50["in_sample"] = {}
                    panel_oos = panel.loc[strategy_df.index[split_idx] : strategy_df.index[-1]].dropna(how="any")
                    if not panel_oos.empty and len(panel_oos) >= 2:
                        cap_oos = out_sample_result["initial_capital"]
                        equity_oos = (panel_oos / panel_oos.iloc[0]).mean(axis=1) * cap_oos
                        benchmark_top50["out_sample"] = _portfolio_metrics_from_equity(equity_oos, cap_oos)
                    else:
                        benchmark_top50["out_sample"] = {}
                else:
                    logger.warning("코스피 상위 50 동일비중 패널 구성 실패(데이터 부족).")
            else:
                logger.warning("코스피 상위 50 종목 리스트 조회 실패. Top50 벤치마크 생략.")

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
            "warnings": [],
        }
        if benchmark_top50 and benchmark_top50.get("out_sample"):
            validation["benchmark_top50_outperformance"] = round(
                out_sample_result["metrics"]["total_return"]
                - benchmark_top50["out_sample"].get("total_return", 0),
                2,
            )
        else:
            validation["benchmark_top50_outperformance"] = None

        # 손익비 자동 경고 (특히 추세 추종 전략은 profit_factor ≥ 2.0 필요)
        self._check_profit_factor_warnings(
            validation, strategy_name, full_result["metrics"], out_sample_result["metrics"]
        )

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
            "benchmark_top50": benchmark_top50,
            "validation": validation,
        }
        result["report_path"] = str(self.save_report(result))
        return result

    def run_walk_forward(
        self,
        symbol: str,
        strategy_name: str,
        start_date: str = None,
        end_date: str = None,
        benchmark_symbol: str = "KS11",
        validation_years: int = 6,
        train_days: int = 504,
        test_days: int = 252,
        step_days: int = 252,
        min_sharpe: float = 1.0,
        max_mdd: float = -20.0,
    ) -> dict:
        """
        워크포워드(슬라이딩 윈도우) 검증.
        train_days 기간 훈련 구간 다음 test_days 기간을 테스트로 사용하고, step_days만큼 슬라이드해 반복.
        예: train_days=504(2년), test_days=252(1년), step_days=252 → 2019~2020 훈련→2021 테스트, 2020~2021→2022 테스트, ...
        """
        if validation_years < 3:
            logger.warning("검증 연수는 최소 3년 권장. {}년 → 3년으로 적용합니다.", validation_years)
            validation_years = 3
        start_date, end_date = self._resolve_dates(start_date, end_date, validation_years)

        strategy_df = self.collector.fetch_korean_stock(symbol, start_date, end_date)
        if strategy_df.empty or len(strategy_df) < train_days + test_days:
            raise ValueError(
                f"전략 워크포워드 검증용 데이터가 부족합니다: {symbol} "
                f"(최소 {train_days + test_days}일 필요, 현재 {len(strategy_df)}일)"
            )

        benchmark_df = self.collector.fetch_korean_stock(benchmark_symbol, start_date, end_date)
        if benchmark_df.empty:
            logger.warning("벤치마크 데이터 조회 실패: {}", benchmark_symbol)

        n_max = (len(strategy_df) - train_days - test_days) // step_days + 1
        n_max = max(0, n_max)
        windows = []
        for i in range(n_max):
            train_start = i * step_days
            train_end = train_start + train_days
            test_start = train_end
            test_end = test_start + test_days
            if test_end > len(strategy_df):
                break
            test_df = strategy_df.iloc[test_start:test_end].copy()
            try:
                test_result = self.backtester.run(
                    test_df,
                    strategy_name=strategy_name,
                    strict_lookahead=True,
                )
            except Exception as e:
                logger.warning("워크포워드 창 {} 백테스트 실패: {}", i + 1, e)
                windows.append({
                    "window": i + 1,
                    "train_period": f"{strategy_df.index[train_start].date()} ~ {strategy_df.index[train_end - 1].date()}",
                    "test_period": f"{strategy_df.index[test_start].date()} ~ {strategy_df.index[test_end - 1].date()}",
                    "metrics": None,
                    "passed": False,
                    "error": str(e),
                })
                continue
            metrics = test_result["metrics"]
            passed = self._passes(metrics, min_sharpe, max_mdd)
            bench = {}
            if benchmark_df is not None and not benchmark_df.empty:
                bench_slice = benchmark_df.loc[test_df.index.min() : test_df.index.max()]
                if len(bench_slice) >= 2:
                    bench = self._buy_and_hold_metrics(bench_slice, test_result["initial_capital"])
            windows.append({
                "window": i + 1,
                "train_period": f"{strategy_df.index[train_start].date()} ~ {strategy_df.index[train_end - 1].date()}",
                "test_period": f"{strategy_df.index[test_start].date()} ~ {strategy_df.index[test_end - 1].date()}",
                "metrics": metrics,
                "benchmark": bench,
                "passed": passed,
            })
        n_passed = sum(1 for w in windows if w.get("passed", False))
        n_total = len(windows)
        n_failed = n_total - n_passed
        pass_rate = (n_passed / n_total) if n_total > 0 else 0.0
        min_pass_ratio = 0.8
        wf_passed = n_total > 0 and pass_rate >= min_pass_ratio - 1e-12
        all_passed = n_total > 0 and n_passed == n_total
        ratio_passed = wf_passed

        metrics_list = [w["metrics"] for w in windows if w.get("metrics")]
        if metrics_list:
            avg_oos_sharpe = float(np.mean([m.get("sharpe_ratio") or 0 for m in metrics_list]))
            avg_oos_mdd = float(np.mean([m.get("max_drawdown") or 0 for m in metrics_list]))
        else:
            avg_oos_sharpe = 0.0
            avg_oos_mdd = 0.0

        if wf_passed:
            print("워크포워드 검증 통과")
            logger.info("워크포워드 검증 통과")
        else:
            print(f"워크포워드 검증 실패: {n_passed}/{n_total}")
            logger.warning(
                "워크포워드 검증 실패: {}/{} — 통과율 {:.1f}% < {:.0f}%. 전략 사용을 권장하지 않습니다.",
                n_passed, n_total, pass_rate * 100, min_pass_ratio * 100,
            )

        # 워크포워드 전체 창의 손익비 경고
        wf_warnings = []
        is_trend = strategy_name and "trend" in strategy_name.lower()
        pf_threshold = 2.0 if is_trend else 1.0
        for w in windows:
            m = w.get("metrics")
            if m and m.get("profit_factor", 0) < pf_threshold:
                msg = (
                    f"WARN: 창 {w['window']} ({w['test_period']}) 손익비 "
                    f"{m['profit_factor']:.2f} < {pf_threshold:.1f}"
                )
                wf_warnings.append(msg)
                logger.warning(msg)

        result = {
            "symbol": symbol,
            "strategy": strategy_name,
            "benchmark_symbol": benchmark_symbol,
            "period": f"{strategy_df.index[0].date()} ~ {strategy_df.index[-1].date()}",
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days,
            "min_sharpe": min_sharpe,
            "max_mdd": max_mdd,
            "windows": windows,
            "n_passed": n_passed,
            "n_total": n_total,
            "n_failed": n_failed,
            "pass_rate": round(pass_rate, 4),
            "avg_oos_sharpe": round(avg_oos_sharpe, 2),
            "avg_oos_mdd": round(avg_oos_mdd, 2),
            "all_passed": all_passed,
            "ratio_passed": ratio_passed,
            "wf_passed": wf_passed,
            "min_pass_ratio": min_pass_ratio,
            "warnings": wf_warnings,
        }
        result["report_path"] = str(self._save_walk_forward_report(result))
        self._notify_walk_forward(result)
        return result

    def _notify_walk_forward(self, result: dict) -> None:
        """워크포워드 요약을 디스코드(Notifier)로 전송."""
        strat = result["strategy"]
        npass, ntot = result["n_passed"], result["n_total"]
        avg_s = result.get("avg_oos_sharpe", 0)
        avg_m = result.get("avg_oos_mdd", 0)
        verdict = "통과" if result.get("wf_passed") else "실패"
        body = (
            f"[전략 검증] {strat} 전략 워크포워드 결과: {npass}/{ntot} 통과\n"
            f"OOS 평균 샤프: {avg_s} | OOS 평균 MDD: {avg_m}%\n"
            f"판정: {verdict}"
        )
        try:
            Notifier(self.config).send(body)
        except Exception as e:
            logger.warning("워크포워드 검증 알림 전송 실패: {}", e)

    def _save_walk_forward_report(self, result: dict) -> Path:
        date_part = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_strategy = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in str(result["strategy"]))
        path = self.output_dir / f"validation_walkforward_{date_part}_{safe_strategy}.txt"
        path.write_text(self._render_walk_forward_report(result), encoding="utf-8")
        logger.info("워크포워드 검증 리포트 저장: {}", path)
        return path

    def _render_walk_forward_report(self, result: dict) -> str:
        lines = [
            "=" * 70,
            f"워크포워드 검증 리포트 | {result['strategy']} | {result['symbol']}",
            f"기간: {result['period']}",
            f"train_days={result['train_days']} test_days={result['test_days']} step_days={result['step_days']}",
            f"기준: 샤프 ≥ {result['min_sharpe']}, MDD ≤ {abs(result['max_mdd']):.0f}% (지표값 max_drawdown ≥ {result['max_mdd']})",
            f"창별 통과: {result['n_passed']}/{result['n_total']} | 기준 미달 창: {result.get('n_failed', 0)}",
            f"통과율: {result.get('pass_rate', 0) * 100:.1f}% | 80% 이상 워크포워드 통과: {result.get('wf_passed', False)}",
            f"OOS 평균 샤프: {result.get('avg_oos_sharpe', 0)} | OOS 평균 MDD: {result.get('avg_oos_mdd', 0)}%",
            f"전체 창 통과: {result['all_passed']}",
            "=" * 70,
            "",
            f"{'창':>4} | {'테스트 구간':^21} | {'샤프':>7} | {'MDD%':>8} | {'거래건수':>8} | {'손익비':>8} | {'판정':^6}",
            "-" * 86,
        ]
        for w in result["windows"]:
            status = "PASS" if w.get("passed") else "FAIL"
            m = w.get("metrics")
            if m:
                tp = w.get("test_period", "")[:21]
                lines.append(
                    f"{w['window']:>4} | {tp:^21} | {m.get('sharpe_ratio', 0):>7.2f} | "
                    f"{m.get('max_drawdown', 0):>8.2f} | {m.get('total_trades', 0):>8} | "
                    f"{m.get('profit_factor', 0):>8.2f} | {status:^6}"
                )
            else:
                err = (w.get("error") or "N/A")[:40]
                lines.append(
                    f"{w['window']:>4} | {w.get('test_period', '')[:21]:^21} | {'N/A':>7} | {'N/A':>8} | {'N/A':>8} | "
                    f"{'N/A':>8} | {status:^6}  # {err}"
                )
        lines.append("")
        for w in result["windows"]:
            if w.get("error"):
                lines.append(f"[창 {w['window']}] 오류: {w['error']}")
        if result.get("warnings"):
            lines.append("")
            lines.append("⚠️ 경고:")
            for msg in result["warnings"]:
                lines.append(f"  {msg}")
        return "\n".join(lines) + "\n"

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
            f"out-of-sample 벤치마크({result['benchmark_symbol']}) 초과수익: {validation['benchmark_outperformance']:.2f}%",
        ]
        top50 = result.get("benchmark_top50") or {}
        if top50:
            lines.extend([
                "",
                "벤치마크(코스피 상위 50종목 동일비중)",
                self._format_section("FULL", result["full"]["metrics"], top50.get("full", {})),
                self._format_section("IN_SAMPLE", result["in_sample"]["metrics"], top50.get("in_sample", {})),
                self._format_section("OUT_OF_SAMPLE", result["out_sample"]["metrics"], top50.get("out_sample", {})),
                "-" * 70,
                f"out-of-sample Top50 벤치마크 초과수익: "
                f"{validation['benchmark_top50_outperformance']:.2f}%"
                if validation.get("benchmark_top50_outperformance") is not None
                else "out-of-sample Top50 벤치마크 초과수익: N/A",
            ])
        else:
            lines.append("벤치마크(코스피 상위 50종목): 미사용 또는 데이터 없음")
        lines.extend([
            "-" * 70,
            f"손익비(Profit Factor): FULL {validation.get('full_profit_factor', 0):.2f} | OOS {validation.get('oos_profit_factor', 0):.2f}",
            f"샤프 기준({validation['min_sharpe']:.2f}) 충족: {validation['full_passed']}",
            f"Out-of-sample 기준 통과: {validation['out_sample_passed']}",
        ])
        if validation.get("warnings"):
            lines.append("")
            lines.append("⚠️ 경고:")
            for w in validation["warnings"]:
                lines.append(f"  {w}")
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

    @staticmethod
    def _check_profit_factor_warnings(
        validation: dict,
        strategy_name: str,
        full_metrics: dict,
        oos_metrics: dict,
        min_profit_factor: float = 2.0,
    ):
        """
        손익비(profit_factor) 자동 경고.
        추세 추종은 승률이 낮고 손익비로 수익을 내는 구조이므로 ≥ 2.0 필수.
        다른 전략도 1.0 미만이면 순손실이므로 경고.
        """
        warnings = validation.setdefault("warnings", [])
        is_trend = strategy_name and "trend" in strategy_name.lower()
        threshold = min_profit_factor if is_trend else 1.0

        full_pf = full_metrics.get("profit_factor", 0)
        oos_pf = oos_metrics.get("profit_factor", 0)

        if is_trend:
            if full_pf < min_profit_factor:
                msg = (
                    f"WARN: 추세 추종 전략 FULL 기간 손익비 {full_pf:.2f} < {min_profit_factor:.1f} — "
                    f"승률이 낮은 추세 추종은 손익비 ≥ {min_profit_factor:.1f} 필요"
                )
                warnings.append(msg)
                logger.warning(msg)
            if oos_pf < min_profit_factor:
                msg = (
                    f"WARN: 추세 추종 전략 OOS 기간 손익비 {oos_pf:.2f} < {min_profit_factor:.1f} — "
                    f"실전 적용 전 검토 필요"
                )
                warnings.append(msg)
                logger.warning(msg)
        else:
            if full_pf < threshold:
                msg = f"WARN: FULL 기간 손익비 {full_pf:.2f} < {threshold:.1f} — 순손실 구조"
                warnings.append(msg)
                logger.warning(msg)
            if oos_pf < threshold:
                msg = f"WARN: OOS 기간 손익비 {oos_pf:.2f} < {threshold:.1f} — 순손실 구조"
                warnings.append(msg)
                logger.warning(msg)

        validation["full_profit_factor"] = full_pf
        validation["oos_profit_factor"] = oos_pf

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
