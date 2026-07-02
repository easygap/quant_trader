"""rebalance CLI의 일일 NAV 스냅샷·백업 회귀 테스트.

paper 트랙레코드는 상시 스케줄러 없이 일일 `--mode rebalance` 실행만으로도
바스켓 전용 계정(basket_rebalance:<name>)에 NAV 시계열이 쌓여야 한다.
거래 발생 여부와 무관하게 바스켓별 save_daily_nav_snapshot을 호출하고
(가격 완전성 가드는 헬퍼 내부 책임), dry-run에서는 호출하지 않는다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _args(basket="kr_test", dry_run=False):
    return SimpleNamespace(basket=basket, dry_run=dry_run, confirm_live=False)


@pytest.fixture
def patched_rebalance(monkeypatch):
    """run_rebalance의 협력자들을 모킹하고 fake_rebalancer를 돌려준다."""
    from config.config_loader import Config
    Config._instance = None  # settings.yaml 재로드 (trading.mode=paper)

    fake_rb = MagicMock()
    fake_rb.get_status_report.return_value = "status"
    # 거래가 발생하지 않는 날에도 스냅샷은 저장돼야 한다.
    fake_rb.should_rebalance.return_value = (False, "드리프트 미달")
    monkeypatch.setattr(
        "core.basket_rebalancer.BasketRebalancer", MagicMock(return_value=fake_rb)
    )
    monkeypatch.setattr("core.notifier.Notifier", MagicMock())
    return fake_rb


def test_paper_rebalance_saves_basket_nav_snapshot_even_without_trades(patched_rebalance):
    import main as main_mod

    main_mod.run_rebalance(_args(dry_run=False))
    patched_rebalance.save_daily_nav_snapshot.assert_called_once()


def test_dry_run_rebalance_does_not_save_snapshot(patched_rebalance):
    import main as main_mod

    main_mod.run_rebalance(_args(dry_run=True))
    patched_rebalance.save_daily_nav_snapshot.assert_not_called()


def test_paper_rebalance_triggers_daily_db_backup(patched_rebalance, monkeypatch):
    """일일 rebalance CLI가 DB 백업도 호출한다(트랙레코드 단일 파일 보호).

    backup_path 미설정이면 run_daily_backup이 no-op(False)이므로 호출 자체는 항상 안전.
    """
    import main as main_mod

    called = {}
    monkeypatch.setattr(
        "database.backup.run_daily_backup",
        lambda config=None: called.setdefault("backup", True),
    )
    main_mod.run_rebalance(_args(dry_run=False))
    assert called.get("backup") is True


def test_dry_run_rebalance_skips_db_backup(patched_rebalance, monkeypatch):
    import main as main_mod

    called = {}
    monkeypatch.setattr(
        "database.backup.run_daily_backup",
        lambda config=None: called.setdefault("backup", True),
    )
    main_mod.run_rebalance(_args(dry_run=True))
    assert "backup" not in called


def test_paper_rebalance_sends_daily_discord_report(patched_rebalance, monkeypatch):
    """일일 CLI 사이클이 디스코드 일일 리포트를 발송한다(상시 스케줄러 없이도
    운영자가 매일 푸시를 받도록). 실패해도 사이클에는 영향 없어야 한다."""
    import main as main_mod
    from unittest.mock import MagicMock

    fake_notifier = MagicMock()
    monkeypatch.setattr("core.notifier.Notifier", MagicMock(return_value=fake_notifier))
    patched_rebalance._market_snapshot = {"005930": {"price": 61000.0}}
    patched_rebalance.portfolio_mgr.get_portfolio_summary.return_value = {
        "total_value": 9_800_000, "cash": 2_000_000, "total_return": -2.0,
        "mdd": 2.0, "position_count": 9,
    }
    main_mod.run_rebalance(_args(dry_run=False))
    assert fake_notifier.send_daily_report.called
    payload = fake_notifier.send_daily_report.call_args.args[0]
    assert payload["total_value"] == 9_800_000
    assert payload["position_count"] == 9
    # 시장가 평가 계약: current_prices 없이 부르면 paper 포지션이 avg_price로 평가돼
    # 누적수익 -0.0%/MDD 0%가 60일 내내 표시된다(자기검토 2라운드 HIGH) — 가격 전달 고정.
    summary_kwargs = patched_rebalance.portfolio_mgr.get_portfolio_summary.call_args.kwargs
    assert summary_kwargs.get("current_prices") == {"005930": 61000.0}


def test_dry_run_rebalance_does_not_send_daily_report(patched_rebalance, monkeypatch):
    import main as main_mod
    from unittest.mock import MagicMock

    fake_notifier = MagicMock()
    monkeypatch.setattr("core.notifier.Notifier", MagicMock(return_value=fake_notifier))
    main_mod.run_rebalance(_args(dry_run=True))
    assert not fake_notifier.send_daily_report.called


# --- P0-1 사이클 관측성 배선(run_rebalance 통합) ---

def _summary(**over):
    base = {"total_value": 1_000_000, "cash": 1_000_000, "total_return": 0.0,
            "mdd": 0.0, "position_count": 0}
    base.update(over)
    return base


def test_cycle_events_emitted_on_normal_paper_cycle(patched_rebalance, monkeypatch):
    """정상 사이클: CYCLE_START → SNAPSHOT_SAVED → CYCLE_END(1/1 저장)."""
    import main as main_mod
    import core.cycle_observability as co

    events = []
    monkeypatch.setattr(co, "record_cycle_event",
                        lambda et, msg, **kw: events.append((et, msg, kw.get("severity", "info"))) or True)
    monkeypatch.setattr(co, "detect_snapshot_gaps_for_account", lambda *a, **k: [])
    patched_rebalance.save_daily_nav_snapshot.return_value = True
    patched_rebalance._market_snapshot = {"005930": {"price": 61000.0}}
    patched_rebalance.portfolio_mgr.get_portfolio_summary.return_value = _summary()

    main_mod.run_rebalance(_args(dry_run=False))

    types = [e[0] for e in events]
    assert "CYCLE_START" in types
    assert "SNAPSHOT_SAVED" in types
    assert "SNAPSHOT_SKIPPED" not in types
    end = [e for e in events if e[0] == "CYCLE_END"]
    assert end and "1/1" in end[0][1]


def test_snapshot_skipped_path_records_warning_and_undercounts(patched_rebalance, monkeypatch):
    """스냅샷 스킵(가격 미확보 등): SNAPSHOT_SKIPPED(warning) + CYCLE_END 0/1 저장."""
    import main as main_mod
    import core.cycle_observability as co

    events = []
    monkeypatch.setattr(co, "record_cycle_event",
                        lambda et, msg, **kw: events.append((et, msg, kw.get("severity", "info"))) or True)
    monkeypatch.setattr(co, "detect_snapshot_gaps_for_account", lambda *a, **k: [])
    patched_rebalance.save_daily_nav_snapshot.return_value = False
    patched_rebalance._market_snapshot = {"005930": {"price": 61000.0}}
    patched_rebalance.portfolio_mgr.get_portfolio_summary.return_value = _summary()

    main_mod.run_rebalance(_args(dry_run=False))

    skipped = [e for e in events if e[0] == "SNAPSHOT_SKIPPED"]
    assert skipped and skipped[0][2] == "warning"
    assert "SNAPSHOT_SAVED" not in [e[0] for e in events]
    end = [e for e in events if e[0] == "CYCLE_END"]
    assert end and "0/1" in end[0][1]


def test_gap_today_missing_pages_critical(patched_rebalance, monkeypatch):
    """오늘 결측이면 critical 경보(즉시 조치)."""
    import main as main_mod
    import core.cycle_observability as co
    from unittest.mock import MagicMock

    fake_notifier = MagicMock()
    monkeypatch.setattr("core.notifier.Notifier", MagicMock(return_value=fake_notifier))
    # detect가 '오늘'을 결측으로 반환 → today_missing True
    monkeypatch.setattr(co, "detect_snapshot_gaps_for_account",
                        lambda cfg, key, today, **k: [today.date()])
    patched_rebalance._market_snapshot = {"005930": {"price": 61000.0}}
    patched_rebalance.portfolio_mgr.get_portfolio_summary.return_value = _summary()

    main_mod.run_rebalance(_args(dry_run=False))

    calls = fake_notifier.send_message.call_args_list
    assert any(c.kwargs.get("critical") is True for c in calls)


def test_gap_prior_only_not_critical(patched_rebalance, monkeypatch):
    """복구 불가한 과거 결측은 매일 critical로 울리지 않는다(피로 방지)."""
    import main as main_mod
    import core.cycle_observability as co
    from datetime import date
    from unittest.mock import MagicMock

    fake_notifier = MagicMock()
    monkeypatch.setattr("core.notifier.Notifier", MagicMock(return_value=fake_notifier))
    monkeypatch.setattr(co, "detect_snapshot_gaps_for_account",
                        lambda cfg, key, today, **k: [date(2020, 1, 2)])  # 먼 과거
    patched_rebalance._market_snapshot = {"005930": {"price": 61000.0}}
    patched_rebalance.portfolio_mgr.get_portfolio_summary.return_value = _summary()

    main_mod.run_rebalance(_args(dry_run=False))

    calls = fake_notifier.send_message.call_args_list
    # gap 경보는 발송되되 critical은 아니어야 한다
    assert any(c.kwargs.get("critical") is False for c in calls)
    assert all(c.kwargs.get("critical") is not True for c in calls)
