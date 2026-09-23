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
    auto_downgrade: 행마다 직전 independence_window행(당일 포함)의 구성 신호 상관이
      |r| >= independence_threshold이면 그 행만 conservative로 판정한다.
      (예전에는 첫 analyze 호출의 신호 전체로 인스턴스 모드를 한 번 바꿨다 —
       포트폴리오 백테스트에서는 첫 종목의 전 기간(미래 신호 포함)이 모든 날짜·종목의
       모드를 정해 결과가 종목 순서에 따라 달라졌다.)
    """

    def __init__(self, config: Config = None, skip_independence_check: bool = False):
        self.config = config or Config.get()
        self.strategies_config = self.config.strategies
        ensemble_cfg = self.strategies_config.get("ensemble", {})
        self._ensemble_cfg = ensemble_cfg
        self._configured_mode = ensemble_cfg.get("mode", "majority_vote")
        # self.mode는 설정 모드 그대로 둔다. 행별 전환은 analyze()의 ensemble_mode 컬럼에 남는다.
        self.mode = self._configured_mode
        self.auto_downgrade = ensemble_cfg.get("auto_downgrade", True)
        self.confidence_weights = ensemble_cfg.get("confidence_weight", {})
        self._independence_window = max(2, int(ensemble_cfg.get("independence_window", 60)))
        self._independence_threshold = float(ensemble_cfg.get("independence_threshold", 0.6))
        self._downgraded = False            # 한 행이라도 conservative로 전환된 적 있음 (진단용)
        self._check_error_logged = False
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

        # 고상관 자동 전환: 행마다 그 행까지의 직전 window행 신호만 본다 (미래 신호·종목 순서 무관)
        row_modes = self._row_modes(base_df)
        base_df["ensemble_mode"] = row_modes

        skip_cols = [f"ensemble_skip_{n}" for n, _, _ in self._strategies]
        meta = base_df[skip_cols] if skip_cols else pd.DataFrame(index=base_df.index)

        base_df["signal"] = [
            self._resolve_row_signal(
                signal_frame.iloc[i],
                meta.iloc[i] if len(meta.columns) else None,
                row_modes.iloc[i],
            )
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

    def _row_modes(self, base_df: pd.DataFrame) -> pd.Series:
        """행별 판정 모드. auto_downgrade 조건을 만족하는 행만 conservative."""
        modes = pd.Series(self.mode, index=base_df.index, dtype=object)
        if (
            self._skip_independence_check
            or not self.auto_downgrade
            or self.mode not in ("majority_vote", "weighted_sum")
        ):
            return modes
        try:
            high = self._trailing_high_correlation(base_df)
        except Exception as e:  # 진단 실패로 분석 전체를 멈추지 않되, 조용히 넘기지 않는다
            if not self._check_error_logged:
                logger.warning(
                    "앙상블 독립성 검사 실패 — 설정 모드({})로 판정합니다 (auto_downgrade 미적용): {}",
                    self.mode, e,
                )
                self._check_error_logged = True
            return modes
        if high.any():
            modes[high] = "conservative"
            if not self._downgraded:
                logger.warning(
                    "앙상블 모드 자동 전환: 직전 {}행 구성 신호 상관 |r| >= {:.2f}인 {}개 행을 "
                    "{} 대신 conservative로 판정 (참여 전략이 모두 동의할 때만 BUY/SELL). "
                    "전략 구성 재검토 권장. auto_downgrade: false로 비활성화 가능.",
                    self._independence_window,
                    self._independence_threshold,
                    int(high.sum()),
                    self.mode,
                )
            self._downgraded = True
        return modes

    def _trailing_high_correlation(self, base_df: pd.DataFrame) -> pd.Series:
        """행마다 직전 window행(당일 포함) 신호 상관이 임계 이상인 구성 쌍이 있는지.

        롤링 창이라 각 행의 판정은 그 행까지의 신호만 쓴다. 창 안에서 한쪽 신호가
        변하지 않으면(예: 데이터 없는 펀더멘털 HOLD) 상관을 정의할 수 없어 제외한다.
        """
        from core.ensemble_correlation import SIGNAL_TO_NUM, ENSEMBLE_SIGNAL_COLS

        high = pd.Series(False, index=base_df.index)
        cols = [c for c in ENSEMBLE_SIGNAL_COLS if c in base_df.columns]
        if len(cols) < 2:
            return high

        window = self._independence_window
        numeric = {
            c: base_df[c].map(lambda s: SIGNAL_TO_NUM.get(str(s).strip().upper(), 0)).astype(float)
            for c in cols
        }
        # 신호값이 -1/0/1이라 한 번이라도 바뀐 창의 표준편차는 0.1 이상이다. 부동소수
        # 잔차(1e-17 수준)를 '변동'으로 읽어 엉뚱한 상관이 나오지 않게 문턱을 둔다.
        varies = {
            c: numeric[c].rolling(window, min_periods=window).std() > 1e-9 for c in cols
        }
        for i, c1 in enumerate(cols):
            for c2 in cols[i + 1:]:
                r = numeric[c1].rolling(window, min_periods=window).corr(numeric[c2])
                valid = varies[c1] & varies[c2] & r.notna()
                high = high | (valid & (r.abs() >= self._independence_threshold))
        return high

    def generate_signal(self, df: pd.DataFrame, **kwargs) -> dict:
        """각 전략 신호를 수집 후 앙상블 모드에 따라 통합 신호 반환"""
        if not self._strategies:
            return {"signal": "HOLD", "score": 0, "details": {"ensemble": "전략 없음"}}

        analyzed = self.analyze(df)
        if analyzed.empty:
            return {"signal": "HOLD", "score": 0, "details": {"ensemble": "분석 결과 없음"}}

        last = analyzed.iloc[-1]
        details = {"ensemble_mode": last.get("ensemble_mode", self.mode)}
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

    def _resolve_row_signal(
        self,
        signal_row: pd.Series,
        skip_row: pd.Series | None,
        mode: str | None = None,
    ) -> str:
        parts = self._participating(signal_row, skip_row)
        if not parts:
            return "HOLD"
        mode = mode or self.mode
        if mode == "conservative":
            return self._resolve_conservative(parts)
        if mode == "weighted_sum":
            return self._resolve_weighted_sum(parts)
        return self._resolve_majority_vote(parts)

    def _resolve_majority_vote(self, parts: list[tuple[str, str, float]]) -> str:
        """다수결: 가장 많은 신호 선택. 최다 득표가 동률이면 다수가 없으므로 HOLD.

        (예: 4개 구성에서 BUY 2 / SELL 2면 다수 없음 → 첫 구성 방향으로 매매하지
        않고 HOLD로 둔다.)
        """
        from collections import Counter

        votes = [p[1] for p in parts]
        if not votes:
            return "HOLD"
        ranked = Counter(votes).most_common()
        if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
            return "HOLD"
        return ranked[0][0]

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
