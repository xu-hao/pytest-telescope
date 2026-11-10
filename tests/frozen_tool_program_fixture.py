"""Finite declarations for the owned Node and full Chromium programs.

The caller supplies reviewed, existing executables. This fixture selects their bytes
and named resources; it neither installs programs nor runs a discovery executable.
The independent worker fixture supplies the actual invoking Python/pytest closure.
"""

import base64
import copy
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from frozen_tool_elf import SYSTEM_SEARCH, native_relations
from frozen_tool_fixtures import entries, file_digest, manifest, put
from frozen_tool_worker_fixture import WorkerConfiguration

PAYLOADS = Path(__file__).with_name("frozen_tool_payloads")
PROGRAM_BYTES = 768 * 1024 * 1024
PROGRAM_FILE_BYTES = 512 * 1024 * 1024
PROGRAM_ENTRIES = 10000
FONT_BYTES = 64 * 1024 * 1024


def prerequisite_identity(documents):
    """Bind all declared bytes, normalizing only the owned driver's scratch path."""
    value = copy.deepcopy(documents)
    for document in value.values():
        for root in document["roots"]:
            if root["id"] == "driver":
                root["source"] = "<owned-driver>"
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_prerequisites(name, identity):
    filename = os.environ.get("TELESCOPE_FIXTURE_ADMISSION")
    expected = os.environ.get("TELESCOPE_FIXTURE_ADMISSION_SHA256")
    if not filename or not expected:
        raise ValueError("an exact reviewed prerequisite receipt is required")
    with Path(filename).open("rb") as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("prerequisite receipt changed or exceeded its bound")
    receipt = json.loads(content)
    if receipt["schema"] != 1 or receipt["profiles"][name]["identity"] != identity:
        raise ValueError("selected runtime bytes differ from the reviewed preparation")


@contextmanager
def capture_evidence(monkeypatch, directory):
    """Faithful bounded observations of actual worker capture, never another launch."""
    from pytest_telescope import sandbox

    destination = directory / "native-capture.jsonl"
    destination.open("xb").close()
    original = sandbox.capture
    state = {"calls": 0, "bytes": 0, "overflow": False, "write_failed": False}

    def record(argv, result=None, error=None):
        state["calls"] += 1
        if state["overflow"]:
            return
        encoded_argv = json.dumps(argv).encode()
        row = {"call": state["calls"]}
        oversized = state["calls"] > 16 or len(encoded_argv) > 65536
        if len(encoded_argv) <= 65536:
            row["argv"] = argv
        if error is not None:
            row["exception_type"] = type(error).__module__ + "." + type(error).__qualname__
        else:
            code, output = result
            row.update(
                returncode=code,
                output_bytes=len(output),
                output_sha256=hashlib.sha256(output).hexdigest(),
            )
            if len(output) <= 1024 * 1024:
                row["output_base64"] = base64.b64encode(output).decode("ascii")
            else:
                oversized = True
        encoded = json.dumps(row, ensure_ascii=True).encode() + b"\n"
        if oversized or state["bytes"] + len(encoded) > 2 * 1024 * 1024 - 1024:
            state["overflow"] = True
            encoded = b'{"diagnostic_bound_exceeded":true}\n'
        with destination.open("ab") as stream:
            stream.write(encoded)
        state["bytes"] += len(encoded)

    def observed(argv, *args, **kwargs):
        matched = bool(argv) and Path(argv[0]).name == "bwrap" and argv[-1] == sandbox.WORKER_PATH
        try:
            result = original(argv, *args, **kwargs)
        except BaseException as error:
            if matched:
                try:
                    record(argv, error=error)
                except Exception:
                    state["write_failed"] = True
            raise
        if matched:
            try:
                record(argv, result=result)
            except Exception:
                state["write_failed"] = True
        return result

    monkeypatch.setattr(sandbox, "capture", observed)
    try:
        yield
    finally:
        try:
            with destination.open("ab") as stream:
                stream.write(json.dumps({"capture_summary": state}).encode() + b"\n")
        except Exception:
            # The row wrapper refuses missing/incomplete evidence. Do not replace
            # the test's original outcome with a diagnostic write exception.
            pass


