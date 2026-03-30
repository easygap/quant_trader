"""
멀티종목 포트폴리오 백테스터
- 여러 종목에 대해 동시에 매매 신호를 평가하고 포트폴리오 수준에서 자금을 관리
- 분산 투자 제한, 업종 비중, 최대 포지션 수 등 실전 리스크 관리 반영
- 단일 종목 백테스트와 달리 "포트폴리오 MDD", "종목 간 상관관계 영향" 등을 측정
"""

import pandas as pd
import numpy as np
from loguru import logger
from datetime import timedelta

from config.config_loader import Config
from core.risk_manager import RiskManager


class PortfolioBacktester:
    """
    멀티종목 포트폴리오 백테스터

    사용법:
        pbt = PortfolioBacktester()
        result = pbt.run(symbols=["005930", "000660", "035720"], strategy_name="scoring")
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.risk_params = self.config.risk_params
        self.risk_manager = RiskManager(self.config)
        logger.info("PortfolioBacktester 초기화 완료")

    def run(
        self,
        symbols: list[str],
        strategy_name: str = "scoring",
        initial_capital: float = None,
        start_date: str = None,
        end_date: str = None,
    ) -> dict:
        """
        멀티종목 포트폴리오 백테스트 실행.

        Args:
            symbols: 종목 코드 리스트
            strategy_name: 전략명
            initial_capital: 초기 투자금
            start_date: 시작일
            end_date: 종료일

        Returns:
            포트폴리오 수준 백테스트 결과
        """
        from core.data_collector import DataCollector
        from strategies import create_strategy

        if initial_capital is None:
            initial_capital = self.risk_params.get(
                "position_sizing", {}
            ).get("initial_capital", 10000000)

        collector = DataCollector()
        strategy = create_strategy(strategy_name, self.config)

        all_data = {}
        all_signals = {}

        logger.info("포트폴리오 백테스트: {}개 종목 데이터 수집 중...", len(symbols))
        for symbol in symbols:
            try:
                df = collector.fetch_stock(symbol, start_date, end_date)
                if df is None or df.empty or len(df) < 60:
                    logger.warning("종목 {} 데이터 부족 — 스킵", symbol)
                    continue

                analyzed = strategy.analyze(df.copy())
                if analyzed.empty or "signal" not in analyzed.columns:
                    continue

                all_data[symbol] = df
                all_signals[symbol] = analyzed
            except Exception as e:
                logger.warning("종목 {} 처리 실패: {} — 스킵", symbol, e)

        if not all_signals:
            logger.error("유효한 종목이 없습니다.")
            return {}

        valid_symbols = list(all_signals.keys())
        logger.info("유효 종목 {}개: {}", len(valid_symbols), valid_symbols)

        all_dates = sorted(set().union(*(s.index for s in all_signals.values())))

        # 시장국면 시리즈 사전 계산 (TICKET-05, backtester.py TICKET-02와 동일 로직 재사용)
        regime_series = self._precompute_regime_series(all_dates)

        result = self._simulate_portfolio(
            all_signals, all_data, all_dates, initial_capital, valid_symbols,
            regime_series=regime_series,
        )

        metrics = self._calculate_portfolio_metrics(result, initial_capital)

        logger.info(
            "포트폴리오 백테스트 완료 | 종목={}개 | 수익률: {:.2f}% | 샤프: {:.2f} | MDD: {:.2f}%",
            len(valid_symbols), metrics["total_return"],
            metrics["sharpe_ratio"], metrics["max_drawdown"],
        )

        return {
            "metrics": metrics,
            "trades": result["trades"],
            "equity_curve": result["equity_curve"],
            "strategy": strategy_name,
            "symbols": valid_symbols,
            "initial_capital": initial_capital,
            "per_symbol_stats": result.get("per_symbol_stats", {}),
        }

    def _precompute_regime_series(self, all_dates: list) -> pd.Series:
        """
        포트폴리오 백테스트 기간의 시장국면을 사전 계산.

        Look-ahead bias 방지:
          시점 T의 regime은 T-1일까지의 지수 종가만으로 계산.
          backtester.py TICKET-02와 동일 로직.
        """
        import pandas as _pd
        regime_cfg = self.risk_params.get("backtest_regime_filter", {})
        idx = _pd.DatetimeIndex(all_dates) if all_dates else _pd.DatetimeIndex([])
        default = _pd.Series("bullish", index=idx)

        if not regime_cfg.get("enabled", False):
            return default

        index_symbol = regime_cfg.get("index_symbol", "KS11")
        ma_days = max(20, int(regime_cfg.get("ma_days", 200)))
        short_days = max(1, int(regime_cfg.get("short_momentum_days", 20)))
        short_threshold = float(regime_cfg.get("short_momentum_threshold", -5.0))

        try:
            from core.data_collector import DataCollector
            collector = DataCollector()
            margin_days = ma_days + 60
            first_date = all_dates[0] if all_dates else None
            if first_date and hasattr(first_date, "strftime"):
                start_str = (first_date - timedelta(days=margin_days + 30)).strftime("%Y-%m-%d")
            else:
                start_str = None
            last_date = all_dates[-1] if all_dates else None
            end_str = last_date.strftime("%Y-%m-%d") if last_date and hasattr(last_date, "strftime") else None

            index_df = collector.fetch_korean_stock(index_symbol, start_date=start_str, end_date=end_str)
            if index_df is None or index_df.empty or len(index_df) < ma_days:
                logger.warning("포트폴리오 regime: 지수 {} 데이터 부족 — 국면 필터 비활성화", index_symbol)
                return default
        except Exception as e:
            logger.warning("포트폴리오 regime: 지수 데이터 로드 실패 — 국면 필터 비활성화: {}", e)
            return default

        idx_close = index_df["close"].astype(float)
        idx_ma = idx_close.rolling(ma_days, min_periods=ma_days).mean()
        idx_momentum = idx_close.pct_change(short_days) * 100

        regime_map = {}
        idx_dates = index_df.index.tolist()
        for i in range(1, len(idx_dates)):
            prev_idx = i - 1
            prev_close = float(idx_close.iloc[prev_idx])
            prev_ma = idx_ma.iloc[prev_idx]
            prev_momentum = idx_momentum.iloc[prev_idx]

            if _pd.isna(prev_ma) or _pd.isna(prev_momentum):
                regime_map[idx_dates[i]] = "bullish"
                continue

            below_ma = prev_close < float(prev_ma)
            momentum_triggered = float(prev_momentum) <= short_threshold
            triggered = sum([below_ma, momentum_triggered])

            if triggered >= 2:
                regime_map[idx_dates[i]] = "bearish"
            elif triggered == 1:
                regime_map[idx_dates[i]] = "caution"
            else:
                regime_map[idx_dates[i]] = "bullish"

        regimes = []
        for date in all_dates:
            if date in regime_map:
                regimes.append(regime_map[date])
            else:
                matched = "bullish"
                for idx_date in sorted(regime_map.keys()):
                    if idx_date <= date:
                        matched = regime_map[idx_date]
                    else:
                        break
                regimes.append(matched)

        result = _pd.Series(regimes, index=idx)
        n_bearish = (result == "bearish").sum()
        n_caution = (result == "caution").sum()
        n_bullish = (result == "bullish").sum()
        logger.info(
            "포트폴리오 regime 사전계산 완료: bullish={}일, caution={}일, bearish={}일",
            n_bullish, n_caution, n_bearish,
        )
        return result

    def _simulate_portfolio(
        self,
        signals: dict[str, pd.DataFrame],
        data: dict[str, pd.DataFrame],
        all_dates: list,
        initial_capital: float,
        symbols: list[str],
        regime_series: pd.Series = None,
    ) -> dict:
        cash = initial_capital
        positions = {}  # symbol -> {qty, avg_price, buy_date, high_water_mark}
        trades = []
        equity_curve = []

        div_cfg = self.risk_params.get("diversification", {})
        max_positions = div_cfg.get("max_positions", 10)
        max_position_ratio = div_cfg.get("max_position_ratio", 0.20)
        max_investment_ratio = div_cfg.get("max_investment_ratio", 0.70)
        min_cash_ratio = div_cfg.get("min_cash_ratio", 0.20)

        sl_config = self.risk_params.get("stop_loss", {})
        sl_rate = sl_config.get("fixed_rate", 0.03)
        tp_config = self.risk_params.get("take_profit", {})
        tp_rate = tp_config.get("fixed_rate", 0.08)
        ts_config = self.risk_params.get("trailing_stop", {})
        ts_enabled = ts_config.get("enabled", False)
        ts_rate = ts_config.get("fixed_rate", 0.05)

        pos_limits = self.risk_params.get("position_limits", {}) or {}
        max_holding_days = pos_limits.get("max_holding_days", 0)

        per_symbol_pnl = {s: 0 for s in symbols}

        # 시장국면 필터 설정 (TICKET-05)
        regime_cfg = self.risk_params.get("backtest_regime_filter", {})
        regime_enabled = regime_cfg.get("enabled", False) and regime_series is not None
        caution_scale = float(regime_cfg.get("caution_scale", 0.5))
        regime_buy_blocks = 0
        regime_caution_buys = 0

        for date in all_dates:
            total_pos_value = sum(
                self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                for s in positions
            )
            total_equity = cash + total_pos_value

            to_sell = []
            for sym in list(positions.keys()):
                sig_df = signals.get(sym)
                if sig_df is None or date not in sig_df.index:
                    continue
                row = sig_df.loc[date]
                close = float(row.get("close", positions[sym]["avg_price"]))
                pos = positions[sym]
                pos["high_water_mark"] = max(pos["high_water_mark"], close)

                sell_reason = None
                if max_holding_days > 0 and hasattr(date, "date"):
                    hd = (date - pos["buy_date"]).days if pos["buy_date"] is not None else 0
                    if hd >= max_holding_days:
                        sell_reason = "MAX_HOLD"
                if not sell_reason and close <= pos["avg_price"] * (1 - sl_rate):
                    sell_reason = "STOP_LOSS"
                if not sell_reason and close >= pos["avg_price"] * (1 + tp_rate):
                    sell_reason = "TAKE_PROFIT"
                if not sell_reason and ts_enabled and close <= pos["high_water_mark"] * (1 - ts_rate):
                    sell_reason = "TRAILING_STOP"
                if not sell_reason and row.get("signal") == "SELL":
                    sell_reason = "SELL"

                if sell_reason:
                    to_sell.append((sym, close, sell_reason))

            for sym, close, reason in to_sell:
                pos = positions.pop(sym)
                costs = self.risk_manager.calculate_transaction_costs(
                    close, pos["qty"], "SELL", avg_price=pos["avg_price"],
                )
                sell_price = costs["execution_price"]
                pnl = (sell_price - pos["avg_price"]) * pos["qty"] - costs["commission"] - costs["tax"]
                cash += sell_price * pos["qty"] - costs["commission"] - costs["tax"]
                per_symbol_pnl[sym] = per_symbol_pnl.get(sym, 0) + pnl
                trades.append({
                    "date": date, "symbol": sym, "action": reason,
                    "price": sell_price, "quantity": pos["qty"],
                    "pnl": pnl, "pnl_rate": ((sell_price / pos["avg_price"]) - 1) * 100,
                })

            # 시장국면 판별 (TICKET-05): T-1일 지수 기준, look-ahead bias 없음
            regime_at_t = "bullish"
            if regime_enabled and date in regime_series.index:
                regime_at_t = regime_series.loc[date]

            buy_candidates = []
            if regime_at_t != "bearish":
                for sym in symbols:
                    if sym in positions:
                        continue
                    sig_df = signals.get(sym)
                    if sig_df is None or date not in sig_df.index:
                        continue
                    row = sig_df.loc[date]
                    if row.get("signal") == "BUY":
                        score = float(row.get("total_score", row.get("strategy_score", 0)))
                        buy_candidates.append((sym, float(row.get("close", 0)), score))
            else:
                # bearish: 모든 BUY 신호 차단. 차단 수 집계 (종목 무관하게 날짜 1회)
                n_blocked = sum(
                    1 for sym in symbols
                    if sym not in positions
                    and signals.get(sym) is not None
                    and date in signals[sym].index
                    and signals[sym].loc[date].get("signal") == "BUY"
                )
                regime_buy_blocks += n_blocked

            buy_candidates.sort(key=lambda x: -x[2])

            for sym, close, score in buy_candidates:
                if len(positions) >= max_positions:
                    break
                total_equity_now = cash + sum(
                    self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                    for s in positions
                )
                if total_equity_now <= 0:
                    break

                invested_now = sum(
                    self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                    for s in positions
                )
                if total_equity_now > 0 and invested_now / total_equity_now >= max_investment_ratio:
                    break
                if total_equity_now > 0 and cash / total_equity_now < min_cash_ratio:
                    break

                max_invest = total_equity_now * max_position_ratio
                stop_loss = close * (1 - sl_rate)
                risk_per_share = max(close - stop_loss, close * 0.001)
                risk_amount = total_equity_now * self.risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)
                qty = min(
                    int(risk_amount / risk_per_share),
                    int(max_invest / close) if close > 0 else 0,
                )

                scale = self.risk_manager._signal_scale(score)
                qty = int(qty * scale)

                # 시장국면 caution 시 포지션 축소 (TICKET-05)
                if qty > 0 and regime_at_t == "caution":
                    qty = max(1, int(qty * caution_scale))
                    regime_caution_buys += 1

                if qty <= 0 or close * qty > cash * 0.95:
                    continue

                costs = self.risk_manager.calculate_transaction_costs(close, qty, "BUY")
                buy_price = costs["execution_price"]
                total_cost = buy_price * qty + costs["commission"]
                if total_cost > cash:
                    continue

                cash -= total_cost
                positions[sym] = {
                    "qty": qty, "avg_price": buy_price,
                    "buy_date": date, "high_water_mark": buy_price,
                }
                trades.append({
                    "date": date, "symbol": sym, "action": "BUY",
                    "price": buy_price, "quantity": qty, "pnl": 0, "pnl_rate": 0,
                })

            portfolio_value = cash + sum(
                self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                for s in positions
            )
            equity_curve.append({
                "date": date,
                "value": portfolio_value,
                "cash": cash,
                "n_positions": len(positions),
            })

        per_symbol_stats = {}
        for sym in symbols:
            sym_trades = [t for t in trades if t["symbol"] == sym and t["action"] != "BUY"]
            wins = sum(1 for t in sym_trades if t["pnl"] > 0)
            per_symbol_stats[sym] = {
                "total_pnl": round(per_symbol_pnl.get(sym, 0), 0),
                "trades": len(sym_trades),
                "win_rate": round(wins / len(sym_trades) * 100, 1) if sym_trades else 0,
            }

        return {
            "trades": trades,
            "equity_curve": pd.DataFrame(equity_curve),
            "per_symbol_stats": per_symbol_stats,
            "regime_buy_blocks": regime_buy_blocks,
            "regime_caution_buys": regime_caution_buys,
        }

    @staticmethod
    def _get_close(signals, symbol, date, fallback):
        sig_df = signals.get(symbol)
        if sig_df is not None and date in sig_df.index:
            return float(sig_df.loc[date].get("close", fallback))
        return fallback

    def _calculate_portfolio_metrics(self, result: dict, initial_capital: float) -> dict:
        equity = result["equity_curve"]
        trades = result["trades"]

        if equity.empty:
            return {"total_return": 0, "sharpe_ratio": 0, "max_drawdown": 0}

        final_value = equity["value"].iloc[-1]
        total_return = ((final_value / initial_capital) - 1) * 100

        equity["daily_return"] = equity["value"].pct_change()
        daily_returns = equity["daily_return"].dropna()

        if len(daily_returns) > 0 and daily_returns.std() > 0:
            annual_return = daily_returns.mean() * 252
            annual_std = daily_returns.std() * np.sqrt(252)
            sharpe = (annual_return - 0.03) / annual_std
        else:
            sharpe = 0

        downside = daily_returns[daily_returns < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = (daily_returns.mean() * 252 - 0.03) / (downside.std() * np.sqrt(252))
        else:
            sortino = sharpe

        equity["peak"] = equity["value"].cummax()
        equity["drawdown"] = (equity["value"] - equity["peak"]) / equity["peak"]
        max_drawdown = equity["drawdown"].min() * 100

        sell_trades = [t for t in trades if t["action"] != "BUY"]
        winning = [t for t in sell_trades if t["pnl"] > 0]
        losing = [t for t in sell_trades if t["pnl"] <= 0]
        win_rate = (len(winning) / len(sell_trades) * 100) if sell_trades else 0

        gross_profit = sum(t["pnl"] for t in winning)
        gross_loss = abs(sum(t["pnl"] for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        years = len(equity) / 252 if len(equity) > 0 else 1
        annual_return_pct = total_return / years
        calmar = abs(annual_return_pct / max_drawdown) if max_drawdown != 0 else 0

        if len(daily_returns) >= 20:
            var_95 = float(np.percentile(daily_returns, 5))
            cvar_95 = float(daily_returns[daily_returns <= var_95].mean()) if (daily_returns <= var_95).any() else var_95
        else:
            var_95 = 0
            cvar_95 = 0

        unique_symbols_traded = len(set(t["symbol"] for t in trades))
        avg_positions = equity["n_positions"].mean() if "n_positions" in equity.columns else 0

        return {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "max_drawdown": round(max_drawdown, 2),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "calmar_ratio": round(calmar, 2),
            "var_95_daily": round(var_95 * 100, 3),
            "cvar_95_daily": round(cvar_95 * 100, 3),
            "total_trades": len(sell_trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "final_value": round(final_value, 0),
            "initial_capital": initial_capital,
            "symbols_traded": unique_symbols_traded,
            "avg_positions": round(avg_positions, 1),
            "regime_buy_blocks": result.get("regime_buy_blocks", 0),
            "regime_caution_buys": result.get("regime_caution_buys", 0),
        }

    def print_report(self, result: dict):
        if not result:
            print("결과 없음")
            return

        m = result["metrics"]
        print("\n" + "=" * 60)
        print(f"  포트폴리오 백테스트 결과 ({result['strategy']})")
        print(f"  종목: {result['symbols']}")
        print("=" * 60)
        print(f"  초기 자본      : {m['initial_capital']:>14,.0f}원")
        print(f"  최종 자본      : {m['final_value']:>14,.0f}원")
        print(f"  총 수익률      : {m['total_return']:>13.2f}%")
        print(f"  연간 수익률    : {m['annual_return']:>13.2f}%")
        print("-" * 60)
        print(f"  샤프 지수      : {m['sharpe_ratio']:>13.2f}")
        print(f"  소르티노 비율  : {m.get('sortino_ratio', 0):>13.2f}")
        print(f"  최대 낙폭      : {m['max_drawdown']:>13.2f}%")
        print(f"  칼마 비율      : {m.get('calmar_ratio', 0):>13.2f}")
        print(f"  VaR 95%(일)    : {m.get('var_95_daily', 0):>13.3f}%")
        print(f"  CVaR 95%(일)   : {m.get('cvar_95_daily', 0):>13.3f}%")
        print("-" * 60)
        print(f"  총 매매 횟수   : {m['total_trades']:>13d}회")
        print(f"  승률           : {m['win_rate']:>13.1f}%")
        print(f"  손익비         : {m['profit_factor']:>13.2f}")
        print(f"  거래 종목 수   : {m.get('symbols_traded', 0):>13d}개")
        print(f"  평균 보유 종목 : {m.get('avg_positions', 0):>13.1f}개")
        print("-" * 60)

        if result.get("per_symbol_stats"):
            print("  [종목별 성과]")
            for sym, stats in sorted(
                result["per_symbol_stats"].items(),
                key=lambda x: -x[1]["total_pnl"],
            ):
                print(
                    f"    {sym}: PnL={stats['total_pnl']:>12,.0f}원 | "
                    f"거래={stats['trades']}회 | 승률={stats['win_rate']:.1f}%"
                )
        print("=" * 60 + "\n")
