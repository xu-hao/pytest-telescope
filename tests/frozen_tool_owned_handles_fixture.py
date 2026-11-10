"""Finite owned-directory fixtures and faithful descriptor observations."""

import errno
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frozen_tool_permission_fixture import PermissionFixture, identity, record_material


def mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def inode(info):
    return info.st_dev, info.st_ino


def remember(records, path):
    assert len(records) < 64 or str(path) in records
    records[str(path)] = identity(path)


def moved(records, source, destination):
    """Update only the finite already-registered members; never discover a tree."""
    for name in list(records):
        path = Path(name)
        if path.is_relative_to(source):
            records[str(destination / path.relative_to(source))] = records.pop(name)


def remove_registered(records):
    existing = []
    for name, expected in sorted(records.items(), key=lambda item: len(Path(item[0]).parts)):
        path = Path(name)
        try:
            actual = identity(path)
        except FileNotFoundError:
            continue
        assert actual == expected and actual[2] == os.getuid()
        assert actual[3] in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK)
        if actual[3] == stat.S_IFDIR:
            os.chmod(path, mode(path) | 0o700, follow_symlinks=False)
            assert identity(path) == expected
        existing.append((path, actual[3]))
    for path, kind in reversed(existing):
        path.rmdir() if kind == stat.S_IFDIR else path.unlink()


@contextmanager
def owned_capture(tmp_path, *, projection=False):
    fixture = PermissionFixture(tmp_path, True, 0o111)
    root_identity = identity(fixture.source)
    records = {}
    capture = None
    configured = False
    try:
        root = fixture.project.documents["owned"]["roots"][0]
        root["entries"][0]["mode"] = 0o111
        if projection:
            fixture.protected.chmod(0o755)
            (fixture.protected / "alias").symlink_to("value")
            fixture.protected.chmod(0o111)
            root["entries"].append({"path": "inner/alias", "kind": "symlink", "target": "value"})
            root["preserve_source"] = True
        fixture.project.save("owned")
        fixture.source.chmod(0o111)
        configured = True
        assert os.geteuid() != 0, "permission controls require an actual non-root owner"
        try:
            descriptor = os.open(fixture.source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except PermissionError as error:
            assert error.errno == errno.EACCES
        else:
            os.close(descriptor)
            raise AssertionError("owned source directory unexpectedly readable")
        stage = tmp_path / "capture"
        stage.mkdir()
        capture = fixture.capture(stage)
        original_link, original_symlink = os.link, os.symlink

        def link(source, destination, *args, **kwargs):
            value = original_link(source, destination, *args, **kwargs)
            if Path(destination).is_relative_to(capture.directory):
                remember(records, destination)
            return value

        def symlink(source, destination, *args, **kwargs):
            value = original_symlink(source, destination, *args, **kwargs)
            if Path(destination).is_relative_to(capture.directory):
                remember(records, destination)
            return value

        with record_material(capture.directory, records), patch.object(os, "link", link), patch.object(os, "symlink", symlink):
            yield SimpleNamespace(fixture=fixture, capture=capture, stage=stage, records=records)
    finally:
        try:
            if capture is not None:
                capture.owned_directories.close()
            remove_registered(records)
        finally:
            try:
                assert identity(fixture.source) == root_identity
                if configured:
                    assert fixture.state() == fixture.before and mode(fixture.source) == 0o111
            finally:
                assert identity(fixture.source) == root_identity
                fixture.source.chmod(0o755)
                fixture.finish()


class DirectoryReads:
    """Observe actual successful owned read opens/dups and their real closes only."""

    def __init__(self, directory):
        self.directory = directory
        self.active = {}
        self.history = []
        self.inodes = set()
        self.lock = threading.RLock()
        self.real_open, self.real_dup, self.real_close = os.open, os.dup, os.close

    def opened(self, descriptor):
        info = os.fstat(descriptor)
        record = {"fd": descriptor, "inode": inode(info), "closed": False}
        self.inodes.add(record["inode"])
        self.active[descriptor] = record
        self.history.append(record)
        assert len(self.history) <= 256

    def open(self, path, flags, *args, **kwargs):
        descriptor = self.real_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY and not flags & os.O_PATH:
            with self.lock:
                explicit = isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)).is_relative_to(self.directory)
                if explicit or inode(os.fstat(descriptor)) in self.inodes:
                    self.opened(descriptor)
        return descriptor

    def dup(self, descriptor):
        with self.lock:
            result = self.real_dup(descriptor)
            if descriptor in self.active:
                self.opened(result)
            return result

    def close(self, descriptor):
        with self.lock:
            result = self.real_close(descriptor)
            record = self.active.pop(descriptor, None)
            if record is not None:
                record["closed"] = True
            return result

    @contextmanager
    def observe(self):
        with patch.object(os, "open", self.open), patch.object(os, "dup", self.dup), patch.object(os, "close", self.close):
            yield self

    def assert_closed(self):
        assert self.history and not self.active
        assert all(record["closed"] for record in self.history)
        for descriptor in {record["fd"] for record in self.history}:
            try:
                current = os.fstat(descriptor)
            except OSError as error:
                assert error.errno == errno.EBADF
            else:
                assert inode(current) not in self.inodes, "an observed owned read handle survived cleanup"
