from contextlib import contextmanager
import multiprocessing
from types import SimpleNamespace

import pytest


def _try_runtime_lock_in_child(lock_path, result_queue):
    from core.runtime_lock import process_runtime_lock

    with process_runtime_lock(lock_path, label="child") as acquired:
        result_queue.put(acquired)


def test_process_runtime_lock_is_exclusive_and_releases(tmp_path):
    from core.runtime_lock import process_runtime_lock

    lock_path = tmp_path / "runtime.lock"
    with process_runtime_lock(lock_path, label="first") as first:
        assert first is True
        with process_runtime_lock(lock_path, label="second") as second:
            assert second is False

    with process_runtime_lock(lock_path, label="after-release") as acquired_again:
        assert acquired_again is True


def test_process_runtime_lock_blocks_a_separate_process(tmp_path):
    from core.runtime_lock import process_runtime_lock

    lock_path = tmp_path / "runtime.lock"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()

    with process_runtime_lock(lock_path, label="parent") as acquired:
        assert acquired is True
        child = context.Process(
            target=_try_runtime_lock_in_child,
            args=(lock_path, result_queue),
        )
        child.start()
        assert result_queue.get(timeout=10) is False
        child.join(timeout=10)

    assert child.exitcode == 0


def test_process_runtime_lock_fails_closed_without_backend(tmp_path, monkeypatch):
    import core.runtime_lock as runtime_lock

    monkeypatch.setattr(runtime_lock, "fcntl", None)
    monkeypatch.setattr(runtime_lock, "msvcrt", None)

    with runtime_lock.process_runtime_lock(tmp_path / "runtime.lock") as acquired:
        assert acquired is False


def test_process_runtime_lock_fails_closed_when_lock_file_unavailable(tmp_path):
    from core.runtime_lock import process_runtime_lock

    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("block child creation", encoding="utf-8")

    with process_runtime_lock(not_a_directory / "runtime.lock") as acquired:
        assert acquired is False


def test_live_runtime_lock_uses_one_global_path(tmp_path):
    from core.runtime_lock import LIVE_RUNTIME_LOCK_FILENAME, live_runtime_lock

    with live_runtime_lock(tmp_path) as first:
        assert first is True
        assert (tmp_path / "data" / LIVE_RUNTIME_LOCK_FILENAME).exists()
        with live_runtime_lock(tmp_path) as second:
            assert second is False


def test_run_live_trading_fails_before_workflow_when_runtime_lock_unavailable(monkeypatch):
    import core.runtime_lock as runtime_lock
    import main as main_mod

    calls = []

    @contextmanager
    def denied_lock(project_root):
        calls.append(project_root)
        yield False

    monkeypatch.setattr(runtime_lock, "live_runtime_lock", denied_lock)
    monkeypatch.setattr(
        main_mod,
        "_run_live_trading_impl",
        lambda args: pytest.fail("lock 없이 live workflow를 실행하면 안 됨"),
    )

    with pytest.raises(SystemExit) as exc:
        main_mod.run_live_trading(SimpleNamespace(strategy="scoring", confirm_live=True))

    assert exc.value.code == 1
    assert len(calls) == 1


def test_live_rebalance_fails_before_workflow_when_runtime_lock_unavailable(monkeypatch):
    import core.runtime_lock as runtime_lock
    import main as main_mod

    config = SimpleNamespace(trading={"mode": "live"})
    calls = []

    @contextmanager
    def denied_lock(project_root):
        calls.append(project_root)
        yield False

    monkeypatch.setattr(main_mod.Config, "get", lambda: config)
    monkeypatch.setattr(runtime_lock, "live_runtime_lock", denied_lock)
    monkeypatch.setattr(
        main_mod,
        "_run_rebalance_impl",
        lambda args: pytest.fail("lock 없이 live rebalance를 실행하면 안 됨"),
    )

    with pytest.raises(SystemExit) as exc:
        main_mod.run_rebalance(
            SimpleNamespace(basket="test_basket", dry_run=False, confirm_live=True)
        )

    assert exc.value.code == 1
    assert len(calls) == 1
