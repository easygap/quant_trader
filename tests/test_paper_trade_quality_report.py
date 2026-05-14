from datetime import datetime
import json
import sys

import pytest

from config.config_loader import Config


@pytest.fixture
def fresh_db():
    Config._instance = None
    from database.models import (
        DailyReport,
        FailedOrder,
        OperationEvent,
        PendingOrderGuard,
        PortfolioSnapshot,
        Position,
        TradeHistory,
        get_session,
        init_database,
    )

    init_database()
    session = get_session()
    for model in [
        TradeHistory,
        OperationEvent,
        PortfolioSnapshot,
        Position,
        FailedOrder,
        PendingOrderGuard,
        DailyReport,
    ]:
        try:
            session.query(model).delete()
        except Exception:
            pass
    session.commit()
    session.close()
    return True


def _seed_trade(**overrides):
    from database.models import TradeHistory, get_session

    payload = {
        "account_key": "scoring",
        "symbol": "005930",
        "action": "BUY",
        "price": 101.0,
        "quantity": 10,
        "total_amount": 1010.0,
        "commission": 1.0,
        "tax": 0.0,
        "slippage": 2.0,
        "expected_price": 100.0,
        "actual_slippage_pct": 0.1,
        "strategy": "scoring",
        "reason": "paper quality fixture",
        "mode": "paper",
        "executed_at": datetime(2026, 5, 11, 10, 0),
        "price_gap": 1.0,
        "execution_session_id": "paper-quality-session",
        "order_id": "ORD-PAPER-QUALITY",
    }
    payload.update(overrides)
    session = get_session()
    try:
        session.add(TradeHistory(**payload))
        session.commit()
    finally:
        session.close()


def test_build_paper_trade_quality_report_summarizes_adverse_gaps(fresh_db):
    from tools.paper_trade_quality_report import build_paper_trade_quality_report

    _seed_trade(symbol="005930", action="BUY", price=101, quantity=10, total_amount=1010, price_gap=1)
    _seed_trade(
        symbol="000660",
        action="SELL",
        price=98,
        quantity=5,
        total_amount=490,
        price_gap=-2,
        actual_slippage_pct=-0.2,
    )
    _seed_trade(
        symbol="005930",
        action="BUY",
        price=99,
        quantity=10,
        total_amount=990,
        price_gap=-1,
        actual_slippage_pct=-0.1,
    )

    report = build_paper_trade_quality_report(
        account_key="scoring",
        start_date=datetime(2026, 5, 11),
        end_date=datetime(2026, 5, 11, 23, 59, 59),
        max_gap_cost_bps=50.0,
    )

    assert report["quality_status"] == "review"
    assert report["summary"]["trade_count"] == 3
    assert report["summary"]["buy_count"] == 2
    assert report["summary"]["sell_count"] == 1
    assert report["summary"]["adverse_gap_cost"] == 20.0
    assert report["summary"]["favorable_gap_cost"] == -10.0
    assert report["summary"]["signed_gap_cost"] == 10.0
    assert report["summary"]["adverse_gap_bps_of_notional"] > 50
    assert report["summary"]["avg_abs_price_gap_pct"] == 1.3333
    assert any("불리한 체결 갭" in issue for issue in report["issues"])
    assert report["by_symbol"]["005930"]["trade_count"] == 2
    assert report["by_date"]["2026-05-11"]["adverse_fill_count"] == 2


def test_paper_trade_quality_report_detects_missing_expected_price(fresh_db):
    from tools.paper_trade_quality_report import build_paper_trade_quality_report

    _seed_trade(expected_price=None, price_gap=None, actual_slippage_pct=None)

    report = build_paper_trade_quality_report(
        account_key="scoring",
        max_missing_expected_ratio=0.0,
    )

    assert report["quality_status"] == "review"
    assert report["summary"]["missing_expected_price_count"] == 1
    assert report["summary"]["missing_expected_price_ratio"] == 1.0
    assert any("expected_price 누락" in issue for issue in report["issues"])


def test_paper_trade_quality_report_detects_missing_execution_link(fresh_db):
    from tools.paper_trade_quality_report import build_paper_trade_quality_report

    _seed_trade(execution_session_id="", order_id="")

    report = build_paper_trade_quality_report(
        account_key="scoring",
        max_missing_execution_link_ratio=0.0,
    )

    assert report["quality_status"] == "review"
    assert report["summary"]["missing_execution_session_id_count"] == 1
    assert report["summary"]["missing_order_id_count"] == 1
    assert report["summary"]["missing_execution_link_count"] == 1
    assert report["summary"]["missing_execution_link_ratio"] == 1.0
    assert any("execution_session_id/order_id 누락" in issue for issue in report["issues"])


def test_write_paper_trade_quality_report_outputs_json_and_markdown(fresh_db, tmp_path):
    from tools.paper_trade_quality_report import (
        build_paper_trade_quality_report,
        write_paper_trade_quality_report,
    )

    _seed_trade()
    report = build_paper_trade_quality_report(account_key="scoring")
    json_path, md_path = write_paper_trade_quality_report(report, tmp_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    md = md_path.read_text(encoding="utf-8")

    assert payload["artifact_type"] == "paper_trade_quality_report"
    assert payload["summary"]["trade_count"] == 1
    assert "# Paper Trade Quality Report" in md
    assert "005930" in md


def test_paper_trade_quality_report_handles_no_trades(fresh_db):
    from tools.paper_trade_quality_report import build_paper_trade_quality_report

    report = build_paper_trade_quality_report(account_key="empty")

    assert report["quality_status"] == "no_trades"
    assert report["summary"]["trade_count"] == 0
    assert report["issues"] == ["선택한 기간에 paper 체결 기록이 없습니다."]


def test_paper_trade_quality_cli_fails_on_no_trades_by_default(fresh_db, tmp_path, monkeypatch):
    import tools.paper_trade_quality_report as report_cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paper_trade_quality_report.py",
            "--account-key",
            "empty",
            "--output-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        report_cli.main()

    assert exc.value.code == 2
    assert list(tmp_path.glob("paper_trade_quality_empty_*.json"))


def test_paper_trade_quality_cli_can_allow_no_trades_for_report_only(
    fresh_db, tmp_path, monkeypatch
):
    import tools.paper_trade_quality_report as report_cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paper_trade_quality_report.py",
            "--account-key",
            "empty",
            "--output-dir",
            str(tmp_path),
            "--allow-no-trades",
        ],
    )

    report_cli.main()

    assert list(tmp_path.glob("paper_trade_quality_empty_*.json"))
