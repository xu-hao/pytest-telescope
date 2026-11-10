"""Restrictive owned copies keep identity, permissions and bounded handle lifetimes."""

import errno
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from frozen_tool_owned_handles_fixture import DirectoryReads, inode, mode, moved, owned_capture, remember
from frozen_tool_permission_fixture import identity

from pytest_telescope import sandbox
from pytest_telescope.model import InfrastructureError


def decoy(tmp_path, records):
    target = tmp_path / "decoy"
    target.mkdir()
    value = target / "value"
    value.write_bytes(b"untouched owned replacement\n")
    value.chmod(0o644)
    target.chmod(0o111)
    remember(records, target)
    remember(records, value)
    return target, (identity(target), mode(target), value.read_bytes(), mode(value))


def assert_decoy(target, before):
    assert (identity(target), mode(target), (target / "value").read_bytes(), mode(target / "value")) == before


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_frozen_copy_replacement_refuses_without_reading_replacement(tmp_path, replacement):
    with owned_capture(tmp_path) as owned:
        capture = owned.capture
        reads = DirectoryReads(capture.directory)
        with reads.observe():
            with capture:
                copied = capture.directory / "owned/install/inner"
                target, before = decoy(tmp_path, owned.records)
                displaced = owned.stage / "displaced"
                copied.parent.chmod(0o755)
                copied.rename(displaced)
                moved(owned.records, copied, displaced)
                if replacement == "directory":
                    copied.mkdir(mode=0o111)
                    copied.chmod(0o111)
                else:
                    copied.symlink_to(target, target_is_directory=True)
                remember(owned.records, copied)
                copied.parent.chmod(0o111)
                forbidden = {inode(target.lstat()), inode(copied.lstat())}
                original = os.scandir
                scanned = []

                def scandir(path):
                    current = os.fstat(path) if isinstance(path, int) else Path(path).lstat()
                    assert inode(current) not in forbidden, "replacement directory was enumerated"
                    scanned.append(inode(current))
                    return original(path)

                with patch.object(os, "scandir", scandir), pytest.raises(InfrastructureError):
                    capture.verify(["owned"])
                assert scanned
                assert_decoy(target, before)
            reads.assert_closed()
        assert_decoy(target, before)


def test_cleanup_rejects_labelled_foreign_owner_before_chmod_or_traversal(tmp_path):
    with owned_capture(tmp_path) as owned:
        capture = owned.capture
        reads = DirectoryReads(capture.directory)
        with reads.observe():
            capture.__enter__()
            expected = inode(capture.directory.lstat())
            original = os.stat
            labelled = []

            def ownership_stat(*args, **kwargs):
                actual = original(*args, **kwargs)
                if inode(actual) == expected:
                    labelled.append(expected)
                    return SimpleNamespace(st_dev=actual.st_dev, st_ino=actual.st_ino,
                                           st_mode=actual.st_mode, st_uid=os.getuid() + 1)
                return actual

            def forbidden(*args, **kwargs):
                pytest.fail("cleanup changed permissions or traversed after ownership refusal")

            before = mode(capture.directory / "owned/install/inner")
            with patch.object(os, "stat", ownership_stat), patch.object(os, "chmod", forbidden), patch.object(os, "scandir", forbidden):
                with pytest.raises(sandbox.CleanupError, match="unowned"):
                    capture.__exit__(None, None, None)
            assert labelled == [expected]
            assert mode(capture.directory / "owned/install/inner") == before == 0o111
            reads.assert_closed()


def test_cleanup_anchor_swap_restores_only_held_owned_inode_and_refuses(tmp_path):
    with owned_capture(tmp_path) as owned:
        capture = owned.capture
        reads = DirectoryReads(capture.directory)
        with reads.observe():
            capture.__enter__()
            copied = capture.directory / "owned/install/inner"
            expected = inode(copied.lstat())
            target, before = decoy(tmp_path, owned.records)
            displaced = owned.stage / "displaced"
            original_chmod, original_scan = os.chmod, os.scandir
            swapped = []

            def chmod(path, permissions, *args, **kwargs):
                if isinstance(path, str) and path.startswith("/proc/self/fd/"):
                    descriptor = int(path.rsplit("/", 1)[1])
                    if inode(os.fstat(descriptor)) == expected:
                        assert not swapped
                        # Only this fixture-owned parent changes during terminal cleanup.
                        original_chmod(copied.parent, 0o755, follow_symlinks=False)
                        copied.rename(displaced)
                        moved(owned.records, copied, displaced)
                        copied.symlink_to(target, target_is_directory=True)
                        remember(owned.records, copied)
                        swapped.append(descriptor)
                return original_chmod(path, permissions, *args, **kwargs)

            def scandir(path):
                current = os.fstat(path) if isinstance(path, int) else Path(path).lstat()
                assert inode(current) != inode(target.lstat()), "cleanup followed replacement target"
                return original_scan(path)

            with patch.object(os, "chmod", chmod), patch.object(os, "scandir", scandir):
                with pytest.raises(sandbox.CleanupError, match="moved"):
                    capture.__exit__(None, None, None)
            assert len(swapped) == 1 and inode(displaced.lstat()) == expected
            assert mode(displaced) == 0o711 and copied.is_symlink()
            assert not (displaced / "value").exists()
            assert_decoy(target, before)
            reads.assert_closed()


