"""지수 자료가 최신인지 확인하는 회귀 테스트 (2026-09-23 점검).

FDR의 KS200·KS11 자료가 2026-09-17에서 멈췄다. 비어 있지 않은 표는 성공으로 보던
탓에 폴백이 한 번도 시도되지 않았고, kr_pocket 추세 필터는 '직전 상태 유지'로 동결,
평가의 'NAV vs KOSPI'는 며칠 전 종가로 계산됐다 — 전부 경고 로그만 남았다.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import core.data_collector as dc_mod
from core.data_collector import DataCollector


def _frame(days, start=100.0):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="date")
    closes = [start + i for i in range(len(days))]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000},
        index=idx,
    )


@pytest.fixture
def fixed_target(monkeypatch):
    monkeypatch.setattr(dc_mod, "_freshness_target", lambda end: date(2026, 9, 22))


def test_stale_fdr_tail_is_filled_from_yfinance(monkeypatch, fixed_target):
    dc = DataCollector()
    fdr_df = _frame(["2026-09-15", "2026-09-16", "2026-09-17"])
    yf_df = _frame(["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"], 200)
    monkeypatch.setattr(dc, "_fetch_korean_stock_via_yfinance", lambda s, a, b: yf_df)

    merged, source = dc._fill_stale_tail("KS200", fdr_df, "2026-09-23")

    assert source == "FinanceDataReader+yfinance"
    assert merged.index.max().date() == date(2026, 9, 22)
    # 기존 FDR 봉은 바꾸지 않는다
    assert merged.loc["2026-09-17", "close"] == fdr_df.loc["2026-09-17", "close"]
    assert list(merged.index.strftime("%m-%d")) == ["09-15", "09-16", "09-17", "09-18", "09-21", "09-22"]


def test_fresh_fdr_is_left_alone(monkeypatch, fixed_target):
    dc = DataCollector()
    fdr_df = _frame(["2026-09-21", "2026-09-22"])
    called = MagicMock()
    monkeypatch.setattr(dc, "_fetch_korean_stock_via_yfinance", called)

    out, source = dc._fill_stale_tail("069500", fdr_df, "2026-09-23")

    assert source == "FinanceDataReader" and out is fdr_df
    called.assert_not_called()


def test_old_requests_are_not_checked(monkeypatch):
    """백테스트처럼 과거 구간 요청은 최신 여부를 확인하지 않는다(네트워크를 안 쓴다)."""
    assert dc_mod._freshness_target("2020-01-31") is None


def test_freshness_target_skips_holidays_and_today():
    from core.trading_hours import _now_kst

    target = dc_mod._freshness_target(_now_kst().date().isoformat())
    assert target is not None and target < _now_kst().date()


def test_benchmark_return_refuses_stale_last_bar(monkeypatch, fixed_target):
    """마지막 봉이 기준 거래일보다 오래됐으면 그 값으로 수익률을 내지 않는다."""
    import FinanceDataReader as fdr

    stale = _frame(["2026-06-10", "2026-09-17"]).rename(columns={"close": "Close"})
    monkeypatch.setattr(fdr, "DataReader", lambda *a, **k: stale)
    monkeypatch.setattr(dc_mod, "HAS_YF", False)

    assert DataCollector.fetch_benchmark_return("2026-06-10", "2026-09-22", "KS11") is None


def test_benchmark_return_uses_fresh_fallback(monkeypatch, fixed_target):
    import FinanceDataReader as fdr

    stale = _frame(["2026-06-10", "2026-09-17"]).rename(columns={"close": "Close"})
    fresh = pd.DataFrame({"Close": [100.0, 90.0]},
                         index=pd.DatetimeIndex([pd.Timestamp("2026-06-10"), pd.Timestamp("2026-09-22")]))
    monkeypatch.setattr(fdr, "DataReader", lambda *a, **k: stale)
    monkeypatch.setattr(dc_mod, "HAS_YF", True)
    monkeypatch.setattr(dc_mod, "_yf_download_flat", lambda t, s, e: fresh)

    assert DataCollector.fetch_benchmark_return("2026-06-10", "2026-09-22", "KS11") == pytest.approx(-10.0)


def test_yfinance_fallback_maps_index_and_includes_end_day(monkeypatch):
    dc = DataCollector()
    seen = []

    def _fake(ticker, start, end):
        seen.append((ticker, end))
        return pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
             "Close": [1.0, 2.0], "Volume": [1, 1]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-09-21"), pd.Timestamp("2026-09-22")], name="Date"),
        )

    monkeypatch.setattr(dc_mod, "_yf_download_flat", _fake)
    out = dc._fetch_korean_stock_via_yfinance("KS200", "2026-09-01", "2026-09-22")

    assert seen[0] == ("^KS200", "2026-09-23")   # end는 배타적 — 하루 뒤로
    assert not out.empty and out.index.max().date() == date(2026, 9, 22)


def test_yfinance_fallback_tries_kosdaq_suffix(monkeypatch):
    dc = DataCollector()
    tried = []

    def _fake(ticker, start, end):
        tried.append(ticker)
        if ticker.endswith(".KS"):
            return pd.DataFrame()
        return pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
             "Close": [1.0, 2.0], "Volume": [1, 1]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-09-21"), pd.Timestamp("2026-09-22")], name="Date"),
        )

    monkeypatch.setattr(dc_mod, "_yf_download_flat", _fake)
    out = dc._fetch_korean_stock_via_yfinance("091990", "2026-09-01", "2026-09-22")

    assert tried == ["091990.KS", "091990.KQ"]
    assert not out.empty


def test_flat_download_flattens_multiindex(monkeypatch):
    cols = pd.MultiIndex.from_tuples([("Close", "^KS200"), ("Open", "^KS200")])
    df = pd.DataFrame([[1.0, 1.0]], columns=cols)
    monkeypatch.setattr(dc_mod.yf, "download", lambda *a, **k: df.copy())
    out = dc_mod._yf_download_flat("^KS200", "2026-09-01", "2026-09-23")
    assert list(out.columns) == ["Close", "Open"]


# ------------------------------------------------------------ 추세 필터를 ETF로 대신 판단

def _rebalancer_for_proxy(monkeypatch, index_df, proxy_df):
    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "kr_pocket"
    rb.config = MagicMock()
    rb.data_collector = MagicMock()
    rb.data_collector.fetch_korean_stock.side_effect = (
        lambda sym, s, e: index_df if sym == "KS200" else proxy_df
    )
    rb._overlay_input_issues = []
    rb._overlay_source_dates = {}
    monkeypatch.setattr(rb, "_overlay_previous_session", lambda: date(2026, 9, 22))
    return rb


def test_trend_filter_falls_back_to_tracking_etf_when_index_is_stale(monkeypatch):
    days = pd.bdate_range("2025-06-01", "2026-09-22")
    stale_index = _frame([d for d in days if d <= pd.Timestamp("2026-09-17")])
    proxy = _frame(list(days), 50_000)
    rb = _rebalancer_for_proxy(monkeypatch, stale_index, proxy)

    closes = rb._fetch_index_closes("KS200", 200)

    assert closes is not None and len(closes) >= 200
    assert closes[-1] == pytest.approx(proxy["close"].iloc[-1])
    assert any("069500" in i for i in rb._overlay_input_issues)
    # ETF로 판단했으니 '비중 확대 보류' 문구는 남기지 않는다
    assert not any(i.startswith("지수 종가:") for i in rb._overlay_input_issues)
    assert rb._overlay_source_dates.get("069500 종가(지수 대신)") == "2026-09-22"


def test_trend_filter_keeps_issue_when_proxy_also_stale(monkeypatch):
    days = pd.bdate_range("2025-06-01", "2026-09-17")
    stale = _frame(list(days))
    rb = _rebalancer_for_proxy(monkeypatch, stale, stale)

    assert rb._fetch_index_closes("KS200", 200) is None
    assert any(i.startswith("지수 종가:") for i in rb._overlay_input_issues)


def test_overlay_message_names_the_real_cause():
    from core.risk_overlays import compute_decision, parse_overlay_config

    cfg = parse_overlay_config({"overlays": {"trend_filter": {"enabled": True, "ma_days": 200}}})
    empty = compute_decision(cfg, index_closes=None, prev_state={"trend_below": False})
    short = compute_decision(cfg, index_closes=[1.0] * 50, prev_state={"trend_below": False})
    assert any("쓸 수 없음" in i for i in empty.data_issues)
    assert any("200일치 부족" in i for i in short.data_issues)


def test_health_lists_overlay_data_issues():
    from core.operator_health import summarize_basket_operation

    out = summarize_basket_operation(
        ["kr_pocket"], date(2026, 9, 23), 2, date(2026, 9, 23),
        data_notes=["바스켓 'kr_pocket' 위험 관리에 쓸 자료 문제: 지수 종가: 최근 기록 2026-09-17"],
    )
    assert out["verdict"] == "ATTENTION"
    assert any("위험 관리에 쓸 자료 문제" in n for n in out["notes"])


def test_benchmark_return_ignores_empty_close_rows(monkeypatch, fixed_target):
    """yfinance가 값이 빈 봉을 끼워 줘도 NaN 수익률을 내지 않는다(실측: 9/22 빈 봉)."""
    import FinanceDataReader as fdr

    monkeypatch.setattr(fdr, "DataReader", lambda *a, **k: pd.DataFrame())
    gappy = pd.DataFrame(
        {"Close": [100.0, 95.0, float("nan")]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-06-10"), pd.Timestamp("2026-09-21"),
                                pd.Timestamp("2026-09-22")]),
    )
    monkeypatch.setattr(dc_mod, "HAS_YF", True)
    monkeypatch.setattr(dc_mod, "_yf_download_flat", lambda t, s, e: gappy)

    # 마지막 유효 봉(9/21)이 기준(9/22)보다 오래됐으니 계산하지 않는다 — NaN도 아니다
    assert DataCollector.fetch_benchmark_return("2026-06-10", "2026-09-22", "KS11") is None
