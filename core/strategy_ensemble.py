"""
전략 앙상블 모듈
- 복수 전략의 신호를 다수결/가중합/보수적 방식으로 통합
- 정보 소스 분리: 기술적 지표 + 모멘텀 + 변동성 (+ 선택 시 펀더멘털)
"""

import pandas as pd
from loguru import logger

from config.config_loader import Config


# 신호를 수치로 (가중합용)
SIGNAL_TO_VALUE = {"BUY": 1, "HOLD": 0, "SELL": -1}
VALUE_TO_SIGNAL = {1: "BUY", 0: "HOLD", -1: "SELL"}

# name → (module, class_name) — strategies.yaml ensemble.components.name 과 일치
_COMPONENT_CLASSES: dict[str, tuple[str, str]] = {
    "technical": ("strategies.scoring_strategy", "ScoringStrategy"),
    "momentum_factor": ("strategies.momentum_factor", "MomentumFactorStrategy"),
    "volatility_condition": ("strategies.volatility_condition", "VolatilityConditionStrategy"),
    "fundamental_factor": ("strategies.fundamental_factor", "FundamentalFactorStrategy"),
}


def _default_components(ensemble_cfg: dict) -> list[dict]:
    """components 미설정 시 (레거시) confidence_weight 기반 기본 3구성 — 펀더멘털은 components에 명시 시만 포함."""
    cw = ensemble_cfg.get("confidence_weight") or {}
    return [
        {"name": "technical", "enabled": True, "weight": float(cw.get("technical", 1.0))},
        {"name": "momentum_factor", "enabled": True, "weight": float(cw.get("momentum_factor", 1.0))},
        {"name": "volatility_condition", "enabled": True, "weight": float(cw.get("volatility_condition", 1.0))},
    ]


