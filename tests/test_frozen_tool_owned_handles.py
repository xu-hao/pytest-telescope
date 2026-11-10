"""Restrictive owned copies keep identity, permissions and bounded handle lifetimes."""

import errno
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from frozen_tool_owned_handles_fixture import (
    DirectoryReads,
    inode,
    mode,
    moved,
    owned_capture,
    remember,
)
from frozen_tool_permission_fixture import errors, identity

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
                copied = capture.directory / "owned/install"
                expected = identity(copied)
                held = capture.owned_directories.handles[copied]
                assert inode(os.fstat(held)) == tuple(expected[:2])
                assert mode(copied) == mode(copied / "inner") == 0o111
                target, before = decoy(tmp_path, owned.records)
                displaced = copied.with_name("displaced")
                parent_identity, parent_mode = identity(copied.parent), mode(copied.parent)
                assert parent_mode == 0o555
                original_open, original_scan = capture.owned_directories.open, os.scandir
                swapped, scanned = [], []

                def opened(path, parent, leaf):
                    assert Path(path) == copied and not swapped
                    assert identity(copied) == expected and mode(copied) == 0o111
                    # Fault construction changes only this owned container, then
                    # restores its frozen mode before real verification resumes.
                    os.chmod(copied.parent, parent_mode | 0o200, follow_symlinks=False)
                    try:
                        copied.rename(displaced)  # Same parent: no '..' update.
                        moved(owned.records, copied, displaced)
                        if replacement == "directory":
                            copied.mkdir(mode=0o111)
                            copied.chmod(0o111)
                        else:
                            copied.symlink_to(target, target_is_directory=True)
                        remember(owned.records, copied)
                    finally:
                        assert identity(copied.parent) == parent_identity
                        os.chmod(copied.parent, parent_mode, follow_symlinks=False)
                    assert identity(displaced) == expected and mode(displaced) == 0o111
                    assert mode(displaced / "inner") == 0o111
                    assert inode(os.fstat(held)) == tuple(expected[:2])
                    assert inode(copied.lstat()) != tuple(expected[:2])
                    if replacement == "directory":
                        assert mode(copied) == 0o111
                    else:
                        assert copied.is_symlink()
                    assert mode(copied.parent) == parent_mode
                    assert owned.fixture.state() == owned.fixture.before
                    assert mode(owned.fixture.source) == 0o111
                    swapped.append(inode(copied.lstat()))
                    return original_open(path, parent, leaf)

                def scandir(path):
                    current = os.fstat(path) if isinstance(path, int) else Path(path).lstat()
                    assert inode(current) not in {inode(target.lstat()), inode(copied.lstat())}, "replacement directory was enumerated"
                    scanned.append(inode(current))
                    return original_scan(path)

                with patch.object(capture.owned_directories, "open", opened), patch.object(os, "scandir", scandir):
                    with pytest.raises(InfrastructureError, match="^owned frozen-tool directory moved after freezing$") as caught:
                        try:
                            capture.verify(["owned"])
                        except BaseException as error:
                            print("owned replacement cause chain:", errors(error))
                            raise
                assert errors(caught.value) == [["InfrastructureError", None]]
                assert len(swapped) == 1 and not scanned
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
            displaced = copied.with_name("displaced")
            original_open, original_chmod, original_scan = os.open, os.chmod, os.scandir
            anchors, swapped = [], []

            def opened(path, flags, *args, **kwargs):
                descriptor = original_open(path, flags, *args, **kwargs)
                if flags & os.O_PATH and inode(os.fstat(descriptor)) == expected:
                    try:
                        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
                        assert not anchors and mode(copied) == 0o111
                        anchors.append(descriptor)
                    except BaseException:
                        os.close(descriptor)
                        raise
                return descriptor

            def chmod(path, permissions, *args, **kwargs):
                if isinstance(path, str) and path.startswith("/proc/self/fd/"):
                    descriptor = int(path.rsplit("/", 1)[1])
                    if inode(os.fstat(descriptor)) == expected:
                        assert anchors == [descriptor] and not swapped
                        assert permissions == 0o711 and mode(copied) == 0o111
                        assert mode(copied.parent) == 0o711
                        assert mode(owned.fixture.source) == 0o111
                        assert owned.fixture.state() == owned.fixture.before
                        copied.rename(displaced)
                        moved(owned.records, copied, displaced)
                        copied.symlink_to(target, target_is_directory=True)
                        remember(owned.records, copied)
                        assert inode(displaced.lstat()) == expected and mode(displaced) == 0o111
                        assert inode(os.fstat(descriptor)) == expected and copied.is_symlink()
                        assert inode(copied.lstat()) != expected
                        assert_decoy(target, before)
                        swapped.append(descriptor)
                return original_chmod(path, permissions, *args, **kwargs)

            def scandir(path):
                current = os.fstat(path) if isinstance(path, int) else Path(path).lstat()
                assert inode(current) != inode(target.lstat()), "cleanup followed replacement target"
                return original_scan(path)

            with patch.object(os, "open", opened), patch.object(os, "chmod", chmod), patch.object(os, "scandir", scandir):
                with pytest.raises(sandbox.CleanupError, match="^owned frozen-tool cleanup directory moved$") as caught:
                    try:
                        capture.__exit__(None, None, None)
                    except BaseException as error:
                        print("owned cleanup cause chain:", errors(error))
                        raise
            assert errors(caught.value) == [["CleanupError", None]]
            assert anchors == swapped
            assert len(swapped) == 1 and inode(displaced.lstat()) == expected
            assert mode(displaced) == 0o711 and copied.is_symlink()
            assert not (displaced / "value").exists()
            with pytest.raises(OSError) as closed:
                os.fstat(anchors[0])
            assert closed.value.errno == errno.EBADF
            assert mode(owned.fixture.source) == 0o111
            assert owned.fixture.state() == owned.fixture.before
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
