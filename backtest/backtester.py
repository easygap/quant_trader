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
    ) -> dict:
        """
        백테스팅 실행

        Args:
            df: OHLCV 데이터프레임
            strategy_name: 전략명
            initial_capital: 초기 투자금 (None이면 설정값 사용)

        Returns:
            백테스팅 결과 딕셔너리
        """
        if initial_capital is None:
            initial_capital = self.risk_params.get(
                "position_sizing", {}
            ).get("initial_capital", 10000000)

        # 전략에 따라 지표계산 + 신호생성
        strategy = self._get_strategy(strategy_name)
        df_analyzed = strategy.analyze(df.copy())

        if df_analyzed.empty or "signal" not in df_analyzed.columns:
            logger.error("백테스팅 실패: 신호 생성 불가")
            return {}

        # 시뮬레이션 실행
        result = self._simulate(df_analyzed, initial_capital)

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
        else:
            from strategies.scoring_strategy import ScoringStrategy
            return ScoringStrategy(self.config)

    def _simulate(self, df: pd.DataFrame, initial_capital: float) -> dict:
        """
        거래 시뮬레이션 실행

        Returns:
            {"trades": 거래 리스트, "equity_curve": 자본금 변화}
        """
        cash = initial_capital
        position = 0            # 보유 수량
        avg_price = 0           # 평균 매수가
        trades = []
        equity_curve = []

        # 손절/익절 설정
        sl_config = self.risk_params.get("stop_loss", {})
        tp_config = self.risk_params.get("take_profit", {})
        sl_rate = sl_config.get("fixed_rate", 0.03)
        tp_rate = tp_config.get("fixed_rate", 0.10)

        commission_rate = self.costs.get("commission_rate", 0.00015)
        tax_rate = self.costs.get("tax_rate", 0.002)
        slippage = self.costs.get("slippage", 0.0005)

        for i, (date, row) in enumerate(df.iterrows()):
            close = row["close"]
            signal = row.get("signal", "HOLD")

            # 보유 중일 때 손절/익절 체크
            if position > 0:
                stop_loss_price = avg_price * (1 - sl_rate)
                take_profit_price = avg_price * (1 + tp_rate)

                # 손절
                if close <= stop_loss_price:
                    sell_price = close * (1 - slippage)
                    sell_amount = sell_price * position
                    commission = sell_amount * commission_rate
                    tax = sell_amount * tax_rate
                    pnl = (sell_price - avg_price) * position - commission - tax

                    cash += sell_amount - commission - tax
                    trades.append({
                        "date": date, "action": "STOP_LOSS", "price": sell_price,
                        "quantity": position, "pnl": pnl,
                        "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                    })
                    position = 0
                    avg_price = 0

                # 익절
                elif close >= take_profit_price:
                    sell_price = close * (1 - slippage)
                    sell_amount = sell_price * position
                    commission = sell_amount * commission_rate
                    tax = sell_amount * tax_rate
                    pnl = (sell_price - avg_price) * position - commission - tax

                    cash += sell_amount - commission - tax
                    trades.append({
                        "date": date, "action": "TAKE_PROFIT", "price": sell_price,
                        "quantity": position, "pnl": pnl,
                        "pnl_rate": ((sell_price / avg_price) - 1) * 100,
                    })
                    position = 0
                    avg_price = 0

            # 신호 기반 매매
            if signal == "BUY" and position == 0:
                buy_price = close * (1 + slippage)
                # 투자금의 최대 20%만 사용
                invest = cash * 0.20
                quantity = int(invest / buy_price)

                if quantity > 0:
                    buy_amount = buy_price * quantity
                    commission = buy_amount * commission_rate
                    cash -= (buy_amount + commission)
                    position = quantity
                    avg_price = buy_price

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
        sell_trades = [t for t in trades if t["action"] in ("SELL", "STOP_LOSS", "TAKE_PROFIT")]
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