def _parse_components(ensemble_cfg: dict) -> list[tuple[str, float]]:
    """활성화된 (전략명, 가중치) 목록."""
    raw = ensemble_cfg.get("components")
    if not raw or not isinstance(raw, list):
        raw = _default_components(ensemble_cfg)
    out: list[tuple[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name or not item.get("enabled", True):
            continue
        w = float(item.get("weight", 1.0))
        out.append((str(name), w))
    return out


class StrategyEnsemble:
    """
    전략 앙상블: 서로 다른 정보 소스를 통합.

    - technical: 스코어링(RSI, MACD, 볼린저, MA, 거래량)
    - momentum_factor: N일 수익률
    - volatility_condition: 실현변동성 구간
    - fundamental_factor: 펀더멘털(가격 독립, 선택)

    구성·가중치는 strategies.yaml → ensemble.components 로 켜고 끌 수 있음.
    fundamental_factor는 데이터 부재 시 ensemble_skip=True 로 집계에서 제외.

    모드: majority_vote | weighted_sum | conservative
    auto_downgrade: 고상관 감지 시 majority_vote/weighted_sum → conservative 자동 전환
    """

    def __init__(self, config: Config = None, skip_independence_check: bool = False):
        self.config = config or Config.get()
        self.strategies_config = self.config.strategies
        ensemble_cfg = self.strategies_config.get("ensemble", {})
        self._ensemble_cfg = ensemble_cfg
        self._configured_mode = ensemble_cfg.get("mode", "majority_vote")
        self.mode = self._configured_mode
        self.auto_downgrade = ensemble_cfg.get("auto_downgrade", True)
        self.confidence_weights = ensemble_cfg.get("confidence_weight", {})
        self._independence_checked = False
        self._downgraded = False
        self._strategies: list[tuple[str, object, float]] = []
        self._load_strategies()
        self._skip_independence_check = skip_independence_check
        logger.info(
            "StrategyEnsemble 초기화 (모드: {}, 전략 수: {}, auto_downgrade: {})",
            self.mode,
            len(self._strategies),
            self.auto_downgrade,
        )

    def _load_strategies(self):
        """ensemble.components 에 따라 전략 인스턴스 로드."""
        import importlib

        specs = _parse_components(self._ensemble_cfg)
        for name, weight in specs:
            mod_cls = _COMPONENT_CLASSES.get(name)
            if not mod_cls:
                logger.warning("앙상블 알 수 없는 구성 이름 스킵: {}", name)
                continue
            mod_path, cls_name = mod_cls
            try:
                mod = importlib.import_module(mod_path)
                cls = getattr(mod, cls_name)
                self._strategies.append((name, cls(self.config), weight))
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
        for name, strategy, _w in self._strategies:
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
            signal_frame[name] = strat_df.get("signal", pd.Series("HOLD", index=base_df.index)).reindex(
                base_df.index
            ).fillna("HOLD")
            score_frame[name] = strat_df.get("strategy_score", pd.Series(0.0, index=base_df.index)).reindex(
                base_df.index
            ).fillna(0.0)
            base_df[f"signal_{name}"] = signal_frame[name]
            base_df[f"score_{name}"] = score_frame[name]
            skip = strat_df.get("ensemble_skip")
            if skip is not None:
                base_df[f"ensemble_skip_{name}"] = skip.reindex(base_df.index).fillna(False).astype(bool)
            else:
                base_df[f"ensemble_skip_{name}"] = False

        for name, _, _ in self._strategies:
            col = f"ensemble_skip_{name}"
            if col not in base_df.columns:
                base_df[col] = False

        # 첫 analyze 호출 시 독립성 검사 → 고상관이면 conservative로 자동 다운그레이드
        if not self._independence_checked and not self._skip_independence_check and len(base_df) >= 60:
            self._run_independence_check(base_df)

        skip_cols = [f"ensemble_skip_{n}" for n, _, _ in self._strategies]
        meta = base_df[skip_cols] if skip_cols else pd.DataFrame(index=base_df.index)

        base_df["signal"] = [
            self._resolve_row_signal(signal_frame.iloc[i], meta.iloc[i] if len(meta.columns) else None)
            for i in range(len(signal_frame))
        ]
        base_df["strategy_score"] = [
            self._mean_participating_score(score_frame.iloc[i], meta.iloc[i] if len(meta.columns) else None, score_frame.columns)
            for i in range(len(score_frame))
        ]
        return base_df

    def _mean_participating_score(
        self,
        score_row: pd.Series,
        skip_row: pd.Series | None,
        score_columns: pd.Index,
    ) -> float:
        vals = []
        for name in score_columns:
            if skip_row is not None and skip_row.get(f"ensemble_skip_{name}", False):
                continue
            v = score_row.get(name, 0.0)
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                vals.append(0.0)
        return float(sum(vals) / len(vals)) if vals else 0.0

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
                    l1,
                    l2,
                    r,
                    threshold,
                )

            if self.auto_downgrade and self.mode in ("majority_vote", "weighted_sum"):
                old_mode = self.mode
                self.mode = "conservative"
                self._downgraded = True
                logger.warning(
                    "앙상블 모드 자동 전환: {} → conservative (고상관 쌍 {}개 감지). "
                    "참여 전략이 모두 동의할 때만 BUY/SELL. 전략 구성 재검토 권장. "
                    "auto_downgrade: false로 비활성화 가능.",
                    old_mode,
                    len(high_pairs),
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
        details = {}
        for name, _, _ in self._strategies:
            details[name] = last.get(f"signal_{name}", "ERR")
            sk = last.get(f"ensemble_skip_{name}", False)
            if sk:
                details[f"{name}_skipped"] = True

        return {
            "signal": last.get("signal", "HOLD"),
            "score": last.get("strategy_score", 0),
            "details": details,
            "close": last.get("close", 0),
            "atr": last.get("atr", 0),
            "date": last.name if hasattr(last, "name") else None,
        }

    def _participating(
        self, signal_row: pd.Series, skip_row: pd.Series | None
    ) -> list[tuple[str, str, float]]:
        out = []
        for name, _strat, weight in self._strategies:
            if skip_row is not None and bool(skip_row.get(f"ensemble_skip_{name}", False)):
                continue
            sig = signal_row.get(name, "HOLD")
            out.append((name, sig, weight))
        return out

    def _resolve_row_signal(self, signal_row: pd.Series, skip_row: pd.Series | None) -> str:
        parts = self._participating(signal_row, skip_row)
        if not parts:
            return "HOLD"
        if self.mode == "conservative":
            return self._resolve_conservative(parts)
        if self.mode == "weighted_sum":
            return self._resolve_weighted_sum(parts)
        return self._resolve_majority_vote(parts)

    def _resolve_majority_vote(self, parts: list[tuple[str, str, float]]) -> str:
        """다수결: 가장 많은 신호 선택"""
        from collections import Counter

        votes = [p[1] for p in parts]
        if not votes:
            return "HOLD"
        count = Counter(votes)
        return count.most_common(1)[0][0]

    def _resolve_weighted_sum(self, parts: list[tuple[str, str, float]]) -> str:
        """가중합: 전략별 가중치 * 신호값 합산 후 임계값으로 판단"""
        weighted = 0.0
        total_w = 0.0
        for _name, sig, w in parts:
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

    def _resolve_conservative(self, parts: list[tuple[str, str, float]]) -> str:
        """보수적: 참여 전략이 모두 같은 신호일 때만 해당 신호, 아니면 HOLD"""
        votes = [p[1] for p in parts]
        if not votes:
            return "HOLD"
        if all(v == "BUY" for v in votes):
            return "BUY"
        if all(v == "SELL" for v in votes):
            return "SELL"
        return "HOLD"
