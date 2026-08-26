"""선언한 비중표가 전역 안전 파라미터에 막히지 않는지 검증한다.

배경(2026-08-26): kr_pocket에 첫 적립금 10만원을 넣었는데 매수로 전환되지 않았다.
개별 주식용 전역 파라미터가 ETF 2종 바스켓의 설계를 차례로 막았기 때문이다.

    단일 종목 20% 상한   vs 선언 47.5%  → '단일 종목 비중 20% 초과'
    전체 투자 70% 상한   vs 선언 95%    → (위를 통과했어도 곧 걸림)
    최소 현금 20%        vs 선언 5%     → '최소 현금 비중 20% 미만'
    업종 비중(KRX)       ETF는 업종 코드 없음 → '업종 매핑 없음'
    실적 발표일 필터     ETF는 실적일 없음    → '실적일 조회 불가'

다섯 개 다 fail-closed라 조용히 '매수 0건'으로만 나타났다. 전역값은 재량 매매용
안전판이므로, 비중을 명시로 선언한 주문에는 그 선언에서 파생한 상한으로 바꿔 단다.
상한을 없애는 게 아니다 — 어떤 경우에도 목표를 드리프트 임계값 이상 넘길 수 없다.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.basket_rebalancer import BasketRebalancer
from core.instrument_classes import is_non_company_symbol, non_company_symbols
from core.risk_manager import RiskManager


class _Cfg:
    def __init__(self, risk_params):
        self.risk_params = risk_params
        self.trading = {"mode": "paper"}
        self.settings = {}


def _rm(**div):
    base = {
        "max_position_ratio": 0.20,
        "max_investment_ratio": 0.70,
        "min_cash_ratio": 0.20,
        "max_positions": 10,
        "sector_map_strict": True,
        "max_sector_ratio": 0.40,
    }
    base.update(div)
    return RiskManager(_Cfg({"diversification": base}))


# ------------------------------------------------------- 종목 성격 분류

def test_non_company_symbols_reads_config():
    rp = {"instrument_classes": {"non_company_symbols": ["069500", "357870"]}}
    assert non_company_symbols(rp) == {"069500", "357870"}
    assert is_non_company_symbol("069500", rp) is True
    assert is_non_company_symbol("005930", rp) is False


@pytest.mark.parametrize("rp", [
    None, {}, {"instrument_classes": None}, {"instrument_classes": {}},
    {"instrument_classes": {"non_company_symbols": None}},
    {"instrument_classes": {"non_company_symbols": "069500"}},  # 문자열은 무시
])
def test_non_company_symbols_malformed_config_is_empty(rp):
    assert non_company_symbols(rp) == set()
    assert is_non_company_symbol("069500", rp) is False


def test_empty_symbol_is_never_exempt():
    rp = {"instrument_classes": {"non_company_symbols": ["069500"]}}
    assert is_non_company_symbol("", rp) is False


# ------------------------------------------------------- 노출 상한 위임

def _exposure(rm, **kw):
    args = dict(
        current_positions=1, position_value=100_000, total_value=1_000_000,
        available_cash=900_000, current_invested=100_000, symbol="069500",
        existing_position_value=100_000, is_new_position=False,
    )
    args.update(kw)
    return rm.check_projected_exposure(**args)


def test_global_position_cap_blocks_without_override():
    """기본 동작은 그대로 — 전역 20% 상한이 살아 있어야 한다."""
    r = _exposure(_rm(), position_value=150_000)  # 기존 10만 + 15만 = 25%
    assert r["can_buy"] is False
    assert "단일 종목" in r["reason"]


def test_declared_weight_override_allows_the_design():
    """선언 비중에서 파생한 상한을 주면 설계대로 담을 수 있다."""
    r = _exposure(
        _rm(), position_value=150_000,
        exposure_limits={"max_position_ratio": 0.55},
    )
    assert r["can_buy"] is True


def test_override_still_caps_beyond_declared_weight():
    """위임은 무제한이 아니다 — 준 상한을 넘으면 여전히 막는다."""
    r = _exposure(
        _rm(), position_value=600_000,   # 기존 10만 + 60만 = 70%
        exposure_limits={"max_position_ratio": 0.55},
    )
    assert r["can_buy"] is False


def test_investment_and_cash_overrides():
    rm = _rm()
    blocked = _exposure(rm, position_value=250_000, current_invested=500_000)
    assert blocked["can_buy"] is False

    allowed = _exposure(
        rm, position_value=250_000, current_invested=500_000,
        exposure_limits={
            "max_position_ratio": 0.55,
            "max_investment_ratio": 0.98,
            "min_cash_ratio": 0.05,
        },
    )
    assert allowed["can_buy"] is True, allowed["reason"]


def test_partial_override_falls_back_to_global_for_unset_keys():
    """일부만 지정하면 나머지는 전역값을 그대로 쓴다."""
    r = _exposure(
        _rm(), position_value=150_000, current_invested=650_000,
        exposure_limits={"max_position_ratio": 0.55},   # 투자비율은 미지정
    )
    assert r["can_buy"] is False
    assert "전체 투자 비중" in r["reason"]


# ------------------------------------------------------- 업종 검사 면제

def _sector_args(**kw):
    args = dict(
        current_positions=1, position_value=100_000, total_value=1_000_000,
        available_cash=900_000, current_invested=100_000, symbol="069500",
        sector_map={"005930": "전기전자"}, positions=[],
        existing_position_value=100_000, is_new_position=False,
    )
    args.update(kw)
    return args


def test_unmapped_stock_is_still_blocked_fail_closed():
    """개별 주식의 업종 매핑 누락은 계속 fail-closed여야 한다."""
    rm = _rm()
    rm.risk_params["instrument_classes"] = {"non_company_symbols": ["069500"]}
    r = rm.check_diversification(**_sector_args(symbol="000660"))
    assert r["can_buy"] is False
    assert "업종" in r["reason"]


def test_non_company_symbol_skips_sector_check():
    """ETF는 KRX 업종 코드가 없다 — 면제 목록에 있으면 통과한다."""
    rm = _rm()
    rm.risk_params["instrument_classes"] = {"non_company_symbols": ["069500"]}
    r = rm.check_diversification(**_sector_args(symbol="069500"))
    assert r["can_buy"] is True, r["reason"]


# ------------------------------------- 리밸런서가 만드는 상한이 설계와 맞는가

def _rebalancer(basket):
    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "t"
    rb.basket = basket
    rb.holdings = basket["holdings"]
    rb.rebalance_cfg = basket.get("rebalance", {})
    rb._target_stock_weight = basket.get("target_stock_weight")
    rb._risk_params = {"diversification": {"min_cash_ratio": 0.20}}
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}
    return rb


def test_policy_limits_make_pocket_design_reachable():
    """kr_pocket 설계(종목 47.5% / 투자 95% / 현금 5%)가 상한 안에 들어와야 한다."""
    rb = _rebalancer({
        "target_stock_weight": 0.95,
        "min_cash_ratio": 0.05,
        "holdings": {"069500": 0.5, "357870": 0.5},
        "rebalance": {"drift_threshold": 0.08, "deployment_band": 0.03},
    })
    limits = rb._policy_exposure_limits("069500")
    assert limits["max_position_ratio"] >= 0.475
    assert limits["max_investment_ratio"] >= 0.95
    assert limits["min_cash_ratio"] == pytest.approx(0.05)


def test_policy_limits_do_not_exceed_target_plus_drift():
    """상한은 목표 + 드리프트 임계값을 넘지 않는다(무제한 위임 금지)."""
    rb = _rebalancer({
        "target_stock_weight": 0.60,
        "holdings": {f"A{i}": 1 / 9 for i in range(9)},
        "rebalance": {"drift_threshold": 0.08, "deployment_band": 0.03},
    })
    limits = rb._policy_exposure_limits("A0")
    assert limits["max_position_ratio"] == pytest.approx((1 / 9 + 0.08) * 0.60)
    assert limits["max_position_ratio"] < 0.20


def test_policy_limits_none_for_unknown_symbol():
    """비중표에 없는 종목은 위임하지 않는다(전역 상한 유지)."""
    rb = _rebalancer({
        "target_stock_weight": 0.60,
        "holdings": {"005930": 1.0},
        "rebalance": {},
    })
    assert rb._policy_exposure_limits("999999") is None


def test_policy_limits_bad_min_cash_falls_back_to_global():
    rb = _rebalancer({
        "target_stock_weight": 0.60,
        "min_cash_ratio": "많이",
        "holdings": {"005930": 1.0},
        "rebalance": {},
    })
    assert rb._policy_exposure_limits("005930")["min_cash_ratio"] == pytest.approx(0.20)


# ------------------------------------------------- 운영 설정 불변식

def test_shipped_etf_baskets_are_actually_buyable():
    """운영 중인 ETF 바스켓의 종목이 기업 단위 필터에 막히지 않아야 한다.

    이 불변식이 깨지면 적립금이 들어와도 매수 0건이 되고, 증상은 '배치율 미달'로만
    보여 원인이 가려진다.
    """
    from config.config_loader import Config

    risk_params = Config.get().risk_params
    baskets = BasketRebalancer._load_baskets_config()
    pocket = baskets.get("kr_pocket")
    assert pocket and pocket.get("enabled"), "kr_pocket이 없거나 비활성"
    for symbol in pocket["holdings"]:
        assert is_non_company_symbol(symbol, risk_params), (
            f"{symbol}이 instrument_classes.non_company_symbols에 없다 — "
            "업종·실적 필터에 fail-closed로 막힌다"
        )
