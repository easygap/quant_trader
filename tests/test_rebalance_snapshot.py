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
