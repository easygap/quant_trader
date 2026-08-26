"""트랙레코드 재시작 도구 회귀 — 아카이브 이전(비파괴)·되돌리기·fail-closed."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess
from datetime import datetime

VENV = sys.executable
KEY = "basket_rebalance:rt_test"


def _seed():
    from database.models import (
        CashFlow,
        Position,
        PortfolioSnapshot,
        TradeHistory,
        get_session,
        init_database,
    )
    init_database()
    s = get_session()
    try:
        for model in (TradeHistory, Position, PortfolioSnapshot, CashFlow):
            s.query(model).filter(model.account_key.like(f"{KEY}%")).delete(
                synchronize_session=False)
        s.add(TradeHistory(symbol="005930", action="BUY", quantity=1, price=60000,
                           total_amount=60000, strategy=KEY, mode="paper",
                           account_key=KEY, executed_at=datetime(2026, 6, 10, 10, 0)))
        s.add(Position(mode="paper", account_key=KEY, symbol="005930",
                       avg_price=60_000, quantity=1, total_invested=60_000,
                       strategy=KEY))
        s.add(PortfolioSnapshot(account_key=KEY, date=datetime(2026, 6, 10),
                                total_value=10_000_000, cash=4_000_000,
                                invested=6_000_000, mdd=0.0, position_count=1))
        s.add(CashFlow(mode="paper", account_key=KEY, amount=100_000,
                       occurred_at=datetime(2026, 6, 10, 9, 0), note="paper"))
        s.commit()
    finally:
        s.close()


def _counts(key):
    from database.models import get_session, TradeHistory, PortfolioSnapshot
    s = get_session()
    try:
        t = s.query(TradeHistory).filter(TradeHistory.account_key == key).count()
        p = s.query(PortfolioSnapshot).filter(PortfolioSnapshot.account_key == key).count()
        return t, p
    finally:
        s.close()


def _run(*extra):
    return subprocess.run(
        [VENV, "tools/restart_basket_track_record.py", "--basket", "rt_test", *extra],
        capture_output=True,
        text=True,
        env={**os.environ},
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def test_dry_run_moves_nothing():
    _seed()
    r = _run("--archive-suffix", "t1")
    assert r.returncode == 0
    assert _counts(KEY) == (1, 1)
    assert _counts(f"{KEY}@t1") == (0, 0)


def test_apply_archives_and_undo_restores():
    _seed()
    r = _run("--archive-suffix", "t2", "--apply")
    assert r.returncode == 0, r.stderr
    assert _counts(KEY) == (0, 0)
    assert _counts(f"{KEY}@t2") == (1, 1)
    # 평가 수집기(정확 키 매칭)에서 아카이브 기록이 제외되는 것이 재시작의 본질
    r2 = _run("--undo", "--archive-suffix", "t2", "--apply")
    assert r2.returncode == 0, r2.stderr
    assert _counts(KEY) == (1, 1)
    assert _counts(f"{KEY}@t2") == (0, 0)


def test_undo_fails_closed_when_live_records_exist():
    _seed()
    _run("--archive-suffix", "t3", "--apply")
    _seed()  # 살아있는 키에 새 기록 생성
    r = _run("--undo", "--archive-suffix", "t3", "--apply")
    assert r.returncode == 1  # 섞임 방지


def test_apply_archives_only_paper_ledger_rows():
    from database.models import (
        CashFlow,
        Position,
        PortfolioSnapshot,
        TradeHistory,
        get_session,
    )

    _seed()
    s = get_session()
    try:
        s.add(TradeHistory(
            symbol="005930", action="BUY", quantity=2, price=61_000,
            total_amount=122_000, strategy=KEY, mode="live",
            account_key=KEY, executed_at=datetime(2026, 6, 10, 10, 1),
        ))
        s.add(Position(
            mode="live", account_key=KEY, symbol="005930",
            avg_price=61_000, quantity=2, total_invested=122_000,
            strategy=KEY,
        ))
        s.add(PortfolioSnapshot(
            mode="live", account_key=KEY, date=datetime(2026, 6, 10),
            total_value=20_000_000, cash=10_000_000,
            invested=10_000_000, mdd=0.0, position_count=1,
        ))
        s.add(CashFlow(
            mode="live", account_key=KEY, amount=200_000,
            occurred_at=datetime(2026, 6, 10, 9, 1), note="live",
        ))
        s.commit()
    finally:
        s.close()

    archive_key = f"{KEY}@t-mode"
    result = _run("--archive-suffix", "t-mode", "--apply")
    assert result.returncode == 0, result.stderr

    s = get_session()
    try:
        for model in (TradeHistory, Position, PortfolioSnapshot, CashFlow):
            assert s.query(model).filter(
                model.mode == "paper", model.account_key == KEY,
            ).count() == 0
            assert s.query(model).filter(
                model.mode == "paper", model.account_key == archive_key,
            ).count() == 1
            assert s.query(model).filter(
                model.mode == "live", model.account_key == KEY,
            ).count() == 1
            assert s.query(model).filter(
                model.mode == "live", model.account_key == archive_key,
            ).count() == 0
    finally:
        s.close()
