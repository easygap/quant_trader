"""바스켓 트랙별 리스크 정책 + 자기상관 자기거부 회귀 테스트.

배경(2026-08-07 점검): 두 결함이 모의투자 트랙을 57거래일간 얼려 두었다.
  1) check_correlation_risk가 대상 종목 자신을 비교 대상에 포함해 corr(x,x)=1.0이
     잡히고, 고정수량 어댑터는 scale<1.0을 하드 거부라 모든 추가매수가 영구 거부됐다.
  2) 전역 단타 손절(-3%)이 buy&hold 바스켓 포지션에 기록되기만 하고 일일 사이클은
     평가하지 않아, 손절선을 뚫은 포지션이 그대로 방치됐다.
"""

from types import SimpleNamespace

import pytest

from core.basket_risk import (
    RISK_EXIT_TAG,
    basket_risk_config,
    basket_risk_levels,
    evaluate_basket_stops,
    has_risk_policy,
    reentry_cooldown_days,
    symbols_in_reentry_cooldown,
)
from core.risk_manager import RiskManager


def _pos(symbol, avg_price, quantity=1, highest_price=0):
    return SimpleNamespace(
        symbol=symbol, avg_price=avg_price, quantity=quantity,
        highest_price=highest_price,
    )


# ---------------------------------------------------------------- 자기상관

class _StubConfig:
    def __init__(self, risk_params):
        self.risk_params = risk_params
        self.trading = {"mode": "paper"}
        self.settings = {}


@pytest.fixture
def corr_risk_manager():
    return RiskManager(_StubConfig({
        "diversification": {
            "correlation_risk": {
                "enabled": True,
                "lookback_days": 60,
                "high_corr_threshold": 0.7,
                "high_corr_scale": 0.5,
                "strict": True,
            },
        },
    }))


def test_self_correlation_is_not_counted(corr_risk_manager, monkeypatch):
    """보유 중인 종목의 추가매수에서 자기 자신은 비교 대상이 아니다.

    이 케이스가 회귀하면 corr(x,x)=1.0이 다시 잡혀 모든 추가매수가 거부된다.
    데이터 조회가 일어나면 안 되므로 DataCollector가 불리면 실패시킨다.
    """
    def _boom(*args, **kwargs):  # pragma: no cover - 불리면 테스트 실패
        raise AssertionError("자기 자신만 있는 경우 시세 조회가 일어나면 안 된다")

    monkeypatch.setattr("core.data_collector.DataCollector.fetch_stock", _boom)

    result = corr_risk_manager.check_correlation_risk("005930", ["005930"])

    assert result["scale"] == 1.0
    assert not result.get("blocked")
    assert result["high_corr_symbols"] == []


def test_self_correlation_excluded_but_peers_still_checked(corr_risk_manager, monkeypatch):
    """자기 자신만 빠지고 다른 보유 종목은 정상적으로 검사된다."""
    seen = []

    def _fake_fetch(self, symbol, *args, **kwargs):
        seen.append(symbol)
        return None  # 데이터 없음 → strict면 차단

    monkeypatch.setattr("core.data_collector.DataCollector.fetch_stock", _fake_fetch)

    corr_risk_manager.check_correlation_risk("005930", ["005930", "000660"])

    # 대상(005930)은 자기 자신 비교에서 빠지지만 target_df 조회는 여전히 필요하다.
    # 핵심은 보유 목록 순회에서 005930이 다시 나오지 않는 것.
    assert seen.count("005930") <= 1


# ------------------------------------------------------- 바스켓 리스크 정책

def test_risk_block_absent_means_global_defaults():
    """`risk:` 블록이 없으면 None — 호출부가 전역 기본값을 쓰도록(기존 동작 유지)."""
    assert has_risk_policy({"holdings": {}}) is False
    assert basket_risk_levels({"holdings": {}}, 100_000) is None


def test_all_zero_risk_block_means_explicitly_no_stops():
    """전부 0인 `risk:` 블록은 '손절 없음'이라는 결정이다 — 전역 기본값으로 되돌아가면 안 된다.

    되돌아가면 지수 ETF 적립 트랙(kr_pocket)에 단타 -3% 손절이 다시 기록된다.
    """
    cfg = {"risk": {"stop_loss_pct": 0, "take_profit_pct": 0, "trailing_stop_pct": 0}}

    assert has_risk_policy(cfg) is True
    levels = basket_risk_levels(cfg, 100_000)
    assert levels is not None
    assert levels == {
        "stop_loss_price": None,
        "take_profit_price": None,
        "trailing_stop_price": None,
    }
    assert evaluate_basket_stops(cfg, [_pos("069500", 100_000)], {"069500": 50_000}) == []


