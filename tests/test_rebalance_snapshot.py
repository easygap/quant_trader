"""rebalance CLI의 일일 NAV 스냅샷 저장 회귀 테스트.

paper 트랙레코드는 상시 스케줄러(장마감 단계) 없이 일일 `--mode rebalance` 실행만으로도
NAV 시계열이 쌓여야 한다. 거래 발생 여부와 무관하게 paper에서 스냅샷을 저장하고,
dry-run에서는 저장하지 않는다.
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
    """run_rebalance의 협력자들을 모킹하고 (fake_rebalancer, fake_pm)을 돌려준다."""
    from config.config_loader import Config
    Config._instance = None  # settings.yaml 재로드 (trading.mode=paper)

    fake_rb = MagicMock()
    fake_rb.get_status_report.return_value = "status"
    # 거래가 발생하지 않는 날에도 스냅샷은 저장돼야 한다.
    fake_rb.should_rebalance.return_value = (False, "드리프트 미달")
    fake_rb._market_snapshot = {"005930": {"price": 70_000.0, "avg_volume": 1e6}}
    monkeypatch.setattr(
        "core.basket_rebalancer.BasketRebalancer", MagicMock(return_value=fake_rb)
    )
    monkeypatch.setattr("core.notifier.Notifier", MagicMock())

    fake_pm = MagicMock()
    monkeypatch.setattr(
        "core.portfolio_manager.PortfolioManager", MagicMock(return_value=fake_pm)
    )
    return fake_rb, fake_pm


def test_paper_rebalance_saves_daily_snapshot_even_without_trades(patched_rebalance):
    import main as main_mod

    _, fake_pm = patched_rebalance
    main_mod.run_rebalance(_args(dry_run=False))

    fake_pm.save_daily_snapshot.assert_called_once()
    kwargs = fake_pm.save_daily_snapshot.call_args.kwargs
    # 수집된 현재가가 스냅샷 평가에 전달된다.
    assert kwargs["current_prices"] == {"005930": 70_000.0}


def test_dry_run_rebalance_does_not_save_snapshot(patched_rebalance):
    import main as main_mod

    _, fake_pm = patched_rebalance
    main_mod.run_rebalance(_args(dry_run=True))

    fake_pm.save_daily_snapshot.assert_not_called()


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
