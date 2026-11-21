"""Scratch ownership across overlapping runs in one checkout."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import pytest

from pytest_telescope import sandbox


def abandoned_copy(root, index=0):
    """An exited owner's copy: valid registration, no remaining lock holder."""
    state = sandbox._state(root)
    directory = state / "scratch" / f"invocation-{index:032x}"
    directory.mkdir()
    (directory / "owner.lock").touch(mode=0o600)
    return directory


@pytest.mark.parametrize("cleanup", ["owner", "recovery"])
def test_recovery_cannot_observe_half_removed_invocation(tmp_path, monkeypatch, cleanup):
    """Pause at the actual owner unlink, and recover during that teardown window."""
    unlink = os.unlink
    observed = []
    pending = []
    state = tmp_path / ".pytest-telescope"

    def inspect_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if Path(path).name != "owner.lock":
            return result
        observed.append(path)
        # A second descriptor represents another run trying to scan scratch. If
        # teardown excludes that scan, queue recovery until its lock is released.
        descriptor = os.open(state / "allocation.lock", os.O_RDWR)
        try:
            try:
                sandbox.fcntl.flock(descriptor, sandbox.fcntl.LOCK_EX | sandbox.fcntl.LOCK_NB)
            except BlockingIOError:
                pending.append(pool.submit(sandbox.recover, tmp_path))
            else:
                sandbox.fcntl.flock(descriptor, sandbox.fcntl.LOCK_UN)
                sandbox.recover(tmp_path)
        finally:
            os.close(descriptor)
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        manager = sandbox.invocation(tmp_path) if cleanup == "owner" else None
        directory = manager.__enter__() if manager is not None else abandoned_copy(tmp_path)
        (directory / "project").mkdir()
        (directory / "project" / "input.txt").write_text("frozen")
        monkeypatch.setattr(sandbox.os, "unlink", inspect_unlink)
        if cleanup == "owner":
            manager.__exit__(None, None, None)
        else:
            sandbox.recover(tmp_path)
        for recovery in pending:
            recovery.result(timeout=10)
    assert len(observed) == 1
    assert not directory.exists()


def test_same_checkout_overlapping_owners_exceed_old_shared_ceiling(tmp_path):
    """Two runs each hold an outer copy plus 32 worker copies, under real locks."""
    with ExitStack() as first, ExitStack() as second:
        left = [first.enter_context(sandbox.invocation(tmp_path)) for _ in range(33)]
        right = [second.enter_context(sandbox.invocation(tmp_path)) for _ in range(33)]
        sandbox.recover(tmp_path)
        assert all(path.exists() for path in left + right)
    assert list((tmp_path / ".pytest-telescope/scratch").iterdir()) == []