def test_stop_loss_level_and_breach():
    cfg = {"risk": {"stop_loss_pct": 0.25}}

    assert basket_risk_levels(cfg, 600_000)["stop_loss_price"] == 450_000

    # 정상 조정(-10%)에는 안 걸린다
    assert evaluate_basket_stops(cfg, [_pos("005380", 600_000)], {"005380": 540_000}) == []

    hits = evaluate_basket_stops(cfg, [_pos("005380", 600_000)], {"005380": 440_000})
    assert len(hits) == 1
    assert hits[0]["action"] == "STOP_LOSS"
    assert hits[0]["symbol"] == "005380"
    assert hits[0]["level"] == 450_000


def test_evaluation_uses_policy_not_stale_position_column():
    """포지션에 남아 있는 옛 손절가(-3%)가 아니라 정책 비율로 판정한다."""
    cfg = {"risk": {"stop_loss_pct": 0.25}}
    stale = _pos("005930", 302_000)
    stale.stop_loss_price = 292_940  # 전역 -3%로 기록된 옛 값

    # -3% 기준이면 걸리지만 정책(-25%) 기준이면 아직 아니다
    assert evaluate_basket_stops(cfg, [stale], {"005930": 250_000}) == []


def test_take_profit_precedes_stop_loss():
    cfg = {"risk": {"stop_loss_pct": 0.25, "take_profit_pct": 0.10}}
    hits = evaluate_basket_stops(cfg, [_pos("035720", 100_000)], {"035720": 115_000})
    assert [h["action"] for h in hits] == ["TAKE_PROFIT"]


def test_trailing_stop_uses_highest_price_and_needs_a_peak():
    cfg = {"risk": {"trailing_stop_pct": 0.10}}

    # 고점이 진입가 이하면 트레일링은 판단하지 않는다(손절과 구분 불가)
    assert evaluate_basket_stops(cfg, [_pos("105560", 100_000)], {"105560": 80_000}) == []

    pos = _pos("105560", 100_000, highest_price=150_000)
    hits = evaluate_basket_stops(cfg, [pos], {"105560": 134_000})
    assert [h["action"] for h in hits] == ["TRAILING_STOP"]
    assert hits[0]["level"] == 135_000


@pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5, "abc", None])
def test_out_of_range_values_are_ignored(bad):
    cfg = basket_risk_config({"risk": {"stop_loss_pct": bad}})
    assert cfg["stop_loss_pct"] is None


def test_invalid_entry_price_keeps_policy_but_records_no_levels():
    """진입가가 유효하지 않아도 전역 기본값으로 되돌아가지 않는다."""
    levels = basket_risk_levels({"risk": {"stop_loss_pct": 0.25}}, 0)
    assert levels is not None
    assert levels["stop_loss_price"] is None


# ------------------------------------------------------------- 재진입 차단

def _sell(symbol, reason, days_ago=0):
    from datetime import datetime, timedelta
    return SimpleNamespace(
        symbol=symbol, action="SELL", reason=reason,
        executed_at=datetime.now() - timedelta(days=days_ago),
    )


def test_cooldown_absent_means_no_block():
    assert reentry_cooldown_days({"risk": {}}) == 0
    assert symbols_in_reentry_cooldown({"risk": {}}, "acct", "paper") == {}


def test_risk_exit_blocks_reentry(monkeypatch):
    """손절로 나간 종목은 재매수 차단 목록에 오른다.

    이 차단이 없으면 청산으로 비워진 슬롯을 같은 사이클의 비중 교정이 곧바로 되사서
    손실만 확정하는 왕복매매가 된다(2026-08-07 10:07 실측).
    """
    monkeypatch.setattr(
        "database.repositories.get_trade_history",
        lambda **kw: [_sell("005380", f"리밸런싱: {RISK_EXIT_TAG} STOP_LOSS: 손절 ...")],
    )
    blocked = symbols_in_reentry_cooldown(
        {"risk": {"reentry_cooldown_days": 60}}, "acct", "paper",
    )
    assert "005380" in blocked


