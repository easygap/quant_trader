"""RiskManager 단위 테스트 — 포지션 사이징 엣지 케이스"""
from types import SimpleNamespace

import pytest

from core.risk_manager import RiskManager, _get_tick_size


class _MockConfig:
    risk_params = {
        "position_sizing": {"max_risk_per_trade": 0.01},
        "diversification": {
            "max_position_ratio": 0.20,
            "max_investment_ratio": 0.70,
            "min_cash_ratio": 0.20,
            "correlation_risk": {
                "enabled": True,
                "lookback_days": 60,
                "high_corr_threshold": 0.7,
                "high_corr_scale": 0.5,
                "strict": True,
            },
        },
        "transaction_costs": {
            "slippage": 0.0005,
            "slippage_ticks": 2,
            "dynamic_slippage": {
                "enabled": True,
                "warn_at_volume_ratio": 0.01,
                "warn_slippage_multiplier": 2.0,
                "critical_at_volume_ratio": 0.03,
                "critical_slippage_multiplier": 4.0,
            },
        },
    }


@pytest.fixture
def risk_manager():
    return RiskManager(_MockConfig())


def test_position_size_entry_price_zero(risk_manager):
    """진입가 0이면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, 0, 9_000) == 0


def test_position_size_entry_price_negative(risk_manager):
    """진입가 음수면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, -100, 90) == 0


def test_position_size_risk_per_share_zero(risk_manager):
    """손절가 == 진입가면 0 반환"""
    assert risk_manager.calculate_position_size(10_000_000, 50_000, 50_000) == 0


def test_position_size_capital_zero(risk_manager):
    """자본 0이면 0 반환"""
    assert risk_manager.calculate_position_size(0, 50_000, 49_000) == 0


def test_position_size_normal(risk_manager):
    """정상 케이스: 양수 수량 반환"""
    qty = risk_manager.calculate_position_size(10_000_000, 50_000, 49_000)
    assert qty > 0
    assert isinstance(qty, int)


def test_tick_size():
    """호가 단위 헬퍼 (KRX: 5만원 미만 50원, 이상 100원 등)"""
    assert _get_tick_size(1000) == 1
    assert _get_tick_size(2000) == 5
    assert _get_tick_size(49999) == 50
    assert _get_tick_size(50000) == 100
    assert _get_tick_size(499999) == 500
    assert _get_tick_size(500000) == 1000
    assert _get_tick_size(0) == 1


def test_diversification_blocks_when_remaining_cash_too_low(risk_manager):
    """주문 후 남는 현금 비중이 설정값보다 낮으면 차단 (단일 종목 비중은 20% 이하로 두어 해당 검사 통과)"""
    result = risk_manager.check_diversification(
        current_positions=1,
        position_value=50_000,   # 5% of total → max_position_ratio 통과
        total_value=1_000_000,
        available_cash=100_000,  # 매수 후 잔여 현금 50k → 5% < min_cash_ratio 20%
    )
    assert result["can_buy"] is False
    assert "최소 현금 비중" in result["reason"]


def test_diversification_allows_when_cash_ratio_is_safe(risk_manager):
    """남는 현금 비중이 충분하면 통과"""
    result = risk_manager.check_diversification(
        current_positions=1,
        position_value=150_000,
        total_value=1_000_000,
        available_cash=450_000,
    )
    assert result["can_buy"] is True


def test_diversification_blocks_when_total_investment_ratio_exceeds_limit(risk_manager):
    result = risk_manager.check_diversification(
        current_positions=2,
        position_value=150_000,
        total_value=1_000_000,
        available_cash=400_000,
        current_invested=600_000,
    )
    assert result["can_buy"] is False
    assert "전체 투자 비중" in result["reason"]


def test_diversification_blocks_when_sector_map_missing_and_strict(risk_manager):
    """업종 cap이 켜져 있으면 섹터 맵 자체가 없을 때 신규 BUY를 막는다."""
    risk_manager.risk_params["diversification"]["max_sector_ratio"] = 0.40
    risk_manager.risk_params["diversification"]["sector_map_strict"] = True

    result = risk_manager.check_diversification(
        current_positions=0,
        position_value=100_000,
        total_value=1_000_000,
        available_cash=900_000,
        symbol="005930",
        sector_map={},
        positions=[],
    )

    assert result["can_buy"] is False
    assert "섹터 맵 없음" in result["reason"]


def test_diversification_blocks_when_target_sector_missing_and_strict(risk_manager):
    """매수 대상 업종 매핑이 없으면 업종 상한을 검증할 수 없으므로 차단한다."""
    risk_manager.risk_params["diversification"]["max_sector_ratio"] = 0.40
    risk_manager.risk_params["diversification"]["sector_map_strict"] = True

    result = risk_manager.check_diversification(
        current_positions=0,
        position_value=100_000,
        total_value=1_000_000,
        available_cash=900_000,
        symbol="005930",
        sector_map={"000660": "반도체"},
        positions=[],
    )

    assert result["can_buy"] is False
    assert "005930 업종 매핑 없음" in result["reason"]


def test_diversification_blocks_when_held_sector_missing_and_strict(risk_manager):
    """보유 종목 업종 매핑이 없으면 같은 업종 누적 비중을 계산할 수 없으므로 차단한다."""
    risk_manager.risk_params["diversification"]["max_sector_ratio"] = 0.40
    risk_manager.risk_params["diversification"]["sector_map_strict"] = True

    result = risk_manager.check_diversification(
        current_positions=1,
        position_value=100_000,
        total_value=1_000_000,
        available_cash=900_000,
        symbol="005930",
        sector_map={"005930": "반도체"},
        positions=[SimpleNamespace(symbol="000660", total_invested=100_000)],
    )

    assert result["can_buy"] is False
    assert "보유 종목 업종 매핑 없음" in result["reason"]
    assert "000660" in result["reason"]


