"""Validate mandatory native relations against one tool's frozen combined view."""

import os
import re
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
        self.needed_targets = {}
        self.directories = ()
        self.cache = {}
        self.commands = {}
        self.executables = {}
        self.control = None
        self.native = False
        self.dynamic = False
        self.validate()

    def covered(self, filename):
        filename = Path(self.graph.resolved_path(filename))
        return any(filename.is_relative_to(directory) for directory in self.directories)

    def elf(self, key):
        if key not in self.elfs:
            self.budget.tick(visit=True)
            if len(self.elfs) >= MAX_RELATIONS:
                layout("frozen-tool ELF graph exceeds its relation limit")
            with self.graph.open(key) as stream:
                self.elfs[key] = native.read_elf(stream, self.graph.nodes[key]["size"])
        return self.elfs[key]

    def _find(self, key, info, name, alias):
        pair, _ = self.lookup.find(info, name, alias)
        return pair[1]

    def _relations(self, key, info):
        self.graph.preserve(key)
        for directory in native.search_paths(info, self.graph.original(key)):
            self.budget.tick(visit=True)
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
            self.needed_targets[key, name] = digest(self.graph.token(original))
        if info.interpreter is not None:
            loader = self.graph.resolve(info.interpreter)
            if self.graph.nodes[loader]["kind"] != "file" or self.elf(loader) is None:
                layout("ELF interpreter is not a pinned native loader")
            self.graph.preserve(loader)
        self.dependencies[key] = found

    def validate_union(self, originals, budget):
        """Reprove selection and process consistency against the combined view."""
        from .tool_ordering import validate_processes

        return validate_processes((self,), budget, originals)

    def _resource_elfs(self, references, budget):
        for row in references:
            budget.tick(visit=True)
            key = self.graph.reference(row)
            if self.graph.nodes[key]["kind"] == "directory":
                directory = self.graph.original(key)
                for candidate in self.elfs:
                    budget.tick(visit=True)
                    if Path(self.graph.original(candidate)).is_relative_to(directory):
                        yield candidate
            elif key in self.elfs:
                yield key

    def processes(self, budget):
        """Explicit resources belong to their executable, not every subprocess."""
        for key, record in self.executables.items():
            budget.tick(visit=True)
            if record["format"] == "script":
                continue
            seeds = {key}
            seeds.update(self._resource_elfs(record["resources"], budget))
            # Installed package/runtime associations describe this invoking Python
            # process. They never attach another executable's resources globally.
            if self.graph.resolve(os.path.normpath(sys.executable), required=False) == key:
                for package in self.graph.tool.document["python"]:
                    seeds.update(self._resource_elfs([*package["packages"], *package["runtime"]], budget))
            yield key, seeds

    def _closure(self, key):
        found = set(self.dependencies[key])
        pending = list(found)
        while pending:
            self.budget.tick(visit=True)
            current = pending.pop()
            for child in self.dependencies[current]:
                self.budget.tick(visit=True)
                if child not in found:
                    if len(found) >= MAX_RELATIONS:
                        layout("native dependency traversal exceeds its relation limit")
                    found.add(child)
                    pending.append(child)
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
                self.budget.tick(visit=True)
                with self.graph.open(key) as stream:
                    magic = stream.read(4)
                if magic == b"\x7fELF":
                    self.elf(key)
        loader = document["loader"]
        dynamic = any(info is not None and info.dynamic for info in self.elfs.values())
        self.dynamic = dynamic
        self.native = bool(self.elfs) or loader is not None
        if not self.native and "/etc/ld.so.preload" in self.graph.mapping:
            layout("loader control aliases require an explicit native selection")
        if dynamic and (loader is None or loader["cache"] is None):
            layout("dynamic native tools require their pinned loader cache")
        if loader is not None:
            normalized = sorted(set(self.graph.resolved_path(name) for name in loader["directories"]))
            for name in normalized:
                path = Path(name)
                entry = self.graph.resolve(name, required=False)
                if entry is not None and self.graph.nodes[entry]["kind"] != "directory":
                    layout("native search root is not a pinned directory")
                if name in ("/", "/usr", "/etc", "/tmp", "/dev/shm", "/home", "/root", "/opt", "/var", "/usr/local", str(Path.home()), sys.prefix, sys.base_prefix) or any(path.is_relative_to(point) for point in ("/proc", "/dev", "/sys", "/run", "/tools", "/telescope", "/metadata")):
                    layout("native search roots cannot replace nonlibrary system roots")
            self.directories = tuple(name for name in normalized if not any(name != other and Path(name).is_relative_to(other) for other in normalized))
            if dynamic:
                if any(not self.covered(name) for name in native.DEFAULT_SEARCH):
                    layout("native profile does not close every default search directory")
            if loader["cache"] is not None:
                cache_key = self.graph.reference(loader["cache"], kind="file")
                self.graph.preserve(cache_key)
                if self.graph.token(self.graph.resolve("/etc/ld.so.cache")) != self.graph.token(cache_key):
                    layout("loader cache reference does not preserve its actual control path")
                content = self.graph.bytes(cache_key)
                self.cache = native.read_cache(content)
                self.control = cache_key
        from .tool_ordering import Limit, Lookup

        self.lookup = Lookup(self, Limit(self.budget)) if self.native else None
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
        for key in self.executables:
            current, seen = key, set()
            while self.executables[current]["format"] == "script":
                if current in seen or len(seen) >= 4:
                    layout("script interpreter chain is cyclic or exceeds the supported depth")
                seen.add(current)
                current = self.graph.reference(self.executables[current]["interpreter"], kind="file")
        if dynamic:
            worker = self.graph.resolve(os.path.normpath(sys.executable))
            if worker not in self.executables or self.elf(worker) is None:
                layout("closed native view lacks the invoking worker executable")
            # Actual imports are checked in the closed view by worker startup, before
            # project imports. All selected native stdlib bytes above are prevalidated.

        from .tool_ordering import validate_processes

        del self.lookup
        self.proof_identity = validate_processes((self,), self.budget)

    def script(self, key, record, commands):
        if record["libraries"] or record["interpreter"] is None:
            layout("script requires exactly its pinned interpreter relation")
        with self.graph.open(key) as stream:
            line = stream.readline(257)
        if len(line) > 256 or not line.startswith(b"#!") or not line.endswith(b"\n"):
            layout("unsupported or oversized tool shebang")
        try:
            value = line[2:-1].decode("utf-8", errors="strict")
        except UnicodeError:
            layout("invalid tool shebang encoding")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            layout("unsupported control in tool shebang")
        words = re.split(r"[ \t]+", value.strip(" \t"))
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
