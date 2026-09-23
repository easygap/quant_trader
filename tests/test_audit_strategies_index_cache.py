"""지수 캐시 감사 회귀 테스트 — RSR 시장 필터.

예전 시장 필터는 첫 analyze() 호출의 날짜 범위로 KS11을 한 번만 받아 캐시했다.
strict 백테스트의 첫 호출은 df.iloc[:1]이라 1일치 상태가 전 기간에 ffill되어 필터가
한 번도 작동하지 않았고, 조회 실패 시에는 경고만 남기고 필터 없이 돌았다.
"""

import numpy as np
import pandas as pd
import pytest

from config.config_loader import Config


def _ks11_series():
    """2018~2021 영업일 KS11: 상승 → 2020년 중반 급락 → 회복 (SMA200 이탈 구간 생성)."""
    dates = pd.bdate_range("2018-01-02", "2021-12-30")
    n = len(dates)
    t = np.arange(n)
    level = 2000 + 1.2 * t
    crash = (t > 600) & (t < 760)
    level = np.where(crash, level - 6.0 * (t - 600), level)
    level = np.where(t >= 760, level - 6.0 * 160 + 4.0 * (t - 760), level)
    return pd.Series(level.astype(float), index=dates)


class _FakeCollector:
    """요청 구간만 잘라 주는 가짜 수집기. 호출 횟수를 센다."""

    calls: list = []
    fail = False

    def __init__(self):
        self.quiet_ohlcv_log = False

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        type(self).calls.append((symbol, start_date, end_date))
        if type(self).fail:
            raise ConnectionError("rate limited")
        s = _ks11_series().loc[pd.Timestamp(start_date): pd.Timestamp(end_date)]
        return pd.DataFrame(
            {"open": s, "high": s, "low": s, "close": s, "volume": 1.0}, index=s.index
        )


@pytest.fixture
def fake_collector(monkeypatch):
    import core.data_collector as dc

    _FakeCollector.calls = []
    _FakeCollector.fail = False
    monkeypatch.setattr(dc, "DataCollector", _FakeCollector)
    return _FakeCollector


def _stock_frame(start="2019-06-03", n=400):
    dates = pd.bdate_range(start, periods=n)
    close = np.linspace(100, 160, n)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=dates,
    )


def _rotation(**params):
    from strategies.relative_strength_rotation import RelativeStrengthRotationStrategy

    strategy = RelativeStrengthRotationStrategy(Config.get())
    strategy.params = {
        "short_lookback": 2,
        "long_lookback": 3,
        "sma_period": 2,
        "short_weight": 0.6,
        "market_filter_sma200": True,
        "market_filter_exit": True,
        "market_filter_ma_period": 200,
        **params,
    }
    strategy.indicator_engine.calculate_all = lambda df: df
    return strategy


def test_strict_prefix_market_filter_matches_full_run(fake_collector):
    df = _stock_frame()
    full = _rotation().analyze(df)
    assert (~full["market_filter_pass"]).sum() > 20, "시나리오에 필터 차단 구간이 있어야 한다"
    assert full["market_filter_active"].all()

    strategy = _rotation()
    fake_collector.calls = []
    for i in range(len(df)):
        last = strategy.analyze(df.iloc[: i + 1].copy()).iloc[-1]
        assert bool(last["market_filter_pass"]) == bool(full["market_filter_pass"].iloc[i]), i
        assert bool(last["market_filter_exit"]) == bool(full["market_filter_exit"].iloc[i]), i
    # 봉마다가 아니라 인스턴스당 한 번만 받는다
    assert len(fake_collector.calls) == 1


def test_market_filter_value_uses_only_previous_day(fake_collector):
    """날짜 d의 통과 여부는 d-1 종가·SMA로 정해진다 (넓게 받아도 미래 정보 없음)."""
    df = _stock_frame()
    out = _rotation().analyze(df)
    ks = _ks11_series()
    sma = ks.rolling(200, min_periods=200).mean()
    expected_prev = (ks > sma).shift(1)
    for d in df.index[::17]:
        assert bool(out.loc[d, "market_filter_pass"]) == bool(expected_prev.loc[d]), d


def test_load_failure_is_recorded_and_not_retried_per_bar(fake_collector):
    fake_collector.fail = True
    df = _stock_frame(n=60)
    strategy = _rotation()
    for i in range(len(df)):
        out = strategy.analyze(df.iloc[: i + 1].copy())
    assert len(fake_collector.calls) == 1
    assert not out["market_filter_active"].any()
    assert out["market_filter_pass"].all()
    assert strategy.market_filter_status["ok"] is False
    assert "rate limited" in strategy.market_filter_status["reason"]
    res = strategy.generate_signal(df)
    assert res["details"]["market_filter_active"] is False


def test_sma_warmup_days_are_unknown_not_bearish(fake_collector):
    """SMA가 아직 없는 날은 '아래'(차단·강제청산)가 아니라 판단 불가로 둔다."""
    df = _stock_frame(start="2018-01-02", n=260)
    out = _rotation().analyze(df)
    head = out.iloc[:150]
    assert not head["market_filter_active"].any()
    assert head["market_filter_pass"].all()
    assert not head["market_filter_exit"].any()
    assert out["market_filter_active"].iloc[-1]


