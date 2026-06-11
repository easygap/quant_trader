"""run_daily_backup의 격리 환경 가드 회귀 테스트.

사고(2026-06-10): 테스트 스위트가 run_daily_backup을 monkeypatch 없이 실행해
격리 DB(QUANT_DB_PATH의 빈 DB)를 같은 날짜 파일명으로 운영 data/backups/에
덮어썼다 — 당일 운영 백업이 빈 DB로 대체됨(복원 리허설에서 발견).
가드: QUANT_DB_PATH(격리)가 설정돼 있으면 QUANT_BACKUP_PATH를 명시하지 않는 한
운영 backup_path에 쓰지 않는다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from types import SimpleNamespace

from database.backup import run_daily_backup


def _config(tmp_db, backup_path="data/backups"):
    return SimpleNamespace(database={
        "sqlite_path": str(tmp_db),
        "backup_path": backup_path,
        "backup_retention_days": 14,
    })


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()


def test_isolated_db_does_not_write_operational_backup(tmp_path, monkeypatch):
    """QUANT_DB_PATH 환경에서는 운영 backup_path 백업을 거부한다(오염 방지)."""
    db = tmp_path / "isolated.db"
    _make_db(db)
    monkeypatch.setenv("QUANT_DB_PATH", str(db))
    monkeypatch.delenv("QUANT_BACKUP_PATH", raising=False)

    assert run_daily_backup(_config(db)) is False


def test_isolated_db_backs_up_when_backup_path_also_overridden(tmp_path, monkeypatch):
    """QUANT_BACKUP_PATH까지 명시하면(완전 격리) 백업을 수행한다."""
    db = tmp_path / "isolated.db"
    _make_db(db)
    bdir = tmp_path / "bk"
    monkeypatch.setenv("QUANT_DB_PATH", str(db))
    monkeypatch.setenv("QUANT_BACKUP_PATH", str(bdir))

    assert run_daily_backup(_config(db)) is True
    assert list(bdir.glob("quant_trader_*.db"))


def test_operational_env_backs_up_normally(tmp_path, monkeypatch):
    """운영 환경(QUANT_DB_PATH 미설정)에서는 기존대로 백업한다."""
    db = tmp_path / "op.db"
    _make_db(db)
    bdir = tmp_path / "opbk"
    monkeypatch.delenv("QUANT_DB_PATH", raising=False)
    monkeypatch.delenv("QUANT_BACKUP_PATH", raising=False)

    assert run_daily_backup(_config(db, backup_path=str(bdir))) is True
    assert list(bdir.glob("quant_trader_*.db"))
