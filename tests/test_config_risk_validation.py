"""실거래 손실 한도에 직접 영향을 주는 설정은 로드 시 fail-closed 검증한다."""

import copy

import pytest

from config.config_loader import Config


def _base_risk_params() -> dict:
    return {
        "position_sizing": {
            "initial_capital": 10_000_000,
            "max_risk_per_trade": 0.01,
            "signal_scaling": {
                "enabled": True,
                "min_scale": 0.5,
                "max_scale": 1.0,
            },
        },
        "drawdown": {
            "max_portfolio_mdd": 0.15,
            "max_daily_loss": 0.03,
        },
        "diversification": {
            "max_position_ratio": 0.2,
            "max_investment_ratio": 0.7,
            "max_sector_ratio": 0.4,
            "min_cash_ratio": 0.2,
        },
        "blackswan": {
            "single_stock_threshold": -0.05,
            "portfolio_threshold": -0.03,
            "consecutive_days": 3,
            "consecutive_threshold": -0.02,
            "cooldown_minutes": 60,
            "recovery_minutes": 120,
            "recovery_scale": 0.5,
        },
    }


def _validate(risk_params: dict, settings: dict | None = None) -> None:
    config = object.__new__(Config)
    config._risk_params = risk_params
    config._settings = settings or {"trading": {"mode": "paper"}}
    config._validate_critical_params()


def test_valid_conservative_risk_config_is_accepted():
    _validate(_base_risk_params())


@pytest.mark.parametrize(
    ("section", "key", "bad_value"),
    [
        ("position_sizing", "max_risk_per_trade", 0),
        ("position_sizing", "max_risk_per_trade", 0.051),
        ("position_sizing", "max_risk_per_trade", float("nan")),
        ("drawdown", "max_daily_loss", -0.01),
        ("drawdown", "max_portfolio_mdd", float("inf")),
        ("diversification", "max_position_ratio", 1.1),
        ("diversification", "min_cash_ratio", -0.1),
    ],
)
def test_invalid_loss_limit_ratios_are_rejected(section, key, bad_value):
    params = copy.deepcopy(_base_risk_params())
    params[section][key] = bad_value

    with pytest.raises(ValueError, match=key):
        _validate(params)


def test_signal_scale_cannot_raise_position_above_risk_budget():
    params = _base_risk_params()
    params["position_sizing"]["signal_scaling"]["max_scale"] = 1.5

    with pytest.raises(ValueError, match="max_scale"):
        _validate(params)


def test_signal_scale_min_must_not_exceed_max():
    params = _base_risk_params()
    params["position_sizing"]["signal_scaling"]["min_scale"] = 0.9
    params["position_sizing"]["signal_scaling"]["max_scale"] = 0.5

    with pytest.raises(ValueError, match="min_scale ≤ max_scale"):
        _validate(params)


def test_holding_period_income_tax_rate_must_be_a_ratio():
    params = _base_risk_params()
    params["transaction_costs"] = {
        "holding_period_income_tax": {
            "enabled": True,
            "rate": 1.54,
            "symbols": ["357870"],
        },
    }

    with pytest.raises(ValueError, match="holding_period_income_tax.rate"):
        _validate(params)


@pytest.mark.parametrize("symbols", [None, [], "357870"])
def test_enabled_holding_period_income_tax_requires_symbol_list(symbols):
    params = _base_risk_params()
    params["transaction_costs"] = {
        "holding_period_income_tax": {
            "enabled": True,
            "rate": 0.154,
            "symbols": symbols,
        },
    }

    with pytest.raises(ValueError, match="holding_period_income_tax.symbols"):
        _validate(params)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("single_stock_threshold", 0),
        ("portfolio_threshold", -1),
        ("consecutive_threshold", float("nan")),
        ("consecutive_days", 0),
        ("cooldown_minutes", -1),
        ("recovery_minutes", 1.5),
        ("recovery_scale", 1.1),
    ],
)
def test_invalid_blackswan_controls_are_rejected(key, bad_value):
    params = _base_risk_params()
    params["blackswan"][key] = bad_value

    with pytest.raises(ValueError, match=key):
        _validate(params)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("pending_order_ttl_seconds", 59),
        ("pending_order_ttl_seconds", float("nan")),
        ("ledger_reconcile_guard_ttl_seconds", 3_599),
        ("skip_earnings_days", -1),
        ("skip_earnings_days", True),
    ],
)
def test_invalid_trading_safety_controls_are_rejected(key, bad_value):
    settings = {"trading": {"mode": "paper", key: bad_value}}

    with pytest.raises(ValueError, match=key):
        _validate(_base_risk_params(), settings)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("max_calls_per_sec", 0),
        ("max_calls_per_sec", float("inf")),
        ("max_calls_per_min", 0),
        ("max_calls_per_min", 1.5),
    ],
)
def test_invalid_kis_rate_limits_are_rejected(key, bad_value):
    settings = {
        "trading": {"mode": "paper"},
        "kis_api": {key: bad_value},
    }

    with pytest.raises(ValueError, match=key):
        _validate(_base_risk_params(), settings)


@pytest.mark.parametrize(
    ("section", "key", "bad_value"),
    [
        ("correlation_risk", "high_corr_threshold", float("nan")),
        ("correlation_risk", "high_corr_scale", 0),
        ("correlation_risk", "lookback_days", 29),
        ("gap_risk", "gap_down_threshold", 0),
        ("gap_risk", "gap_up_entry_block", float("inf")),
        ("performance_degradation", "min_win_rate", float("nan")),
        ("performance_degradation", "recent_trades", 4),
    ],
)
def test_invalid_entry_filter_controls_are_rejected(section, key, bad_value):
    params = _base_risk_params()
    if section == "correlation_risk":
        params["diversification"]["correlation_risk"] = {
            "enabled": True,
            "high_corr_threshold": 0.7,
            "high_corr_scale": 0.5,
            "lookback_days": 60,
        }
        params["diversification"]["correlation_risk"][key] = bad_value
    elif section == "gap_risk":
        params["gap_risk"] = {
            "enabled": True,
            "gap_down_threshold": -0.03,
            "gap_up_entry_block": 0.05,
        }
        params["gap_risk"][key] = bad_value
    else:
        params["performance_degradation"] = {
            "enabled": True,
            "min_win_rate": 0.35,
            "recent_trades": 20,
        }
        params["performance_degradation"][key] = bad_value

    with pytest.raises(ValueError, match=key):
        _validate(params)
