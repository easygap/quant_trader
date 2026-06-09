"""내장(폴백) 지표 수식이 pandas-ta와 일치하는지 회귀 검증.

pandas-ta가 없을 때만 폴백이 동작하지만, pandas-ta는 deprecation 경고(Pandas4)를
내고 있어 향후 pandas 업그레이드로 폴백이 활성화될 수 있다. 그때 폴백 RSI/ATR/ADX가
틀리면 스코어링 신호·손절(ATR)이 조용히 오염되므로, 폴백을 canonical(pandas-ta)에
맞춰 둔다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

import core.indicator_engine as ie_mod
from core.indicator_engine import IndicatorEngine


def _ohlcv(n=150, seed=11):
    rng = np.random.RandomState(seed)
    close = pd.Series(100 + np.cumsum(rng.randn(n)))
    high = close + rng.rand(n) * 2
    low = close - rng.rand(n) * 2
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": high, "low": low, "close": close,
        "volume": (rng.rand(n) * 1e6).astype(float),
    })


def _engine():
    cfg = SimpleNamespace(indicators={
        "rsi": {"period": 14},
        "atr": {"period": 14},
        "adx": {"period": 14},
        "bollinger": {"period": 20, "std_dev": 2.0},
    })
    return IndicatorEngine(cfg)


def _max_abs_diff(a, b):
    m = a.notna() & b.notna()
    return float((a[m] - b[m]).abs().max())


def _max_rel_diff_tail(a, b, tail=40):
    """정착(수렴) 구간(말미 tail봉)의 최대 상대 오차.

    Wilder RMA는 pandas-ta가 SMA presma seed, 폴백은 순수 ewm seed라 워밍업 구간만
    미세히 다르고 지수적으로 수렴한다. 정착 구간으로 '같은 Wilder 수식'임을 검증한다.
    """
    a, b = a.iloc[-tail:], b.iloc[-tail:]
    m = a.notna() & b.notna()
    return float(((a[m] - b[m]).abs() / b[m].abs()).max())


@pytest.fixture
def force_fallback(monkeypatch):
    monkeypatch.setattr(ie_mod, "HAS_PANDAS_TA", False)


def test_fallback_rsi_matches_pandas_ta(force_fallback):
    df = _ohlcv()
    ref = ta.rsi(df["close"], length=14)
    out = _engine().add_rsi(df.copy())
    assert _max_abs_diff(out["rsi"], ref) < 1e-6


def test_fallback_atr_matches_pandas_ta(force_fallback):
    df = _ohlcv()
    ref = ta.atr(df["high"], df["low"], df["close"], length=14)
    out = _engine().add_atr(df.copy())
    # 정착 구간 상대오차 < 0.1% (구 SMA 폴백은 수십 % 어긋났음)
    assert _max_rel_diff_tail(out["atr"], ref) < 1e-3


def test_fallback_adx_matches_pandas_ta(force_fallback):
    df = _ohlcv()
    ref = ta.adx(df["high"], df["low"], df["close"], length=14)
    out = _engine().add_adx(df.copy())
    assert _max_rel_diff_tail(out["adx"], ref.iloc[:, 0]) < 1e-3
    # +DI/-DI 컬럼이 폴백에서도 채워지는지(컬럼 패리티). 값은 근사.
    assert "di_plus" in out.columns and "di_minus" in out.columns
    assert out["di_plus"].notna().any() and out["di_minus"].notna().any()


def test_fallback_bb_bandwidth_matches_pandas_ta_scale(force_fallback):
    df = _ohlcv()
    ref = ta.bbands(df["close"], length=20, std=2.0)
    out = _engine().add_bollinger_bands(df.copy())
    # pandas-ta BBB(밴드폭 %)와 동일 스케일(×100)인지
    assert _max_abs_diff(out["bb_bandwidth"], ref.iloc[:, 3]) < 1e-6
