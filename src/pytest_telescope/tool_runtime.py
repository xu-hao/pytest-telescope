"""Validate mandatory native relations against one tool's frozen combined view."""

import os
import posixpath
import sys
from pathlib import Path

from . import tool_native as native
from .tool_context import MAX_RELATIONS, digest
from .tool_graph import layout


class Runtime:
    def __init__(self, graph, budget):
        self.graph = graph
        self.budget = budget
        self.elfs = {}
        self.dependencies = {}
        self.directories = ()
        self.cache = {}
        self.commands = {}
        self.executables = {}
        self.control = None
        self.validate()

    def covered(self, filename):
        filename = Path(self.graph.normalize_prefix(filename))
        return any(filename.is_relative_to(directory) for directory in self.directories)

    def elf(self, key):
        if key not in self.elfs:
            self.budget.tick(visit=True)
            if len(self.elfs) >= MAX_RELATIONS:
                layout("frozen-tool ELF graph exceeds its relation limit")
            with self.graph.open(key) as stream:
                self.elfs[key] = native.read_elf(stream, self.graph.nodes[key]["size"])
        return self.elfs[key]

    def _candidate(self, filename):
        key = self.graph.resolve(filename, required=False)
        if key is not None and self.graph.nodes[key]["kind"] != "file":
            layout("native search candidate is not a regular pinned file")
        return key

    def _find(self, key, info, name, alias):
        if "/" in name:
            if not name.startswith("/") or posixpath.normpath(name) != name:
                layout("relative native dependency names are unsupported")
            candidates = [name]
        else:
            candidates = [posixpath.join(directory, name) for directory in native.search_paths(info, alias)]
            candidates += [filename for filename, _ in self.cache.get(name, ())]
            candidates += [posixpath.join(directory, name) for directory in native.DEFAULT_SEARCH]
        resolved = {}
        for filename in candidates:
            is_logical = filename.startswith("/tools/" + self.graph.tool.name + "/")
            if not is_logical and not self.covered(posixpath.dirname(filename)):
                layout("native search candidate lies outside the declared closed directories")
            selected = self._candidate(filename)
            if selected is not None:
                resolved[digest(self.graph.token(selected))] = selected
        if len(resolved) != 1:
            layout("native dependency is absent or ambiguous in the frozen view")
        return next(iter(resolved.values()))

    def _relations(self, key, info):
        self.graph.preserve(key)
        for directory in native.search_paths(info, self.graph.original(key)):
            if not self.covered(directory):
                layout("ELF search path is not covered by closed directories")
        found = set()
        for name in info.needed:
            original = self._find(key, info, name, self.graph.original(key))
            logical = self._find(key, info, name, self.graph.logical(key))
            if self.graph.token(original) != self.graph.token(logical):
                layout("logical and original ELF lookups select different pinned files")
            if self.elf(original) is None:
                layout("native library dependency is not a supported ELF file")
            found.add(original)
        if info.interpreter is not None:
            loader = self.graph.resolve(info.interpreter)
            if self.graph.nodes[loader]["kind"] != "file" or self.elf(loader) is None:
                layout("ELF interpreter is not a pinned native loader")
            self.graph.preserve(loader)
        self.dependencies[key] = found

    def _closure(self, key):
        found, pending = set(), list(self.dependencies[key])
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            if len(found) > MAX_RELATIONS:
                layout("native dependency traversal exceeds its relation limit")
            pending.extend(self.dependencies[current])
        return {digest(self.graph.token(item)) for item in found}

    def validate(self):
        document = self.graph.tool.document
        for record in document["executables"]:
            key = self.graph.reference(record["file"], kind="file")
            if not self.graph.nodes[key]["mode"] & 0o111 or key in self.executables:
                layout("executable relation must select a unique executable file")
            self.executables[key] = record
        for command in document["commands"]:
            key = self.graph.reference(command["file"], kind="file")
            if key not in self.executables:
                layout("command mapping lacks its executable relation")
            self.commands[command["name"]] = key
        # Read all declared ELF files, not just executable entrypoints: native Python
        # extensions and dlopen resources must not borrow missing live dependencies.
        for key, row in self.graph.nodes.items():
            if row["kind"] == "file":
                with self.graph.open(key) as stream:
                    magic = stream.read(4)
                if magic == b"\x7fELF":
                    self.elf(key)
        loader = document["loader"]
        dynamic = any(info is not None and info.dynamic for info in self.elfs.values())
        if dynamic and (loader is None or loader["cache"] is None):
            layout("dynamic native tools require their pinned loader cache")
        if loader is not None:
            normalized = sorted(set(self.graph.normalize_prefix(name) for name in loader["directories"]))
            for name in normalized:
                path = Path(name)
                if name in ("/", "/usr", "/etc", "/tmp", "/dev/shm") or any(path.is_relative_to(point) for point in ("/proc", "/dev", "/sys", "/run", "/tools", "/telescope", "/metadata")):
                    layout("native search roots cannot replace nonlibrary system roots")
            self.directories = tuple(name for name in normalized if not any(name != other and Path(name).is_relative_to(other) for other in normalized))
            if dynamic:
                if any(not self.covered(name) for name in native.DEFAULT_SEARCH):
                    layout("native profile does not close every default search directory")
                cache_key = self.graph.reference(loader["cache"], kind="file")
                self.graph.preserve(cache_key)
                if self.graph.token(self.graph.resolve("/etc/ld.so.cache")) != self.graph.token(cache_key):
                    layout("loader cache reference does not preserve its actual control path")
                content = self.graph.bytes(cache_key)
                self.cache = native.read_cache(content)
                self.control = cache_key
        for key, info in list(self.elfs.items()):
            self._relations(key, info)
        # _relations may reach a duplicate alias of a library already parsed.
        pending = [key for key in self.elfs if key not in self.dependencies]
        while pending:
            key = pending.pop()
            self._relations(key, self.elfs[key])
            pending = [item for item in self.elfs if item not in self.dependencies]
        for key, record in self.executables.items():
            self.graph.preserve(key)
            for row in record["resources"]:
                self.graph.reference(row)
            command_keys = {self.graph.reference(row, kind="file") for row in record["commands"]}
            if not command_keys <= self.executables.keys():
                layout("subprocess relation lacks its executable record")
            if record["format"] == "script":
                self.script(key, record, command_keys)
                continue
            info = self.elf(key)
            if info is None:
                layout("native executable is not a supported ELF file")
            if info.interpreter is None:
                if record["interpreter"] is not None:
                    layout("static executable cannot declare an ELF interpreter")
            else:
                if record["interpreter"] is None:
                    layout("dynamic executable lacks its actual ELF interpreter")
                declared = self.graph.reference(record["interpreter"], kind="file")
                if self.graph.token(self.graph.resolve(info.interpreter)) != self.graph.token(declared):
                    layout("ELF interpreter bytes or mapping disagree")
            declared_libraries = {digest(self.graph.token(self.graph.reference(row, kind="file"))) for row in record["libraries"]}
            if self._closure(key) != declared_libraries:
                layout("declared ELF libraries disagree with the actual transitive graph")
        if dynamic:
            worker = self.graph.resolve(os.path.normpath(sys.executable))
            if worker not in self.executables or self.elf(worker) is None:
                layout("closed native view lacks the invoking worker executable")
            # Actual imports are checked in the closed view by worker startup, before
            # project imports. All selected native stdlib bytes above are prevalidated.

    def script(self, key, record, commands):
        if record["libraries"] or record["interpreter"] is None:
            layout("script requires exactly its pinned interpreter relation")
        with self.graph.open(key) as stream:
            line = stream.readline(257)
        if len(line) > 256 or not line.startswith(b"#!") or not line.endswith(b"\n"):
            layout("unsupported or oversized tool shebang")
        try:
            words = line[2:-1].decode("utf-8", errors="strict").split()
        except UnicodeError:
            layout("invalid tool shebang encoding")
        if len(words) not in (1, 2) or not words[0].startswith("/"):
            layout("unsupported tool shebang syntax")
        interpreter = self.graph.reference(record["interpreter"], kind="file")
        if interpreter not in self.executables or self.graph.token(self.graph.resolve(words[0])) != self.graph.token(interpreter):
            layout("shebang interpreter does not match the pinned executable")
        self.graph.preserve(interpreter)
        if len(words) == 2:
            if self.executables[interpreter]["format"] != "env" or words[1] not in self.commands or self.commands[words[1]] not in commands:
                layout("env shebang requires its explicit pinned command mapping")
        elif self.executables[interpreter]["format"] == "env":
            layout("env shebang requires exactly one declared command")
