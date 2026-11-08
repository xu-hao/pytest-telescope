"""Explicit installed-runtime declarations for the required real-child controls.

This is caller fixture code, independent of the producer's parser and capture code.
It reads only named trees and the bounded native dependency graph. It never installs,
executes or repairs an application to make its closure fit.
"""

import os
import stat
import sys
import sysconfig
from pathlib import Path

from frozen_tool_elf import SYSTEM_SEARCH, bounded_tree_size, native_relations
from frozen_tool_fixtures import entries, file_digest, manifest


BASELINE_PREFIXES = {"bin", "sbin", "lib", "lib32", "lib64", "libx32"}


class RuntimeManifest:
    def __init__(self, project, name, *, interpreter=None, stdlib=None):
        self.project = project
        self.name = name
        self.interpreter = Path(interpreter or sys.executable)
        self.stdlib = Path(stdlib or sysconfig.get_path("stdlib")).resolve(strict=True)
        self.document = manifest()
        self.roots = {}
        self.selected = {}
        self.entrypoints = {}
        self.native_resources = set()
        self.tree_bytes = 0
        self.add_files("system", Path("/usr"))
        self.add_files("loader_control", Path("/etc"))
        self.add_files("invoking_environment", self.interpreter.parent.parent.resolve())
        self.add_runtime_files(self.stdlib)
        self.pin(self.interpreter)
        self.pin(self.interpreter.parent.parent / "pyvenv.cfg")
        self.pin(Path("/etc/ld.so.cache"))
        self.entrypoints[self.interpreter.resolve(strict=True)] = "elf"

    def add_files(self, name, source):
        source = Path(source).resolve(strict=True)
        if not source.is_dir() or source == Path("/"):
            raise ValueError("fixture files anchor must be an explicit non-root directory")
        self._root(name, source, "files", [])
        self.selected[name] = set()
        self._pin_within(name, source)
        return source

    def add_tree(self, name, source):
        source = Path(source).resolve(strict=True)
        if not source.is_dir():
            raise ValueError("fixture tree must be an installed directory")
        if name == "stdlib" and any(
            (source / child).exists() for child in ("site-packages", "dist-packages")
        ):
            raise ValueError("fixture requires a narrow stdlib without installed packages")
        self.tree_bytes += bounded_tree_size(source)
        if self.tree_bytes > 768 * 1024 * 1024:
            raise ValueError("fixture preparation exceeds the reviewed 768 MiB tree ceiling")
        rows = entries(source)
        self._root(name, source, "tree", rows)
        for row in rows:
            path = source / row["path"]
            if row["kind"] == "file" and (
                path.suffix == ".so" or ".so." in path.name
            ):
                self.native_resources.add(path)
        return source

    def add_runtime_files(self, source):
        """Enumerate this named stdlib only; preserve its relative library topology.

        Its exact entries share the /usr files anchor with pinned native libraries.
        This avoids splitting a relative stdlib-to-library link across logical roots.
        No other child of the system anchor is recursively discovered.
        """
        source = Path(source).resolve(strict=True)
        if not source.is_relative_to(Path("/usr")) or any(
            (source / child).exists() for child in ("site-packages", "dist-packages")
        ):
            raise ValueError("fixture requires the explicit narrow system stdlib layout")
        self.tree_bytes += bounded_tree_size(source)
        if self.tree_bytes > 768 * 1024 * 1024:
            raise ValueError("fixture runtime exceeds its reviewed byte ceiling")
        for row in entries(source):
            path = source / row["path"]
            self.pin(path)
            if row["kind"] in {"file", "symlink"} and (
                path.suffix == ".so" or ".so." in path.name
            ):
                self.native_resources.add(path.resolve(strict=True))

    def _root(self, name, source, coverage, rows):
        if name in self.roots:
            raise ValueError("fixture root name is already used")
        self.roots[name] = {
            "id": name, "source": str(source), "coverage": coverage,
            "preserve_source": True, "entries": rows,
        }

    def _anchor(self, path):
        candidates = [
            (len(Path(root["source"]).parts), name)
            for name, root in self.roots.items()
            if path.is_relative_to(Path(root["source"]))
        ]
        if not candidates:
            raise ValueError("fixture path has no explicitly declared source anchor")
        return max(candidates)[1]

    def _baseline(self, path):
        """Leave fixed prefix links to the producer's separately captured mapping."""
        if len(path.parts) > 1 and path.parts[1] in BASELINE_PREFIXES:
            prefix = Path("/") / path.parts[1]
            if prefix.is_symlink():
                target = prefix.resolve(strict=True)
                return target.joinpath(*path.parts[2:])
        return path

    def _pin_within(self, name, path):
        root = self.roots[name]
        source = Path(root["source"])
        if root["coverage"] == "tree":
            return
        selected = self.selected[name]
        for part in (source, *reversed(path.parents)):
            if part.is_relative_to(source) and part != path:
                selected.add(part)
        selected.add(path)

    def pin(self, path, *, hops=0):
        if hops > 32:
            raise ValueError("fixture link exceeds its hop bound")
        path = self._baseline(Path(os.path.normpath(path)))
        if not path.is_absolute():
            raise ValueError("fixture paths must be absolute")
        name = self._anchor(path)
        source = Path(self.roots[name]["source"])
        current = source
        for component in path.relative_to(source).parts:
            current /= component
            self._pin_within(name, current)
            if current.is_symlink():
                target = Path(os.readlink(current))
                if not target.is_absolute():
                    target = current.parent / target
                target = Path(os.path.normpath(target))
                self.pin(target, hops=hops + 1)
                remainder = path.relative_to(current)
                if remainder != Path("."):
                    return self.pin(target / remainder, hops=hops + 1)
                return
        if not path.exists():
            raise ValueError("fixture explicitly named input is missing")
        self._pin_within(name, path)

    def reference(self, path):
        actual = Path(path).resolve(strict=True)
        name = self._anchor(actual)
        source = Path(self.roots[name]["source"])
        return {"root": name, "path": actual.relative_to(source).as_posix()}

    def logical(self, path):
        ref = self.reference(path)
        return str(Path("/tools") / self.name / ref["root"] / ref["path"])

    def executable(self, path, kind="elf"):
        actual = Path(path).resolve(strict=True)
        self.pin(actual)
        self.entrypoints[actual] = kind
        return actual

    def _file_rows(self, name):
        source = Path(self.roots[name]["source"])
        rows = []
        for path in sorted(self.selected[name]):
            info = path.lstat()
            relative = path.relative_to(source).as_posix()
            if stat.S_ISLNK(info.st_mode):
                rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            elif stat.S_ISDIR(info.st_mode):
                rows.append({"path": relative, "kind": "directory", "mode": stat.S_IMODE(info.st_mode)})
            elif stat.S_ISREG(info.st_mode):
                rows.append({
                    "path": relative, "kind": "file", "mode": stat.S_IMODE(info.st_mode),
                    "size": info.st_size, "sha256": file_digest(path),
                })
            else:
                raise ValueError("fixture sparse runtime input is a special file")
        return sorted(rows, key=lambda row: row["path"])

    def finish(self, *, commands=None, scripts=(), python=(), library_pins=None):
        commands = {} if commands is None else commands
        search = {path.parent for path in self.native_resources}
        information, dependencies, paths, runtime_search = native_relations(
            [*self.entrypoints, *self.native_resources], sorted(search), library_pins,
        )
        for path in sorted(paths):
            self.pin(path)
        # Complete trees can contain declared links into other explicit source roots.
        for root in list(self.roots.values()):
            if root["coverage"] == "tree":
                for row in root["entries"]:
                    if row["kind"] == "symlink":
                        self.pin(Path(root["source"]) / row["path"])
        # Count each concrete file once across permitted sparse/tree overlap before
        # hashing the additional native files. The admitted fixture ceiling is total.
        sizes = {}
        for name, root in self.roots.items():
            source = Path(root["source"])
            if root["coverage"] == "tree":
                for row in root["entries"]:
                    if row["kind"] == "file":
                        sizes[source / row["path"]] = row["size"]
            else:
                for path in self.selected[name]:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        sizes[path] = info.st_size
        if sum(sizes.values()) > 768 * 1024 * 1024:
            raise ValueError("complete runtime exceeds the reviewed 768 MiB fixture ceiling")
        for name in self.selected:
            self.roots[name]["entries"] = self._file_rows(name)
        self.document["roots"] = list(self.roots.values())

        def transitive(path):
            pending = list(dependencies[path])
            seen = set()
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                pending.extend(dependencies[current])
            return [self.reference(item) for item in sorted(seen)]

        resources = [{"root": name, "path": "."} for name in sorted(self.roots)]
        executables = []
        for path, kind in sorted(self.entrypoints.items()):
            info = information[path]
            interpreter = info["interpreter"]
            executables.append({
                "file": self.reference(path), "format": kind,
                "interpreter": None if interpreter is None else self.reference(interpreter),
                "libraries": transitive(path), "resources": resources, "commands": [],
            })
        for path, interpreter, command in scripts:
            executables.append({
                "file": self.reference(path), "format": "script",
                "interpreter": self.reference(interpreter), "libraries": [],
                "resources": resources, "commands": [self.reference(commands[command])],
            })
        self.document["executables"] = executables
        self.document["commands"] = [
            {"name": name, "file": self.reference(path)}
            for name, path in sorted(commands.items())
        ]
        directories = {path.resolve(strict=False) for path in SYSTEM_SEARCH} | runtime_search
        minimal = [
            path for path in directories
            if not any(other != path and path.is_relative_to(other) for other in directories)
        ]
        self.document["loader"] = {
            "profile": "linux-x86_64-glibc-v1",
            "directories": sorted(str(path) for path in minimal),
            "cache": self.reference(Path("/etc/ld.so.cache")),
        }
        self.document["python"] = list(python)
        self.project.documents[self.name] = self.document
        self.project.definitions[self.name] = {
            "format": "frozen-tool-v1", "manifest": f"tool-locks/{self.name}.json",
        }
        self.project.save(self.name)
        self.project.configure()
        return self.document
