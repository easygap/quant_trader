"""Paper/live 장부가 계좌 키·종목·날짜가 같아도 서로 간섭하지 않는다."""

from datetime import datetime, timedelta

import pytest

from database.models import Base, Position, PortfolioSnapshot, init_database
from database.repositories import (
    delete_position,
    get_all_positions,
    get_cash_flow_total,
    get_cash_flow_total_between,
    get_latest_peak_value,
    get_latest_snapshot_summary,
    get_portfolio_snapshots_between,
    get_position,
    has_cash_flows,
    record_cash_flow,
    reduce_position,
    save_portfolio_snapshot,
    save_position,
    update_position_targets,
)


def _unique_columns(table):
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_models_require_mode_and_scope_uniques_by_mode():
    assert Position.__table__.c.mode.nullable is False
    assert PortfolioSnapshot.__table__.c.mode.nullable is False
    assert Position.__table__.c.mode.server_default.arg == "paper"
    assert PortfolioSnapshot.__table__.c.mode.server_default.arg == "paper"
    assert ("mode", "account_key", "symbol") in _unique_columns(Position.__table__)
    assert ("mode", "account_key", "date") in _unique_columns(
        PortfolioSnapshot.__table__
    )
    # Base 전체 메타데이터에서도 동일 계약이 유지된다.
    assert Base.metadata.tables["positions"] is Position.__table__


def test_position_crud_isolated_by_mode():
    init_database()
    account_key = "mode_isolation:positions"
    symbol = "MODE-POS"

    save_position(symbol, 100.0, 2, account_key=account_key)  # default paper
    save_position(symbol, 200.0, 5, account_key=account_key, mode="live")
    save_position(symbol, 300.0, 1, account_key=account_key, mode="LIVE")

    paper = get_position(symbol, account_key=account_key)
    live = get_position(symbol, account_key=account_key, mode="live")
    assert (paper.quantity, paper.avg_price) == (2, pytest.approx(100.0))
    assert live.quantity == 6
    assert live.avg_price == pytest.approx((200 * 5 + 300) / 6)
    assert {row.mode for row in get_all_positions(account_key=account_key)} == {"paper"}
    assert {row.mode for row in get_all_positions(account_key=account_key, mode="live")} == {"live"}

    update_position_targets(
        symbol,
        stop_loss_price=150.0,
        account_key=account_key,
        mode="live",
    )
    assert get_position(symbol, account_key=account_key).stop_loss_price is None
    assert get_position(symbol, account_key=account_key, mode="live").stop_loss_price == 150.0

    reduce_position(symbol, 2, account_key=account_key, mode="live")
    assert get_position(symbol, account_key=account_key, mode="live").quantity == 4
    assert get_position(symbol, account_key=account_key).quantity == 2

    delete_position(symbol, account_key=account_key)  # default paper only
    assert get_position(symbol, account_key=account_key) is None
    assert get_position(symbol, account_key=account_key, mode="live") is not None


def test_snapshot_upsert_peak_and_ranges_are_isolated_by_mode():
    init_database()
    account_key = "mode_isolation:snapshots"
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    assert save_portfolio_snapshot(
        1_000.0,
        600.0,
        400.0,
        cumulative_return=1.0,
        peak_value=1_100.0,
        account_key=account_key,
        snapshot_date=day,
    )
    assert save_portfolio_snapshot(
        9_000.0,
        5_000.0,
        4_000.0,
        cumulative_return=9.0,
        peak_value=9_900.0,
        account_key=account_key,
        snapshot_date=day,
        mode="live",
    )
    # paper upsert는 같은 날 live 행을 갱신하지 않는다.
    assert save_portfolio_snapshot(
        1_200.0,
        700.0,
        500.0,
        cumulative_return=2.0,
        peak_value=1_250.0,
        account_key=account_key,
        snapshot_date=day,
    )

    assert get_latest_peak_value(account_key) == pytest.approx(1_250.0)
    assert get_latest_peak_value(account_key, mode="live") == pytest.approx(9_900.0)
    assert get_latest_snapshot_summary(account_key)["total_value"] == pytest.approx(1_200.0)
    assert get_latest_snapshot_summary(account_key, mode="live")["total_value"] == pytest.approx(9_000.0)

    start = day - timedelta(days=1)
    end = day + timedelta(days=1)
    paper = get_portfolio_snapshots_between(start, end, account_key=account_key)
    live = get_portfolio_snapshots_between(
        start, end, account_key=account_key, mode="live"
    )
    assert [row["total_value"] for row in paper] == [pytest.approx(1_200.0)]
    assert [row["total_value"] for row in live] == [pytest.approx(9_000.0)]


def test_cash_flow_totals_and_ranges_are_isolated_by_mode():
    init_database()
    account_key = "mode_isolation:cash_flows"
    t0 = datetime.now().replace(microsecond=0) - timedelta(hours=2)
    t1 = t0 + timedelta(hours=1)

    record_cash_flow(100.0, account_key=account_key, occurred_at=t0)
    record_cash_flow(25.0, account_key=account_key, occurred_at=t1)
    record_cash_flow(900.0, account_key=account_key, occurred_at=t0, mode="live")
    record_cash_flow(-100.0, account_key=account_key, occurred_at=t1, mode="LIVE")

    assert get_cash_flow_total(account_key) == pytest.approx(125.0)
    assert get_cash_flow_total(account_key, mode="live") == pytest.approx(800.0)
    assert get_cash_flow_total_between(
        account_key, after=t0, until=t1
    ) == pytest.approx(25.0)
    assert get_cash_flow_total_between(
        account_key, after=t0, until=t1, mode="live"
    ) == pytest.approx(-100.0)
    assert has_cash_flows(account_key) is True
    assert has_cash_flows(account_key, mode="live") is True
    assert has_cash_flows(account_key, mode="legacy") is False
