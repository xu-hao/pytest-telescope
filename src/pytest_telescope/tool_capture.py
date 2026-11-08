"""Bounded owned copies of explicit tool bytes, never read-only live-source binds."""

import hashlib
import os
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import tool_context as declaration
from .model import DeclarationError, InfrastructureError

CHUNK = 1024 * 1024


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _parent(filename):
    """Open every absolute ancestor without following links, including above roots."""
    filename = Path(filename)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in filename.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, filename.name
    finally:
        os.close(descriptor)


@dataclass
class Budget:
    deadline: float
    cancellation: object
    visits: int = 0
    size: int = 0

    def tick(self, *, size=0, visit=False):
        if time.monotonic() >= self.deadline:
            raise InfrastructureError("frozen-tool capture deadline exceeded")
        if self.cancellation is not None and self.cancellation.is_set():
            from .sandbox import InvocationInterrupted

            raise InvocationInterrupted("frozen-tool capture cancelled")
        self.size += size
        self.visits += int(visit)
        if self.size > declaration.MAX_TOTAL_BYTES or self.visits > declaration.MAX_VISITS:
            declaration.unsupported("bounds", "frozen-tool capture budget exceeded")


def _measure(filename, expected, budget, destination=None, *, frozen=False):
    budget.tick(visit=True)
    try:
        with _parent(filename) as (directory, leaf):
            before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            kind = expected["kind"]
            if kind == "symlink":
                if not stat.S_ISLNK(before.st_mode):
                    raise DeclarationError("frozen-tool input changed type")
                target = os.readlink(leaf, dir_fd=directory)
                valid = target == expected["target"]
                if destination is not None:
                    destination.symlink_to(target)
            else:
                is_file = kind == "file"
                if not (stat.S_ISREG(before.st_mode) if is_file else stat.S_ISDIR(before.st_mode)):
                    raise DeclarationError("frozen-tool input has an unsupported type")
                expected_mode = expected["mode"] & 0o555 if frozen else expected["mode"]
                valid = stat.S_IMODE(before.st_mode) == expected_mode
                if is_file:
                    if before.st_size > declaration.MAX_FILE_BYTES:
                        declaration.unsupported("bounds", "frozen-tool input exceeds its file limit")
                    descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(before):
                            raise InfrastructureError("frozen-tool input changed before capture")
                        digest = hashlib.sha256()
                        size = 0
                        sink = destination.open("xb") if destination is not None else None
                        try:
                            while chunk := source.read(CHUNK):
                                budget.tick(size=len(chunk))
                                size += len(chunk)
                                if size > declaration.MAX_FILE_BYTES:
                                    declaration.unsupported("bounds", "frozen-tool input exceeds its file limit")
                                digest.update(chunk)
                                if sink is not None:
                                    sink.write(chunk)
                        finally:
                            if sink is not None:
                                sink.close()
                        if _signature(os.fstat(source.fileno())) != _signature(opened):
                            raise InfrastructureError("frozen-tool input changed during capture")
                    valid = valid and size == expected["size"] and digest.hexdigest() == expected["sha256"]
                    if destination is not None:
                        destination.chmod(expected["mode"] & 0o555)
                elif destination is not None:
                    destination.mkdir(mode=0o700)
            after = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            if _signature(after) != _signature(before):
                raise InfrastructureError("frozen-tool input changed during capture")
            if kind == "symlink" and os.readlink(leaf, dir_fd=directory) != target:
                raise InfrastructureError("frozen-tool link changed during capture")
            if not valid:
                if frozen:
                    raise InfrastructureError("owned frozen-tool material failed integrity verification")
                declaration.unsupported("identity", "frozen-tool input does not match its declared pin")
            return _signature(before)
    except FileNotFoundError as exc:
        if frozen:
            raise InfrastructureError("owned frozen-tool material is incomplete") from exc
        raise DeclarationError("a declared frozen-tool input is missing") from exc
    except OSError as exc:
        raise InfrastructureError("cannot capture or verify a frozen-tool input") from exc


def _tree_names(source, budget):
    """Complete tree enumeration via owned descriptors; never follow directory links."""
    found = {"."}
    with _parent(source) as (parent, leaf):
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return found
        descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    pending = [(descriptor, ".")]
    try:
        while pending:
            current, prefix = pending.pop()
            try:
                before = _signature(os.fstat(current))
                with os.scandir(current) as children:
                    for entry in children:
                        budget.tick(visit=True)
                        name = str(Path(prefix) / entry.name)
                        if len(found) >= declaration.MAX_ENTRIES:
                            declaration.unsupported("bounds", "frozen-tool tree exceeds its entry limit")
                        if declaration.PROTECTED_PARTS.intersection(Path(name).parts):
                            declaration.unsupported("layout", "frozen-tool tree contains a protected namespace")
                        found.add(name)
                        item = os.stat(entry.name, dir_fd=current, follow_symlinks=False)
                        if stat.S_ISDIR(item.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                            pending.append((child, name))
                        elif not (stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)):
                            raise DeclarationError("frozen-tool tree contains an unsupported type")
                if _signature(os.fstat(current)) != before:
                    raise InfrastructureError("frozen-tool tree changed during enumeration")
            finally:
                os.close(current)
    finally:
        for descriptor, _ in pending:
            os.close(descriptor)
    return found


