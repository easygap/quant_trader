"""스코어링 신호 감사 회귀 테스트 — MACD 워밍업 NaN 처리.

예전 _score_macd는 NaN 비교(False)를 'Signal 아래'로 읽어, 시그널선이 나오기 전
워밍업 구간 전체를 -1로 채점하고 첫 유효 봉을 가짜 골든크로스(+2)로 만들었다.
"""

import numpy as np
import pandas as pd

from core.signal_generator import SignalGenerator


_WEIGHTS = {
    "rsi_oversold": 2,
    "rsi_overbought": -2,
    "macd_golden_cross": 2,
    "macd_dead_cross": -2,
    "bollinger_lower": 1,
    "bollinger_upper": -1,
    "volume_surge": 1,
    "ma_golden_cross": 1,
    "ma_dead_cross": -1,
}


class _Cfg:
    """운영 설정과 같은 모드(representative_only + 히스터리시스)의 최소 설정."""

    def __init__(self, *, mode="representative_only", indicators=None):
        self.strategies = {
            "scoring": {
                "buy_threshold": 2,
                "sell_threshold": -2,
                "collinearity_mode": mode,
                "hysteresis": {
                    "enabled": True,
                    "exit_sell_threshold": -1,
                    "exit_buy_threshold": 0.5,
                },
                "weights": dict(_WEIGHTS),
            }
        }
        self.indicators = indicators or {
            "rsi": {"oversold": 30, "overbought": 70},
            "volume": {"surge_ratio": 1.5},
            "moving_average": {"short_period": 5, "mid_period": 20},
        }


def _indicator_frame(n=120, seed=7):
    """실제 IndicatorEngine(pandas-ta)으로 지표를 붙인 무작위 일봉."""
    from config.config_loader import Config
    from core.indicator_engine import IndicatorEngine

    rng = np.random.default_rng(seed)
    close = 10_000 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    dates = pd.bdate_range("2024-01-02", periods=n)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        },
        index=dates,
    )
    return IndicatorEngine(Config.get()).calculate_all(df)


class TestMacdWarmup:
    def test_warmup_rows_score_zero_and_first_valid_row_is_not_a_cross(self):
        gen = SignalGenerator(_Cfg())
        for seed in range(20):
            df = _indicator_frame(seed=seed)
            first_valid = df["macd_signal"].first_valid_index()
            assert first_valid is not None
            pos = df.index.get_loc(first_valid)
            assert pos > 0, "시그널선 워밍업이 있어야 하는 시나리오"

            scores = gen._score_macd(df)
            assert (scores.iloc[:pos] == 0.0).all(), (
                f"seed={seed}: 워밍업 구간에 MACD 점수가 들어감 {scores.iloc[:pos].unique()}"
            )
            # 첫 유효 봉은 비교할 전일이 없으므로 크로스(±2)가 아니라 유지 점수(±1)다.
            assert abs(scores.iloc[pos]) == 1.0, (
                f"seed={seed}: 첫 유효 봉 점수 {scores.iloc[pos]} (크로스로 잡히면 안 됨)"
            )

    def test_cross_still_detected_between_two_valid_rows(self):
        gen = SignalGenerator(_Cfg())
        dates = pd.bdate_range("2024-01-01", periods=6)
        df = pd.DataFrame(
            {
                "macd": [np.nan, np.nan, -1.0, -0.5, 1.0, -1.0],
                "macd_signal": [np.nan, np.nan, 0.0, 0.0, 0.0, 0.0],
                "close": [100.0] * 6,
            },
            index=dates,
        )
        scores = gen._score_macd(df)
        assert scores.tolist() == [0.0, 0.0, -1.0, -1.0, 2.0, -2.0]

    def test_histogram_bonus_not_applied_on_invalid_rows(self):
        gen = SignalGenerator(_Cfg())
        dates = pd.bdate_range("2024-01-01", periods=4)
        # 히스토그램만 값이 있고 시그널선이 비어 있는 행은 여전히 0점이어야 한다.
        df = pd.DataFrame(
            {
                "macd": [1.0, 2.0, 3.0, 4.0],
                "macd_signal": [np.nan, np.nan, 1.0, 1.0],
                "macd_histogram": [0.5, 1.0, 2.0, 3.0],
                "close": [100.0] * 4,
            },
            index=dates,
        )
        scores = gen._score_macd(df)
        assert scores.iloc[0] == 0.0
        assert scores.iloc[1] == 0.0
        # 2행: 첫 유효 봉(유지 +1) + 히스토그램 상승 보너스 +0.5
        assert scores.iloc[2] == 1.5

    def test_warmup_does_not_carry_sell_state_into_first_valid_bar(self):
        """워밍업 -1과 거래량 하락일 -1이 합쳐져 SELL 상태가 이월되던 경로."""
        gen = SignalGenerator(_Cfg())
        n = 12
        dates = pd.bdate_range("2024-01-01", periods=n)
        macd = [np.nan] * 8 + [1.0, 1.2, 1.3, 1.4]
        signal = [np.nan] * 8 + [0.5, 0.5, 0.5, 0.5]
        close = [100.0] * n
        close[3] = 95.0  # 하락일
        volume_ratio = [1.0] * n
        volume_ratio[3] = 3.0  # 하락일 거래량 급증 → score_volume -1
        df = pd.DataFrame(
            {
                "macd": macd,
                "macd_signal": signal,
                "close": close,
                "volume_ratio": volume_ratio,
            },
            index=dates,
        )
        out = gen.generate(df)
        assert (out["score_macd"].iloc[:8] == 0.0).all()
        assert "SELL" not in set(out["signal"].iloc[:8]), out["signal"].tolist()


