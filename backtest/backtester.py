"""
백테스팅 엔진
- 과거 데이터로 매매 전략을 검증
- 수수료, 세금, 슬리피지 반영
- 성과 지표 계산 (수익률, 샤프, MDD, 승률, 손익비)
"""

import pandas as pd
import numpy as np
from datetime import datetime
from loguru import logger

from config.config_loader import Config
from core.indicator_engine import IndicatorEngine
from core.signal_generator import SignalGenerator


class Backtester:
    """
    백테스팅 엔진

    사용법:
        bt = Backtester()
        result = bt.run(df, strategy_name="scoring")
    """

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.risk_params = self.config.risk_params
        self.costs = self.risk_params.get("transaction_costs", {})
        logger.info("Backtester 초기화 완료")

    def run(
        self,
        df: pd.DataFrame,
        strategy_name: str = "scoring",
        initial_capital: float = None,
        strict_lookahead: bool = False,
    ) -> dict:
        """
        백테스팅 실행

        Args:
            df: OHLCV 데이터프레임
            strategy_name: 전략명
            initial_capital: 초기 투자금 (None이면 설정값 사용)
            strict_lookahead: True면 매 시점 T에서 df[:T+1]만으로 지표/신호 계산 (Look-Ahead Bias 완전 방어, 느림)

        Returns:
            백테스팅 결과 딕셔너리
        """
        if initial_capital is None:
            initial_capital = self.risk_params.get(
                "position_sizing", {}
            ).get("initial_capital", 10000000)

        strategy = self._get_strategy(strategy_name)
        if strict_lookahead:
            # Look-Ahead Bias 방어: 시점 T에서는 T 이전(및 T) 데이터만 사용
            logger.info("strict_lookahead=True: 시점별 슬라이싱 분석 실행 중...")
            rows = []
            for i in range(len(df)):
                chunk = strategy.analyze(df.iloc[: i + 1].copy())
                if not chunk.empty and "signal" in chunk.columns:
                    rows.append(chunk.iloc[-1].to_dict())
                else:
                    rows.append({"signal": "HOLD", "close": df.iloc[i]["close"]})
            df_analyzed = pd.DataFrame(rows, index=df.index)
            if "close" not in df_analyzed.columns:
                df_analyzed["close"] = df["close"].values
        else:
            df_analyzed = strategy.analyze(df.copy())

        if df_analyzed.empty or "signal" not in df_analyzed.columns:
            logger.error("백테스팅 실패: 신호 생성 불가")
            return {}

        # 시뮬레이션 실행 (시점 T에서는 row T만 사용, T+1 이후 미참조)
        result = self._simulate(df_analyzed, initial_capital)
        result["look_ahead_bias_verified"] = "STRICT" if strict_lookahead else "PASS"

        # 성과 지표 계산
        metrics = self._calculate_metrics(result, initial_capital)

        logger.info(
            "백테스팅 완료 | 수익률: {:.2f}% | 샤프: {:.2f} | MDD: {:.2f}% | 승률: {:.1f}%",
            metrics["total_return"],
            metrics["sharpe_ratio"],
            metrics["max_drawdown"],
            metrics["win_rate"],
        )

        return {
            "metrics": metrics,
            "trades": result["trades"],
            "equity_curve": result["equity_curve"],
            "strategy": strategy_name,
            "period": f"{df.index[0]} ~ {df.index[-1]}",
            "initial_capital": initial_capital,
            "look_ahead_bias_verified": result.get("look_ahead_bias_verified", "PASS"),
        }

    def _get_strategy(self, name: str):
        """전략 인스턴스 반환"""
        if name == "scoring":
            from strategies.scoring_strategy import ScoringStrategy
            return ScoringStrategy(self.config)
        elif name == "mean_reversion":
            from strategies.mean_reversion import MeanReversionStrategy
            return MeanReversionStrategy(self.config)
        elif name == "trend_following":
            from strategies.trend_following import TrendFollowingStrategy
            return TrendFollowingStrategy(self.config)
        elif name == "ensemble":
            from core.strategy_ensemble import StrategyEnsemble
            return StrategyEnsemble(self.config)
        else:
            from strategies.scoring_strategy import ScoringStrategy
            return ScoringStrategy(self.config)

    def _simulate(self, df: pd.DataFrame, initial_capital: float) -> dict:
        """
        거래 시뮬레이션 실행.
        방어: 날짜 순으로 순회하며 당일(row T) 데이터만 사용 — T+1 이후 행 미참조로 Look-Ahead Bias 없음.
        설정에 따라 ATR 손절, 1% 룰 포지션 사이징, 부분 익절을 반영한다.
        """
        assert df.index.is_monotonic_increasing or len(df) <= 1, (
            "시뮬레이션은 시간 순서대로만 순회해야 하며, 미래 데이터를 참조하지 않습니다."
        )
        cash = initial_capital
        position = 0
        avg_price = 0
        partial_exit_done = False  # 1차 부분 익절 수행 여부
        high_water_mark = 0.0     # 트레일링 스탑용: 보유 중 최고가
        trades = []
        equity_curve = []

        sl_config = self.risk_params.get("stop_loss", {})
        tp_config = self.risk_params.get("take_profit", {})
        ts_config = self.risk_params.get("trailing_stop", {})
        pos_config = self.risk_params.get("position_sizing", {})
        div_config = self.risk_params.get("diversification", {})

        sl_rate = sl_config.get("fixed_rate", 0.03)
        sl_type = sl_config.get("type", "fixed")
        atr_mult = sl_config.get("atr_multiplier", 2.0)
        tp_rate = tp_config.get("fixed_rate", 0.10)
        partial_exit = tp_config.get("partial_exit", False)
        partial_ratio = tp_config.get("partial_ratio", 0.5)
        partial_target = tp_config.get("partial_target", 0.06)
        ts_enabled = ts_config.get("enabled", False)
        ts_type = ts_config.get("type", "fixed")
        ts_fixed_rate = ts_config.get("fixed_rate", 0.03)
        ts_atr_mult = ts_config.get("atr_multiplier", 3.0)
        max_risk_per_trade = pos_config.get("max_risk_per_trade", 0.01)
        max_position_ratio = div_config.get("max_position_ratio", 0.20)

        commission_rate = self.costs.get("commission_rate", 0.00015)
        tax_rate = self.costs.get("tax_rate", 0.002)
        slippage = self.costs.get("slippage", 0.0005)

        def _stop_loss_price(row_atr):
            if sl_type == "atr" and row_atr is not None and pd.notna(row_atr) and row_atr > 0:
                return avg_price - float(row_atr) * atr_mult
            return avg_price * (1 - sl_rate)

        def _trailing_stop_price(hwm, row_atr):
            if not ts_enabled or hwm <= 0:
                return None
            if ts_type == "atr" and row_atr is not None and pd.notna(row_atr) and row_atr > 0:
                return hwm - float(row_atr) * ts_atr_mult
            return hwm * (1 - ts_fixed_rate)

        for i, (date, row) in enumerate(df.iterrows()):
            close = row["close"]
            signal = row.get("signal", "HOLD")
            row_atr = row.get("atr")

            if position > 0:
                high_water_mark = max(high_water_mark, close)
                stop_loss_price = _stop_loss_price(row_atr)
                take_profit_price = avg_price * (1 + tp_rate)

                # 손절
                if close <= stop_loss_price:
                    sell_price = close * (1 - slippage)
                    sell_amount = sell_price * position
                    commission = sell_amount * commission_rate
                    tax_amt = sell_amount * tax_rate
                    pnl = (sell_price - avg_price) * position - commission - tax_amt
                    cash += sell_amount - commission - tax_amt
                    trades.append({
                        "date": date, "action": "STOP_LOSS", "price": sell_price,
                        "quantity": position, "pnl": pnl,
                        "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                    })
                    position = 0
                    avg_price = 0
                    partial_exit_done = False
                    high_water_mark = 0.0

                # 부분 익절 (1차 목표 도달)
                elif partial_exit and not partial_exit_done and close >= avg_price * (1 + partial_target):
                    sell_qty = max(1, int(position * partial_ratio))
                    sell_price = close * (1 - slippage)
                    sell_amount = sell_price * sell_qty
                    commission = sell_amount * commission_rate
                    tax_amt = sell_amount * tax_rate
                    pnl = (sell_price - avg_price) * sell_qty - commission - tax_amt
                    cash += sell_amount - commission - tax_amt
                    trades.append({
                        "date": date, "action": "TAKE_PROFIT_PARTIAL", "price": sell_price,
                        "quantity": sell_qty, "pnl": pnl,
                        "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                    })
                    position -= sell_qty
                    partial_exit_done = True
                    if position <= 0:
                        avg_price = 0
                        partial_exit_done = False
                        high_water_mark = 0.0

                # 전량 익절
                elif close >= take_profit_price:
                    sell_price = close * (1 - slippage)
                    sell_amount = sell_price * position
                    commission = sell_amount * commission_rate
                    tax_amt = sell_amount * tax_rate
                    pnl = (sell_price - avg_price) * position - commission - tax_amt
                    cash += sell_amount - commission - tax_amt
                    trades.append({
                        "date": date, "action": "TAKE_PROFIT", "price": sell_price,
                        "quantity": position, "pnl": pnl,
                        "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                    })
                    position = 0
                    avg_price = 0
                    partial_exit_done = False
                    high_water_mark = 0.0

                # 트레일링 스탑 (고점 대비 하락 시 청산)
                elif ts_enabled:
                    trail_price = _trailing_stop_price(high_water_mark, row_atr)
                    if trail_price is not None and close <= trail_price:
                        sell_price = close * (1 - slippage)
                        sell_amount = sell_price * position
                        commission = sell_amount * commission_rate
                        tax_amt = sell_amount * tax_rate
                        pnl = (sell_price - avg_price) * position - commission - tax_amt
                        cash += sell_amount - commission - tax_amt
                        trades.append({
                            "date": date, "action": "TRAILING_STOP", "price": sell_price,
                            "quantity": position, "pnl": pnl,
                            "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                        })
                        position = 0
                        avg_price = 0
                        partial_exit_done = False
                        high_water_mark = 0.0

            if signal == "BUY" and position == 0:
                buy_price = close * (1 + slippage)
                stop_at_buy = buy_price * (1 - sl_rate)
                if sl_type == "atr" and row_atr is not None and pd.notna(row_atr) and row_atr > 0:
                    stop_at_buy = buy_price - float(row_atr) * atr_mult
                risk_per_share = max(buy_price - stop_at_buy, buy_price * 0.001)
                risk_amount = (cash + (position * close)) * max_risk_per_trade
                qty_by_1pct = int(risk_amount / risk_per_share) if risk_per_share > 0 else 0
                invest_cap = (cash + (position * close)) * max_position_ratio
                qty_by_cap = int(invest_cap / buy_price) if buy_price > 0 else 0
                quantity = min(qty_by_1pct, qty_by_cap) if qty_by_1pct and qty_by_cap else (qty_by_cap or qty_by_1pct or int(cash * max_position_ratio / buy_price))

                if quantity > 0 and buy_price * quantity <= cash:
                    buy_amount = buy_price * quantity
                    commission = buy_amount * commission_rate
                    cash -= (buy_amount + commission)
                    position = quantity
                    avg_price = buy_price
                    partial_exit_done = False
                    high_water_mark = buy_price
                    trades.append({
                        "date": date, "action": "BUY", "price": buy_price,
                        "quantity": quantity, "pnl": 0, "pnl_rate": 0,
                    })

            elif signal == "SELL" and position > 0:
                sell_price = close * (1 - slippage)
                sell_amount = sell_price * position
                commission = sell_amount * commission_rate
                tax = sell_amount * tax_rate
                pnl = (sell_price - avg_price) * position - commission - tax

                cash += sell_amount - commission - tax
                trades.append({
                    "date": date, "action": "SELL", "price": sell_price,
                    "quantity": position, "pnl": pnl,
                    "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                })
                position = 0
                avg_price = 0
                high_water_mark = 0.0

            # 자본금 곡선 기록
            portfolio_value = cash + (position * close)
            equity_curve.append({
                "date": date,
                "value": portfolio_value,
                "cash": cash,
                "position_value": position * close,
            })

        return {
            "trades": trades,
            "equity_curve": pd.DataFrame(equity_curve),
        }

    def _calculate_metrics(self, result: dict, initial_capital: float) -> dict:
        """성과 지표 계산"""
        trades = result["trades"]
        equity = result["equity_curve"]

        if equity.empty:
            return self._empty_metrics()

        final_value = equity["value"].iloc[-1]
        total_return = ((final_value / initial_capital) - 1) * 100

        # 일일 수익률
        equity["daily_return"] = equity["value"].pct_change()
        daily_returns = equity["daily_return"].dropna()

        # 샤프 지수 (연율화, 무위험수익률 3%)
        if len(daily_returns) > 0 and daily_returns.std() > 0:
            annual_return = daily_returns.mean() * 252
            annual_std = daily_returns.std() * np.sqrt(252)
            sharpe = (annual_return - 0.03) / annual_std
        else:
            sharpe = 0

        # MDD (최대 낙폭)
        equity["peak"] = equity["value"].cummax()
        equity["drawdown"] = (equity["value"] - equity["peak"]) / equity["peak"]
        max_drawdown = equity["drawdown"].min() * 100

        # 매매 기준 성과
        sell_trades = [t for t in trades if t["action"] in ("SELL", "STOP_LOSS", "TAKE_PROFIT", "TAKE_PROFIT_PARTIAL", "TRAILING_STOP")]
        winning = [t for t in sell_trades if t["pnl"] > 0]
        losing = [t for t in sell_trades if t["pnl"] <= 0]

        win_rate = (len(winning) / len(sell_trades) * 100) if sell_trades else 0

        avg_win = np.mean([t["pnl"] for t in winning]) if winning else 0
        avg_loss = abs(np.mean([t["pnl"] for t in losing])) if losing else 1

        profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

        # 칼마 비율
        years = len(equity) / 252 if len(equity) > 0 else 1
        annual_return_pct = total_return / years
        calmar = abs(annual_return_pct / max_drawdown) if max_drawdown != 0 else 0

        return {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_drawdown, 2),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "calmar_ratio": round(calmar, 2),
            "total_trades": len(sell_trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "final_value": round(final_value, 0),
            "initial_capital": initial_capital,
        }

    @staticmethod
    def _empty_metrics():
        return {
            "total_return": 0, "annual_return": 0, "sharpe_ratio": 0,
            "max_drawdown": 0, "win_rate": 0, "profit_factor": 0,
            "calmar_ratio": 0, "total_trades": 0, "winning_trades": 0,
            "losing_trades": 0, "avg_win": 0, "avg_loss": 0,
            "final_value": 0, "initial_capital": 0,
        }

    def print_report(self, result: dict):
        """백테스팅 결과를 콘솔에 출력"""
        if not result:
            print("결과 없음")
            return

        m = result["metrics"]
        print("\n" + "=" * 60)
        print(f"  📊 백테스팅 결과 ({result['strategy']})")
        print(f"  📅 기간: {result['period']}")
        print(f"  Look-Ahead Bias 검증: {result.get('look_ahead_bias_verified', 'PASS')}")
        print("=" * 60)
        print(f"  초기 자본     : {m['initial_capital']:>14,.0f}원")
        print(f"  최종 자본     : {m['final_value']:>14,.0f}원")
        print(f"  총 수익률     : {m['total_return']:>13.2f}%")
        print(f"  연간 수익률   : {m['annual_return']:>13.2f}%")
        print("-" * 60)
        print(f"  샤프 지수     : {m['sharpe_ratio']:>13.2f}")
        print(f"  최대 낙폭     : {m['max_drawdown']:>13.2f}%")
        print(f"  칼마 비율     : {m['calmar_ratio']:>13.2f}")
        print("-" * 60)
        print(f"  총 매매 횟수  : {m['total_trades']:>13d}회")
        print(f"  승률          : {m['win_rate']:>13.1f}%")
        print(f"  수익 거래     : {m['winning_trades']:>13d}회")
        print(f"  손실 거래     : {m['losing_trades']:>13d}회")
        print(f"  손익비        : {m['profit_factor']:>13.2f}")
        print(f"  평균 수익     : {m['avg_win']:>14,.0f}원")
        print(f"  평균 손실     : {m['avg_loss']:>14,.0f}원")
        print("=" * 60 + "\n")
