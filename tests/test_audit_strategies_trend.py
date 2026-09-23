"""trend_following 최적화 파라미터 감사 회귀 테스트.

예전에는 최적화기가 trend_ma_period·atr_stop_multiplier를 탐색했지만 전략은 sma_200을
고정으로 읽고 두 키를 어디서도 읽지 않아, 48개 조합 중 서로 다른 결과는 4개뿐이었다.
"""

import inspect

import numpy as np
import pandas as pd

from config.config_loader import Config


def _trending_frame(n=400, seed=5):
    rng = np.random.default_rng(seed)
    # 상승 → 하락 → 상승 구간을 섞어 추세선 기간에 따라 위/아래 판정이 갈리게 한다.
    drift = np.concatenate([
        np.full(n // 3, 0.003),
        np.full(n // 3, -0.003),
        np.full(n - 2 * (n // 3), 0.003),
    ])
    close = 10_000 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        },
        index=pd.bdate_range("2022-01-03", periods=n),
    )


def _strategy(**overrides):
    from strategies import create_strategy

    config = Config.get()
    if overrides:
        config = config.with_strategy_overrides("trend_following", overrides)
    return create_strategy("trend_following", config)


def test_trend_ma_period_changes_signals():
    df = _trending_frame()
    fast = _strategy(trend_ma_period=60).analyze(df)
    slow = _strategy(trend_ma_period=200).analyze(df)
    assert not fast["strategy_score"].equals(slow["strategy_score"])
    assert (fast["signal"] != slow["signal"]).any()
    # 60일 추세선은 60봉째부터 값이 있다
    assert fast["trend_ma"].first_valid_index() == df.index[59]


def test_default_period_matches_previous_sma200_behaviour():
    """기본값 200은 IndicatorEngine의 sma_200과 같은 선이다 — 기존 결과 불변."""
    df = _trending_frame()
    out = _strategy().analyze(df)
    assert "sma_200" in out.columns
    pd.testing.assert_series_equal(
        out["trend_ma"], out["sma_200"], check_names=False
    )


def test_generate_signal_length_gate_follows_period():
    df = _trending_frame(n=120)
    res = _strategy(trend_ma_period=60).generate_signal(df)
    assert "데이터 부족" not in str(res["details"].get("이유", ""))
    assert "60일선" in res["details"]
    res200 = _strategy(trend_ma_period=200).generate_signal(df)
    assert res200["details"]["이유"] == "데이터 부족(200일 필요)"


def test_every_trend_following_search_key_changes_analyze_output():
    """최적화 탐색 공간의 모든 키가 실제로 전략 출력에 영향을 줘야 한다."""
    from backtest.param_optimizer import DEFAULT_SEARCH_SPACES

    df = _trending_frame()
    space = DEFAULT_SEARCH_SPACES["trend_following"]
    for key, values in space.items():
        low = _strategy(**{key: min(values)}).analyze(df)
        high = _strategy(**{key: max(values)}).analyze(df)
        assert not low["strategy_score"].equals(high["strategy_score"]), (
            f"{key}={min(values)} vs {max(values)} 결과가 같음 — 읽히지 않는 탐색 키"
        )


def test_dead_stop_multiplier_keys_removed_from_optimizer():
    import backtest.param_optimizer as opt

    assert "atr_stop_multiplier" not in opt.DEFAULT_SEARCH_SPACES["trend_following"]
    # bayesian_optimize 안의 default_bounds(지역 dict)도 같은 키를 탐색하지 않아야 한다.
    source = inspect.getsource(opt)
    assert '"atr_stop_multiplier"' not in source
    assert '"trailing_atr_multiplier"' not in source
