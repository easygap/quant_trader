"""
평균 회귀 전략
- Z-Score 기반 가격 이탈 시 되돌아오는 특성 활용
- Z-Score 매수 조건 충족 시 PER·부채비율 등 펀더멘털 필터로 정상 범위 확인 후 매수

한국 시장 한계: 큰 폭 하락 종목 상당수가 실적 악화·분식·대주주 등 펀더멘털 이유로
평균 회귀하지 않고 추가 하락한다. Z-Score만으로는 "기술적 과매도"와 "악화 기업"을
구분할 수 없고, ADX < adx_filter 도 하락 추세 구간에서 낮게 나와 필터가 불완전하다.
유동성·퀄리티 스크리닝 및 손절 필수. 자세한 내용은 quant_trader_design.md §4.2 참고.
"""

import pandas as pd
import numpy as np
from loguru import logger

from strategies.base_strategy import BaseStrategy
from core.indicator_engine import IndicatorEngine
from core.fundamental_loader import check_fundamental_filter
from config.config_loader import Config


class MeanReversionStrategy(BaseStrategy):
    """
    평균 회귀 전략 (중급)

    - Z-Score < -2 → 과도한 하락 → 매수
    - Z-Score > +2 → 과도한 상승 → 매도
    - ADX < adx_filter 일 때만 활성화 (횡보장 가정). 단, 실적 악화 등 하락 추세 구간에서도
      ADX가 낮게 나올 수 있어 "횡보 vs 하락" 구분이 불완전함.
    - 한국 시장: 펀더멘털 악화로 하락한 종목은 평균 회귀가 성립하지 않는 경우가 많으므로
      유동성·퀄리티 필터 및 손절 적용 권장.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="mean_reversion",
            description="Z-Score 기반 평균 회귀 전략 — 횡보장에서 유효",
        )
        self.config = config or Config.get()
        self.indicator_engine = IndicatorEngine(self.config)
        self.params = self.config.strategies.get("mean_reversion", {})
        logger.info("MeanReversionStrategy 초기화 완료")

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """지표 계산 + Z-Score + 전략 signal 컬럼 추가"""
        analyzed = self.indicator_engine.calculate_all(df.copy())
        if analyzed.empty:
            return analyzed

        # Z-Score "평균" = 최근 lookback_period 일 이동평균. 이 값에 따라 신호가 크게 달라지므로 설정·최적화 권장.
        lookback = self.params.get("lookback_period", 20)
        z_buy = self.params.get("z_score_buy", -2.0)
        z_sell = self.params.get("z_score_sell", 2.0)
        adx_filter = self.params.get("adx_filter", 20)
        volume_spike_filter = self.params.get("volume_spike_filter", 3.0)
        # 52주 고점/저점 기반 필터 (실적 악화 장기 하락 구간 회피)
        window_52w = self.params.get("window_52w", 252)
        high_series = analyzed["high"] if "high" in analyzed.columns else analyzed["close"]
        low_series = analyzed["low"] if "low" in analyzed.columns else analyzed["close"]
        analyzed["high_52w"] = high_series.rolling(window=window_52w, min_periods=1).max()
        analyzed["low_52w"] = low_series.rolling(window=window_52w, min_periods=1).min()
        # 52주 고점 대비 하락률: (high_52w - close) / high_52w. 0.3이면 고점 대비 30% 하락
        analyzed["drawdown_from_52w_high"] = (analyzed["high_52w"] - analyzed["close"]) / analyzed["high_52w"].replace(0, np.nan)
        # 52주 저점 대비 상승률: (close - low_52w) / low_52w. 0.05면 저점 대비 5% 위
        analyzed["pct_above_52w_low"] = (analyzed["close"] - analyzed["low_52w"]) / analyzed["low_52w"].replace(0, np.nan)

        analyzed["z_mean"] = analyzed["close"].rolling(window=lookback).mean()
        analyzed["z_std"] = analyzed["close"].rolling(window=lookback).std()
        # 안전: z_std == 0(횡보 구간) → NaN → fillna(0) → HOLD 유도 (Inf/NaN 전파 방지)
        safe_std = analyzed["z_std"].replace(0, np.nan)
        analyzed["z_score"] = (analyzed["close"] - analyzed["z_mean"]) / safe_std
        analyzed["z_score"] = analyzed["z_score"].fillna(0.0)
        analyzed["strategy_score"] = analyzed["z_score"]

        adx_ok = analyzed.get("adx", pd.Series(np.nan, index=analyzed.index)) < adx_filter
        rsi_buy = analyzed.get("rsi", pd.Series(np.nan, index=analyzed.index)) < 40
        rsi_sell = analyzed.get("rsi", pd.Series(np.nan, index=analyzed.index)) > 60
        volume_ratio = analyzed.get("volume_ratio", pd.Series(np.nan, index=analyzed.index))

        buy_signal = adx_ok & (analyzed["z_score"] <= z_buy) & rsi_buy
        sell_signal = adx_ok & (analyzed["z_score"] >= z_sell) & rsi_sell

        # 거래량 급변 시 평균회귀 매수는 차단
        buy_signal = buy_signal & ~((volume_ratio > volume_spike_filter).fillna(False))

        analyzed["signal"] = self.HOLD
        analyzed.loc[buy_signal.fillna(False), "signal"] = self.BUY
        analyzed.loc[sell_signal.fillna(False), "signal"] = self.SELL

        return analyzed

    def _is_kospi200(self, symbol: str) -> bool:
        """symbol이 코스피200 구성 종목인지 확인. 캐싱하여 재사용."""
        if not hasattr(self, "_kospi200_set"):
            self._kospi200_set = set()
            try:
                from pykrx import stock as pykrx_stock
                tickers = pykrx_stock.get_index_portfolio_deposit_file("1028")
                if tickers:
                    self._kospi200_set = set(tickers)
                    logger.debug("코스피200 종목 {}개 로드", len(self._kospi200_set))
            except Exception as e:
                logger.debug("코스피200 목록 로드 실패 (제한 비활성화): {}", e)
        return symbol in self._kospi200_set if self._kospi200_set else True

    def generate_signal(self, df: pd.DataFrame, symbol: str = None, **kwargs) -> dict:
        """Z-Score 기반 신호 생성. 매수 시 symbol이 있으면 펀더멘털 필터(PER·부채비율) 적용."""
        analyzed = self.analyze(df)

        if analyzed.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}

        # 코스피200 제한: 소형주의 영구 하락 위험 회피
        restrict_kospi200 = self.params.get("restrict_to_kospi200", False)
        if restrict_kospi200 and symbol and not self._is_kospi200(symbol):
            return {
                "signal": self.HOLD, "score": 0,
                "details": {"제외사유": "코스피200 외 종목 (restrict_to_kospi200=true)"},
                "close": float(analyzed.iloc[-1].get("close", 0)),
                "atr": float(analyzed.iloc[-1].get("atr", 0)),
            }

        last = analyzed.iloc[-1]
        z_score = last.get("z_score", 0)
        adx = last.get("adx", 50)
        rsi = last.get("rsi", 50)

        adx_filter = self.params.get("adx_filter", 20)
        volume_ratio = last.get("volume_ratio")
        signal = last.get("signal", self.HOLD)
        score = last.get("strategy_score", z_score)
        drawdown_52w = last.get("drawdown_from_52w_high")
        pct_above_52w = last.get("pct_above_52w_low")

        # 52주 고점 대비 하락률 필터: 고점에서 N% 이상 하락한 종목은 실적 악화·장기 하락 가능성 → 매수 제외
        exclude_52w = self.params.get("exclude_52w_low_near", True)
        max_drawdown_52w = self.params.get("max_drawdown_from_52w_high", 0.30)
        if signal == self.BUY and exclude_52w and drawdown_52w is not None:
            if drawdown_52w >= max_drawdown_52w:
                signal = self.HOLD
                logger.info(
                    "평균회귀 매수 보류(52주 고점 대비 급락): {} — 52주고점 대비 -{:.1f}% (한도 -{:.0f}%)",
                    symbol or "?",
                    float(drawdown_52w) * 100,
                    max_drawdown_52w * 100,
                )

        # 52주 저점 근방 필터: 현재가가 저점 대비 N% 이내이면 신저가 구간 → 매수 제외
        near_low_threshold = self.params.get("near_52w_low_pct", 0.05)
        if signal == self.BUY and exclude_52w and pct_above_52w is not None:
            if pct_above_52w <= near_low_threshold:
                signal = self.HOLD
                logger.info(
                    "평균회귀 매수 보류(52주 신저가 근방): {} — 52주저점 대비 +{:.1f}% (한도 +{:.0f}%)",
                    symbol or "?",
                    float(pct_above_52w) * 100,
                    near_low_threshold * 100,
                )

        details = {
            "Z-Score": round(z_score, 2),
            "ADX": round(adx, 2) if pd.notna(adx) else 0,
            "RSI": round(rsi, 2) if pd.notna(rsi) else 0,
            "ADX필터": f"< {adx_filter}",
            "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "drawdown_from_52w_high": round(float(drawdown_52w), 2) if drawdown_52w is not None else None,
            "pct_above_52w_low": round(float(pct_above_52w), 2) if pct_above_52w is not None else None,
        }

        # 매수 신호 시 펀더멘털 필터: PER·부채비율 정상 범위 확인
        fund_cfg = self.params.get("fundamental_filter") or {}
        if signal == self.BUY and symbol and fund_cfg.get("enabled", False):
            per_min = fund_cfg.get("per_min")
            per_max = fund_cfg.get("per_max")
            debt_ratio_max = fund_cfg.get("debt_ratio_max")
            if per_min is not None or per_max is not None or debt_ratio_max is not None:
                passed, reason = check_fundamental_filter(
                    symbol,
                    per_min=per_min,
                    per_max=per_max,
                    debt_ratio_max=debt_ratio_max,
                )
                details["펀더멘털필터"] = reason
                if not passed:
                    signal = self.HOLD
                    logger.info("평균회귀 매수 보류(펀더멘털 필터): {} — {}", symbol, reason)

        return {
            "signal": signal,
            "score": round(score, 2),
            "details": details,
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
        }
