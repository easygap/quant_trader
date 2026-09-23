"""시장 국면 모듈 감사 회귀 테스트."""

from types import SimpleNamespace

import pandas as pd
import pytest


# ── regime_adaptive: 계산만 되고 읽히지 않던 buy_threshold_offset 제거 ──


def _regime_cfg(regime_adaptive):
    return SimpleNamespace(
        trading={"market_regime_filter": False},
        risk_params={},
        strategies={"regime_adaptive": regime_adaptive},
    )


@pytest.mark.parametrize(
    "regime, sl, tp",
    [("bullish", 1.0, 1.0), ("caution", 0.7, 0.8), ("bearish", 0.5, 0.6)],
)
def test_regime_adjusted_params_only_carry_sl_tp_multipliers(monkeypatch, regime, sl, tp):
    import core.market_regime as mr
    from config.config_loader import load_strategies

    monkeypatch.setattr(
        mr,
        "check_market_regime",
        lambda config, collector=None: {
            "regime": regime,
            "allow_buys": regime != "bearish",
            "position_scale": {"bullish": 1.0, "caution": 0.5, "bearish": 0.0}[regime],
            "details": {},
        },
    )
    cfg = _regime_cfg(load_strategies()["regime_adaptive"])
    out = mr.get_regime_adjusted_params(cfg)
    assert "buy_threshold_offset" not in out
    assert out["stop_loss_multiplier"] == sl
    assert out["take_profit_multiplier"] == tp


def test_strategies_yaml_has_no_dead_buy_threshold_offset():
    from config.config_loader import load_strategies

    ra = load_strategies()["regime_adaptive"]
    for regime in ("bullish", "caution", "bearish"):
        assert "buy_threshold_offset" not in ra[regime], regime


# ── check_market_regime: 거래일 수를 달력일로 환산, 오늘 봉 제외, 마지막 MA 유한성 ──


def _runtime_cfg(ma_days=200):
    return SimpleNamespace(
        strategies={},
        risk_params={},
        trading={
            "market_regime_filter": True,
            "market_regime_index": "KS11",
            "market_regime_ma_days": ma_days,
            "market_regime_short_momentum_days": 20,
            "market_regime_short_momentum_threshold": -5.0,
            "market_regime_caution_scale": 0.5,
            "market_regime_ma_cross_enabled": False,
        },
    )


def _krx_days(start, end):
    """요청 구간의 KRX 거래일 (주말·config/holidays.yaml 휴장일 제외)."""
    from core.trading_hours import _load_holidays

    holidays = {pd.Timestamp(h) for h in _load_holidays()}
    days = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    return pd.DatetimeIndex([d for d in days if d not in holidays])


def _today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return pd.Timestamp(datetime.now(ZoneInfo("Asia/Seoul")).date())


class _CalendarCollector:
    """요청 구간 안의 실제 거래일만 돌려주는 가짜 수집기 (예전 테스트 스텁은 N행 고정)."""

    def __init__(self, level_fn, *, drop_last_sessions=0, extra_today_close=None):
        self.level_fn = level_fn
        self.drop_last_sessions = drop_last_sessions
        self.extra_today_close = extra_today_close
        self.requests = []

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        self.requests.append((start_date, end_date))
        days = _krx_days(start_date, end_date)
        days = days[days < _today()]
        if self.drop_last_sessions:
            days = days[: -self.drop_last_sessions]
        closes = [self.level_fn(i) for i in range(len(days))]
        if self.extra_today_close is not None:
            days = days.append(pd.DatetimeIndex([_today()]))
            closes.append(self.extra_today_close)
        return pd.DataFrame({"close": closes}, index=days)


def test_enabled_regime_gets_enough_bars_for_ma200():
    from core.market_regime import check_market_regime

    rising = _CalendarCollector(lambda i: 1000.0 + i)
    result = check_market_regime(_runtime_cfg(), rising)
    assert result["regime"] == "bullish", result
    assert result["allow_buys"] is True
    start, end = rising.requests[0]
    assert len(_krx_days(start, end)) >= 250  # MA200 + 여유

    falling = _CalendarCollector(lambda i: 3000.0 - 5.0 * i)
    result = check_market_regime(_runtime_cfg(), falling)
    assert result["regime"] == "bearish", result
    assert result["details"]["below_ma"] is True


def test_intraday_bar_dated_today_is_not_used():
    from core.market_regime import check_market_regime

    # 오늘 날짜 봉이 폭락(1.0)이어도 확정 봉(어제까지)으로만 판정한다.
    collector = _CalendarCollector(lambda i: 1000.0 + i, extra_today_close=1.0)
    result = check_market_regime(_runtime_cfg(), collector)
    assert result["regime"] == "bullish", result
    assert result["details"]["last_close"] > 1.0
    assert pd.Timestamp(result["details"]["last_bar_date"]) < _today()


def test_non_finite_last_ma_fails_closed():
    from core.market_regime import check_market_regime

    collector = _CalendarCollector(lambda i: float("inf") if i > 150 else 1000.0 + i)
    result = check_market_regime(_runtime_cfg(), collector)
    assert result["regime"] == "unknown"
    assert result["allow_buys"] is False
    assert result["details"]["reason"] == "ma_unavailable"


def test_stale_index_feed_is_reported_in_details():
    from core.market_regime import check_market_regime

    collector = _CalendarCollector(lambda i: 1000.0 + i, drop_last_sessions=3)
    result = check_market_regime(_runtime_cfg(), collector)
    assert result["regime"] == "bullish"
    assert result["details"]["missing_sessions"] == 3


def test_disabled_filter_keeps_default_without_fetch():
    from core.market_regime import check_market_regime

    cfg = _runtime_cfg()
    cfg.trading["market_regime_filter"] = False
    collector = _CalendarCollector(lambda i: 1000.0 + i)
    result = check_market_regime(cfg, collector)
    assert result == {
        "regime": "bullish",
        "position_scale": 1.0,
        "allow_buys": True,
        "details": {},
    }
    assert collector.requests == []
