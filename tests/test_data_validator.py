"""DataValidator OHLCV 정제 회귀 테스트."""

import pandas as pd

from core.data_validator import DataValidator


def test_clean_dataframe_does_not_backfill_leading_ohlc():
    """선행 OHLC 결측은 미래 가격으로 채우지 않고 제거한다."""
    df = pd.DataFrame(
        {
            "open": [None, 100.0, 101.0],
            "high": [None, 105.0, 106.0],
            "low": [None, 99.0, 100.0],
            "close": [None, 104.0, 105.0],
            "volume": [None, 1000, 1200],
        },
        index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
    )

    cleaned = DataValidator.clean_dataframe(df, "TEST")

    assert list(cleaned.index) == list(pd.to_datetime(["2026-01-05", "2026-01-06"]))
    assert cleaned.iloc[0]["open"] == 100.0


def test_clean_dataframe_forward_fills_only_from_past_rows():
    """중간 결측은 과거 값으로만 보정한다."""
    df = pd.DataFrame(
        {
            "open": [100.0, None, 110.0],
            "high": [105.0, None, 115.0],
            "low": [99.0, None, 109.0],
            "close": [104.0, None, 114.0],
            "volume": [1000, None, 1300],
        },
        index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
    )

    cleaned = DataValidator.clean_dataframe(df, "TEST")

    assert len(cleaned) == 3
    assert cleaned.loc[pd.Timestamp("2026-01-05"), "open"] == 100.0
    assert cleaned.loc[pd.Timestamp("2026-01-05"), "close"] == 104.0
    assert cleaned.loc[pd.Timestamp("2026-01-05"), "volume"] == 1000


def test_clean_dataframe_sorts_before_forward_fill():
    """입력 순서가 뒤섞여도 시간순 과거 값만 사용한다."""
    df = pd.DataFrame(
        {
            "open": [110.0, 100.0, None],
            "high": [115.0, 105.0, None],
            "low": [109.0, 99.0, None],
            "close": [114.0, 104.0, None],
            "volume": [1300, 1000, None],
        },
        index=pd.to_datetime(["2026-01-06", "2026-01-02", "2026-01-05"]),
    )

    cleaned = DataValidator.clean_dataframe(df, "TEST")

    assert list(cleaned.index) == list(
        pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    )
    assert cleaned.loc[pd.Timestamp("2026-01-05"), "open"] == 100.0
    assert cleaned.loc[pd.Timestamp("2026-01-05"), "close"] == 104.0


def test_clean_dataframe_drops_all_ohlc_missing_rows():
    """과거에도 채울 수 없는 OHLC 결측 행은 제거한다."""
    df = pd.DataFrame(
        {
            "open": [None, None, 100.0],
            "high": [None, None, 105.0],
            "low": [None, None, 99.0],
            "close": [None, None, 104.0],
            "volume": [100, 200, 1000],
        },
        index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
    )

    cleaned = DataValidator.clean_dataframe(df, "TEST")

    assert list(cleaned.index) == [pd.Timestamp("2026-01-06")]