def _writable_owned(directory):
    """Only a capture-owned tree is passed here, after all consuming views are gone."""
    from .sandbox import CleanupError

    pending = [directory]
    visited = 0
    while pending:
        path = pending.pop()
        visited += 1
        if visited > declaration.MAX_VISITS:
            raise CleanupError("owned frozen-tool cleanup exceeds its entry limit")
        with _parent(path) as (parent, leaf):
            descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) | 0o700)
            with os.scandir(descriptor) as children:
                for entry in children:
                    visited += 1
                    if visited > declaration.MAX_VISITS:
                        raise CleanupError("owned frozen-tool cleanup exceeds its entry limit")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path / entry.name)
        finally:
            os.close(descriptor)


def _remove(directory):
    from .sandbox import CleanupError

    try:
        _writable_owned(directory)
        shutil.rmtree(directory)
    except OSError as exc:
        raise CleanupError("owned frozen-tool cleanup failed") from exc


@dataclass(frozen=True)
class ToolView:
    directory: Path
    names: tuple
    bindings: tuple
    identity: str
    command_path: str | None = None
    prefixes: tuple = ()
    system: object = None


class Capture:
    def __init__(self, manifest, directory, timeout, cancellation):
        self.manifest = manifest
        self.directory = directory / "tools"
        self.timeout = min(timeout, 120)
        self.cancellation = cancellation
        self.sources = {}
        self.graphs = {}
        self.runtimes = {}
        self.views = {}
        self.view_lock = threading.RLock()
        self.system = None

    def _budget(self, deadline=None):
        return Budget(time.monotonic() + self.timeout if deadline is None else deadline, self.cancellation)

    def __enter__(self):
        from .sandbox import CleanupError

        for attempt in range(2):
            self.sources = {}
            self.graphs = {}
            self.runtimes = {}
            self.views = {}
            self.system = None
            self.directory.mkdir(mode=0o700)
            try:
                deadline = time.monotonic() + self.timeout
                self._capture(deadline)
                self.verify(tuple(self.manifest.tools), deadline=deadline)
                return self
            except BaseException as error:
                self._cleanup()
                if isinstance(error, OSError):
                    error = InfrastructureError("owned frozen-tool capture operation failed")
                if not isinstance(error, InfrastructureError) or isinstance(error, CleanupError) or attempt:
                    raise error

    def __exit__(self, *exc):
        self._cleanup()

    def _cleanup(self):
        try:
            if self.system is not None:
                self.system.close()
        finally:
            _remove(self.directory)

    def _pins(self, tool):
        for lock in tool.document["locks"]:
            try:
                raw, _ = declaration.regular_bytes(self.manifest.root, lock["path"], 64 * 1024 * 1024)
            except FileNotFoundError as exc:
                raise DeclarationError("a declared frozen-tool lock is missing") from exc
            except OSError as exc:
                raise InfrastructureError("cannot read a declared frozen-tool lock") from exc
            trees = sorted([key, declaration.digest(tool.roots[key]["entries"])] for key in lock["roots"])
            if hashlib.sha256(raw).hexdigest() != lock["sha256"] or declaration.digest(trees) != lock["tree_sha256"]:
                declaration.unsupported("identity", "frozen-tool lock correspondence does not match")
        for link in declaration.project_links(self.manifest.tools, [tool.name]).values():
            declaration.validate_project_link(self.manifest.root, link)

    def _capture(self, deadline):
        from .tool_graph import Graph
        from .tool_python import validate as validate_python
        from .tool_runtime import Runtime
        from .tool_system import BaselineEtc

        budget = self._budget(deadline)
        (self.directory / ".views").mkdir(mode=0o700)
        for name, tool in self.manifest.tools.items():
            self._pins(tool)
            target = self.directory / name
            target.mkdir(mode=0o700)
            for root in tool.document["roots"]:
                source = Path(root["source"])
                if root["coverage"] == "tree" and any(row["path"] == "pyvenv.cfg" or "site-packages" in Path(row["path"]).parts or "dist-packages" in Path(row["path"]).parts for row in root["entries"]):
                    declaration.unsupported("layout", "an interpreter environment is not a tool root")
                if root["coverage"] == "tree" and _tree_names(source, budget) != {row["path"] for row in root["entries"]}:
                    declaration.unsupported("identity", "frozen-tool tree differs from its complete inventory")
                frozen = target / root["id"]
                for row in root["entries"]:
                    filename = source / row["path"]
                    if filename == Path("/etc/ld.so.preload") and (row["kind"] != "file" or row["size"] != 0):
                        declaration.unsupported("layout", "nonempty or indirect native preload inputs are unsupported")
                    self.sources[name, root["id"], row["path"]] = _measure(filename, row, budget, frozen / row["path"])
                for row in reversed(root["entries"]):
                    if row["kind"] == "directory":
                        (frozen / row["path"]).chmod(row["mode"] & 0o555)
            target.chmod(0o555)
            graph_budget = self._budget(deadline)
            graph = Graph(tool, target, graph_budget)
            graph.validate_links(graph_budget)
            runtime = Runtime(graph, self._budget(deadline))
            validate_python(graph, self._budget(deadline))
            self.graphs[name], self.runtimes[name] = graph, runtime
        if any(runtime.control is not None for runtime in self.runtimes.values()):
            self.system = BaselineEtc(self._budget(deadline))
        self.directory.chmod(0o555)

    def verify(self, names, *, deadline=None):
        names = tuple(names)
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        budget = self._budget(deadline)
        frozen_budget = self._budget(deadline)
        if self.system is not None and any(self.runtimes[name].control is not None for name in names):
            self.system.verify(self._budget(deadline))
        for name in names:
            tool = self.manifest.tools[name]
            try:
                raw, _ = declaration.regular_bytes(self.manifest.root, tool.registration["manifest"], declaration.MAX_MANIFEST_BYTES)
                if raw != tool.raw:
                    raise InfrastructureError("frozen-tool manifest changed during the invocation")
                for root in tool.document["roots"]:
                    if root["coverage"] == "tree" and _tree_names(Path(root["source"]), budget) != {row["path"] for row in root["entries"]}:
                        raise InfrastructureError("frozen-tool source tree changed after capture")
                    if _tree_names(self.directory / name / root["id"], frozen_budget) != {row["path"] for row in root["entries"]}:
                        raise InfrastructureError("owned frozen-tool tree failed integrity verification")
                    for row in root["entries"]:
                        observed = _measure(Path(root["source"]) / row["path"], row, budget)
                        recorded = self.sources[name, root["id"], row["path"]]
                        if root["coverage"] == "files" and row["kind"] == "directory":
                            observed, recorded = observed[:3], recorded[:3]
                        if observed != recorded:
                            raise InfrastructureError("frozen-tool source changed after capture")
                        _measure(self.directory / name / root["id"] / row["path"], row, frozen_budget, frozen=True)
                self._pins(tool)
                self.graphs[name].verify_prefixes()
            except DeclarationError as exc:
                raise InfrastructureError("frozen-tool source or declaration changed after capture") from exc
        with self.view_lock:
            for selected, projection in self.views.items():
                if set(selected) <= set(names):
                    projection.verify()

    def verify_project(self, records):
        """The frozen project lock bytes must match, not merely the current source."""
        for tool in self.manifest.tools.values():
            expected = {tool.registration["manifest"]: hashlib.sha256(tool.raw).hexdigest()}
            expected.update({row["path"]: row["sha256"] for row in tool.document["locks"]})
            for filename, digest in expected.items():
                record = records.get(filename)
                if record is None or record["kind"] != "file" or record["sha256"] != digest:
                    raise InfrastructureError("frozen project records disagree with tool pins")

    def view(self, names):
        from .tool_view import Projection

        names = tuple(sorted(names))
        self.verify(names)
        with self.view_lock:
            if names not in self.views:
                try:
                    self.views[names] = Projection(self, names)
                except OSError as exc:
                    raise InfrastructureError("owned frozen-tool view construction failed") from exc
            projection = self.views[names]
            projection.verify()
            prefixes = {}
            for name in names:
                for prefix, (row, _) in self.graphs[name].prefixes.items():
                    previous = prefixes.get(prefix)
                    if previous is not None and previous != row:
                        declaration.unsupported("layout", "selected native prefix mappings disagree")
                    prefixes[prefix] = row
            return ToolView(
                self.directory, names,
                tuple((source, destination) for destination, source in sorted(projection.bindings.items(), key=lambda item: (len(Path(item[0]).parts), item[0]))),
                projection.identity, projection.command_path,
                tuple(sorted(prefixes.items())), projection.system,
            )