def test_diversification_can_skip_sector_map_when_strict_disabled(risk_manager):
    """수동 설정으로 strict를 끄면 기존처럼 업종 맵 없이도 다른 분산 규칙만 본다."""
    risk_manager.risk_params["diversification"]["max_sector_ratio"] = 0.40
    risk_manager.risk_params["diversification"]["sector_map_strict"] = False

    result = risk_manager.check_diversification(
        current_positions=0,
        position_value=100_000,
        total_value=1_000_000,
        available_cash=900_000,
        symbol="005930",
        sector_map={},
        positions=[],
    )

    assert result["can_buy"] is True


def test_dynamic_slippage_increases_when_order_participation_is_high(risk_manager):
    low = risk_manager.calculate_transaction_costs(
        price=50_000,
        quantity=100,
        action="BUY",
        avg_daily_volume=1_000_000,
    )
    high = risk_manager.calculate_transaction_costs(
        price=50_000,
        quantity=30_000,
        action="BUY",
        avg_daily_volume=1_000_000,
    )
    assert high["slippage"] > low["slippage"]
    assert high["slippage_multiplier"] >= 2.0


def test_correlation_risk_blocks_when_target_price_data_lookup_fails(risk_manager, monkeypatch):
    """상관관계 리스크가 켜져 있으면 대상 종목 가격 조회 실패 시 fail-closed."""

    class FailingCollector:
        def fetch_stock(self, symbol):
            raise RuntimeError("price provider unavailable")

    monkeypatch.setattr("core.data_collector.DataCollector", lambda: FailingCollector())

    result = risk_manager.check_correlation_risk(
        "005930",
        ["000660"],
        lookback_days=60,
    )

    assert result["blocked"] is True
    assert result["scale"] == 0.0
    assert "상관관계 리스크 확인 실패" in result["reason"]


def test_correlation_risk_blocks_when_existing_position_data_is_insufficient(risk_manager, monkeypatch):
    """기존 보유 종목 데이터가 부족하면 신규 BUY 리스크 확인을 차단한다."""
    import pandas as pd

    dates = pd.date_range("2026-01-01", periods=70, freq="D")

    class PartialCollector:
        def fetch_stock(self, symbol):
            if symbol == "005930":
                return pd.DataFrame({"close": range(1, 71)}, index=dates)
            return pd.DataFrame({"close": [1, 2]}, index=dates[:2])

    monkeypatch.setattr("core.data_collector.DataCollector", lambda: PartialCollector())

    result = risk_manager.check_correlation_risk(
        "005930",
        ["000660"],
        lookback_days=60,
    )

    assert result["blocked"] is True
    assert result["scale"] == 0.0
    assert result["missing_symbols"] == ["000660"]


class TestEtfSellTaxExemption:
    """국내 상장 ETF 매도세 면제 — tax_exempt_symbols 등록 종목은 매도세 0.

    개별 주식 세율(0.20%)을 ETF에 일괄 적용하면 kr_pocket 같은 ETF 전용 바스켓의
    매도 비용이 과대계상돼 승격 게이트의 비용 상한(연 1%) 판정까지 왜곡된다.
    """

    @pytest.fixture
    def rm(self):
        class _Cfg:
            risk_params = {
                "transaction_costs": {
                    "commission_rate": 0.00015,
                    "tax_rate": 0.0020,
                    # yaml에서 숫자로 적혀도(따옴표 누락) 문자열 비교로 매칭돼야 한다
                    "tax_exempt_symbols": ["069500", 357870],
                    "slippage": 0.0005,
                    "slippage_ticks": 1,
                    "dynamic_slippage": {"enabled": False},
                },
            }
        return RiskManager(_Cfg())

    def test_exempt_symbol_sell_has_no_tax(self, rm):
        out = rm.calculate_transaction_costs(57715, 2, "SELL", symbol="069500")
        assert out["tax"] == 0

    def test_int_coded_yaml_entry_still_matches(self, rm):
        out = rm.calculate_transaction_costs(57715, 2, "SELL", symbol="357870")
        assert out["tax"] == 0

    def test_non_exempt_symbol_keeps_tax(self, rm):
        out = rm.calculate_transaction_costs(70000, 10, "SELL", symbol="005930")
        assert out["tax"] == round(700000 * 0.0020, 0)

    def test_no_symbol_keeps_legacy_behavior(self, rm):
        # symbol 미전달 호출부(백테스트·스크립트)는 기존 일괄 과세 유지
        out = rm.calculate_transaction_costs(70000, 10, "SELL")
        assert out["tax"] == round(700000 * 0.0020, 0)

    def test_exempt_sell_effective_price_excludes_tax(self, rm):
        exempt = rm.calculate_transaction_costs(57715, 2, "SELL", symbol="069500")
        taxed = rm.calculate_transaction_costs(57715, 2, "SELL", symbol="005930")
        assert exempt["effective_price"] > taxed["effective_price"]

    def test_buy_unaffected_by_exemption(self, rm):
        etf = rm.calculate_transaction_costs(57715, 2, "BUY", symbol="069500")
        stock = rm.calculate_transaction_costs(57715, 2, "BUY", symbol="005930")
        assert etf["tax"] == 0 and stock["tax"] == 0
        assert etf["total_cost"] == stock["total_cost"]
