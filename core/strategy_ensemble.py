"""
전략 앙상블 모듈
- 복수 전략의 신호를 다수결/가중합/보수적 방식으로 통합
- 정보 소스 분리: 기술적 지표 + 모멘텀 팩터 + 변동성 조건 (quant_trader_design.md §4.4)
"""

import pandas as pd
from loguru import logger

from config.config_loader import Config


# 신호를 수치로 (가중합용)
SIGNAL_TO_VALUE = {"BUY": 1, "HOLD": 0, "SELL": -1}
VALUE_TO_SIGNAL = {1: "BUY", 0: "HOLD", -1: "SELL"}


class StrategyEnsemble:
    """
    전략 앙상블: 서로 다른 정보 소스 세 가지를 통합.

    - technical: 기술적 지표(RSI, MACD, 볼린저, 거래량, MA) 기반 스코어링
    - momentum_factor: 가격 모멘텀(N일 수익률)만 사용
    - volatility_condition: 실현변동성 구간만 사용 (저변동성=매수, 고변동성=매도)

    모드: majority_vote | weighted_sum | conservative
    auto_downgrade: 고상관 감지 시 majority_vote/weighted_sum → conservative 자동 전환
    """

    def __init__(self, config: Config = None, skip_independence_check: bool = False):
        self.config = config or Config.get()
        self.strategies_config = self.config.strategies
        ensemble_cfg = self.strategies_config.get("ensemble", {})
        self._configured_mode = ensemble_cfg.get("mode", "majority_vote")
        self.mode = self._configured_mode
        self.auto_downgrade = ensemble_cfg.get("auto_downgrade", True)
        self.confidence_weights = ensemble_cfg.get("confidence_weight", {})
        self._independence_checked = False
        self._downgraded = False
        self._strategies = []
        self._load_strategies()
        self._skip_independence_check = skip_independence_check
        logger.info("StrategyEnsemble 초기화 (모드: {}, 전략 수: {}, auto_downgrade: {})",
                     self.mode, len(self._strategies), self.auto_downgrade)

    def _load_strategies(self):
        """전략 인스턴스 로드 — 정보 소스가 다른 세 전략"""
        from strategies.scoring_strategy import ScoringStrategy
        from strategies.momentum_factor import MomentumFactorStrategy
        from strategies.volatility_condition import VolatilityConditionStrategy

        for name, cls in [
            ("technical", ScoringStrategy),
            ("momentum_factor", MomentumFactorStrategy),
            ("volatility_condition", VolatilityConditionStrategy),
        ]:
            try:
                self._strategies.append((name, cls(self.config)))
            except Exception as e:
                logger.warning("전략 {} 로드 스킵: {}", name, e)

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        """하위 전략 신호를 합성한 signal 컬럼을 포함한 DataFrame 반환."""
        if not self._strategies:
            analyzed = df.copy()
            analyzed["signal"] = "HOLD"
            analyzed["strategy_score"] = 0.0
            return analyzed

        analyzed_frames = {}
        base_df = None
        for name, strategy in self._strategies:
            try:
                strat_df = strategy.analyze(df.copy())
                if strat_df.empty:
                    continue
                if base_df is None:
                    base_df = strat_df.copy()
                analyzed_frames[name] = strat_df
            except Exception as e:
                logger.warning("전략 {} 분석 실패: {}", name, e)

        if base_df is None:
            analyzed = df.copy()
            analyzed["signal"] = "HOLD"
            analyzed["strategy_score"] = 0.0
            return analyzed

        signal_frame = pd.DataFrame(index=base_df.index)
        score_frame = pd.DataFrame(index=base_df.index)

        for name, strat_df in analyzed_frames.items():
            signal_frame[name] = strat_df.get("signal", pd.Series("HOLD", index=base_df.index)).reindex(base_df.index).fillna("HOLD")
            score_frame[name] = strat_df.get("strategy_score", pd.Series(0.0, index=base_df.index)).reindex(base_df.index).fillna(0.0)
            base_df[f"signal_{name}"] = signal_frame[name]
            base_df[f"score_{name}"] = score_frame[name]

        # 첫 analyze 호출 시 독립성 검사 → 고상관이면 conservative로 자동 다운그레이드
        if not self._independence_checked and not self._skip_independence_check and len(base_df) >= 60:
            self._run_independence_check(base_df)

        base_df["signal"] = signal_frame.apply(self._resolve_row_signal, axis=1)
        base_df["strategy_score"] = score_frame.mean(axis=1).fillna(0.0)
        return base_df

    def _run_independence_check(self, analyzed_df: pd.DataFrame):
        """첫 analyze 호출 시 전략 신호 독립성을 검사하고, 필요 시 모드를 다운그레이드한다."""
        self._independence_checked = True
        try:
            from core.ensemble_correlation import SIGNAL_TO_NUM, ENSEMBLE_SIGNAL_COLS, ENSEMBLE_LABELS

            cols = [c for c in ENSEMBLE_SIGNAL_COLS if c in analyzed_df.columns]
            if len(cols) < 2:
                return

            numeric = pd.DataFrame(index=analyzed_df.index)
            for c in cols:
                numeric[c] = analyzed_df[c].map(lambda s: SIGNAL_TO_NUM.get(str(s).strip().upper(), 0))

            corr = numeric[cols].corr()
            threshold = self.strategies_config.get("ensemble", {}).get("independence_threshold", 0.6)
            high_pairs = []
            for i, c1 in enumerate(cols):
                for j, c2 in enumerate(cols):
                    if i >= j:
                        continue
                    r = corr.loc[c1, c2]
                    if pd.notna(r) and abs(r) >= threshold:
                        high_pairs.append((c1, c2, float(r)))

            if not high_pairs:
                logger.info("앙상블 독립성 검사 통과: 모든 전략 쌍 |r| < {:.1f}", threshold)
                return

            for c1, c2, r in high_pairs:
                l1 = ENSEMBLE_LABELS.get(c1, c1)
                l2 = ENSEMBLE_LABELS.get(c2, c2)
                logger.warning(
                    "⚠️ 앙상블 독립성 위반: {}–{} 신호 상관계수 {:.2f} (>= {:.1f}). "
                    "다수결 의미 퇴색 위험.",
                    l1, l2, r, threshold,
                )

            if self.auto_downgrade and self.mode in ("majority_vote", "weighted_sum"):
                old_mode = self.mode
                self.mode = "conservative"
                self._downgraded = True
                logger.warning(
                    "앙상블 모드 자동 전환: {} → conservative (고상관 쌍 {}개 감지). "
                    "세 전략 모두 동의해야 BUY/SELL. 전략 구성 재검토 권장. "
                    "auto_downgrade: false로 비활성화 가능.",
                    old_mode, len(high_pairs),
                )
        except Exception as e:
            logger.debug("앙상블 독립성 검사 중 오류 (무시): {}", e)

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """각 전략 신호를 수집 후 앙상블 모드에 따라 통합 신호 반환"""
        if not self._strategies:
            return {"signal": "HOLD", "score": 0, "details": {"ensemble": "전략 없음"}}

        analyzed = self.analyze(df)
        if analyzed.empty:
            return {"signal": "HOLD", "score": 0, "details": {"ensemble": "분석 결과 없음"}}

        last = analyzed.iloc[-1]
        details = {
            name: last.get(f"signal_{name}", "ERR")
            for name, _ in self._strategies
        }

        return {
            "signal": last.get("signal", "HOLD"),
            "score": last.get("strategy_score", 0),
            "details": details,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
            "date": last.name if hasattr(last, "name") else None,
        }

    def _resolve_row_signal(self, row: pd.Series) -> str:
        names = [name for name, _ in self._strategies]
        signals = [(name, row.get(name, "HOLD"), 0) for name in names]
        if self.mode == "conservative":
            return self._resolve_conservative(signals)
        if self.mode == "weighted_sum":
            return self._resolve_weighted_sum(signals)
        return self._resolve_majority_vote(signals)

    def _resolve_majority_vote(self, signals: list) -> str:
        """다수결: 가장 많은 신호 선택"""
        from collections import Counter
        votes = [s[1] for s in signals]
        count = Counter(votes)
        return count.most_common(1)[0][0]

    def _resolve_weighted_sum(self, signals: list) -> str:
        """가중합: 전략별 가중치 * 신호값 합산 후 임계값으로 판단"""
        weighted = 0.0
        total_w = 0.0
        for name, sig, _ in signals:
            w = self.confidence_weights.get(name, 1.0)
            weighted += w * SIGNAL_TO_VALUE.get(sig, 0)
            total_w += w
        if total_w <= 0:
            return "HOLD"
        avg = weighted / total_w
        buy_th = self.strategies_config.get("ensemble", {}).get("weighted_buy_threshold", 0.3)
        sell_th = self.strategies_config.get("ensemble", {}).get("weighted_sell_threshold", -0.3)
        if avg >= buy_th:
            return "BUY"
        if avg <= sell_th:
            return "SELL"
        return "HOLD"

    def _resolve_conservative(self, signals: list) -> str:
        """보수적: 모든 전략이 같은 신호일 때만 해당 신호, 아니면 HOLD"""
        votes = [s[1] for s in signals]
        if all(v == "BUY" for v in votes):
            return "BUY"
        if all(v == "SELL" for v in votes):
            return "SELL"
        return "HOLD"
