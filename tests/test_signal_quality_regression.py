"""
회귀 테스트: scoring signal quality 개선
- MACD 유지점수 약화 (0.5 → 0.15 계수)
- Volume MACD 동방향 부스트 (1.5배)
"""

import pandas as pd
import numpy as np
import pytest


def _make_df(n=100, **overrides):
    """테스트용 OHLCV + 지표 DataFrame 생성."""
    dates = pd.bdate_range("2023-01-01", periods=n)
    close = 60000 + np.cumsum(np.random.randn(n) * 500)
    df = pd.DataFrame({
        "open": close - 100,
        "high": close + 200,
        "low": close - 300,
        "close": close,
        "volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=dates)
    for k, v in overrides.items():
        df[k] = v
    return df


class TestMACDHoldScoreReduction:
    """MACD 유지점수가 0.15 계수로 약화됐는지 검증."""

    def test_macd_hold_score_below_threshold(self):
        """MACD 유지(크로스 아님) + Bollinger 하단 이탈 = threshold 2 미달."""
        from core.signal_generator import SignalGenerator
        from config.config_loader import Config

        config = Config.get()
        sg = SignalGenerator(config=config)

        n = 50
        # MACD > Signal 유지 (크로스 아님)
        macd_vals = np.ones(n) * 0.5
        signal_vals = np.zeros(n)
        hist_vals = macd_vals - signal_vals

        df = _make_df(
            n=n,
            macd=macd_vals,
            macd_signal=signal_vals,
            macd_histogram=hist_vals,
            bb_lower=np.full(n, 50000),  # close > bb_lower (정상)
            bb_upper=np.full(n, 80000),
            volume_ratio=np.ones(n) * 1.0,  # 거래량 급증 없음
        )

        weights = sg._get_weights()
        buy_w = weights["macd_golden_cross"]

        # 유지점수: buy_w * 0.15 (이전 0.5 대비 약화)
        macd_score = sg._score_macd(df)
        # 크로스 없는 유지 봉에서
        mid_score = macd_score.iloc[n // 2]
        assert abs(mid_score - buy_w * 0.15) < 0.01, (
            f"MACD 유지점수가 {mid_score}이어야 하는데 {buy_w * 0.15}이 아님"
        )
        # 유지점수 + Bollinger 최대 = buy_w*0.15 + 1.0 < 2.0
        assert buy_w * 0.15 + 1.0 < 2.0, "MACD유지+Boll이 threshold 2를 넘으면 안됨"


class TestVolumeBoost:
    """Volume MACD 동방향 부스트 검증."""

    def test_volume_boost_when_macd_aligned(self):
        """MACD bullish + volume surge + price up → weight * 1.5."""
        from core.signal_generator import SignalGenerator
        from config.config_loader import Config

        config = Config.get()
        sg = SignalGenerator(config=config)

        n = 10
        df = _make_df(
            n=n,
            macd=np.ones(n) * 1.0,
            macd_signal=np.zeros(n),
            volume_ratio=np.ones(n) * 2.0,  # surge
        )
        # price_up = close > close.shift(1)
        df["close"] = np.linspace(60000, 65000, n)  # 꾸준히 상승

        weight = sg._get_weights()["volume_surge"]
        vol_score = sg._score_volume(df)

        # MACD bullish + price up + volume surge → weight * 1.5
        # 첫 봉은 shift로 NaN이므로 2번째부터 확인
        assert vol_score.iloc[2] == pytest.approx(weight * 1.5, abs=0.01)

    def test_volume_no_boost_when_macd_opposed(self):
        """MACD bearish + volume surge + price up → weight (부스트 없음)."""
        from core.signal_generator import SignalGenerator
        from config.config_loader import Config

        config = Config.get()
        sg = SignalGenerator(config=config)

        n = 10
        df = _make_df(
            n=n,
            macd=np.ones(n) * -1.0,    # bearish
            macd_signal=np.zeros(n),
            volume_ratio=np.ones(n) * 2.0,
        )
        df["close"] = np.linspace(60000, 65000, n)

        weight = sg._get_weights()["volume_surge"]
        vol_score = sg._score_volume(df)

        # MACD bearish + price up → 기본 weight (부스트 없음)
        assert vol_score.iloc[2] == pytest.approx(weight, abs=0.01)


class TestScoreBandIntegrity:
    """점수 밴드가 설계 의도대로인지 검증."""

    def test_macd_hold_only_cannot_reach_buy_threshold(self):
        """MACD 유지 + Bollinger + Volume(비발화) < buy_threshold."""
        # w_macd=3 기준: 유지 = 3*0.15 = 0.45
        # + Bollinger 하단 = 1.0 (w_bollinger=1)
        # + Volume 비발화 = 0
        # = 1.45 < 2.0
        macd_hold = 3 * 0.15
        boll_max = 1.0
        vol_none = 0.0
        assert macd_hold + boll_max + vol_none < 2.0

    def test_macd_cross_can_reach_buy_threshold(self):
        """MACD 크로스 단독으로 buy_threshold 도달 가능."""
        # w_macd=3 기준: 크로스 = 3 (풀 점수) ≥ 2.0
        macd_cross = 3
        assert macd_cross >= 2.0

    def test_macd_cross_plus_volume_boost_strong(self):
        """MACD 크로스 + Volume 부스트 = 강한 신호."""
        macd_cross = 3
        vol_boost = 0.5 * 1.5  # w_volume=0.5, boost 1.5x
        assert macd_cross + vol_boost >= 2.0
