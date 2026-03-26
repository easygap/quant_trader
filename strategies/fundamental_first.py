"""
Fundamental-First 전략
- 1단계: 펀더멘털 팩터(PER·ROE·부채·영업이익)로 종목 선별 (분기 리밸런싱)
- 2단계: Scoring(기술 지표)으로 진입/퇴출 타이밍 보조 (선택적)

설계 근거:
- 펀더멘털만 사용하면 진입 시점이 불리할 수 있음 (나쁜 타이밍에 매수)
- 기술 지표만 사용하면 turnover가 과다하고 비용에 취약
- 펀더멘털로 "무엇을" 사고, 기술로 "언제" 사는지 분리

비용 프로필:
- 펀더멘털 리밸런싱: 분기 1회 → 연 4회 왕복 (~1.3% 비용)
- 기술 타이밍 필터: BUY를 지연시킬 수 있지만 추가 거래는 생성하지 않음
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.base_strategy import BaseStrategy
from strategies.fundamental_factor import FundamentalFactorStrategy
from config.config_loader import Config


class FundamentalFirstStrategy(BaseStrategy):
    """
    펀더멘털 선별 → 기술 타이밍 2단계 전략.

    - 펀더멘털 score ≥ 3: 매수 대기 (기술 필터 통과 시 매수)
    - 펀더멘털 score ≤ 1: 즉시 매도 (기술 필터 무시)
    - 펀더멘털 score = 2: HOLD (현 상태 유지)

    기술 타이밍 필터 (선택적):
    - 펀더멘털 BUY일 때, Scoring total_score ≥ 0이면 매수 (타이밍 OK)
    - 펀더멘털 BUY이지만 Scoring < 0이면 매수 보류 (타이밍 불리)
    - timing_filter_enabled: false면 펀더멘털 신호만으로 매수
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    def __init__(self, config: Config = None):
        super().__init__(
            name="fundamental_first",
            description="펀더멘털 선별 → 기술 타이밍 2단계 전략",
        )
        self.config = config or Config.get()
        self.params = self.config.strategies.get("fundamental_first", {})
        self._timing_enabled = bool(self.params.get("timing_filter_enabled", True))
        self._timing_threshold = float(self.params.get("timing_score_threshold", 0))

        self._fundamental = FundamentalFactorStrategy(config)
        self._scoring = None
        if self._timing_enabled:
            try:
                from strategies.scoring_strategy import ScoringStrategy
                self._scoring = ScoringStrategy(config)
            except Exception as e:
                logger.warning("Scoring 전략 로드 실패 — 타이밍 필터 비활성화: {}", e)
                self._timing_enabled = False

        logger.info(
            "FundamentalFirstStrategy 초기화 (타이밍 필터: {}, 임계: {})",
            self._timing_enabled, self._timing_threshold,
        )

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """펀더멘털 + 기술 타이밍 합성 신호 생성."""
        result = df.copy()
        if result.empty:
            result["signal"] = self.HOLD
            result["strategy_score"] = 0.0
            return result

        # 1단계: 펀더멘털 분석
        fund_df = self._fundamental.analyze(result.copy())
        fund_signal = fund_df["signal"] if "signal" in fund_df.columns else pd.Series(self.HOLD, index=result.index)
        fund_score = fund_df["fundamental_score"] if "fundamental_score" in fund_df.columns else pd.Series(0, index=result.index)

        # 2단계: 기술 타이밍 (선택적)
        tech_score = pd.Series(0.0, index=result.index)
        if self._timing_enabled and self._scoring is not None:
            try:
                tech_df = self._scoring.analyze(result.copy())
                if "total_score" in tech_df.columns:
                    tech_score = tech_df["total_score"].fillna(0.0)
                elif "strategy_score" in tech_df.columns:
                    tech_score = tech_df["strategy_score"].fillna(0.0)
            except Exception as e:
                logger.debug("기술 타이밍 분석 실패 (펀더멘털 단독 사용): {}", e)

        # 합성 로직
        final_signal = []
        for i in range(len(result)):
            fs = fund_signal.iloc[i] if i < len(fund_signal) else self.HOLD
            ts = float(tech_score.iloc[i]) if i < len(tech_score) else 0.0

            if fs == self.SELL:
                # 펀더멘털 SELL → 즉시 매도 (기술 무시)
                final_signal.append(self.SELL)
            elif fs == self.BUY:
                if not self._timing_enabled:
                    # 타이밍 필터 꺼짐 → 펀더멘털 BUY만으로 매수
                    final_signal.append(self.BUY)
                elif ts >= self._timing_threshold:
                    # 기술 타이밍 OK → 매수
                    final_signal.append(self.BUY)
                else:
                    # 펀더멘털 BUY이지만 기술 타이밍 불리 → 대기
                    final_signal.append(self.HOLD)
            else:
                final_signal.append(self.HOLD)

        result["signal"] = final_signal
        result["strategy_score"] = fund_score.values
        result["fundamental_score"] = fund_score.values
        result["timing_score"] = tech_score.values
        result["ensemble_skip"] = fund_df.get("ensemble_skip", pd.Series(False, index=result.index)).values

        return result

    def generate_signal(self, df: pd.DataFrame, symbol: str = None, **kwargs) -> dict:
        """단일 종목 신호 생성."""
        # 펀더멘털 신호
        fund_result = self._fundamental.generate_signal(df, symbol=symbol, **kwargs)
        fund_sig = fund_result.get("signal", self.HOLD)
        fund_score = fund_result.get("score", 0)

        # 기술 타이밍
        timing_score = 0.0
        if self._timing_enabled and self._scoring is not None and fund_sig == self.BUY:
            try:
                tech_df = self._scoring.analyze(df.copy())
                if not tech_df.empty:
                    last = tech_df.iloc[-1]
                    timing_score = float(last.get("total_score", last.get("strategy_score", 0)))
            except Exception:
                pass

        # 합성
        if fund_sig == self.SELL:
            final_sig = self.SELL
        elif fund_sig == self.BUY:
            if not self._timing_enabled or timing_score >= self._timing_threshold:
                final_sig = self.BUY
            else:
                final_sig = self.HOLD
        else:
            final_sig = self.HOLD

        details = fund_result.get("details", {})
        details["timing_score"] = round(timing_score, 2)
        details["timing_filter"] = "PASS" if final_sig == self.BUY else ("BLOCKED" if fund_sig == self.BUY else "N/A")

        return {
            "signal": final_sig,
            "score": fund_score,
            "details": details,
            "close": fund_result.get("close", 0),
            "date": fund_result.get("date"),
            "atr": fund_result.get("atr", 0),
            "source": fund_result.get("source", ""),
        }
