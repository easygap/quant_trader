import sys
from datetime import datetime

import pytest


def test_shadow_bootstrap_fails_when_watchlist_empty(monkeypatch):
    from tools import paper_bootstrap
    import core.paper_evidence as paper_evidence

    monkeypatch.setattr(paper_bootstrap, "_get_watchlist", lambda: [])
    monkeypatch.setattr(
        paper_evidence,
        "collect_daily_evidence",
        lambda **kwargs: pytest.fail("empty watchlist must block collection"),
    )

    stats = paper_bootstrap.run_shadow_bootstrap(
        "bootstrap_s",
        [datetime(2026, 4, 6)],
    )

    assert stats["complete"] is False
    assert stats["failure_reason"] == "watchlist_empty"
    assert stats["collected"] == 0


def test_shadow_bootstrap_fails_when_requested_date_missing(monkeypatch):
    from tools import paper_bootstrap
    import core.paper_evidence as paper_evidence

    monkeypatch.setattr(paper_bootstrap, "_get_watchlist", lambda: ["005930"])
    monkeypatch.setattr(paper_evidence, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(paper_evidence, "finalize_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(paper_evidence, "get_canonical_records", lambda strategy: [])
    monkeypatch.setattr(paper_evidence, "generate_weekly_summary", lambda *args, **kwargs: None)

    stats = paper_bootstrap.run_shadow_bootstrap(
        "bootstrap_s",
        [datetime(2026, 4, 6)],
    )

    assert stats["complete"] is False
    assert stats["missing_dates"] == ["2026-04-06"]
    assert stats["failure_reason"] == "missing_requested_dates:2026-04-06"


def test_shadow_bootstrap_duplicate_existing_date_is_complete(monkeypatch):
    from tools import paper_bootstrap
    import core.paper_evidence as paper_evidence

    monkeypatch.setattr(paper_bootstrap, "_get_watchlist", lambda: ["005930"])
    monkeypatch.setattr(paper_evidence, "collect_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(paper_evidence, "finalize_daily_evidence", lambda **kwargs: None)
    monkeypatch.setattr(
        paper_evidence,
        "get_canonical_records",
        lambda strategy: [{"date": "2026-04-06"}],
    )
    monkeypatch.setattr(paper_evidence, "generate_weekly_summary", lambda *args, **kwargs: None)

    stats = paper_bootstrap.run_shadow_bootstrap(
        "bootstrap_s",
        [datetime(2026, 4, 6)],
    )

    assert stats["complete"] is True
    assert stats["missing_dates"] == []
    assert stats["skipped"] == 1


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            ["paper_bootstrap.py", "--strategy", "scoring", "--date", "2026-04-06", "--from", "2026-04-01", "--to", "2026-04-06"],
            "--date",
        ),
        (
            ["paper_bootstrap.py", "--strategy", "scoring", "--from", "2026-04-07", "--to", "2026-04-06"],
            "--from",
        ),
        (
            ["paper_bootstrap.py", "--strategy", "scoring", "--from", "2026-04-11", "--to", "2026-04-12"],
            "평일",
        ),
    ],
)
def test_paper_bootstrap_rejects_invalid_date_scope(monkeypatch, capsys, argv, expected):
    from tools import paper_bootstrap

    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        paper_bootstrap.main()

    assert exc.value.code == 2
    assert expected in capsys.readouterr().err


def test_paper_bootstrap_cli_exits_nonzero_when_incomplete(monkeypatch):
    from tools import paper_bootstrap
    import core.paper_runtime as paper_runtime
    import core.strategy_universe as strategy_universe
    import database.models as models

    monkeypatch.setattr(sys, "argv", ["paper_bootstrap.py", "--strategy", "scoring", "--date", "2026-04-06"])
    monkeypatch.setattr(models, "init_database", lambda: None)
    monkeypatch.setattr(strategy_universe, "is_paper_eligible", lambda strategy: True)
    monkeypatch.setattr(paper_runtime, "is_paper_trade_allowed", lambda strategy, action: True)
    monkeypatch.setattr(
        paper_bootstrap,
        "run_shadow_bootstrap",
        lambda strategy, dates: {"complete": False, "failure_reason": "watchlist_empty"},
    )

    with pytest.raises(SystemExit) as exc:
        paper_bootstrap.main()

    assert exc.value.code == 1
