"""
SQLite DB 일일 백업.
- SQLite 파일 손상 시 포지션/거래 기록 전체 소실 가능 — 일일 자동 백업으로 복구 가능하게 함
- 장마감 후(및 live 모드에서 KIS 잔고 크로스체크 후) backup_path에 날짜별 복사본 생성

WAL 모드에서 shutil.copy2는 -wal/-shm 파일을 함께 복사하지 않으면 손상될 수 있다.
→ SQLite Online Backup API (sqlite3.Connection.backup)를 사용해 라이브 DB를 안전하게 복사.
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from loguru import logger


def _backup_via_sqlite_api(src_path: Path, dest_path: Path) -> bool:
    """SQLite Online Backup API로 안전 백업. WAL 모드에서도 일관된 스냅샷 보장."""
    src_conn = None
    dst_conn = None
    try:
        src_conn = sqlite3.connect(str(src_path))
        dst_conn = sqlite3.connect(str(dest_path))
        src_conn.backup(dst_conn)
        return True
    finally:
        if dst_conn:
            dst_conn.close()
        if src_conn:
            src_conn.close()


def run_daily_backup(config=None) -> bool:
    """
    설정에 backup_path가 있으면 SQLite 파일을 날짜별로 백업.
    SQLite Online Backup API 우선 사용 → 실패 시 shutil.copy2 폴백.

    Args:
        config: Config 인스턴스 (None이면 Config.get())

    Returns:
        백업 수행 여부 (설정 없으면 False)
    """
    if config is None:
        from config.config_loader import Config
        config = Config.get()

    db_config = config.database
    backup_path = db_config.get("backup_path") or db_config.get("backup_dir")
    if not backup_path:
        return False

    sqlite_path = db_config.get("sqlite_path", "data/quant_trader.db")
    root = Path(__file__).resolve().parent.parent
    src = root / sqlite_path if not Path(sqlite_path).is_absolute() else Path(sqlite_path)
    if not src.exists():
        logger.warning("DB 백업 스킵: 소스 파일 없음 {}", src)
        return False

    dest_dir = root / backup_path if not Path(backup_path).is_absolute() else Path(backup_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    dest = dest_dir / f"quant_trader_{date_str}.db"

    try:
        if _backup_via_sqlite_api(src, dest):
            logger.info("DB 백업 완료 (Online Backup API): {}", dest)
        else:
            raise RuntimeError("backup API 반환값 False")
    except Exception as e:
        logger.warning("SQLite Online Backup 실패 ({}), shutil.copy2 폴백 시도", e)
        try:
            for suffix in ("-wal", "-shm"):
                wal = src.parent / (src.name + suffix)
                if wal.exists():
                    shutil.copy2(wal, dest.parent / (dest.name + suffix))
            shutil.copy2(src, dest)
            logger.info("DB 백업 완료 (shutil 폴백, -wal/-shm 포함): {}", dest)
        except Exception as e2:
            logger.error("DB 백업 실패: {}", e2)
            return False

    retention_days = int(db_config.get("backup_retention_days", 7))
    _purge_old_backups(dest_dir, retention_days)
    return True


def _purge_old_backups(backup_dir: Path, retention_days: int):
    """보관 일수 초과 백업 파일 삭제."""
    if retention_days <= 0:
        return
    cutoff = datetime.now().timestamp() - (retention_days * 86400)
    for f in backup_dir.glob("quant_trader_*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.debug("오래된 백업 삭제: {}", f)
        except Exception as e:
            logger.warning("백업 파일 삭제 실패 {}: {}", f, e)
