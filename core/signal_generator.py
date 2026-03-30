"""
매매 신호 생성기
- 기술 지표 DataFrame을 입력받아 매수/매도/홀드 신호 생성
- 멀티 지표 스코어링 시스템
"""

import pandas as pd
import numpy as np
from loguru import logger

from config.config_loader import Config


class SignalGenerator:
    """
    매매 신호 생성기

    사용법:
        generator = SignalGenerator()
        signals = generator.generate(indicator_dataframe)
    """

    # 신호 상수
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    # 스코어링 가중치 필수 키 (YAML에 반드시 정의, 코드 내 기본값 없음)
    REQUIRED_WEIGHT_KEYS = (
        "rsi_oversold", "rsi_overbought",
        "macd_golden_cross", "macd_dead_cross",
        "bollinger_lower", "bollinger_upper",
        "volume_surge", "ma_golden_cross", "ma_dead_cross",
    )

    # 독립 정보 3그룹. 그룹 내 지표는 같은 정보의 변형이므로 가중치 중복 시 경고.
    COLLINEARITY_GROUPS = {
        "가격 모멘텀": {
            "weights": ["rsi_oversold", "macd_golden_cross", "ma_golden_cross"],
            "representative": "macd_golden_cross",
        },
        "변동성": {
            "weights": ["bollinger_lower"],
            "representative": "bollinger_lower",
        },
        "거래량": {
            "weights": ["volume_surge"],
            "representative": "volume_surge",
        },
    }

    def __init__(self, config: Config = None):
        self.config = config or Config.get()
        self.strategy_params = self.config.strategies
        self.indicator_params = self.config.indicators
        self._collinearity_mode = (
            self.strategy_params.get("scoring", {})
            .get("collinearity_mode", "max_per_direction")
        )
        scoring = self.strategy_params.get("scoring", {})
        hyst = scoring.get("hysteresis", {})
        self._hysteresis_enabled = hyst.get("enabled", False)
        self._exit_sell_threshold = hyst.get("exit_sell_threshold", None)
        self._exit_buy_threshold = hyst.get("exit_buy_threshold", None)

        self._dynamic_threshold_enabled = bool(scoring.get("dynamic_threshold", False))
        self._dynamic_atr_high = float(scoring.get("dynamic_threshold_atr_high", 1.5))
        self._dynamic_atr_low = float(scoring.get("dynamic_threshold_atr_low", 0.7))

        self._warn_price_momentum_collinearity()
        self._diagnose_weight_collinearity()
        logger.info(
            "SignalGenerator 초기화 완료 (collinearity_mode={}, hysteresis={}, dynamic_threshold={})",
            self._collinearity_mode,
            self._hysteresis_enabled,
            self._dynamic_threshold_enabled,
        )

    def _warn_price_momentum_collinearity(self):
        """가격 모멘텀 그룹(RSI, MACD, MA) 다중 활성화 시 권장 모드 경고."""
        try:
            weights = self._get_weights()
        except KeyError:
            return

        active_indicators = []
        if abs(weights.get("rsi_oversold", 0)) > 0 or abs(weights.get("rsi_overbought", 0)) > 0:
            active_indicators.append("RSI")
        if abs(weights.get("macd_golden_cross", 0)) > 0 or abs(weights.get("macd_dead_cross", 0)) > 0:
            active_indicators.append("MACD")
        if abs(weights.get("ma_golden_cross", 0)) > 0 or abs(weights.get("ma_dead_cross", 0)) > 0:
            active_indicators.append("MA")

        if len(active_indicators) >= 2:
            logger.warning(
                "[SignalGenerator] 다중공선성 경고: 가격 모멘텀 그룹에서 {}이 모두 활성화되어 있습니다. "
                "collinearity_mode: representative_only 설정을 권장합니다.",
                ", ".join(active_indicators),
            )

    def _diagnose_weight_collinearity(self):
        """현재 가중치 설정에서 다중공선성 위험을 진단하고 경고 로그 출력."""
        try:
            weights = self._get_weights()
        except KeyError:
            return

        for group_name, info in self.COLLINEARITY_GROUPS.items():
            active = [
                w for w in info["weights"]
                if abs(weights.get(w, 0)) > 0
            ]
            if len(active) > 1:
                rep = info["representative"]
                others = [w for w in active if w != rep]
                logger.warning(
                    "⚠️ 다중공선성: {} 그룹에서 {}개 지표가 동시 활성 ({}). "
                    "같은 정보를 중복 측정 중. 대표 지표({})만 남기고 나머지({})를 "
                    "가중치 0 또는 --mode optimize --auto-correlation 으로 자동 정리 권장.",
                    group_name, len(active), active, rep, others,
                )

    def _get_weights(self) -> dict:
        """스코어링 가중치 dict 반환. 미설정·필수 키 누락·값 이상 시 예외."""
        weights = (self.strategy_params.get("scoring") or {}).get("weights")
        if not weights:
            raise KeyError(
                "config/strategies.yaml에 scoring.weights 섹션이 없습니다. "
                "가중치를 완전히 외부화하려면 해당 섹션을 정의하세요."
            )
        missing = [k for k in self.REQUIRED_WEIGHT_KEYS if k not in weights]
        if missing:
            raise KeyError(
                f"scoring.weights에 필수 키가 없습니다: {missing}. "
                "strategies.yaml의 scoring.weights를 확인하세요."
            )
        self._validate_weights(weights)
        return weights

    @staticmethod
    def _validate_weights(weights: dict):
        """가중치 값 유효성 검증: 숫자·유한·합리적 범위."""
        import numpy as _np
        for key, val in weights.items():
            if val is None:
                raise ValueError(f"가중치 '{key}'가 None입니다.")
            if not isinstance(val, (int, float)):
                raise ValueError(f"가중치 '{key}'가 숫자가 아닙니다: {val!r} (type={type(val).__name__})")
            if not _np.isfinite(val):
                raise ValueError(f"가중치 '{key}'가 유한하지 않습니다: {val}")
            if abs(val) > 10:
                logger.warning(
                    "⚠️ 가중치 '{}'가 비정상적으로 큽니다: {}. 의도된 값인지 확인하세요.", key, val
                )

    # 그룹 → score 컬럼 매핑 (representative_only 모드용)
    _SCORE_GROUP_MAP = {
        "가격 모멘텀": {
            "columns": ["score_rsi", "score_macd", "score_ma"],
            "representative": "score_macd",
        },
        "변동성": {
            "columns": ["score_bollinger"],
            "representative": "score_bollinger",
        },
        "거래량": {
            "columns": ["score_volume"],
            "representative": "score_volume",
        },
    }

    def compute_score_columns_for_correlation(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        collinearity_mode 적용 **전** 일별 개별 점수(score_rsi 등)만 계산.

        `representative_only`에서는 generate() 이후 RSI·MA 점수가 0으로 고정되어
        상관행렬이 NaN이 되므로, 지표 독립성 검증(check_correlation)은 이 메서드 결과를 사용한다.
        """
        if df.empty:
            return pd.DataFrame(
                columns=["score_rsi", "score_macd", "score_bollinger", "score_volume", "score_ma"],
            )

        work = df.copy()
        out = pd.DataFrame(index=work.index)
        out["score_rsi"] = self._score_rsi(work)
        out["score_macd"] = self._score_macd(work)
        out["score_bollinger"] = self._score_bollinger(work)
        out["score_volume"] = self._score_volume(work)
        out["score_ma"] = self._score_ma(work)
        return out

    def generate(self, df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
        """
        멀티 지표 스코어링 방식으로 매매 신호 생성.

        collinearity_mode (strategies.yaml → scoring.collinearity_mode):
          - "max_per_direction" (기본): 가격 그룹(RSI/MACD/볼린저/MA) 점수를 방향별
            최대 1개만 반영 (매수=양수 max, 매도=음수 min). 완화 수준: 중간.
          - "representative_only": 3그룹(가격 모멘텀/변동성/거래량)에서 각 대표 지표
            1개만 사용하고 나머지 점수를 0으로 강제. 완화 수준: 강력.
            대표: MACD(가격 모멘텀), 볼린저(변동성), volume(거래량).

        Args:
            df: 기술 지표가 계산된 DataFrame
            symbol: 동적 임계값 조정 시 debug 로그용 종목코드 (미지정 시 "-")

        Returns:
            신호 컬럼(signal, score, score_price_group, score_volume 등)이 추가된 DataFrame
        """
        if df.empty:
            return df

        result = df.copy()

        # 각 개별 점수 계산
        result["score_rsi"] = self._score_rsi(result)
        result["score_macd"] = self._score_macd(result)
        result["score_bollinger"] = self._score_bollinger(result)
        result["score_volume"] = self._score_volume(result)
        result["score_ma"] = self._score_ma(result)

        mode = self._collinearity_mode

        if mode == "representative_only":
            result = self._apply_representative_only(result)
        else:
            result = self._apply_max_per_direction(result)

        # 매수/매도 임계값 (ATR 비율 기반 동적 매수 임계값 선택적 적용)
        scoring = self.strategy_params.get("scoring", {})
        base_buy_threshold = float(scoring.get("buy_threshold", 2))
        sell_threshold = float(scoring.get("sell_threshold", -2))

        buy_threshold, atr_ratio_series = self._resolve_buy_threshold_series(
            result, base_buy_threshold
        )
        self._log_dynamic_threshold_adjustments(
            symbol or "-", base_buy_threshold, buy_threshold, atr_ratio_series,
        )

        # 신호 생성
        if self._hysteresis_enabled:
            result = self._generate_with_hysteresis(result, buy_threshold, sell_threshold)
        else:
            result["signal"] = self.HOLD
            result.loc[result["total_score"] >= buy_threshold, "signal"] = self.BUY
            result.loc[result["total_score"] <= sell_threshold, "signal"] = self.SELL

        logger.info(
            "신호 생성 완료 (collinearity={}, hysteresis={}) — 매수: {}건, 매도: {}건, 홀드: {}건",
            mode, self._hysteresis_enabled,
            (result["signal"] == self.BUY).sum(),
            (result["signal"] == self.SELL).sum(),
            (result["signal"] == self.HOLD).sum(),
        )

        return result

    def _resolve_buy_threshold_series(
        self, df: pd.DataFrame, base_buy: float,
    ) -> tuple[pd.Series, pd.Series | None]:
        """
        기본 buy_threshold를 시리즈로 두고, 동적 모드이고 atr 컬럼이 있으면 행별 조정.
        atr_ratio = ATR / 최근 20일 ATR 평균 (rolling, min_periods=20).
        """
        n = len(df)
        idx = df.index
        buy_series = pd.Series(base_buy, index=idx, dtype=float)
        if not self._dynamic_threshold_enabled or n == 0:
            return buy_series, None
        if "atr" not in df.columns:
            return buy_series, None

        atr = pd.to_numeric(df["atr"], errors="coerce").astype(float)
        mean20 = atr.rolling(window=20, min_periods=20).mean()
        ratio = atr / mean20.replace(0, np.nan)

        adj = buy_series.copy()
        valid = ratio.notna()
        high_m = valid & (ratio >= self._dynamic_atr_high)
        low_m = valid & (ratio <= self._dynamic_atr_low)
        adj = adj.where(~high_m, base_buy + 1.0)
        adj = adj.where(~low_m, base_buy - 0.5)
        return adj, ratio

    def _log_dynamic_threshold_adjustments(
        self,
        symbol: str,
        base_buy: float,
        buy_threshold: pd.Series,
        atr_ratio: pd.Series | None,
    ) -> None:
        if atr_ratio is None or buy_threshold is None:
            return
        diff = (buy_threshold != base_buy) & atr_ratio.notna()
        if not diff.any():
            return
        for idx in buy_threshold.loc[diff].index:
            logger.debug(
                "[SignalGenerator] {} 동적 임계값 조정: ATR 비율={:.2f}, buy_threshold {} → {}",
                symbol,
                float(atr_ratio.loc[idx]),
                base_buy,
                float(buy_threshold.loc[idx]),
            )

    def _generate_with_hysteresis(
        self,
        df: pd.DataFrame,
        buy_threshold: float | pd.Series,
        sell_threshold: float,
    ) -> pd.DataFrame:
        """
        히스터리시스 적용 신호 생성.

        상태 전이는 반드시 BUY ↔ HOLD ↔ SELL 순서를 따릅니다.
        BUY → SELL 또는 SELL → BUY 직접 전환은 허용하지 않아 과매매를 방지합니다.

        상태 전이 규칙:
          HOLD → BUY:  score >= buy_threshold
          HOLD → SELL: score <= sell_threshold
          BUY  → HOLD: score < exit_buy_threshold (BUY 유지 해제)
          SELL → HOLD: score > -exit_sell_threshold (SELL 유지 해제)
        """
        exit_sell = self._exit_sell_threshold if self._exit_sell_threshold is not None else sell_threshold
        exit_buy = self._exit_buy_threshold if self._exit_buy_threshold is not None else 0.0

        scores = df["total_score"].values
        if isinstance(buy_threshold, pd.Series):
            buy_thr_arr = buy_threshold.reindex(df.index).astype(float).values
        else:
            buy_thr_arr = np.full(len(scores), float(buy_threshold), dtype=float)

        signals = np.full(len(scores), self.HOLD, dtype=object)
        state = self.HOLD

        for i in range(len(scores)):
            s = scores[i]
            bt = buy_thr_arr[i]
            if state == self.HOLD:
                if s >= bt:
                    state = self.BUY
                elif s <= sell_threshold:
                    state = self.SELL
            elif state == self.BUY:
                if s < exit_buy:
                    state = self.HOLD
            elif state == self.SELL:
                if s > (-exit_sell):
                    state = self.HOLD
            signals[i] = state

        df["signal"] = signals
        return df

    def _apply_max_per_direction(self, result: pd.DataFrame) -> pd.DataFrame:
        """기본 모드: 가격 그룹에서 방향별 최대 1개만 반영."""
        price_columns = ["score_rsi", "score_macd", "score_bollinger", "score_ma"]
        price_df = result[price_columns]
        buy_side = price_df.clip(lower=0).max(axis=1)
        sell_side = price_df.clip(upper=0).min(axis=1)
        result["score_price_group"] = buy_side + sell_side
        result["total_score"] = result["score_price_group"] + result["score_volume"]
        return result

    def _apply_representative_only(self, result: pd.DataFrame) -> pd.DataFrame:
        """강력 모드: 3그룹 각 대표 지표 1개만 사용, 나머지 점수 0 강제."""
        total = pd.Series(0.0, index=result.index)
        for group_name, info in self._SCORE_GROUP_MAP.items():
            rep_col = info["representative"]
            if rep_col in result.columns:
                total += result[rep_col]
            for col in info["columns"]:
                if col != rep_col and col in result.columns:
                    result[col] = 0.0
        result["score_price_group"] = total - result.get("score_volume", 0.0)
        result["total_score"] = total
        return result

    def get_latest_signal(self, df: pd.DataFrame) -> dict:
        """
        최신(마지막 행) 신호 정보 반환

        Returns:
            {
                "signal": "BUY" / "SELL" / "HOLD",
                "score": 총점,
                "details": 개별 점수 딕셔너리,
                "date": 날짜,
                "close": 종가,
            }
        """
        if df.empty:
            return {"signal": self.HOLD, "score": 0, "details": {}}

        last = df.iloc[-1]

        return {
            "signal": last.get("signal", self.HOLD),
            "score": last.get("total_score", 0),
            "details": {
                "가격그룹": last.get("score_price_group", 0),  # RSI/MACD/볼린저/MA 방향별 최대 1개
                "RSI": last.get("score_rsi", 0),
                "MACD": last.get("score_macd", 0),
                "볼린저": last.get("score_bollinger", 0),
                "거래량": last.get("score_volume", 0),
                "이동평균": last.get("score_ma", 0),
            },
            "date": last.name if hasattr(last, "name") else None,
            "close": last.get("close", 0),
            "rsi": last.get("rsi", 0),
            "adx": last.get("adx", 0),
            "atr": last.get("atr", 0),
        }

    # =============================================================
    # 개별 지표 점수 계산
    # =============================================================

    def _score_rsi(self, df: pd.DataFrame) -> pd.Series:
        """
        RSI 점수 계산
        - RSI < 30 (과매도) → +2점
        - RSI > 70 (과매수) → -2점
        - 중간 → 0점
        """
        rsi_params = self.indicator_params.get("rsi", {})
        oversold = rsi_params.get("oversold", 30)
        overbought = rsi_params.get("overbought", 70)

        weights = self._get_weights()
        buy_weight = weights["rsi_oversold"]
        sell_weight = weights["rsi_overbought"]

        score = pd.Series(0.0, index=df.index)

        if "rsi" in df.columns:
            score = score.where(~(df["rsi"] < oversold), buy_weight)
            score = score.where(~(df["rsi"] > overbought), sell_weight)

        return score

    def _score_macd(self, df: pd.DataFrame) -> pd.Series:
        """
        MACD 점수 계산 (3단계 점수 체계)
        - 골든크로스 당일: buy_weight (기본 +2)
        - MACD > Signal 유지 중: buy_weight × 0.5 (기본 +1) — 상승 추세 지속
        - 데드크로스 당일: sell_weight (기본 -2)
        - MACD < Signal 유지 중: sell_weight × 0.5 (기본 -1) — 하락 추세 지속
        - 히스토그램 방향 전환 보너스: ±0.5 (기존과 동일)
        """
        weights = self._get_weights()
        buy_weight = weights["macd_golden_cross"]
        sell_weight = weights["macd_dead_cross"]

        score = pd.Series(0.0, index=df.index)

        if "macd" in df.columns and "macd_signal" in df.columns:
            macd_above = df["macd"] > df["macd_signal"]
            macd_above_prev = macd_above.shift(1, fill_value=False)
            golden_cross = macd_above & (~macd_above_prev)
            dead_cross = (~macd_above) & macd_above_prev

            # 기본: MACD > Signal 유지 중 절반 점수
            score[macd_above] = buy_weight * 0.5
            score[~macd_above] = sell_weight * 0.5

            # 크로스 당일은 풀 점수로 덮어쓰기
            score[golden_cross] = buy_weight
            score[dead_cross] = sell_weight

            # 히스토그램 방향 보너스 (크로스 아닌 날만)
            if "macd_histogram" in df.columns:
                hist_positive = df["macd_histogram"] > 0
                hist_turning_up = (
                    df["macd_histogram"] > df["macd_histogram"].shift(1)
                ) & hist_positive
                hist_turning_down = (
                    df["macd_histogram"] < df["macd_histogram"].shift(1)
                ) & (~hist_positive)

                is_cross_day = golden_cross | dead_cross
                score = score.where(~(~is_cross_day & hist_turning_up), score + 0.5)
                score = score.where(~(~is_cross_day & hist_turning_down), score - 0.5)

        return score

    def _score_bollinger(self, df: pd.DataFrame) -> pd.Series:
        """
        볼린저 밴드 점수 계산
        - 종가 < 하단 밴드 → +1점 (과매도)
        - 종가 > 상단 밴드 → -1점 (과매수)
        """
        weights = self._get_weights()
        buy_weight = weights["bollinger_lower"]
        sell_weight = weights["bollinger_upper"]

        score = pd.Series(0.0, index=df.index)

        if "bb_lower" in df.columns and "bb_upper" in df.columns:
            score = score.where(~(df["close"] < df["bb_lower"]), buy_weight)
            score = score.where(~(df["close"] > df["bb_upper"]), sell_weight)

        return score

    def _score_volume(self, df: pd.DataFrame) -> pd.Series:
        """
        거래량 점수 계산
        - 거래량이 평균 대비 150% 이상이면 추세 확인 신호
        - 가격 상승 + 거래량 급증 → +1점
        - 가격 하락 + 거래량 급증 → -1점
        """
        surge_ratio = self.indicator_params.get("volume", {}).get("surge_ratio", 1.5)
        weight = self._get_weights()["volume_surge"]

        score = pd.Series(0.0, index=df.index)

        if "volume_ratio" in df.columns:
            volume_surge = df["volume_ratio"] > surge_ratio
            price_up = df["close"] > df["close"].shift(1)

            score = score.where(~(volume_surge & price_up), weight)
            score = score.where(~(volume_surge & ~price_up), -weight)

        return score

    def _score_ma(self, df: pd.DataFrame) -> pd.Series:
        """
        이동평균 점수 계산
        - 5일선이 20일선을 상향 돌파 (골든크로스) → +1점
        - 5일선이 20일선을 하향 돌파 (데드크로스) → -1점
        """
        weights = self._get_weights()
        buy_weight = weights["ma_golden_cross"]
        sell_weight = weights["ma_dead_cross"]

        score = pd.Series(0.0, index=df.index)

        sma_short = None
        sma_mid = None

        # SMA 컬럼 찾기
        for col in df.columns:
            if col.startswith("sma_5") or col.startswith("ema_5"):
                sma_short = col
            if col.startswith("sma_20") or col.startswith("ema_20"):
                sma_mid = col

        if sma_short and sma_mid and sma_short in df.columns and sma_mid in df.columns:
            short_above = df[sma_short] > df[sma_mid]
            short_above_prev = short_above.shift(1, fill_value=False)
            golden_cross = short_above & (~short_above_prev)
            dead_cross = (~short_above) & short_above_prev

            score[golden_cross] = buy_weight
            score[dead_cross] = sell_weight

        return score
