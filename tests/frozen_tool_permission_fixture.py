"""Finite owned permission fixtures with cleanup independent of capture cleanup."""

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frozen_tool_fixtures import ToolProject, manifest, put

from pytest_telescope import sandbox, tool_capture, tool_context


def identity(path):
    value = path.lstat()
    return [value.st_dev, value.st_ino, value.st_uid, stat.S_IFMT(value.st_mode)]


def errors(error):
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 8:
        seen.add(id(error))
        result.append([type(error).__name__, getattr(error, "errno", None)])
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return result


def restore_owned(records):
    """No discovery: restore/remove only recorded, still-identical owned members."""
    existing = []
    for name, expected in sorted(records.items(), key=lambda row: len(Path(row[0]).parts)):
        path = Path(name)
        try:
            current = identity(path)
        except FileNotFoundError:
            continue
        assert current == expected and current[2] == os.getuid()
        assert current[3] in (stat.S_IFDIR, stat.S_IFREG)
        if current[3] == stat.S_IFDIR:
            os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | 0o700, follow_symlinks=False)
            assert identity(path) == expected
        existing.append((path, current[3]))
    for path, kind in reversed(existing):
        if kind == stat.S_IFDIR:
            path.rmdir()
        else:
            path.unlink()


@contextmanager
def record_material(directory, records):
    original_mkdir, original_measure = os.mkdir, tool_capture._measure

    def remember(path):
        path = Path(path)
        if path.is_relative_to(directory):
            assert len(records) < 32 or str(path) in records
            records[str(path)] = identity(path)

    def mkdir(path, mode=0o777, *, dir_fd=None):
        result = original_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is None:
            remember(path)
        return result

    def measure(filename, expected, budget, destination=None, *, frozen=False):
        result = original_measure(filename, expected, budget, destination, frozen=frozen)
        if destination is not None:
            remember(destination)
        return result

    with patch.object(os, "mkdir", mkdir), patch.object(tool_capture, "_measure", measure):
        yield


class PermissionFixture:
    def __init__(self, directory, interior, mode):
        self.project = ToolProject(directory / "project", directory / "external")
        self.source = self.project.external / "owned"
        self.source.mkdir(mode=0o755)
        self.source.chmod(0o755)
        self.protected = self.source / "inner" if interior else self.source
        if interior:
            self.protected.mkdir(mode=0o755)
        self.file = put(self.protected, "value", b"owned readable bytes\n")
        filename = "inner/value" if interior else "value"
        rows = [{"path": ".", "kind": "directory", "mode": 0o755 if interior else mode}]
        if interior:
            rows.append({"path": "inner", "kind": "directory", "mode": mode})
        rows.append(
            {
                "path": filename,
                "kind": "file",
                "mode": 0o644,
                "size": self.file.stat().st_size,
                "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest(),
            }
        )
        self.project.documents["owned"] = manifest(
            [
                {
                    "id": "install",
                    "source": str(self.source),
                    "coverage": "files",
                    "preserve_source": False,
                    "entries": rows,
                }
            ]
        )
        self.project.definitions["owned"] = {
            "format": "frozen-tool-v1",
            "manifest": "tool-locks/owned.json",
        }
        self.project.save("owned")
        self.project.configure()
        self.protected.chmod(mode)
        self.mode = mode
        self.before = self.state()

    def state(self):
        return (
            identity(self.protected),
            stat.S_IMODE(self.protected.lstat().st_mode),
            identity(self.file),
            stat.S_IMODE(self.file.lstat().st_mode),
            self.file.read_bytes(),
        )

    def finish(self):
        assert identity(self.protected) == self.before[0]
        assert self.protected.lstat().st_uid == os.getuid()
        os.chmod(self.protected, 0o755, follow_symlinks=False)

    def capture(self, stage):
        return make_capture(self.project.root, stage)


def make_capture(project, stage):
    definitions = {"owned": {"format": "frozen-tool-v1", "manifest": "tool-locks/owned.json"}}
    tools = tool_context.load_selected(project, definitions, ["owned"])
    return tool_capture.Capture(SimpleNamespace(root=project, tools=tools), stage, 5, None)


def abandon(project, ready):
    """Child exits naturally after a bounded receipt of actual capture/failure."""
    manager = sandbox.invocation(project)
    directory = manager.__enter__()
    records = {
        str(directory): identity(directory),
        str(directory / "owner.lock"): identity(directory / "owner.lock"),
    }
    with ready.with_suffix(".owner.json").open("x") as stream:
        json.dump({"directory": str(directory), "identity": identity(directory)}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    capture = make_capture(project, directory)
    failure = []
    with record_material(directory, records):
        try:
            capture.__enter__()
        except BaseException as error:
            failure = errors(error)
        value = {
            "pid": os.getpid(),
            "directory": str(directory),
            "records": records,
            "failure": failure,
            "copy_exists": os.path.lexists(capture.directory / "owned/install/value"),
        }
        encoded = json.dumps(value).encode()
        assert len(encoded) <= 4096
        with ready.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(0)
