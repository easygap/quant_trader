"""Regression: update_stop_loss_price shim + scheduler _update_dynamic_stop_losses import path."""

import pytest


def test_update_stop_loss_price_importable():
    from database.repositories import update_stop_loss_price

    assert callable(update_stop_loss_price)


def test_scheduler_update_dynamic_stop_losses_import_path():
    import core.scheduler as scheduler_mod

    assert hasattr(scheduler_mod, "Scheduler")
    assert hasattr(scheduler_mod.Scheduler, "_update_dynamic_stop_losses")

    from database.repositories import update_stop_loss_price  # noqa: F401


def test_update_stop_loss_price_delegates_to_update_position_targets(monkeypatch):
    import database.repositories as repo

    captured = {}

    def fake_update_position_targets(
        symbol, stop_loss_price=None, take_profit_price=None,
        trailing_stop_price=None, account_key="",
    ):
        captured["symbol"] = symbol
        captured["stop_loss_price"] = stop_loss_price
        captured["take_profit_price"] = take_profit_price
        captured["trailing_stop_price"] = trailing_stop_price
        captured["account_key"] = account_key

    monkeypatch.setattr(repo, "update_position_targets", fake_update_position_targets)

    repo.update_stop_loss_price("005930", 48500.0, account_key="scoring")

    assert captured == {
        "symbol": "005930",
        "stop_loss_price": 48500.0,
        "take_profit_price": None,
        "trailing_stop_price": None,
        "account_key": "scoring",
    }


def test_scheduler_run_monitoring_does_not_skip_on_stop_loss_import(monkeypatch):
    from unittest.mock import MagicMock
    import core.scheduler as scheduler_mod

    sched = scheduler_mod.Scheduler.__new__(scheduler_mod.Scheduler)
    sched.strategy_name = "scoring"
    sched.config = MagicMock()
    sched.config.trading = {"mode": "paper"}

    monkeypatch.setattr(
        "database.repositories.get_all_positions", lambda account_key=None: []
    )

    sched._update_dynamic_stop_losses()