# ── MA 점수: 설정 기간의 SMA 컬럼을 정확히 사용 ──


def _ma_frame(short_vals, mid_vals, *, short_col="sma_5", mid_col="sma_20", extra=None):
    n = len(short_vals)
    dates = pd.bdate_range("2024-01-01", periods=n)
    data = {short_col: short_vals, mid_col: mid_vals, "close": [100.0] * n}
    data.update(extra or {})
    return pd.DataFrame(data, index=dates)


class TestMaColumnPick:
    def test_uses_sma_cross_not_ema_or_sma200(self):
        """SMA는 크로스하고 EMA·sma_200은 크로스하지 않는 프레임 — SMA 크로스만 채점."""
        gen = SignalGenerator(_Cfg(mode="max_per_direction"))
        df = _ma_frame(
            [9.0, 9.0, 11.0, 11.0, 9.0],
            [10.0, 10.0, 10.0, 10.0, 10.0],
            extra={
                # 예전 로직은 마지막 일치 컬럼(ema_5/ema_20)을 썼다. EMA는 계속 위에 있다.
                "ema_5": [12.0] * 5,
                "ema_20": [10.0] * 5,
                "sma_200": [50.0] * 5,
            },
        )
        # IndicatorEngine과 같은 컬럼 순서(sma → ema)로 둔다.
        df = df[["sma_5", "sma_20", "sma_200", "ema_5", "ema_20", "close"]]
        scores = gen._score_ma(df)
        assert scores.tolist() == [0.0, 0.0, 1.0, 0.0, -1.0]

    def test_reconfigured_periods_use_matching_columns(self):
        cfg = _Cfg(
            mode="max_per_direction",
            indicators={
                "rsi": {"oversold": 30, "overbought": 70},
                "moving_average": {"short_period": 10, "mid_period": 30},
            },
        )
        gen = SignalGenerator(cfg)
        df = _ma_frame(
            [9.0, 11.0, 11.0, 9.0],
            [10.0, 10.0, 10.0, 10.0],
            short_col="sma_10",
            mid_col="sma_30",
        )
        scores = gen._score_ma(df)
        assert scores.tolist() == [0.0, 1.0, 0.0, -1.0]

    def test_warmup_nan_is_not_a_cross(self):
        gen = SignalGenerator(_Cfg(mode="max_per_direction"))
        df = _ma_frame(
            [9.0, 9.0, 11.0, 11.0, 12.0],
            [np.nan, np.nan, 10.0, 10.0, 10.0],
        )
        scores = gen._score_ma(df)
        # 2행이 첫 유효 봉: 전일 비교 대상이 없으므로 골든크로스가 아니다.
        assert scores.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0]

    def test_missing_configured_columns_warns_once_and_scores_zero(self):
        from loguru import logger

        messages = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            cfg = _Cfg(
                mode="max_per_direction",
                indicators={"moving_average": {"short_period": 10, "mid_period": 30}},
            )
            gen = SignalGenerator(cfg)
            df = _ma_frame([9.0, 11.0], [10.0, 10.0])  # sma_5/sma_20만 있음
            assert gen._score_ma(df).tolist() == [0.0, 0.0]
            assert gen._score_ma(df).tolist() == [0.0, 0.0]
        finally:
            logger.remove(sink_id)
        hits = [m for m in messages if "MA 점수 컬럼" in m]
        assert len(hits) == 1, messages

    def test_real_indicator_frame_scores_sma_cross(self):
        """IndicatorEngine 출력(sma_200 포함)에서도 SMA5/SMA20 크로스와 일치해야 한다."""
        gen = SignalGenerator(_Cfg(mode="max_per_direction"))
        df = _indicator_frame(n=260, seed=3)
        assert "sma_200" in df.columns
        scores = gen._score_ma(df)
        above = df["sma_5"] > df["sma_20"]
        valid = df["sma_5"].notna() & df["sma_20"].notna()
        prev_valid = valid.shift(1, fill_value=False)
        expected_golden = valid & prev_valid & above & ~above.shift(1, fill_value=False)
        expected_dead = valid & prev_valid & ~above & above.shift(1, fill_value=False)
        assert expected_golden.any() and expected_dead.any()
        assert (scores[expected_golden] == 1.0).all()
        assert (scores[expected_dead] == -1.0).all()
        assert (scores[~(expected_golden | expected_dead)] == 0.0).all()