@pytest.mark.parametrize("failure", ["retry", "exhaust", "cancel"])
def test_owned_read_handles_close_on_retry_exhaustion_and_cancel(tmp_path, failure):
    with owned_capture(tmp_path) as owned:
        capture = owned.capture
        cancellation = threading.Event()
        capture.cancellation = cancellation
        reads = DirectoryReads(capture.directory)
        original_capture = capture._capture
        attempts, faults = [], []

        def attempt(deadline):
            if attempts:
                reads.assert_closed()
            attempts.append(len(attempts) + 1)
            return original_capture(deadline)

        with reads.observe(), patch.object(capture, "_capture", attempt):
            observed_open = os.open

            def opened(path, flags, *args, **kwargs):
                # Inner0111 is already held when the root's exact owned open occurs.
                if Path(path) == capture.directory / "owned/install" and not flags & os.O_PATH:
                    assert reads.active
                    if failure == "exhaust" or (failure == "retry" and not faults):
                        faults.append(attempts[-1])
                        raise OSError(errno.EMFILE, "selected owned directory open exhausted")
                    if failure == "cancel":
                        faults.append(attempts[-1])
                        cancellation.set()
                return observed_open(path, flags, *args, **kwargs)

            with patch.object(os, "open", opened):
                if failure == "retry":
                    with capture:
                        capture.verify(["owned"])
                    assert attempts == [1, 2] and faults == [1]
                else:
                    expected = sandbox.InvocationInterrupted if failure == "cancel" else InfrastructureError
                    with pytest.raises(expected):
                        with capture:
                            pytest.fail("failed capture was admitted")
                    assert attempts == ([1] if failure == "cancel" else [1, 2])
                    assert faults == attempts
            assert not capture.directory.exists()
            reads.assert_closed()


def test_concurrent_repeated_verification_keeps_sparse_projection_modes_and_closes_handles(tmp_path):
    with owned_capture(tmp_path, projection=True) as owned:
        capture = owned.capture
        reads = DirectoryReads(capture.directory)
        with reads.observe():
            with capture:
                view = capture.view(["owned"])
                projection = capture.views[("owned",)]
                mapped = dict((destination, source) for source, destination in view.bindings)
                closed = mapped[str(owned.fixture.protected)]
                paths = (capture.directory / "owned/install", capture.directory / "owned/install/inner", closed)
                assert all(mode(path) == 0o111 for path in paths)
                assert (closed / "alias").is_symlink() and (closed / "alias").read_bytes() == b"owned readable bytes\n"
                barrier = threading.Barrier(2, timeout=5)
                stop = threading.Event()
                completed, errors = [], []
                end = time.monotonic() + 5

                def forbidden(*args, **kwargs):
                    pytest.fail("verification widened an owned view's permissions")

                def verify():
                    try:
                        barrier.wait()
                        for _ in range(4):
                            if stop.is_set() or time.monotonic() >= end:
                                raise AssertionError("bounded concurrent verification did not finish")
                            capture.verify(["owned"])
                            projection.verify()
                            assert all(mode(path) == 0o111 for path in paths)
                            completed.append(1)
                    except BaseException as error:
                        errors.append(error)
                        stop.set()

                threads = [threading.Thread(target=verify, daemon=True) for _ in range(2)]
                with patch.object(os, "chmod", forbidden), patch.object(os, "fchmod", forbidden):
                    capture.verify(["owned"])
                    capture.verify(["owned"])
                    try:
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(max(0, end - time.monotonic()))
                    finally:
                        stop.set()
                        for thread in threads:
                            thread.join(5)
                    assert not any(thread.is_alive() for thread in threads)
                    assert not errors and len(completed) == 8
                assert all(mode(path) == 0o111 for path in paths)
            assert not capture.directory.exists()
            reads.assert_closed()
