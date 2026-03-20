"""
장시간 프로세스(스케줄러) 단일 인스턴스 보장.
Linux/서버: fcntl flock. Windows: 락 생략(개발용) — Oracle/Ubuntu 배포 경로를 우선한다.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from loguru import logger

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


@contextmanager
def scheduler_lock(lock_path: str | Path) -> Generator[bool, None, None]:
    """
    스케줄러 중복 기동 방지. 획득 실패 시 yield False.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not _HAS_FCNTL:
        logger.warning(
            "fcntl 미지원 OS — 스케줄러 단일 인스턴스 락을 건너뜁니다. "
            "프로덕션은 Linux에서 실행하세요."
        )
        yield True
        return

    fp = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error(
                "스케줄러가 이미 실행 중입니다 (락 파일: {}). 중복 기동을 중단합니다.",
                path,
            )
            fp.close()
            yield False
            return

        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        os.fsync(fp.fileno())
        logger.info("스케줄러 락 획득: {} (pid={})", path, os.getpid())
        try:
            yield True
        finally:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fp.close()
    except Exception:
        try:
            fp.close()
        except OSError:
            pass
        raise
