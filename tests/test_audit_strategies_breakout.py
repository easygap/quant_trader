"""breakout_volume 청산 기준 감사 회귀 테스트.

예전 청산은 매 봉의 breakout_ref(직전 N봉 고가 최대)와 비교했다. T+1의 breakout_ref는
돌파봉 T의 고가까지 포함하므로, 돌파 레벨을 다시 깨지 않았는데도 진입 다음 봉에
SELL이 나는 1일 왕복이 대부분이었다. 이제 진입 봉에서 돌파한 레벨(entry_level)을 고정한다.
"""

import numpy as np
import pandas as pd

from config.config_loader import Config


def _strategy(*, real_indicators=False):
    from strategies.breakout_volume import BreakoutVolumeStrategy

    strategy = BreakoutVolumeStrategy(Config.get())
    strategy.params = {"breakout_period": 10, "surge_ratio": 1.5, "adx_min": 20}
    if not real_indicators:
        # ADX는 항상 추세 있음(30)으로 두고 돌파·거래량 조건만 본다.
        strategy.indicator_engine.calculate_all = lambda df: df.assign(adx=30.0)
    return strategy


def _scenario():
    """0~14 횡보(고가 101) → 15 돌파(종가 105, 고가 108, 거래량 3배) → 이후 되밀림."""
    closes = [100.0] * 15 + [105.0, 103.0, 100.0, 99.0, 102.0]
    highs = [101.0] * 15 + [108.0, 104.0, 101.0, 100.0, 103.0]
    volumes = [1000.0] * 15 + [3000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    dates = pd.bdate_range("2024-01-01", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": volumes,
        },
        index=dates,
    )


def test_next_bar_above_broken_level_is_not_a_sell():
    df = _scenario()
    out = _strategy().analyze(df)
    t = 15
    assert out["signal"].iloc[t] == "BUY"
    # T+1 종가 103은 돌파봉 고가(108)보다 낮지만 돌파한 레벨(101)보다 위 → 실패 아님
    assert out["breakout_ref"].iloc[t + 1] == 108.0
    assert out["signal"].iloc[t + 1] != "SELL"
    assert out["entry_level"].iloc[t + 1] == 101.0


def test_close_below_broken_level_sells_every_bar_until_recovered():
    df = _scenario()
    out = _strategy().analyze(df)
    # 17·18: 종가가 101 아래 → 매 봉 SELL (최소 보유일에 막혀도 다음 봉에 다시 나온다)
    assert out["signal"].iloc[17] == "SELL"
    assert out["signal"].iloc[18] == "SELL"
    # 19: 102로 레벨 위 회복 → SELL 없음
    assert out["signal"].iloc[19] != "SELL"
    # 진입 전에는 청산 기준 레벨이 없다
    assert out["entry_level"].iloc[:15].isna().all()
    assert "SELL" not in set(out["signal"].iloc[:15])


def test_strict_prefix_matches_full_analyze():
    df = _scenario()
    strategy = _strategy()
    full = strategy.analyze(df)
    for i in range(len(df)):
        prefix = strategy.analyze(df.iloc[: i + 1].copy())
        assert prefix["signal"].iloc[-1] == full["signal"].iloc[i], i


def test_random_series_never_sells_next_bar_above_broken_level():
    """실제 지표(ADX 포함)로 돌린 무작위 일봉에서 불변식 확인."""
    strategy = _strategy(real_indicators=True)
    buys = 0
    for seed in range(8):
        rng = np.random.default_rng(seed)
        n = 400
        close = 10_000 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
        high = close * (1 + rng.uniform(0, 0.02, n))
        low = close * (1 - rng.uniform(0, 0.02, n))
        volume = rng.lognormal(12, 0.6, n)
        df = pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close, "volume": volume},
            index=pd.bdate_range("2022-01-03", periods=n),
        )
        out = strategy.analyze(df)
        sig = out["signal"].tolist()
        for t in range(len(sig) - 1):
            if sig[t] != "BUY":
                continue
            buys += 1
            broken_level = out["breakout_ref"].iloc[t]
            if sig[t + 1] == "SELL":
                assert out["close"].iloc[t + 1] < broken_level, (seed, t)
    assert buys > 0, "시나리오에 진입이 한 번도 없으면 불변식 검사가 무의미하다"
