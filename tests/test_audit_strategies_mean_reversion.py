"""mean_reversion 백테스트/paper 매수 규칙 일치 감사 회귀 테스트.

예전에는 52주 필터가 generate_signal()에만 있어 백테스트(analyze만 호출)가 필터 없는
다른 전략을 평가했다. 이제 두 52주 필터는 analyze()에서 적용된다. 펀더멘털 필터와
코스피200 제한은 '현재' 데이터만 있어 백테스트에 넣으면 미래 정보가 섞이므로
analyze()에 넣지 않고 '반영 안 됨'으로 기록한다.
"""

import numpy as np
import pandas as pd
import pytest

from config.config_loader import Config

_BASE_PARAMS = {
    "z_score_buy": -2.0,
    "z_score_sell": 2.0,
    "lookback_period": 20,
    "adx_filter": 20,
    "volume_spike_filter": 3.0,
    "exclude_52w_low_near": True,
    "max_drawdown_from_52w_high": 0.30,
    "near_52w_low_pct": 0.05,
    "window_52w": 252,
    "restrict_to_kospi200": False,
    "fundamental_filter": {"enabled": False},
}


def _strategy(**overrides):
    from strategies.mean_reversion import MeanReversionStrategy

    strategy = MeanReversionStrategy(Config.get())
    strategy.params = {**_BASE_PARAMS, **overrides}
    # 지표는 고정: ADX 15(<20), RSI 30(<40), 거래량 정상 → Z-Score와 52주 필터만 신호를 가른다.
    strategy.indicator_engine.calculate_all = lambda df: df.assign(
        adx=15.0, rsi=30.0, volume_ratio=1.0
    )
    return strategy


def _frame(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=pd.bdate_range("2023-01-02", periods=len(closes)),
    )


def _near_low_case():
    # 100 부근 횡보 뒤 마지막 봉 90으로 급락: 52주 저점(89) 대비 +1.1% → 신저가 근방
    return _frame([100.0 + (i % 3) * 0.5 for i in range(60)] + [90.0])


def _drawdown_case():
    # 150 → 80 하락 → 105 회복 횡보 → 마지막 100: 52주 고점(151) 대비 -33.8%, 저점 대비 +26%
    down = list(np.linspace(150, 80, 40))
    up = list(np.linspace(80, 105, 20))
    flat = [105.0 + (i % 2) * 0.3 for i in range(25)]
    return _frame(down + up + flat + [100.0])


def _clean_case():
    # 80 → 110 상승 → 105 횡보 → 마지막 100: 고점 대비 -9.9%, 저점 대비 +26% → 필터 통과
    up = list(np.linspace(80, 110, 40))
    flat = [105.0 + (i % 2) * 0.3 for i in range(25)]
    return _frame(up + flat + [100.0])


@pytest.mark.parametrize(
    "case, expected, veto_col",
    [
        (_near_low_case, "HOLD", "buy_veto_52w_near_low"),
        (_drawdown_case, "HOLD", "buy_veto_52w_drawdown"),
        (_clean_case, "BUY", None),
    ],
)
def test_analyze_matches_generate_signal_on_last_bar(case, expected, veto_col):
    strategy = _strategy()
    df = case()
    analyzed = strategy.analyze(df)
    assert analyzed["z_score"].iloc[-1] <= -2.0, "시나리오 전제: 마지막 봉이 Z-Score 매수 영역"
    assert analyzed["signal"].iloc[-1] == expected
    assert strategy.generate_signal(df)["signal"] == expected
    if veto_col:
        assert bool(analyzed[veto_col].iloc[-1]) is True


def test_52w_filter_off_restores_raw_zscore_buy():
    strategy = _strategy(exclude_52w_low_near=False)
    analyzed = strategy.analyze(_near_low_case())
    assert analyzed["signal"].iloc[-1] == "BUY"
    assert not analyzed["buy_veto_52w_near_low"].any()


def test_prefix_parity_between_backtest_path_and_generate_signal():
    """모든 시점에서 analyze(접두) 마지막 신호 == generate_signal(접두) 신호."""
    rng = np.random.default_rng(11)
    closes = 100 * np.exp(np.cumsum(rng.normal(-0.002, 0.03, 200)))
    df = _frame(closes)
    strategy = _strategy()
    full = strategy.analyze(df)
    assert (full["signal"] == "BUY").any(), "시나리오에 매수 신호가 있어야 한다"
    assert (full["buy_veto_52w_near_low"] | full["buy_veto_52w_drawdown"]).any(), (
        "시나리오에 52주 필터로 걸러진 매수가 있어야 한다"
    )
    for i in range(25, len(df)):
        prefix = df.iloc[: i + 1]
        bt_signal = strategy.analyze(prefix)["signal"].iloc[-1]
        assert bt_signal == full["signal"].iloc[i], i
        assert strategy.generate_signal(prefix)["signal"] == bt_signal, i


def test_backtest_path_never_calls_point_in_time_unsafe_filters(monkeypatch):
    """펀더멘털 필터·코스피200 조회는 현재 데이터라 analyze()가 부르면 안 된다."""
    import strategies.mean_reversion as mr

    def _boom(*args, **kwargs):
        raise AssertionError("analyze()가 현재 시점 펀더멘털을 조회함 (미래 정보)")

    monkeypatch.setattr(mr, "check_fundamental_filter", _boom)
    strategy = _strategy(
        restrict_to_kospi200=True,
        fundamental_filter={"enabled": True, "per_min": 0, "per_max": 50},
    )
    strategy._is_kospi200 = _boom
    analyzed = strategy.analyze(_clean_case())
    assert analyzed["signal"].iloc[-1] == "BUY"
    assert strategy.unmodelled_backtest_filters() == [
        "restrict_to_kospi200",
        "fundamental_filter",
    ]


def test_unmodelled_filters_warn_once_per_instance():
    from loguru import logger

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        strategy = _strategy(fundamental_filter={"enabled": True, "per_min": 0})
        strategy.analyze(_clean_case())
        strategy.analyze(_clean_case())
    finally:
        logger.remove(sink_id)
    hits = [m for m in messages if "반영하지 않습니다" in m]
    assert len(hits) == 1, messages
    assert "fundamental_filter" in hits[0]
