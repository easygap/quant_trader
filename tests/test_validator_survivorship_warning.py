"""검증 벤치마크 유니버스의 런타임 생존자 편향 폴백 경고 회귀 테스트.

생존자 통제 모드(historical/kospi200)를 요청했지만 pykrx 시점 데이터를 못 받아
현재 상장 목록(fdr_fallback)으로 폴백하면, 벤치마크가 과대평가되므로 조용히 넘어가지
않고 크게 경고해야 한다(config mode가 아니라 실제 데이터 출처로 판정).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import pandas as pd
from loguru import logger

import core.data_collector as dc_mod
from core.data_collector import DataCollector
import backtest.strategy_validator as sv


def _cfg(mode):
    return SimpleNamespace(risk_params={"backtest_universe": {"mode": mode, "exclude_administrative": False}})


def _stocks(source):
    return pd.DataFrame([
        {"Code": "005930", "Name": "A", "Market": "KOSPI", "Marcap": 9e12, "universe_source": source},
        {"Code": "000660", "Name": "B", "Market": "KOSPI", "Marcap": 8e12, "universe_source": source},
    ])


def _capture_warnings(fn):
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        fn()
    finally:
        logger.remove(sink)
    return msgs


def test_runtime_fallback_emits_survivorship_warning(monkeypatch):
    monkeypatch.setattr(DataCollector, "get_krx_stock_list",
                        staticmethod(lambda **kw: _stocks("fdr_fallback")))
    msgs = _capture_warnings(
        lambda: sv._get_kospi_top_n_symbols(DataCollector(), top_n=50, config=_cfg("historical"))
    )
    assert any("생존자 편향 미통제" in m for m in msgs), msgs


def test_pit_data_does_not_warn(monkeypatch):
    monkeypatch.setattr(DataCollector, "get_krx_stock_list",
                        staticmethod(lambda **kw: _stocks("pykrx_pit")))
    msgs = _capture_warnings(
        lambda: sv._get_kospi_top_n_symbols(DataCollector(), top_n=50, config=_cfg("kospi200"))
    )
    assert not any("생존자 편향 미통제" in m for m in msgs), msgs


def test_kospi200_fallback_tagged_fdr_fallback():
    """_get_kospi200_constituents 폴백 경로가 universe_source=fdr_fallback로 태깅되는지."""
    import core.data_collector as dc
    # pykrx 비활성 → FDR 폴백 경로 강제
    orig = dc.HAS_PYKRX
    dc.HAS_PYKRX = False
    try:
        df = DataCollector._get_kospi200_constituents("2023-06-01")
    finally:
        dc.HAS_PYKRX = orig
    if not df.empty:
        assert "universe_source" in df.columns
        assert set(df["universe_source"]) == {"fdr_fallback"}