def bounded_rows(source, maximum, *, files=PROGRAM_ENTRIES):
    """Check a named tree before hashing or copying; never follow directory links."""
    pending, count, total = [source], 0, 0
    while pending:
        path = pending.pop()
        info = path.lstat()
        count += 1
        if not stat.S_ISLNK(info.st_mode) and stat.S_IMODE(info.st_mode) > 0o777:
            raise ValueError("named program input has unsupported special permission bits")
        if count + len(pending) > files:
            raise ValueError("named program tree exceeds its entry bound")
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                for child in children:
                    pending.append(Path(child.path))
                    if count + len(pending) > files:
                        raise ValueError("named program traversal exceeds its entry bound")
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if info.st_size > PROGRAM_FILE_BYTES or total > maximum:
                raise ValueError("named program tree exceeds its byte bound")
        elif not stat.S_ISLNK(info.st_mode):
            raise ValueError("named program tree contains a special file")
    return entries(source)


def copy_declared_tree(source, destination, maximum):
    """Copy only a bounded named tree, preserving actual modes and link text."""
    rows = bounded_rows(source, maximum)
    for row in sorted(rows, key=lambda row: (len(Path(row["path"]).parts), row["path"])):
        target = destination / row["path"]
        original = source / row["path"]
        if row["kind"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(row["mode"])
        elif row["kind"] == "symlink":
            target.symlink_to(row["target"])
        else:
            with original.open("rb") as incoming, target.open("xb") as outgoing:
                copied = 0
                while chunk := incoming.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > row["size"]:
                        raise ValueError("named source grew while copied")
                    outgoing.write(chunk)
            target.chmod(row["mode"])
            if copied != row["size"] or file_digest(target) != row["sha256"]:
                raise ValueError("named source changed while copied")
    if bounded_rows(destination, maximum) != rows:
        raise ValueError("owned named-tree copy differs from installed bytes")
    return rows


class ProgramConfiguration:
    def __init__(self, worker, *, browser=False, require_receipt=True):
        self.worker = worker
        self.project = worker.project
        # Each dynamic tool is independently closed, even when its consumer also
        # selects another tool. Reuse the exact known worker inputs, not live binds.
        self.roots = {root["id"]: copy.deepcopy(root) for root in worker.document["roots"]}
        self.selected = {
            name: {path for path in worker.paths if worker.anchor(path) == name}
            for name in worker.anchors
        }
        self.native = set(worker.native)
        self.executables = {}
        self.document = manifest()
        self.executable(Path(sys.executable))
        self.node = self.required_program("TELESCOPE_FIXTURE_NODE")
        if not self.node.is_relative_to("/usr"):
            if self.node.parent.name != "bin":
                raise ValueError(
                    "controlled Node fixture requires its explicit bin/lib prefix layout"
                )
            self.files_root("node", self.node.parent.parent)
        self.executable(self.node)
        self.env = Path("/usr/bin/env").resolve(strict=True)
        self.executable(self.env, "env")
        scripts = self.project.external / "program-driver"
        scripts.mkdir(mode=0o755)
        scripts.chmod(0o755)
        for name in ("command.js", "browser.js", "build.js", "child.py"):
            put(
                scripts,
                name,
                (PAYLOADS / name).read_bytes(),
                0o755 if name.endswith(".js") else 0o644,
            )
        put(scripts, "input.txt", "frozen frontend input\n")
        self.scripts = scripts
        self.tree_root("driver", scripts)
        self.chrome = None
        if browser:
            self.chrome = self.required_program("TELESCOPE_FIXTURE_CHROMIUM")
            if self.chrome.name != "chrome" or "headless_shell" in str(self.chrome):
                raise ValueError("full Chromium fixture requires the chrome executable")
            self.tree_root("browser", self.chrome.parent)
            self.executable(self.chrome)
            self.executable(self.chrome.parent / "chrome_crashpad_handler")
            # Exactly one named configuration subtree, never an ambient /etc walk.
            copy_declared_tree(Path("/etc/fonts"), worker.config / "fonts", FONT_BYTES)
            self.tree_root("fontconfig", Path("/etc/fonts"), owned=worker.config / "fonts")
            for name, path in (
                ("fontdefaults", "/usr/share/fontconfig/conf.avail"),
                ("fonts", "/usr/share/fonts"),
            ):
                self.tree_root(name, Path(path), maximum=FONT_BYTES)
            # The measured local-font directory is empty and has setgid mode.
            # It supplies no page resource: leave it unselected/absent in the
            # closed /usr view rather than changing or masking its source mode.
        self.finish()
        self.identity = prerequisite_identity(
            {"worker": worker.document, "frontend": self.document}
        )
        if require_receipt:
            require_prerequisites("browser" if browser else "node", self.identity)

    @staticmethod
    def required_program(variable):
        value = os.environ.get(variable)
        if not value or not Path(value).is_absolute():
            raise ValueError("reviewed program prerequisite is missing: " + variable)
        actual = Path(value).resolve(strict=True)
        info = actual.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ValueError("reviewed program must resolve to an executable regular file")
        if info.st_size > PROGRAM_FILE_BYTES:
            raise ValueError("reviewed executable exceeds512MiB")
        return actual

    def files_root(self, name, path):
        self.roots[name] = {
            "id": name,
            "source": str(path),
            "coverage": "files",
            "preserve_source": True,
            "entries": [],
        }
        self.selected[name] = {path}

    def tree_root(self, name, path, *, owned=None, maximum=PROGRAM_BYTES):
        rows = bounded_rows(path if owned is None else owned, maximum)
        self.roots[name] = {
            "id": name,
            "source": str(path),
            "coverage": "tree",
            "preserve_source": True,
            "entries": rows,
        }
        for row in rows:
            if row["kind"] == "file":
                with ((path if owned is None else owned) / row["path"]).open("rb") as stream:
                    if stream.read(4) == b"\x7fELF":
                        self.native.add((path / row["path"]).resolve(strict=True))

    def anchor(self, path):
        candidates = [
            (len(Path(root["source"]).parts), name)
            for name, root in self.roots.items()
            if path.is_relative_to(root["source"])
        ]
        if not candidates:
            raise ValueError("program dependency is outside explicitly declared anchors")
        return max(candidates)[1]

    def pin(self, path, hops=0):
        if hops > 32:
            raise ValueError("program link depth exceeded")
        path = Path(os.path.normpath(path))
        if path.parts[1] in {"bin", "sbin", "lib", "lib32", "lib64", "libx32"}:
            path = Path("/").joinpath(path.parts[1]).resolve(strict=True).joinpath(*path.parts[2:])
        name = self.anchor(path)
        root = Path(self.roots[name]["source"])
        current = root
        for component in path.relative_to(root).parts:
            current /= component
            if name in self.selected:
                self.selected[name].add(current)
            if current.is_symlink():
                target = Path(os.readlink(current))
                if not target.is_absolute():
                    target = current.parent / target
                self.pin(target / path.relative_to(current), hops + 1)
                return
        if not path.exists():
            raise ValueError("explicit program dependency is absent")
        if sum(len(paths) for paths in self.selected.values()) > PROGRAM_ENTRIES:
            raise ValueError("program sparse entry bound exceeded")

    def reference(self, path):
        path = Path(path).resolve(strict=True)
        name = self.anchor(path)
        return {"root": name, "path": path.relative_to(self.roots[name]["source"]).as_posix()}

    def logical(self, path):
        row = self.reference(path)
        return str(Path("/tools/frontend") / row["root"] / row["path"])

    def executable(self, path, kind="elf"):
        path = Path(path).resolve(strict=True)
        self.pin(path)
        self.executables[path] = kind

    def finish(self):
        # Preserve both actual runtimes: Node's own encoded library slots and the
        # invoking worker's separate system closure. Producer admission independently
        # proves ancestry and name reuse; no global ABI override certifies these bytes.
        information, dependencies, paths, search = native_relations(
            sorted(set(self.executables) | self.native),
            sorted({path.parent for path in self.native}),
            ordered_own=True,
        )
        for path in sorted(paths):
            self.pin(path)
        for root in list(self.roots.values()):
            if root["coverage"] == "tree":
                for row in root["entries"]:
                    if row["kind"] == "symlink":
                        self.pin(Path(root["source"]) / row["path"])
        for name, selected in self.selected.items():
            root = Path(self.roots[name]["source"])
            sizes = [
                path.lstat().st_size for path in selected if stat.S_ISREG(path.lstat().st_mode)
            ]
            if any(size > PROGRAM_FILE_BYTES for size in sizes) or sum(sizes) > PROGRAM_BYTES:
                raise ValueError("sparse program inputs exceed the reviewed byte bounds")
            self.roots[name]["entries"] = [
                WorkerConfiguration.row(path, path.relative_to(root).as_posix())
                for path in sorted(selected)
            ]
        count = sum(len(root["entries"]) for root in self.roots.values())
        total = sum(row.get("size", 0) for root in self.roots.values() for row in root["entries"])
        if count > PROGRAM_ENTRIES or total > PROGRAM_BYTES:
            raise ValueError("complete program declaration exceeds768MiB or10000 entries")
        self.document["roots"] = list(self.roots.values())
        records = []
        for path, kind in sorted(self.executables.items()):
            pending, seen = list(dependencies[path]), set()
            while pending:
                current = pending.pop()
                if current not in seen:
                    seen.add(current)
                    pending.extend(dependencies[current])
            interpreter = information[path]["interpreter"]
            records.append(
                {
                    "file": self.reference(path),
                    "format": kind,
                    "interpreter": None if interpreter is None else self.reference(interpreter),
                    "libraries": [self.reference(value) for value in sorted(seen)],
                    "resources": [self.reference(value) for value in sorted(self.worker.native)]
                    if path == Path(sys.executable).resolve()
                    else [],
                    "commands": [],
                }
            )
        for name in ("command.js", "browser.js", "build.js"):
            records.append(
                {
                    "file": self.reference(self.scripts / name),
                    "format": "script",
                    "interpreter": self.reference(self.env),
                    "libraries": [],
                    "resources": [{"root": "driver", "path": "."}],
                    "commands": [self.reference(self.node)],
                }
            )
        self.document["executables"] = records
        self.document["commands"] = [{"name": "node", "file": self.reference(self.node)}]
        directories = {path.resolve(strict=False) for path in SYSTEM_SEARCH} | search
        self.document["loader"] = {
            "profile": "linux-x86_64-glibc-v1",
            "directories": sorted(
                str(path)
                for path in directories
                if not any(path != other and path.is_relative_to(other) for other in directories)
            ),
            "cache": {"root": "config", "path": "ld.so.cache"},
        }
        self.save()
        put(
            self.project.external,
            "program-provenance.json",
            json.dumps(
                {
                    "bytes": total,
                    "entries": count,
                    "node": str(self.node),
                    "node_sha256": file_digest(self.node),
                    "chrome": None if self.chrome is None else str(self.chrome),
                    "chrome_sha256": None if self.chrome is None else file_digest(self.chrome),
                    "manifest_sha256": file_digest(self.project.root / "tool-locks/frontend.json"),
                },
                sort_keys=True,
            ),
        )

    def save(self):
        self.project.documents["frontend"] = self.document
        self.project.definitions["frontend"] = {
            "format": "frozen-tool-v1",
            "manifest": "tool-locks/frontend.json",
        }
        self.project.save("frontend")
        self.project.configure()