def test_earlier_symbol_refetches_union_span(fake_collector):
    strategy = _rotation()
    strategy.analyze(_stock_frame(start="2020-01-02", n=200))
    assert len(fake_collector.calls) == 1
    # 같은 인스턴스로 더 이른 기간의 종목을 분석 → 구간을 합쳐 한 번 더 받는다
    out = strategy.analyze(_stock_frame(start="2019-03-04", n=200))
    assert len(fake_collector.calls) == 2
    assert out["market_filter_active"].all()


def test_index_cache_refetches_when_request_passes_cached_end(fake_collector):
    from strategies.index_cache import IndexCloseCache

    cache = IndexCloseCache("KS11", warmup_days=10)
    cache.ensure("2020-01-02", "2020-06-30")
    cache.ensure("2020-02-03", "2020-05-29")  # 캐시 안 → 재조회 없음
    assert cache.fetch_count == 1
    future = pd.Timestamp.today().normalize() + pd.Timedelta(days=30)
    cache.ensure("2020-01-02", future)  # 날짜가 넘어간 장기 실행 → 재조회
    assert cache.fetch_count == 2


# ── 벤치마크 상대 모멘텀·회전: 봉마다 지수를 다시 받지 않는다 ──


def _momentum(**params):
    from strategies.momentum_factor import MomentumFactorStrategy

    strategy = MomentumFactorStrategy(Config.get())
    strategy.params = {
        "benchmark_relative": True,
        "benchmark_symbol": "KS11",
        "lookback_days": 20,
        "buy_threshold_pct": 2.0,
        "sell_threshold_pct": -2.0,
        **params,
    }
    return strategy


def _wavy_stock(start="2019-06-03", n=260):
    dates = pd.bdate_range(start, periods=n)
    t = np.arange(n)
    close = 100 + 10 * np.sin(t / 15.0) + 0.05 * t
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=dates,
    )


def test_momentum_benchmark_fetched_once_in_strict_loop(fake_collector):
    df = _wavy_stock()
    full = _momentum().analyze(df)
    strategy = _momentum()
    fake_collector.calls = []
    # 1행짜리 접두는 analyze가 계산 없이 HOLD로 돌려주므로 2행부터 본다.
    for i in range(1, len(df)):
        last = strategy.analyze(df.iloc[: i + 1].copy()).iloc[-1]
        assert last["signal"] == full["signal"].iloc[i], i
        if pd.notna(full["benchmark_return"].iloc[i]):
            assert np.isclose(last["benchmark_return"], full["benchmark_return"].iloc[i]), i
    assert len(fake_collector.calls) == 1
    # 캐시가 봉마다 늘어나지 않는다 (예전에는 봉마다 시리즈가 하나씩 쌓였다)
    assert len(strategy._benchmark_return_cache) == 1


def test_momentum_benchmark_values_are_point_in_time(fake_collector):
    df = _wavy_stock()
    out = _momentum().analyze(df)
    ks = _ks11_series()
    expected = (ks / ks.shift(20) - 1) * 100
    for d in df.index[::13]:
        assert np.isclose(out.loc[d, "benchmark_return"], expected.loc[d]), d
    assert (out["signal"] == "BUY").any() and (out["signal"] == "SELL").any()


def test_momentum_benchmark_failure_is_not_retried_per_bar(fake_collector):
    fake_collector.fail = True
    df = _wavy_stock(n=60)
    strategy = _momentum()
    for i in range(len(df)):
        out = strategy.analyze(df.iloc[: i + 1].copy())
    assert len(fake_collector.calls) == 1
    assert out["benchmark_return"].isna().all()
    assert "BUY" not in set(out["signal"])


def test_rotation_benchmark_composite_fetched_once_in_strict_loop(fake_collector):
    df = _wavy_stock(n=200)
    params = {
        "market_filter_sma200": False,
        "score_mode": "benchmark_excess",
        "rank_entry_mode": "dense_ranked",
        "use_positive_momentum_filter": False,
        "use_trend_filter": False,
        "exit_trend_edge": False,
        "exit_rebalance_mode": "none",
    }
    full = _rotation(**params).analyze(df)
    strategy = _rotation(**params)
    fake_collector.calls = []
    for i in range(len(df)):
        last = strategy.analyze(df.iloc[: i + 1].copy()).iloc[-1]
        assert last["signal"] == full["signal"].iloc[i], i
        expected = full["benchmark_composite_score"].iloc[i]
        if pd.notna(expected):
            assert np.isclose(last["benchmark_composite_score"], expected), i
    assert len(fake_collector.calls) == 1

    ks = _ks11_series()
    expected_composite = 0.6 * ks.pct_change(2) + 0.4 * ks.pct_change(3)
    d = df.index[100]
    assert np.isclose(full.loc[d, "benchmark_composite_score"], expected_composite.loc[d])
