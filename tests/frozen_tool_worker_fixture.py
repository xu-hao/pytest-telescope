"""Explicit runtime pins for a controlled real pytest worker, without installing it.

Only the invoking standard CPython, named stdlib/package trees, actual loader cache,
and bounded ELF dependencies are read. No host program or discovery helper is executed
to construct this declaration. This is separate from ambient compatibility benchmarks.
"""

import json
import os
import stat
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

from frozen_tool_configuration_fixtures import route_configuration
from frozen_tool_elf import SYSTEM_SEARCH, native_relations
from frozen_tool_fixtures import ToolProject, file_digest, manifest, put

PACKAGES = {
    "pytest": ("pytest", "_pytest"), "pluggy": ("pluggy",),
    "packaging": ("packaging",), "iniconfig": ("iniconfig",), "pygments": ("pygments",),
}
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000


class WorkerConfiguration:
    def __init__(self, directory, monkeypatch):
        self.project = ToolProject(directory / "project", directory / "installed")
        self.config = directory / "owned-etc"
        self.config.mkdir(mode=0o755)
        self.config.chmod(0o755)
        self.config_names = {"selected.txt", "ld.so.cache"}
        put(self.config, "selected.txt", "worker configuration\n", 0o640)
        put(self.config, "unselected.txt", "unselected owned sibling\n")
        # One explicitly named loader input, never an inventory of ambient /etc.
        with Path("/etc/ld.so.cache").open("rb") as stream:
            cache = stream.read(16 * 1024 * 1024 + 1)
        if len(cache) > 16 * 1024 * 1024:
            raise ValueError("fixture loader cache exceeds its exact byte bound")
        put(self.config, "ld.so.cache", cache, 0o644)
        route_configuration(monkeypatch, self.config)
        self.anchors = {"system": Path("/usr")}
        for name, path in (("base", Path(sys.base_prefix)), ("environment", Path(sys.prefix))):
            if not any(path.is_relative_to(root) for root in self.anchors.values()):
                self.anchors[name] = path
        self.paths = set()
        self.native = set()
        self.read_bytes = 0
        self.pin(Path(sys.executable))
        self.pin(Path(sys._base_executable))
        if sys.prefix != sys.base_prefix:
            self.pin(Path(sys.prefix) / "pyvenv.cfg")
        stdlib = Path(sysconfig.get_path("stdlib"))
        if stdlib != Path(sys.base_prefix) / "lib" / f"python3.{sys.version_info.minor}":
            raise ValueError("controlled worker fixture requires standard CPython stdlib layout")
        self.named_tree(stdlib, stdlib=True)
        for name, packages in PACKAGES.items():
            distribution = metadata.distribution(name)
            for package in packages:
                self.named_tree(Path(distribution.locate_file(package)))
            metadata_files = [row for row in distribution.files or () if row.name == "METADATA" and row.parent.name.endswith(".dist-info")]
            if len(metadata_files) != 1:
                raise ValueError("controlled worker package metadata is not uniquely installed")
            self.named_tree(Path(distribution.locate_file(metadata_files[0].parent)))
        information, dependencies, paths, search = native_relations(
            sorted({Path(sys.executable).resolve(), *self.native}),
        )
        for path in paths:
            self.pin(path)
        actual = Path(sys.executable).resolve()
        pending, seen = list(dependencies[actual]), set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(dependencies[current])
        interpreter = information[actual]["interpreter"]
        self.document = manifest()
        self.document["executables"] = [{
            "file": self.reference(actual), "format": "elf",
            "interpreter": None if interpreter is None else self.reference(Path(interpreter).resolve()),
            "libraries": [self.reference(path) for path in sorted(seen)],
            "resources": [], "commands": [],
        }]
        directories = {str(path.resolve(strict=False)) for path in SYSTEM_SEARCH} | {str(path) for path in search}
        self.document["loader"] = {
            "profile": "linux-x86_64-glibc-v1",
            "directories": sorted(path for path in directories if not any(path != other and Path(path).is_relative_to(other) for other in directories)),
            "cache": {"root": "config", "path": "ld.so.cache"},
        }
        self.save()

    def anchor(self, path):
        candidates = [(len(root.parts), name) for name, root in self.anchors.items() if path.is_relative_to(root)]
        if not candidates:
            raise ValueError("runtime dependency lies outside explicitly named fixture anchors")
        return max(candidates)[1]

    def pin(self, filename, hops=0):
        if hops > 32:
            raise ValueError("fixture link depth exceeded")
        path = Path(os.path.normpath(filename))
        if path.parts[1] in {"bin", "lib", "lib32", "lib64", "libx32", "sbin"}:
            path = Path("/").joinpath(path.parts[1]).resolve().joinpath(*path.parts[2:])
        anchor = self.anchors[self.anchor(path)]
        self.paths.add(anchor)
        current = anchor
        for part in path.relative_to(anchor).parts:
            current /= part
            self.paths.add(current)
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                text = Path(os.readlink(current))
                target = text if text.is_absolute() else current.parent / text
                self.pin(target / path.relative_to(current), hops + 1)
                return
        if len(self.paths) > MAX_FILES:
            raise ValueError("controlled runtime fixture exceeds10000 entries")

    def named_tree(self, root, *, stdlib=False):
        if not root.is_dir():
            raise ValueError("required named runtime package tree is missing")
        pending = [root]
        while pending:
            path = pending.pop()
            if path.name == "__pycache__" or (stdlib and path.name in {"site-packages", "dist-packages", "sitecustomize.py", "usercustomize.py"}):
                continue
            self.pin(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif stat.S_ISREG(info.st_mode):
                self.read_bytes += info.st_size
                if info.st_size > 64 * 1024 * 1024 or self.read_bytes > MAX_BYTES:
                    raise ValueError("controlled runtime named trees exceed256MiB")
                with path.open("rb") as stream:
                    if stream.read(4) == b"\x7fELF":
                        self.native.add(path.resolve())
            elif not stat.S_ISLNK(info.st_mode):
                raise ValueError("controlled runtime includes unsupported special input")
            if len(pending) + len(self.paths) > MAX_FILES:
                raise ValueError("controlled runtime traversal exceeds10000 entries")

    def reference(self, path):
        path = Path(path).resolve()
        name = self.anchor(path)
        return {"root": name, "path": path.relative_to(self.anchors[name]).as_posix()}

    @staticmethod
    def row(path, relative):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            return {"path": relative, "kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            return {"path": relative, "kind": "symlink", "target": os.readlink(path)}
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("fixture input is not a regular file, directory or link")
        return {"path": relative, "kind": "file", "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size, "sha256": file_digest(path)}

    def save(self):
        roots = []
        total = 0
        for name, root in self.anchors.items():
            rows = [self.row(path, path.relative_to(root).as_posix()) for path in sorted(self.paths) if self.anchor(path) == name]
            roots.append({"id": name, "source": str(root), "coverage": "files", "preserve_source": True, "entries": rows})
            total += sum(row.get("size", 0) for row in rows)
        selected = {Path(".")}
        for name in self.config_names:
            selected.add(Path(name))
            selected.update(Path(name).parents)
        rows = [self.row(self.config / path, path.as_posix()) for path in sorted(selected)]
        roots.append({"id": "config", "source": "/etc", "coverage": "files", "preserve_source": True, "entries": rows})
        total += sum(row.get("size", 0) for row in rows)
        if total > MAX_BYTES:
            raise ValueError("complete controlled runtime exceeds256MiB")
        self.document["roots"] = roots
        self.project.documents["worker"] = self.document
        self.project.definitions["worker"] = {"format": "frozen-tool-v1", "manifest": "tool-locks/worker.json"}
        self.project.save("worker")
        self.project.configure()
        # Bounded, private fixture provenance inside the claimed scratch directory.
        receipt = {"executable": sys.executable, "base_prefix": sys.base_prefix,
                   "bytes": total, "entries": sum(len(root["entries"]) for root in roots),
                   "manifest_sha256": file_digest(self.project.root / "tool-locks/worker.json")}
        put(self.project.external, "runtime-provenance.json", json.dumps(receipt, sort_keys=True))
