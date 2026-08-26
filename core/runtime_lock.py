"""Cross-platform single-process runtime locks.

The lock file is only metadata.  Ownership is enforced by the operating
system, so a stale file left after a crash does not keep the runtime locked.
Linux/macOS use ``flock`` and Windows uses ``msvcrt.locking``.  If neither
backend is available, or the lock cannot be checked, acquisition fails closed.
"""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from loguru import logger

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None


LIVE_RUNTIME_LOCK_FILENAME = ".live_runtime.lock"


def _write_owner_metadata(fp, label: str) -> None:
    """Write diagnostics without changing the byte used by Windows locking."""
    metadata = f"pid={os.getpid()}\nlabel={label}\n".encode("utf-8", errors="replace")
    fp.seek(1)
    fp.truncate()
    fp.write(metadata)
    fp.flush()
    os.fsync(fp.fileno())


def _acquire_nonblocking(fp) -> str:
    """Acquire an OS lock and return the backend name."""
    if fcntl is not None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "fcntl"
    if msvcrt is not None:
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        return "msvcrt"
    raise RuntimeError("지원되는 프로세스 락 백엔드가 없습니다")


def _release(fp, backend: str) -> None:
    if backend == "fcntl":
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    elif backend == "msvcrt":
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def process_runtime_lock(
    lock_path: str | Path,
    *,
    label: str = "runtime",
) -> Generator[bool, None, None]:
    """Try to hold one non-blocking inter-process lock for the context.

    ``True`` means the caller exclusively owns the runtime.  ``False`` means
    another process owns it *or* exclusivity could not be proved.  Callers that
    can submit orders must treat both cases as a hard stop.
    """
    path = Path(lock_path)
    fp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(path, "a+b")
        # Windows byte-range locks need a real byte to lock.  It remains a
        # permanent sentinel; owner metadata starts at byte 1.
        fp.seek(0, os.SEEK_END)
        if fp.tell() == 0:
            fp.write(b"\0")
            fp.flush()
            os.fsync(fp.fileno())
    except Exception as exc:
        logger.error(
            "{} 락 파일 준비 실패 — 단일 인스턴스를 증명할 수 없어 실행 차단: {} ({})",
            label,
            path,
            exc,
        )
        if fp is not None:
            fp.close()
        yield False
        return

    backend = ""
    acquired = False
    try:
        try:
            backend = _acquire_nonblocking(fp)
            acquired = True
        except BlockingIOError:
            logger.error(
                "{}가 이미 실행 중입니다 (락 파일: {}). 중복 기동을 차단합니다.",
                label,
                path,
            )
            yield False
            return
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                logger.error(
                    "{}가 이미 실행 중입니다 (락 파일: {}). 중복 기동을 차단합니다.",
                    label,
                    path,
                )
            else:
                logger.error(
                    "{} 락 획득 실패 — 단일 인스턴스를 증명할 수 없어 실행 차단: {} ({})",
                    label,
                    path,
                    exc,
                )
            yield False
            return
        except Exception as exc:
            logger.error(
                "{} 락 획득 실패 — 단일 인스턴스를 증명할 수 없어 실행 차단: {} ({})",
                label,
                path,
                exc,
            )
            yield False
            return

        try:
            _write_owner_metadata(fp, label)
        except Exception as exc:
            logger.error(
                "{} 락 소유자 기록 실패 — 안전을 위해 실행 차단: {} ({})",
                label,
                path,
                exc,
            )
            try:
                _release(fp, backend)
            except OSError:
                pass
            acquired = False
            yield False
            return

        logger.info(
            "{} 락 획득: {} (pid={}, backend={})",
            label,
            path,
            os.getpid(),
            backend,
        )
        try:
            yield True
        finally:
            if acquired:
                try:
                    _release(fp, backend)
                except OSError as exc:
                    # Closing the descriptor below still releases the OS lock.
                    logger.error("{} 락 명시적 해제 실패: {} ({})", label, path, exc)
    finally:
        fp.close()


@contextmanager
def scheduler_lock(lock_path: str | Path) -> Generator[bool, None, None]:
    """Backward-compatible scheduler single-instance lock."""
    with process_runtime_lock(lock_path, label="스케줄러") as acquired:
        yield acquired


@contextmanager
def live_runtime_lock(project_root: str | Path) -> Generator[bool, None, None]:
    """Global lock shared by every non-emergency live order runtime."""
    lock_path = Path(project_root) / "data" / LIVE_RUNTIME_LOCK_FILENAME
    with process_runtime_lock(lock_path, label="실전 주문 런타임") as acquired:
        yield acquired
