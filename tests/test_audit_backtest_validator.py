"""StrategyValidator 감사 회귀 테스트.

- 코스피 상위 N 동일비중 벤치마크: 늦게 상장한 종목 하나 때문에 벤치마크 기간 전체가 잘리지 않는다
  (outer 패널 + 첫 가격일 편입 매수·보유).
"""

import numpy as np
import pandas as pd
import pytest


def _dates(n=320):
    return pd.bdate_range("2023-01-02", periods=n)


def _close_frame(dates, close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(close), 1_000_000.0),
        },
        index=dates,
    )


class _PanelCollector:
    def __init__(self, frames: dict):
        self.frames = frames

    def fetch_korean_stock(self, symbol, start_date=None, end_date=None):
        frame = self.frames[symbol]
        start = pd.Timestamp(start_date) if start_date else frame.index.min()
        end = pd.Timestamp(end_date) if end_date else frame.index.max()
        return frame.loc[start:end].copy()


def _top50_frames(dates, late_row=100):
    rising = np.concatenate((np.linspace(100.0, 110.0, late_row), np.full(len(dates) - late_row, 110.0)))
    return {
        "AAA": _close_frame(dates, rising),
        "BBB": _close_frame(dates, rising),
        # 늦게 상장한 종목 (late_row부터 가격 존재)
        "CCC": _close_frame(dates[late_row:], np.full(len(dates) - late_row, 50.0)),
    }


def test_equal_weight_panel_keeps_requested_start_and_marks_late_listing():
    from backtest.strategy_validator import _build_equal_weight_panel

    dates = _dates(20)
    frames = {
        "AAA": _close_frame(dates, np.full(20, 100.0)),
        "LATE": _close_frame(dates[10:], np.full(10, 50.0)),
        # 중간 결측(거래정지)과 조기 종료가 섞인 종목
        "GAPPY": _close_frame(dates[:15].delete(5), np.arange(14, dtype=float) + 1.0),
    }

    panel = _build_equal_weight_panel(
        _PanelCollector(frames), list(frames), str(dates[0].date()), str(dates[-1].date())
    )

    assert panel.index[0] == dates[0]
    assert panel.index[-1] == dates[-1]
    assert panel["LATE"].iloc[:10].isna().all()
    assert panel["LATE"].iloc[10:].notna().all()
    # 중간 결측은 직전 종가로 채우고, 데이터 종료 이후는 NaN으로 둔다.
    assert panel["GAPPY"].iloc[5] == panel["GAPPY"].iloc[4]
    assert panel["GAPPY"].iloc[15:].isna().all()


def test_staggered_equal_weight_equity_enters_late_name_at_its_first_price():
    from backtest.strategy_validator import _staggered_equal_weight_equity

    dates = _dates(6)
    panel = pd.DataFrame(
        {
            "A": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "B": [100.0, 200.0, 200.0, 200.0, 200.0, 200.0],
            "C": [np.nan, np.nan, np.nan, 50.0, 50.0, 100.0],
        },
        index=dates,
    )

    equity = _staggered_equal_weight_equity(panel, 1_000.0)

    assert equity.index[0] == dates[0]
    # A·B 반반 매수 → B 두 배: 500 + 1,000 = 1,500
    assert equity.iloc[1] == pytest.approx(1_500.0)
    # C 편입일: 평가액 1,500의 1/3(500)을 C에 배정, A·B는 2/3로 축소 → 평가액 그대로
    assert equity.iloc[3] == pytest.approx(1_500.0)
    # C 두 배: 1,000(A·B) + 1,000(C) = 2,000 — 동일 비중 재조정 없이 매수·보유
    assert equity.iloc[5] == pytest.approx(2_000.0)


def test_equal_weight_metrics_cover_whole_period_despite_late_listing():
    from backtest.strategy_validator import _equal_weight_buy_and_hold_metrics

    dates = _dates(320)
    frames = _top50_frames(dates)

    metrics = _equal_weight_buy_and_hold_metrics(
        _PanelCollector(frames), list(frames), str(dates[0].date()), str(dates[-1].date()), 1_000_000.0
    )

    # 예전 inner 조인은 CCC 상장일부터만 재서 AAA·BBB의 초기 +10%를 놓쳤다(0%).
    assert metrics["total_return"] == pytest.approx(10.0, abs=0.01)


def test_validator_top50_benchmark_spans_strategy_period(monkeypatch, tmp_path):
    import backtest.strategy_validator as sv

    dates = _dates(320)
    rng = np.random.default_rng(12)
    strategy_close = 50_000 * np.cumprod(1 + rng.normal(0.0003, 0.015, len(dates)))
    frames = {
        "005930": _close_frame(dates, strategy_close),
        "KS11": _close_frame(dates, strategy_close * 0.05),
        **_top50_frames(dates),
    }
    monkeypatch.setattr(sv, "DataCollector", lambda: _PanelCollector(frames))
    monkeypatch.setattr(sv, "_get_kospi_top_n_symbols", lambda *a, **k: ["AAA", "BBB", "CCC"])

    validator = sv.StrategyValidator(output_dir=str(tmp_path))
    result = validator.run(
        symbol="005930",
        strategy_name="scoring",
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )

    top50 = result["benchmark_top50"]
    assert top50["coverage"]["at_start"] == 2
    assert top50["coverage"]["late_entries"] == {"CCC": str(dates[100].date())}
    assert top50["full"]["total_return"] == pytest.approx(10.0, abs=0.01)
    # 인샘플(0~223행)도 CCC 상장일로 잘리지 않고 처음부터 잰다.
    assert top50["in_sample"]["total_return"] == pytest.approx(10.0, abs=0.01)
    assert top50["out_sample"]["total_return"] == pytest.approx(0.0, abs=0.01)
    assert "편입 범위" in validator.render_text_report(result)
