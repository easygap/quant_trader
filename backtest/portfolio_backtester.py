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
        trade_start_date: str = None,
        param_overrides: dict = None,
    ) -> dict:
        """
        멀티종목 포트폴리오 백테스트 실행.

        Args:
            symbols: 종목 코드 리스트
            strategy_name: 전략명
            initial_capital: 초기 투자금
            start_date: 시작일
            end_date: 종료일
            trade_start_date: 데이터 warmup과 별도로 실제 거래/평가를 시작할 날짜
            param_overrides: 전략 파라미터 덮어쓰기. 예:
                {"relative_strength_rotation": {"short_lookback": 40}}

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
        strategy_config = self._strategy_config_for_run(strategy_name, param_overrides)
        strategy = create_strategy(strategy_name, strategy_config)

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
        all_dates = self._filter_trade_dates(all_dates, trade_start_date)
        if not all_dates:
            logger.error("거래 시작일 이후 유효한 날짜가 없습니다.")
            return {}

        # 시장국면 시리즈 사전 계산 (TICKET-05, backtester.py TICKET-02와 동일 로직 재사용)
        regime_series = self._precompute_regime_series(all_dates)

        # 전략별 exit 파라미터 읽기
        strat_cfg = strategy_config.strategies.get(strategy_name, {})
        min_hold_days = int(strat_cfg.get("min_hold_days", 0))
        disable_trailing_stop = bool(strat_cfg.get("disable_trailing_stop", False))
        tp_override = strat_cfg.get("take_profit_rate")  # 전략별 TP 오버라이드

        result = self._simulate_portfolio(
            all_signals, all_data, all_dates, initial_capital, valid_symbols,
            regime_series=regime_series,
            min_hold_days=min_hold_days,
            disable_trailing_stop=disable_trailing_stop,
            tp_rate_override=float(tp_override) if tp_override is not None else None,
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
            # 진단 계측 전달
            "exit_reason_counts": result.get("exit_reason_counts", {}),
            "blocked_buy_examples": result.get("blocked_buy_examples", []),
            "scaled_buy_examples": result.get("scaled_buy_examples", []),
            "avg_bullish_notional": result.get("avg_bullish_notional", 0),
            "avg_caution_notional": result.get("avg_caution_notional", 0),
            "regime_buy_blocks": result.get("regime_buy_blocks", 0),
            "regime_caution_buys": result.get("regime_caution_buys", 0),
            # 신호/체결/스킵 계측
            "signal_buy_count": result.get("signal_buy_count", 0),
            "signal_sell_count": result.get("signal_sell_count", 0),
            "executed_buy_count": result.get("executed_buy_count", 0),
            "executed_sell_count": result.get("executed_sell_count", 0),
            "skipped_reasons": result.get("skipped_reasons", {}),
        }

    def _strategy_config_for_run(self, strategy_name: str, param_overrides: dict | None = None):
        """param_overrides가 있으면 해당 전략만 덮어쓴 Config를 반환."""
        overrides = param_overrides or {}
        if strategy_name in overrides:
            return self.config.with_strategy_overrides(strategy_name, overrides[strategy_name])
        return self.config

    @staticmethod
    def _filter_trade_dates(all_dates: list, trade_start_date: str | None = None) -> list:
        """warmup 데이터는 유지하되 시뮬레이션 날짜만 거래 시작일 이후로 제한."""
        if not trade_start_date:
            return all_dates
        cutoff = pd.Timestamp(trade_start_date)
        return [date for date in all_dates if pd.Timestamp(date) >= cutoff]

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
        min_hold_days: int = 0,
        disable_trailing_stop: bool = False,
        tp_rate_override: float = None,
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
        sl_type = sl_config.get("type", "fixed")
        atr_mult = sl_config.get("atr_multiplier", 2.0)
        tp_config = self.risk_params.get("take_profit", {})
        tp_rate = tp_rate_override if tp_rate_override is not None else tp_config.get("fixed_rate", 0.08)
        ts_config = self.risk_params.get("trailing_stop", {})
        ts_enabled = ts_config.get("enabled", False)
        ts_type = ts_config.get("type", "fixed")
        ts_fixed_rate = ts_config.get("fixed_rate", 0.05)
        ts_atr_mult = ts_config.get("atr_multiplier", 3.0)

        def _get_atr(sig_df, date):
            """signal DataFrame에서 해당 날짜의 ATR 값 추출. 없으면 None."""
            if sig_df is None or date not in sig_df.index:
                return None
            v = sig_df.loc[date].get("atr")
            if v is not None and pd.notna(v) and v > 0:
                return float(v)
            return None

        def _stop_loss_price(avg_price, row_atr):
            """backtester.py L347-350과 동일 로직."""
            if sl_type == "atr" and row_atr is not None:
                return avg_price - row_atr * atr_mult
            return avg_price * (1 - sl_rate)

        def _trailing_stop_price(hwm, row_atr):
            """backtester.py L352-357과 동일 로직."""
            if not ts_enabled or hwm <= 0:
                return None
            if ts_type == "atr" and row_atr is not None:
                return hwm - row_atr * ts_atr_mult
            return hwm * (1 - ts_fixed_rate)

        pos_limits = self.risk_params.get("position_limits", {}) or {}
        max_holding_days = pos_limits.get("max_holding_days", 0)

        per_symbol_pnl = {s: 0 for s in symbols}

        # 시장국면 필터 설정 (TICKET-05)
        regime_cfg = self.risk_params.get("backtest_regime_filter", {})
        regime_enabled = regime_cfg.get("enabled", False) and regime_series is not None
        caution_scale = float(regime_cfg.get("caution_scale", 0.5))
        regime_buy_blocks = 0
        regime_caution_buys = 0

        # ── 진단 계측 (ablation diagnostics) ──
        exit_reason_counts = {}
        blocked_buy_examples = []
        scaled_buy_examples = []
        bullish_buy_notionals = []
        caution_buy_notionals = []

        # ── 신호/체결/스킵 계측 ──
        signal_buy_count = 0      # 전략이 생성한 BUY 신호 총 수
        signal_sell_count = 0     # 전략이 생성한 SELL 신호 총 수
        executed_buy_count = 0    # 실제 체결된 BUY 수
        executed_sell_count = 0   # 실제 체결된 SELL 수
        skipped_reasons = {}      # 미체결 사유별 카운트

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
                row_atr = _get_atr(sig_df, date)
                hd = (date - pos["buy_date"]).days if pos.get("buy_date") and hasattr(date, "date") else 0
                in_cooling = min_hold_days > 0 and hd < min_hold_days

                if max_holding_days > 0 and hd >= max_holding_days:
                    sell_reason = "MAX_HOLD"
                if not sell_reason and close <= _stop_loss_price(pos["avg_price"], row_atr):
                    sell_reason = "STOP_LOSS"
                if not sell_reason and close >= pos["avg_price"] * (1 + tp_rate):
                    sell_reason = "TAKE_PROFIT"
                if not disable_trailing_stop:
                    ts_price = _trailing_stop_price(pos["high_water_mark"], row_atr)
                    if not sell_reason and ts_price is not None and close <= ts_price:
                        if in_cooling:
                            sell_reason = None  # 냉각기: TRAILING_STOP 억제
                        else:
                            sell_reason = "TRAILING_STOP"
                if not sell_reason and row.get("signal") == "SELL":
                    if in_cooling:
                        sell_reason = None  # 냉각기: 전략 SELL 억제
                    else:
                        sell_reason = "SELL"

                if sell_reason:
                    to_sell.append((sym, close, sell_reason))
                    exit_reason_counts[sell_reason] = exit_reason_counts.get(sell_reason, 0) + 1

            executed_sell_count += len(to_sell)
            for sym, close, reason in to_sell:
                pos = positions.pop(sym)
                costs = self.risk_manager.calculate_transaction_costs(
                    close, pos["qty"], "SELL", avg_price=pos["avg_price"],
                )
                sell_price = costs["execution_price"]
                pnl = (sell_price - pos["avg_price"]) * pos["qty"] - costs["commission"] - costs["tax"]
                cash += sell_price * pos["qty"] - costs["commission"] - costs["tax"]
                per_symbol_pnl[sym] = per_symbol_pnl.get(sym, 0) + pnl
                holding_days = (date - pos["buy_date"]).days if pos.get("buy_date") and hasattr(date, "date") else 0
                trades.append({
                    "date": date, "symbol": sym, "action": reason,
                    "price": sell_price, "quantity": pos["qty"],
                    "pnl": pnl, "pnl_rate": ((sell_price / pos["avg_price"]) - 1) * 100,
                    "entry_score": pos.get("entry_score", 0),
                    "score_macd": pos.get("score_macd", 0),
                    "score_bollinger": pos.get("score_bollinger", 0),
                    "score_volume": pos.get("score_volume", 0),
                    "holding_days": holding_days,
                })

            # 시장국면 판별 (TICKET-05): T-1일 지수 기준, look-ahead bias 없음
            regime_at_t = "bullish"
            if regime_enabled and date in regime_series.index:
                regime_at_t = regime_series.loc[date]

            # ── 신호 집계: 전략이 생성한 원본 BUY/SELL 수 ──
            for sym in symbols:
                sig_df = signals.get(sym)
                if sig_df is None or date not in sig_df.index:
                    continue
                sig_val = sig_df.loc[date].get("signal")
                if sig_val == "BUY":
                    signal_buy_count += 1
                    if sym in positions:
                        skipped_reasons["already_in_position"] = skipped_reasons.get("already_in_position", 0) + 1
                elif sig_val == "SELL":
                    signal_sell_count += 1

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
                skipped_reasons["regime_bearish"] = skipped_reasons.get("regime_bearish", 0) + n_blocked
                if n_blocked > 0 and len(blocked_buy_examples) < 10:
                    for sym in symbols:
                        if sym not in positions and signals.get(sym) is not None and date in signals[sym].index:
                            r = signals[sym].loc[date]
                            if r.get("signal") == "BUY":
                                blocked_buy_examples.append({
                                    "date": str(date)[:10], "symbol": sym,
                                    "score": float(r.get("total_score", r.get("strategy_score", 0))),
                                    "reason": "bearish",
                                })

            buy_candidates.sort(key=lambda x: -x[2])

            for sym, close, score in buy_candidates:
                if len(positions) >= max_positions:
                    skipped_reasons["max_positions"] = skipped_reasons.get("max_positions", 0) + 1
                    continue
                total_equity_now = cash + sum(
                    self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                    for s in positions
                )
                if total_equity_now <= 0:
                    skipped_reasons["no_equity"] = skipped_reasons.get("no_equity", 0) + 1
                    continue

                invested_now = sum(
                    self._get_close(signals, s, date, positions[s]["avg_price"]) * positions[s]["qty"]
                    for s in positions
                )
                if total_equity_now > 0 and invested_now / total_equity_now >= max_investment_ratio:
                    skipped_reasons["max_investment_ratio"] = skipped_reasons.get("max_investment_ratio", 0) + 1
                    continue
                if total_equity_now > 0 and cash / total_equity_now < min_cash_ratio:
                    skipped_reasons["min_cash_ratio"] = skipped_reasons.get("min_cash_ratio", 0) + 1
                    continue

                max_invest = total_equity_now * max_position_ratio
                buy_atr = _get_atr(signals.get(sym), date)
                stop_at_buy = _stop_loss_price(close, buy_atr)
                risk_per_share = max(close - stop_at_buy, close * 0.001)
                risk_amount = total_equity_now * self.risk_params.get("position_sizing", {}).get("max_risk_per_trade", 0.01)
                qty = min(
                    int(risk_amount / risk_per_share),
                    int(max_invest / close) if close > 0 else 0,
                )

                scale = self.risk_manager._signal_scale(score)
                qty = int(qty * scale)

                # 시장국면 caution 시 포지션 축소 (TICKET-05)
                if qty > 0 and regime_at_t == "caution":
                    original_qty = qty
                    qty = max(1, int(qty * caution_scale))
                    regime_caution_buys += 1
                    caution_buy_notionals.append(close * qty)
                    if len(scaled_buy_examples) < 10:
                        scaled_buy_examples.append({
                            "date": str(date)[:10], "symbol": sym,
                            "original_qty": original_qty, "scaled_qty": qty,
                            "ratio": round(qty / original_qty, 2) if original_qty > 0 else 0,
                        })
                elif qty > 0 and regime_at_t == "bullish":
                    bullish_buy_notionals.append(close * qty)

                if qty <= 0:
                    skipped_reasons["qty_zero"] = skipped_reasons.get("qty_zero", 0) + 1
                    continue
                if close * qty > cash * 0.95:
                    skipped_reasons["no_cash"] = skipped_reasons.get("no_cash", 0) + 1
                    continue

                costs = self.risk_manager.calculate_transaction_costs(close, qty, "BUY")
                buy_price = costs["execution_price"]
                total_cost = buy_price * qty + costs["commission"]
                if total_cost > cash:
                    skipped_reasons["no_cash"] = skipped_reasons.get("no_cash", 0) + 1
                    continue

                cash -= total_cost
                executed_buy_count += 1
                # 진입 시점 개별 지표 점수 기록 (signal quality 진단용)
                sig_row = signals[sym].loc[date]
                entry_scores = {
                    "entry_score": score,
                    "score_macd": float(sig_row.get("score_macd", 0)),
                    "score_bollinger": float(sig_row.get("score_bollinger", 0)),
                    "score_volume": float(sig_row.get("score_volume", 0)),
                    "score_rsi": float(sig_row.get("score_rsi", 0)),
                    "score_ma": float(sig_row.get("score_ma", 0)),
                }
                positions[sym] = {
                    "qty": qty, "avg_price": buy_price,
                    "buy_date": date, "high_water_mark": buy_price,
                    **entry_scores,
                }
                trades.append({
                    "date": date, "symbol": sym, "action": "BUY",
                    "price": buy_price, "quantity": qty, "pnl": 0, "pnl_rate": 0,
                    **entry_scores,
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
            # ── 진단 계측 ──
            "exit_reason_counts": exit_reason_counts,
            "blocked_buy_examples": blocked_buy_examples,
            "scaled_buy_examples": scaled_buy_examples,
            "avg_bullish_notional": round(sum(bullish_buy_notionals) / len(bullish_buy_notionals), 0) if bullish_buy_notionals else 0,
            "avg_caution_notional": round(sum(caution_buy_notionals) / len(caution_buy_notionals), 0) if caution_buy_notionals else 0,
            # ── 신호/체결/스킵 계측 ──
            "signal_buy_count": signal_buy_count,
            "signal_sell_count": signal_sell_count,
            "executed_buy_count": executed_buy_count,
            "executed_sell_count": executed_sell_count,
            "skipped_reasons": skipped_reasons,
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
        # ── 진단 계측 출력 ──
        print("-" * 60)
        print("  [진단: exit reason 분포]")
        for reason, cnt in sorted(result.get("exit_reason_counts", {}).items(), key=lambda x: -x[1]):
            print(f"    {reason:20s}: {cnt:>5d}건")
        print(f"  [진단: regime 계측]")
        print(f"    regime_buy_blocks  : {result.get('regime_buy_blocks', 0):>5d}건")
        print(f"    regime_caution_buys: {result.get('regime_caution_buys', 0):>5d}건")
        print(f"    avg_bullish_notional: {result.get('avg_bullish_notional', 0):>12,.0f}원")
        print(f"    avg_caution_notional: {result.get('avg_caution_notional', 0):>12,.0f}원")
        if result.get("blocked_buy_examples"):
            print(f"  [진단: blocked BUY 예시 (최대 10건)]")
            for ex in result["blocked_buy_examples"]:
                print(f"    {ex['date']} {ex['symbol']} score={ex['score']:.1f} reason={ex['reason']}")
        if result.get("scaled_buy_examples"):
            print(f"  [진단: caution 축소 예시 (최대 10건)]")
            for ex in result["scaled_buy_examples"]:
                print(f"    {ex['date']} {ex['symbol']} qty={ex['original_qty']}->{ex['scaled_qty']} ratio={ex['ratio']}")

        # ── Signal Quality 진단 ──
        self._print_signal_quality_diagnostics(result.get("trades", []))
        print("=" * 60 + "\n")

    def _print_signal_quality_diagnostics(self, trades: list):
        """신호 품질 진단: score별 성과, exit reason별 PnL, 보유기간 분석, 종목×반기 분해."""
        sell_trades = [t for t in trades if t.get("action") != "BUY"]
        if not sell_trades:
            return

        print("-" * 60)
        print("  [Signal Quality 진단]")

        # --- 1) Entry score 분포 및 score별 성과 ---
        print("  (1) 진입 score별 성과")
        score_buckets = {}
        for t in sell_trades:
            s = t.get("entry_score", 0)
            bucket = f"{int(s)}" if s == int(s) else f"{s:.1f}"
            if s < 2.5:
                bucket = "2.0-2.4"
            elif s < 3.0:
                bucket = "2.5-2.9"
            elif s < 4.0:
                bucket = "3.0-3.9"
            else:
                bucket = "4.0+"
            score_buckets.setdefault(bucket, []).append(t)

        print(f"    {'Score':<10} {'건수':>5} {'승률%':>7} {'평균PnL':>10} {'총PnL':>12}")
        for bucket in sorted(score_buckets.keys()):
            ts = score_buckets[bucket]
            wins = sum(1 for t in ts if t["pnl"] > 0)
            avg_pnl = sum(t["pnl"] for t in ts) / len(ts)
            total_pnl = sum(t["pnl"] for t in ts)
            wr = wins / len(ts) * 100
            print(f"    {bucket:<10} {len(ts):>5d} {wr:>6.1f}% {avg_pnl:>10,.0f} {total_pnl:>12,.0f}")

        # --- 2) Exit reason별 PnL ---
        print("  (2) Exit reason별 PnL")
        reason_buckets = {}
        for t in sell_trades:
            reason_buckets.setdefault(t["action"], []).append(t)

        print(f"    {'Reason':<18} {'건수':>5} {'승률%':>7} {'평균PnL':>10} {'총PnL':>12} {'평균PnL%':>8}")
        for reason in sorted(reason_buckets.keys(), key=lambda r: -sum(t["pnl"] for t in reason_buckets[r])):
            ts = reason_buckets[reason]
            wins = sum(1 for t in ts if t["pnl"] > 0)
            avg_pnl = sum(t["pnl"] for t in ts) / len(ts)
            total_pnl = sum(t["pnl"] for t in ts)
            avg_pnl_rate = sum(t.get("pnl_rate", 0) for t in ts) / len(ts)
            wr = wins / len(ts) * 100
            print(f"    {reason:<18} {len(ts):>5d} {wr:>6.1f}% {avg_pnl:>10,.0f} {total_pnl:>12,.0f} {avg_pnl_rate:>7.2f}%")

        # --- 3) 보유기간 분석 ---
        holding_trades = [t for t in sell_trades if t.get("holding_days") is not None]
        if holding_trades:
            print("  (3) 보유기간별 성과")
            hold_buckets = {}
            for t in holding_trades:
                hd = t["holding_days"]
                if hd <= 3:
                    b = "1-3일"
                elif hd <= 7:
                    b = "4-7일"
                elif hd <= 14:
                    b = "8-14일"
                elif hd <= 30:
                    b = "15-30일"
                else:
                    b = "30일+"
                hold_buckets.setdefault(b, []).append(t)

            print(f"    {'보유기간':<10} {'건수':>5} {'승률%':>7} {'평균PnL':>10} {'총PnL':>12}")
            for b in ["1-3일", "4-7일", "8-14일", "15-30일", "30일+"]:
                if b not in hold_buckets:
                    continue
                ts = hold_buckets[b]
                wins = sum(1 for t in ts if t["pnl"] > 0)
                avg_pnl = sum(t["pnl"] for t in ts) / len(ts)
                total_pnl = sum(t["pnl"] for t in ts)
                wr = wins / len(ts) * 100
                print(f"    {b:<10} {len(ts):>5d} {wr:>6.1f}% {avg_pnl:>10,.0f} {total_pnl:>12,.0f}")

        # --- 4) 종목 × 반기 분해 ---
        print("  (4) 종목 × 반기 PnL 분해")
        sym_half = {}
        for t in sell_trades:
            sym = t["symbol"]
            d = t["date"]
            half = f"{d.year}-H1" if hasattr(d, "month") and d.month <= 6 else f"{d.year}-H2"
            key = (sym, half)
            sym_half.setdefault(key, []).append(t)

        halves = sorted(set(h for _, h in sym_half.keys()))
        syms = sorted(set(s for s, _ in sym_half.keys()))
        header = f"    {'종목':<8}" + "".join(f"{h:>12}" for h in halves) + f"{'합계':>12}"
        print(header)
        for sym in syms:
            row = f"    {sym:<8}"
            total = 0
            for h in halves:
                pnl = sum(t["pnl"] for t in sym_half.get((sym, h), []))
                total += pnl
                row += f"{pnl:>12,.0f}"
            row += f"{total:>12,.0f}"
            print(row)

        # --- 5) 개별 지표 기여도 (진입 시 발화 비율) ---
        print("  (5) 진입 시 지표 발화 비율 및 승률")
        indicators = [("score_macd", "MACD"), ("score_bollinger", "Bollinger"), ("score_volume", "Volume")]
        for key, name in indicators:
            fired = [t for t in sell_trades if t.get(key, 0) > 0]
            not_fired = [t for t in sell_trades if t.get(key, 0) <= 0]
            if fired:
                wr = sum(1 for t in fired if t["pnl"] > 0) / len(fired) * 100
                avg_pnl = sum(t["pnl"] for t in fired) / len(fired)
                print(f"    {name:<12} 발화={len(fired):>3d}건 승률={wr:.1f}% 평균PnL={avg_pnl:>8,.0f}")
            if not_fired:
                wr_nf = sum(1 for t in not_fired if t["pnl"] > 0) / len(not_fired) * 100
                avg_pnl_nf = sum(t["pnl"] for t in not_fired) / len(not_fired)
                print(f"    {name:<12} 미발화={len(not_fired):>3d}건 승률={wr_nf:.1f}% 평균PnL={avg_pnl_nf:>8,.0f}")