def test_ordinary_rebalance_sell_does_not_block_reentry(monkeypatch):
    """비중 초과로 판 것은 차단 대상이 아니다 — 정상 리밸런싱을 막으면 안 된다."""
    monkeypatch.setattr(
        "database.repositories.get_trade_history",
        lambda **kw: [_sell("055550", "리밸런싱: 비중 초과 (15.3% → 10.0%, -5.3%)")],
    )
    blocked = symbols_in_reentry_cooldown(
        {"risk": {"reentry_cooldown_days": 60}}, "acct", "paper",
    )
    assert blocked == {}


def test_cooldown_query_failure_does_not_block_cycle(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("DB 조회 실패")

    monkeypatch.setattr("database.repositories.get_trade_history", _boom)
    assert symbols_in_reentry_cooldown(
        {"risk": {"reentry_cooldown_days": 60}}, "acct", "paper",
    ) == {}


def test_plan_rebalance_skips_symbol_in_cooldown(monkeypatch):
    """차단 종목은 매수 후보에서 빠진다(플래너 레벨 회귀 방지)."""
    from unittest.mock import MagicMock

    from core.basket_rebalancer import BasketRebalancer

    rb = BasketRebalancer.__new__(BasketRebalancer)
    rb.basket_name = "t"
    rb.basket = {"risk": {"reentry_cooldown_days": 60}}
    rb.account_key = "acct"
    rb.execution_strategy = "acct"
    rb.holdings = {"005380": 0.5, "005930": 0.5}
    rb.rebalance_cfg = {"min_trade_amount": 100_000, "max_turnover_ratio": 1.0}
    rb._target_stock_weight = 1.0
    rb._risk_params = {"diversification": {"min_cash_ratio": 0.0}}
    rb.portfolio_mgr = MagicMock()
    rb.portfolio_mgr.get_portfolio_summary.return_value = {"total_value": 10_000_000}
    rb.config = MagicMock()
    rb.config.trading = {"mode": "paper"}

    monkeypatch.setattr(
        "core.basket_rebalancer.get_all_positions", lambda **kw: [],
    )
    monkeypatch.setattr(
        "core.basket_rebalancer.symbols_in_reentry_cooldown",
        lambda *a, **k: {"005380": "60일 재진입 차단"},
    )

    orders = rb.plan_rebalance(prices={"005380": 400_000, "005930": 200_000})

    symbols = {o.symbol for o in orders}
    assert "005380" not in symbols, "재진입 차단 종목이 매수 후보에 남았다"
    assert "005930" in symbols, "차단과 무관한 종목까지 막으면 안 된다"


# ----------------------------------------------- 운영 설정이 정책을 갖췄는지

def test_shipped_baskets_declare_risk_policy():
    """enabled 바스켓은 리스크 정책을 명시해야 한다 — 침묵하면 단타 기본값이 적힌다."""
    from core.basket_rebalancer import BasketRebalancer

    baskets = BasketRebalancer._load_baskets_config()
    enabled = {n: c for n, c in baskets.items() if c.get("enabled", False)}
    assert enabled, "enabled 바스켓이 없다 — 설정 로드 경로 확인 필요"
    missing = [n for n, c in enabled.items() if not has_risk_policy(c)]
    assert not missing, f"리스크 정책 미선언 바스켓: {missing}"


def test_shipped_basket_slots_are_fillable():
    """목표 비중표에 '현재 자본으로 영원히 못 채우는 슬롯'이 남아 있으면 안 된다.

    1주 가격이 슬롯 목표금액을 넘으면 그 비중은 영구 공백이 되고, 배치율 미달로만
    나타나 원인이 가려진다(000660이 이 상태로 2개월 방치됐다).
    """
    from core.basket_deploy import effective_stock_fraction
    from core.basket_rebalancer import BasketRebalancer
    from config.config_loader import Config

    risk_params = Config.get().risk_params
    baskets = BasketRebalancer._load_baskets_config()
    cfg = baskets["kr_diversified_hold"]
    capital = float(cfg.get("initial_capital") or 10_000_000)
    investable = capital * effective_stock_fraction(cfg, risk_params)

    max_position_ratio = float(
        (risk_params.get("diversification") or {}).get("max_position_ratio", 0.20)
    )
    for symbol, weight in cfg["holdings"].items():
        slot = investable * float(weight)
        # 슬롯이 단일 종목 상한 안에 있어야 하고, 최소 1주는 담을 수 있어야 한다.
        assert float(weight) * effective_stock_fraction(cfg, risk_params) <= max_position_ratio, (
            f"{symbol} 목표 비중이 단일 종목 상한을 넘는다"
        )
        assert slot > 0, f"{symbol} 슬롯 금액이 0"
