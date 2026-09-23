"""앙상블 auto_downgrade 감사 회귀 테스트.

예전에는 첫 analyze() 호출의 신호 전체로 상관을 계산해 인스턴스 모드를 영구히 바꿨다.
포트폴리오 백테스트는 한 인스턴스로 종목마다 전 기간 df를 넘기므로, 첫 종목의 미래
신호가 모든 날짜·종목의 모드를 정했고 결과가 종목 순서에 따라 달라졌다.
"""

import numpy as np
import pandas as pd

from config.config_loader import Config


class _FixedSignals:
    """미리 정한 신호를 날짜에 맞춰 돌려주는 구성 전략."""

    def __init__(self, signals: pd.Series):
        self.signals = signals

    def analyze(self, df):
        out = df.copy()
        out["signal"] = self.signals.reindex(out.index).fillna("HOLD")
        out["strategy_score"] = 0.0
        return out


def _random_signals(index, seed):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.choice(["BUY", "HOLD", "SELL"], size=len(index)), index=index)


def _frame(n=200, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=n)
    close = np.linspace(100, 120, n)
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1e6}, index=idx
    )


def _ensemble(components):
    from core.strategy_ensemble import StrategyEnsemble

    ens = StrategyEnsemble(Config.get())
    ens.mode = "majority_vote"
    ens.auto_downgrade = True
    ens._independence_window = 60
    ens._independence_threshold = 0.6
    ens._strategies = [(name, _FixedSignals(sig), 1.0) for name, sig in components]
    return ens


def _correlated_symbol(df):
    """technical과 momentum이 항상 같은 신호 → 모든 창에서 고상관."""
    tech = _random_signals(df.index, 1)
    return [
        ("technical", tech),
        ("momentum_factor", tech.copy()),
        ("volatility_condition", _random_signals(df.index, 3)),
    ]


def _independent_symbol(df):
    return [
        ("technical", _random_signals(df.index, 11)),
        ("momentum_factor", _random_signals(df.index, 12)),
        ("volatility_condition", _random_signals(df.index, 13)),
    ]


def test_result_does_not_depend_on_symbol_order():
    df = _frame()
    corr_parts = _correlated_symbol(df)
    ind_parts = _independent_symbol(df)

    # 종목 순서 A → B
    ens = _ensemble(corr_parts)
    a_first = ens.analyze(df)
    ens._strategies = [(n, _FixedSignals(s), 1.0) for n, s in ind_parts]
    b_second = ens.analyze(df)

    # 종목 순서 B → A (새 인스턴스)
    ens2 = _ensemble(ind_parts)
    b_first = ens2.analyze(df)
    ens2._strategies = [(n, _FixedSignals(s), 1.0) for n, s in corr_parts]
    a_second = ens2.analyze(df)

    pd.testing.assert_series_equal(a_first["signal"], a_second["signal"])
    pd.testing.assert_series_equal(b_first["signal"], b_second["signal"])
    # 설정 모드는 바뀌지 않는다
    assert ens.mode == "majority_vote" and ens2.mode == "majority_vote"


def test_mode_uses_only_trailing_window():
    """고상관 구간이 뒤에만 있으면 앞 구간은 설정 모드 그대로 (미래 신호 미참조)."""
    df = _frame(n=240)
    tech = _random_signals(df.index, 21)
    mom = _random_signals(df.index, 22)
    mom.iloc[120:] = tech.iloc[120:]  # 120행부터 technical과 같은 신호
    ens = _ensemble([
        ("technical", tech),
        ("momentum_factor", mom),
        ("volatility_condition", _random_signals(df.index, 23)),
    ])
    out = ens.analyze(df)
    assert (out["ensemble_mode"].iloc[:120] == "majority_vote").all()
    # 창 전체가 동일 신호 구간(179행 이후)이면 반드시 conservative
    assert (out["ensemble_mode"].iloc[180:] == "conservative").all()


def test_strict_prefix_matches_full_run():
    df = _frame(n=160)
    tech = _random_signals(df.index, 31)
    mom = _random_signals(df.index, 32)
    mom.iloc[80:] = tech.iloc[80:]
    parts = [
        ("technical", tech),
        ("momentum_factor", mom),
        ("volatility_condition", _random_signals(df.index, 33)),
    ]
    full = _ensemble(parts).analyze(df)
    ens = _ensemble(parts)
    for i in range(len(df)):
        last = ens.analyze(df.iloc[: i + 1]).iloc[-1]
        assert last["signal"] == full["signal"].iloc[i], i
        assert last["ensemble_mode"] == full["ensemble_mode"].iloc[i], i


def test_constant_component_is_not_treated_as_correlated():
    """창 안에서 신호가 변하지 않는 구성(데이터 없는 펀더멘털 등)은 상관 판정에서 빠진다."""
    df = _frame(n=150)
    ens = _ensemble([
        ("technical", _random_signals(df.index, 41)),
        ("momentum_factor", _random_signals(df.index, 42)),
        ("fundamental_factor", pd.Series("HOLD", index=df.index)),
    ])
    out = ens.analyze(df)
    assert (out["ensemble_mode"] == "majority_vote").all()


def test_check_failure_is_logged_at_warning(monkeypatch):
    from loguru import logger

    df = _frame(n=100)
    ens = _ensemble(_correlated_symbol(df))

    def boom(_df):
        raise RuntimeError("corr failed")

    monkeypatch.setattr(ens, "_trailing_high_correlation", boom)
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        out = ens.analyze(df)
        ens.analyze(df)
    finally:
        logger.remove(sink_id)
    assert (out["ensemble_mode"] == "majority_vote").all()
    hits = [m for m in messages if "앙상블 독립성 검사 실패" in m]
    assert len(hits) == 1 and "corr failed" in hits[0]
