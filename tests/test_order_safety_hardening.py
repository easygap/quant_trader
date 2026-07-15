"""실전 주문 경계 안전성 회귀 테스트.

이 파일은 위험한 입력이 브로커 호출이나 로컬 장부 변경에 도달하지
않는지, 그리고 실전 노출이 불명확해진 즉시 global HALT가 영속화
시도되는지를 검증한다. tests/conftest.py가 먼저 QUANT_DB_PATH를 임시
DB로 고정하며, 아래 autouse fixture는 운영 DB로의 회귀를 한 번 더 차단한다.
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.kis_api import KISApi, authorized_kis_order_submission
from core.order_executor import OrderExecutor
from core.order_state import OrderBook, OrderRecord, OrderStatus
from core.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def _refuse_production_database_for_safety_tests():
    """주문 테스트가 운영 장부를 만지지 않도록 목적지를 확인한다."""
    configured = os.environ.get("QUANT_DB_PATH")
    assert configured, "tests/conftest.py must isolate QUANT_DB_PATH"
    assert Path(configured).resolve() != Path("data/quant_trader.db").resolve()


class _ForbiddenBroker:
    def __init__(self):
        self.calls = []

    def buy_order(self, *args, **kwargs):
        self.calls.append(("BUY", args, kwargs))
        raise AssertionError("invalid input reached the broker BUY boundary")

    def sell_order(self, *args, **kwargs):
        self.calls.append(("SELL", args, kwargs))
        raise AssertionError("invalid input reached the broker SELL boundary")


def _minimal_risk_config(
    *,
    max_position_ratio=0.20,
    max_investment_ratio=0.70,
    min_cash_ratio=0.20,
    max_risk_per_trade=0.01,
    stop_loss_rate=0.03,
    use_mock=True,
):
    risk_params = {
        "position_sizing": {"max_risk_per_trade": max_risk_per_trade},
        "diversification": {
            "max_positions": 10,
            "max_position_ratio": max_position_ratio,
            "max_investment_ratio": max_investment_ratio,
            "min_cash_ratio": min_cash_ratio,
        },
        "stop_loss": {"type": "fixed", "fixed_rate": stop_loss_rate},
        "transaction_costs": {
            "commission_rate": 0.0,
            "tax_rate": 0.0,
            "slippage": 0.0,
            "slippage_ticks": 0,
            "dynamic_slippage": {"enabled": False},
        },
    }
    return SimpleNamespace(
        trading={
            "pending_order_ttl_seconds": 600,
            "ledger_reconcile_guard_ttl_seconds": 86_400,
        },
        risk_params=risk_params,
        kis_api={"use_mock": use_mock},
    )


def _executor_without_external_initialization(*, config=None, mode="paper"):
    """KIS 인증과 영속 주문 복구 없이 단일 경계만 검증하는 executor."""
    config = config or _minimal_risk_config()
    executor = object.__new__(OrderExecutor)
    executor.config = config
    executor.account_key = "safety_hardening"
    executor.mode = mode
    executor.live_gate_validated = mode == "live"
    executor.risk_manager = RiskManager(config)
    executor.kis_api = _ForbiddenBroker()
    executor.order_book = OrderBook()
    executor._global_trading_halt_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }
    executor._live_buy_gate_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }
    executor._pre_order_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }
    executor._should_block_new_buy_volatility_window = lambda: False
    return executor


def _forbid_ledger_and_exposure_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid input reached broker/ledger/exposure boundary")

    monkeypatch.setattr("core.order_executor.save_trade", forbidden)
    monkeypatch.setattr("core.order_executor.save_position", forbidden)
    monkeypatch.setattr("core.order_executor.get_all_positions", forbidden)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("bad_field", ["capital", "available_cash"])
@pytest.mark.parametrize("fixed_quantity", [False, True], ids=["sized-buy", "fixed-buy"])
def test_buy_paths_reject_nonfinite_funds_before_broker_or_ledger(
    monkeypatch,
    bad_value,
    bad_field,
    fixed_quantity,
):
    """NaN/Inf 자금은 리스크 계산, 장부, 브로커 전에 fail-closed 차단된다."""
    _forbid_ledger_and_exposure_access(monkeypatch)
    executor = _executor_without_external_initialization()
    kwargs = {
        "symbol": "005930",
        "price": 60_000,
        "capital": 10_000_000,
        "available_cash": 10_000_000,
        "reason": "nonfinite guard regression",
        "strategy": "safety_test",
    }
    kwargs[bad_field] = bad_value

    if fixed_quantity:
        result = executor.execute_buy_quantity(quantity=1, **kwargs)
    else:
        result = executor.execute_buy(**kwargs)

    assert result["success"] is False
    assert result["capital_invalid"] is True
    assert executor.kis_api.calls == []


@pytest.mark.parametrize(
    ("positions", "quantity", "expected_fragment"),
    [
        ([], 3, "단일 종목 비중"),
        (
            [
                SimpleNamespace(
                    symbol="005930",
                    avg_price=100_000,
                    quantity=1,
                    total_invested=150_000,
                )
            ],
            1,
            "단일 종목 비중",
        ),
    ],
    ids=["new-position-over-limit", "accumulated-position-over-limit"],
)
def test_fixed_quantity_buy_enforces_projected_single_symbol_cap(
    monkeypatch,
    positions,
    quantity,
    expected_fragment,
):
    """신규 및 추가매수 모두 기존+신규 노출을 합산해 20% 상한을 검사한다."""
    executor = _executor_without_external_initialization()
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: positions)

    def forbidden(*args, **kwargs):
        raise AssertionError("exposure-limit violation reached broker/ledger")

    monkeypatch.setattr("core.order_executor.save_trade", forbidden)
    monkeypatch.setattr("core.order_executor.save_position", forbidden)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=quantity,
        capital=1_000_000,
        available_cash=1_000_000,
        reason="projected symbol exposure regression",
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["exposure_limit_blocked"] is True
    assert expected_fragment in result["reason"]
    assert executor.kis_api.calls == []


def test_fixed_quantity_buy_cannot_exceed_one_trade_loss_budget(monkeypatch):
    """목표비중 어댑터의 고정 수량도 1% 1회 손실 예산을 늘리지 못한다."""
    config = _minimal_risk_config(
        max_position_ratio=0.50,
        max_investment_ratio=0.90,
        min_cash_ratio=0.0,
        max_risk_per_trade=0.01,
        stop_loss_rate=0.10,
    )
    executor = _executor_without_external_initialization(config=config)
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: [])

    def forbidden(*args, **kwargs):
        raise AssertionError("per-trade risk violation reached broker/ledger")

    monkeypatch.setattr("core.order_executor.save_trade", forbidden)
    monkeypatch.setattr("core.order_executor.save_position", forbidden)

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=2,
        capital=1_000_000,
        available_cash=1_000_000,
        reason="fixed risk budget regression",
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["per_trade_risk_blocked"] is True
    assert result["projected_loss"] > result["risk_budget"]
    assert executor.kis_api.calls == []


def test_fixed_quantity_buy_applies_correlation_filter(monkeypatch):
    """고정수량 어댑터도 기존 포지션과의 상관 리스크를 우회하지 않는다."""
    config = _minimal_risk_config(
        max_position_ratio=0.50,
        max_investment_ratio=0.90,
        min_cash_ratio=0.0,
    )
    executor = _executor_without_external_initialization(config=config)
    position = SimpleNamespace(
        symbol="000660",
        avg_price=100_000,
        quantity=1,
        total_invested=100_000,
    )
    monkeypatch.setattr(
        "core.order_executor.get_all_positions", lambda **kwargs: [position]
    )
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {
            "scale": 0.0,
            "blocked": True,
            "reason": "상관 데이터 확인 실패",
        },
    )

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=900_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["correlation_risk_blocked"] is True
    assert executor.kis_api.calls == []


def test_fixed_quantity_buy_applies_gap_filter(monkeypatch):
    executor = _executor_without_external_initialization()
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: [])
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {"scale": 1.0, "reason": ""},
    )
    executor._gap_up_entry_check = lambda *args, **kwargs: {
        "allowed": False,
        "reason": "갭업 추격매수 차단",
        "gap_risk_blocked": True,
    }

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=1_000_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["gap_risk_blocked"] is True


def test_gap_filter_nan_threshold_blocks_before_price_lookup(monkeypatch):
    config = _minimal_risk_config()
    config.risk_params["gap_risk"] = {
        "enabled": True,
        "gap_up_entry_block": float("nan"),
    }
    executor = _executor_without_external_initialization(config=config)
    monkeypatch.setattr(
        "core.data_collector.DataCollector",
        lambda: pytest.fail("invalid gap config must block before price lookup"),
    )

    result = executor._gap_up_entry_check("005930", 100_000)

    assert result["allowed"] is False
    assert result["gap_risk_blocked"] is True
    assert "설정 오류" in result["reason"]


def test_fixed_quantity_buy_applies_earnings_filter(monkeypatch):
    config = _minimal_risk_config()
    config.trading["skip_earnings_days"] = 3
    executor = _executor_without_external_initialization(config=config)
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: [])
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {"scale": 1.0, "reason": ""},
    )
    executor._gap_up_entry_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }
    monkeypatch.setattr(
        "core.earnings_filter.is_near_earnings",
        lambda *args, **kwargs: (True, "실적 발표일 인접"),
    )

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=1_000_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["earnings_filter_blocked"] is True


def test_fixed_quantity_buy_applies_performance_degradation_filter(monkeypatch):
    config = _minimal_risk_config()
    config.risk_params["performance_degradation"] = {
        "enabled": True,
        "recent_trades": 20,
        "min_win_rate": 0.35,
    }
    executor = _executor_without_external_initialization(config=config)
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: [])
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {"scale": 1.0, "reason": ""},
    )
    monkeypatch.setattr(
        executor.risk_manager,
        "check_recent_performance",
        lambda trades: {"allowed": False, "reason": "최근 성과 열화"},
    )
    monkeypatch.setattr(
        "database.repositories.get_recent_sell_trades", lambda **kwargs: []
    )
    executor._gap_up_entry_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=1_000_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["performance_degradation_blocked"] is True


def test_fixed_quantity_buy_applies_market_regime_filter(monkeypatch):
    config = _minimal_risk_config()
    config.trading["market_regime_filter"] = True
    executor = _executor_without_external_initialization(config=config)
    monkeypatch.setattr("core.order_executor.get_all_positions", lambda **kwargs: [])
    monkeypatch.setattr(
        executor.risk_manager,
        "check_correlation_risk",
        lambda *args, **kwargs: {"scale": 1.0, "reason": ""},
    )
    monkeypatch.setattr(
        "database.repositories.get_recent_sell_trades", lambda **kwargs: []
    )
    monkeypatch.setattr(
        "core.market_regime.get_regime_adjusted_params",
        lambda config: {
            "allow_buys": False,
            "regime": "bearish",
            "position_scale": 0.0,
        },
    )
    executor._gap_up_entry_check = lambda *args, **kwargs: {
        "allowed": True,
        "reason": "",
    }

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=1_000_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["market_regime_blocked"] is True


def test_fixed_quantity_buy_applies_sector_cap(monkeypatch):
    config = _minimal_risk_config(
        max_position_ratio=0.50,
        max_investment_ratio=0.90,
        min_cash_ratio=0.0,
    )
    config.risk_params["diversification"].update(
        {"max_sector_ratio": 0.20, "sector_map_strict": True}
    )
    executor = _executor_without_external_initialization(config=config)
    position = SimpleNamespace(
        symbol="000660",
        avg_price=150_000,
        quantity=1,
        total_invested=150_000,
    )
    monkeypatch.setattr(
        "core.order_executor.get_all_positions", lambda **kwargs: [position]
    )
    executor._get_sector_map_cached = lambda: {
        "000660": "반도체",
        "005930": "반도체",
    }

    result = executor.execute_buy_quantity(
        symbol="005930",
        price=100_000,
        quantity=1,
        capital=1_000_000,
        available_cash=850_000,
        strategy="safety_test",
    )

    assert result["success"] is False
    assert result["exposure_limit_blocked"] is True
    assert "업종" in result["reason"]


class _RecordingSellBroker:
    def __init__(self):
        self.calls = []

    def sell_order(self, symbol, quantity, price, order_type="00"):
        self.calls.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "order_type": order_type,
            }
        )
        return {"odno": "SELL-SAFETY-1"}


@pytest.mark.parametrize(
    ("reason", "expected_price", "expected_order_type"),
    [
        ("STOP_LOSS", 0, "01"),
        ("긴급 전량 청산", 0, "01"),
        ("TAKE_PROFIT", 71_000, "00"),
    ],
)
def test_live_sell_uses_market_only_for_emergency_exit(
    monkeypatch,
    reason,
    expected_price,
    expected_order_type,
):
    """손실 방어 청산은 시장가, 일반 익절은 지정가로 KIS에 제출된다."""
    config = _minimal_risk_config(use_mock=False)
    executor = _executor_without_external_initialization(config=config, mode="live")
    broker = _RecordingSellBroker()
    executor.kis_api = broker
    executor._get_min_holding_days = lambda: 0
    executor._pre_order_check = lambda **kwargs: {"allowed": True, "reason": ""}
    executor._persistent_live_order_block = lambda *args, **kwargs: None
    executor._live_unfilled_order_block = lambda *args, **kwargs: None
    executor._claim_live_order_guard = lambda *args, **kwargs: None
    executor._persist_order_record = lambda *args, **kwargs: None
    executor._resolve_live_execution = lambda *args, **kwargs: {
        "confirmed": False,
        "reason": "live_fill_unconfirmed",
        "filled_qty": 0,
        "remaining_qty": 5,
    }
    executor._pending_live_execution_result = lambda **kwargs: {
        "success": False,
        "order_pending": True,
    }
    position = SimpleNamespace(
        symbol="000660",
        quantity=5,
        avg_price=70_000,
        bought_at=datetime.now() - timedelta(days=30),
    )
    monkeypatch.setattr(
        "core.order_executor.get_position", lambda *args, **kwargs: position
    )

    result = executor.execute_sell(
        symbol="000660",
        price=71_000,
        reason=reason,
        strategy="safety_test",
    )

    assert result["order_pending"] is True
    assert broker.calls == [
        {
            "symbol": "000660",
            "quantity": 5,
            "price": expected_price,
            "order_type": expected_order_type,
        }
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "output1": [],
            "output2": [{"dnca_tot_amt": "NaN", "tot_evlu_amt": "1000000"}],
        },
        {
            "output1": [],
            "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "inf"}],
        },
        {
            "output1": [
                {
                    "pdno": "005930",
                    "hldg_qty": "NaN",
                    "pchs_avg_pric": "60000",
                    "prpr": "61000",
                }
            ],
            "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}],
        },
        {
            "output1": [
                {
                    "pdno": "005930",
                    "hldg_qty": "1",
                    "pchs_avg_pric": "60000",
                    "prpr": "NaN",
                }
            ],
            "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}],
        },
    ],
    ids=["nan-cash", "inf-total-value", "nan-quantity", "nan-current-price"],
)
def test_kis_balance_rejects_nonfinite_numeric_response(payload):
    """KIS의 NaN/Inf 잔고 응답을 0이나 유효 잔고로 해석하지 않는다."""
    api = object.__new__(KISApi)
    api.use_mock = True
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    api._request = lambda *args, **kwargs: {"rt_cd": "0", **payload}

    assert api.get_balance() is None


class _SilentNotifier:
    def __init__(self, *args, **kwargs):
        pass

    def send_message(self, *args, **kwargs):
        return True


def _capture_real_live_halt(monkeypatch):
    halts = []
    guard_extensions = []
    monkeypatch.setattr(
        "database.repositories.set_trading_halt",
        lambda reason, **kwargs: halts.append({"reason": reason, **kwargs}),
    )
    monkeypatch.setattr(
        "core.order_guard.OrderGuard.extend_pending",
        lambda symbol, ttl_seconds=0: guard_extensions.append(
            {"symbol": symbol, "ttl_seconds": ttl_seconds}
        ),
    )
    monkeypatch.setattr("core.notifier.Notifier", _SilentNotifier)
    return halts, guard_extensions


def _submitted_live_order(*, partial=False):
    order = OrderRecord(
        order_id="ORD-UNCERTAIN-SAFETY",
        symbol="005380",
        action="BUY",
        requested_qty=3,
        requested_price=60_000,
        strategy="safety_test",
        account_key="safety_hardening",
        mode="live",
    )
    order.transition(OrderStatus.SUBMITTED)
    order.transition(OrderStatus.ACKED, broker_order_id="BROKER-UNCERTAIN-1")
    if partial:
        order.transition(
            OrderStatus.PARTIAL_FILLED,
            fill_qty=1,
            fill_price=60_100,
        )
    return order


def test_real_live_unknown_order_response_sets_global_halt(monkeypatch):
    """실계좌 주문 응답이 유실되면 재전송 대신 global HALT를 설정한다."""
    config = _minimal_risk_config(use_mock=False)
    executor = _executor_without_external_initialization(config=config, mode="live")
    executor._persist_order_record = lambda *args, **kwargs: None
    halts, guard_extensions = _capture_real_live_halt(monkeypatch)
    order = _submitted_live_order()

    result = executor._unknown_response_result(order, "BUY")

    assert result["response_unknown"] is True
    assert result["requires_reconcile"] is True
    assert len(halts) == 1
    assert halts[0]["mode"] == "live"
    assert halts[0]["source"] == "core.order_executor.uncertain_execution"
    assert halts[0]["detail"]["requires_broker_reconcile"] is True
    assert halts[0]["detail"]["execution_reason"] == "broker_order_response_unknown"
    assert guard_extensions == [{"symbol": "005380", "ttl_seconds": 86_400}]


def test_real_live_partial_fill_sets_global_halt(monkeypatch):
    """실계좌 부분체결은 잔량 대조 전까지 global HALT와 장기 guard를 남긴다."""
    config = _minimal_risk_config(use_mock=False)
    executor = _executor_without_external_initialization(config=config, mode="live")
    executor._persist_order_record = lambda *args, **kwargs: None
    halts, guard_extensions = _capture_real_live_halt(monkeypatch)
    order = _submitted_live_order(partial=True)
    execution = {
        "confirmed": False,
        "reason": "live_partial_fill_unreconciled",
        "fill_price": 60_100,
        "filled_qty": 1,
        "remaining_qty": 2,
    }

    result = executor._pending_live_execution_result(
        order=order,
        action="BUY",
        execution=execution,
    )

    assert result["requires_reconcile"] is True
    assert result["order_status"] == OrderStatus.PARTIAL_FILLED.value
    assert len(halts) == 1
    assert halts[0]["mode"] == "live"
    assert halts[0]["detail"]["execution"]["filled_qty"] == 1
    assert halts[0]["detail"]["requires_broker_reconcile"] is True
    assert guard_extensions == [{"symbol": "005380", "ttl_seconds": 86_400}]


@pytest.mark.parametrize(
    "bad_ttl",
    [-1, 0, 59, True, "invalid", float("nan"), float("inf")],
)
def test_invalid_pending_guard_ttl_uses_safe_default(bad_ttl):
    """오설정된 TTL이 실전 중복 주문 가드를 즉시 만료시키지 않는다."""
    config = _minimal_risk_config(use_mock=False)
    config.trading["pending_order_ttl_seconds"] = bad_ttl
    executor = _executor_without_external_initialization(config=config, mode="live")

    assert executor._safe_order_guard_ttl(
        "pending_order_ttl_seconds",
        default=600,
        minimum=60,
    ) == 600


def test_invalid_reconcile_ttl_still_halts_with_one_day_guard(monkeypatch):
    """불명확 체결 사고 처리 중 TTL 파싱 오류로 HALT 자체가 깨지지 않는다."""
    config = _minimal_risk_config(use_mock=False)
    config.trading["ledger_reconcile_guard_ttl_seconds"] = "invalid"
    executor = _executor_without_external_initialization(config=config, mode="live")
    executor._persist_order_record = lambda *args, **kwargs: None
    halts, guard_extensions = _capture_real_live_halt(monkeypatch)

    result = executor._unknown_response_result(_submitted_live_order(), "BUY")

    assert result["requires_reconcile"] is True
    assert len(halts) == 1
    assert guard_extensions == [{"symbol": "005380", "ttl_seconds": 86_400}]


def test_direct_real_kis_order_requires_executor_capability():
    """저수준 KIS API를 직접 호출해 상위 손실 가드를 우회할 수 없다."""
    api = object.__new__(KISApi)
    api.use_mock = False
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    calls = []
    api._request = lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "rt_cd": "0",
        "output": {"odno": "SAFE-1"},
    }

    with pytest.raises(PermissionError, match="OrderExecutor"):
        api.buy_order("005930", 1, 60_000)
    with pytest.raises(PermissionError, match="OrderExecutor"):
        api.sell_order("005930", 1, 60_000)
    assert calls == []

    with authorized_kis_order_submission():
        result = api.sell_order("005930", 1, 0, "01")

    assert result == {"odno": "SAFE-1"}
    assert calls[0][1]["body"]["ORD_DVSN"] == "01"
    assert calls[0][1]["body"]["ORD_UNPR"] == "0"


def test_mock_flag_with_real_domain_still_requires_executor_capability():
    """use_mock 오설정만으로 실전 도메인 주문 보호를 끌 수 없다."""
    api = object.__new__(KISApi)
    api.use_mock = True
    api.base_url = "https://openapi.koreainvestment.com:9443"
    api.cano = "12345678"
    api.acnt_prdt_cd = "01"
    calls = []
    api._request = lambda *args, **kwargs: calls.append((args, kwargs))

    with pytest.raises(PermissionError, match="OrderExecutor"):
        api.buy_order("005930", 1, 60_000)

    assert calls == []


def test_executor_treats_unconfirmed_mock_domain_as_real_money():
    config = _minimal_risk_config(use_mock=True)
    config.kis_api["mock_url"] = "https://openapi.koreainvestment.com:9443"
    executor = _executor_without_external_initialization(config=config, mode="live")

    assert executor._is_real_money_live() is True