def test_recovery_accepts_more_than_64_live_locked_directories(tmp_path):
    """Direct locked owners isolate recovery's limit from allocation's limit."""
    state = sandbox._state(tmp_path)
    directories = []
    with ExitStack() as owners:
        for index in range(66):
            directory = state / "scratch" / f"invocation-{index:032x}"
            directory.mkdir()
            descriptor = os.open(directory / "owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
            owners.callback(os.close, descriptor)
            sandbox.fcntl.flock(descriptor, sandbox.fcntl.LOCK_EX)
            directories.append(directory)
        sandbox.recover(tmp_path)
        assert all(path.exists() for path in directories)
    # Preserve the abandoned-directory bound; this is deliberately too much to recover.
    with pytest.raises(sandbox.CleanupError, match="invocation directory budget exceeded"):
        sandbox.recover(tmp_path)


@pytest.mark.parametrize("operation", ["allocate", "recover"])
@pytest.mark.parametrize("count", [63, 64, 65])
def test_abandoned_directory_budget_still_refuses_unbounded_copies(tmp_path, operation, count):
    directories = [abandoned_copy(tmp_path, index) for index in range(count)]
    for directory in directories:
        (directory / "frozen.txt").write_text("preserve on refusal")
    limit = 64 if operation == "allocate" else 65

    def perform():
        if operation == "allocate":
            with sandbox.invocation(tmp_path):
                pass
        else:
            sandbox.recover(tmp_path)

    if count >= limit:
        with pytest.raises(sandbox.CleanupError, match="invocation directory budget"):
            perform()
        assert all(
            (path / "frozen.txt").read_text() == "preserve on refusal" for path in directories
        )
    else:
        perform()
        assert all(path.exists() == (operation == "allocate") for path in directories)
        sandbox.recover(tmp_path)
    assert len(list((tmp_path / ".pytest-telescope/scratch").iterdir())) == (
        count if count >= limit else 0
    )


@pytest.mark.parametrize("operation", ["allocate", "recover"])
def test_missing_owner_lock_still_refuses_without_removing_evidence(tmp_path, operation):
    directory = abandoned_copy(tmp_path)
    (directory / "owner.lock").unlink()
    (directory / "frozen.txt").write_text("preserve")
    with pytest.raises(sandbox.CleanupError, match="incomplete invocation directory"):
        if operation == "allocate":
            with sandbox.invocation(tmp_path):
                pytest.fail("malformed scratch admitted")
        else:
            sandbox.recover(tmp_path)
    assert (directory / "frozen.txt").read_text() == "preserve"


@pytest.mark.parametrize("change", ["completed", "malformed"])
def test_recovery_rechecks_discovered_directory_after_missing_owner_open(
    tmp_path, monkeypatch, change
):
    directory = abandoned_copy(tmp_path)
    original_open = os.open
    opens = 0

    def changed_open(path, flags, *args, **kwargs):
        nonlocal opens
        if Path(path) == directory / "owner.lock":
            opens += 1
            if opens == 2:
                # The first open was discovery. Another recovery finishes before
                # the later ownership attempt, or corruption leaves a partial copy.
                if change == "completed":
                    sandbox.recover(tmp_path)
                else:
                    (directory / "owner.lock").unlink()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sandbox.os, "open", changed_open)
    if change == "completed":
        sandbox.recover(tmp_path)
        assert not directory.exists()
    else:
        with pytest.raises(sandbox.CleanupError, match="incomplete invocation directory"):
            sandbox.recover(tmp_path)
        assert directory.exists()
    assert opens >= 2


def test_large_content_cleanup_does_not_hold_allocation_lock(tmp_path, monkeypatch):
    original_rmtree = sandbox.shutil.rmtree
    observed = []

    def inspect_cleanup(path, *args, **kwargs):
        if Path(path).name == "project":
            sandbox.recover(tmp_path)
            # Recovery spares this locked owner; another run can register too.
            with sandbox.invocation(tmp_path):
                observed.append(path)
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(sandbox.shutil, "rmtree", inspect_cleanup)
    with sandbox.invocation(tmp_path) as directory:
        (directory / "project").mkdir()
    assert observed == [directory / "project"]
    assert not directory.exists()


@pytest.mark.parametrize("count", [2, 3])
def test_abandoned_budget_applies_alongside_many_live_owners(tmp_path, monkeypatch, count):
    monkeypatch.setattr(sandbox, "MAX_INVOCATIONS", 2)
    with ExitStack() as owners:
        live = [owners.enter_context(sandbox.invocation(tmp_path)) for _ in range(4)]
        abandoned = [abandoned_copy(tmp_path, index) for index in range(count)]
        with pytest.raises(sandbox.CleanupError, match="invocation directory budget exhausted"):
            with sandbox.invocation(tmp_path):
                pytest.fail("abandoned budget ignored")
        if count == 3:
            with pytest.raises(sandbox.CleanupError, match="invocation directory budget exceeded"):
                sandbox.recover(tmp_path)
            assert all(path.exists() for path in abandoned)
            # Remove only this fixture's excess registration, then recover within budget.
            (abandoned[-1] / "owner.lock").unlink()
            abandoned[-1].rmdir()
        sandbox.recover(tmp_path)
        assert all(path.exists() for path in live)
        assert not any(path.exists() for path in abandoned)
    assert list((tmp_path / ".pytest-telescope/scratch").iterdir()) == []


def test_content_cleanup_failure_retains_registration_for_recovery(tmp_path, monkeypatch):
    def missing_content(path, *args, **kwargs):
        raise FileNotFoundError("content disappeared during cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(sandbox.shutil, "rmtree", missing_content)
        with pytest.raises(sandbox.CleanupError, match="owned frozen copy cleanup failed"):
            with sandbox.invocation(tmp_path) as directory:
                (directory / "project").mkdir()
        assert (directory / "owner.lock").is_file()
    sandbox.recover(tmp_path)
    assert not directory.exists()
