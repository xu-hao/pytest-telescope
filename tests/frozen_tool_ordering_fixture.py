"""Owned ELF metadata for real Capture/Graph/Runtime proof, never executed programs."""

import copy
import struct
from types import SimpleNamespace

from frozen_tool_configuration_fixtures import StaticConfiguration, sparse_rows
from frozen_tool_fixtures import put

from pytest_telescope import tool_capture, tool_context, tool_runtime


def elf(*, needed=(), soname=None, rpath=None, runpath=None, extra=()):
    strings = bytearray(b"\0")
    tags = []
    values = [(1, value) for value in needed]
    values += [(tag, value) for tag, value in ((14, soname), (15, rpath), (29, runpath)) if value is not None]
    values += list(extra)
    for tag, value in values:
        tags.append((tag, len(strings)))
        strings.extend(value.encode() + b"\0")
    dynamic_offset = 64 + 2 * 56
    strings_offset = dynamic_offset + (len(tags) + 3) * 16
    tags += [(5, 0x400000 + strings_offset), (10, len(strings)), (0, 0)]
    size = strings_offset + len(strings)
    header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                         3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0)
    load = struct.pack("<IIQQQQQQ", 1, 4, 0, 0x400000, 0x400000, size, size, 4096)
    dynamic = struct.pack("<IIQQQQQQ", 2, 4, dynamic_offset, 0x400000 + dynamic_offset,
                          0x400000 + dynamic_offset, len(tags) * 16, len(tags) * 16, 8)
    return header + load + dynamic + b"".join(struct.pack("<qQ", *tag) for tag in tags) + strings


def cache(rows):
    strings = bytearray()
    entries = []
    start = 48 + len(rows) * 24
    for name, path in rows:
        key = start + len(strings)
        strings.extend(name.encode() + b"\0")
        value = start + len(strings)
        strings.extend(str(path).encode() + b"\0")
        entries.append(struct.pack("<iIIIQ", 0x303, key, value, 0, 0))
    header = bytearray(48)
    header[:20] = b"glibc-ld.so.cache1.1"
    struct.pack_into("<II", header, 20, len(rows), len(strings))
    header[28] = 2
    return bytes(header) + b"".join(entries) + strings


class Ordering(StaticConfiguration):
    """Synthetic invoking path model; all admission/selection/cleanup is production."""

    def __init__(self, directory, monkeypatch):
        super().__init__(directory, monkeypatch)
        # Only Runtime's invoking-path observation is supplied for this graph layer.
        # The synthetic executable is never launched or represented as real Python.
        monkeypatch.setattr(tool_runtime, "sys", SimpleNamespace(
            executable=str(self.executable), prefix=str(self.prefix), base_prefix=str(self.prefix)))
        self.config_names = {"selected.txt", "ld.so.cache"}
        self.selected = self.config_names
        self.cache_rows = []
        self.records = {}
        self.entry("bin/python3.12")

    def object(self, path, **kwargs):
        return put(self.prefix, path, elf(**kwargs), 0o755)

    def ref(self, path):
        return {"root": "prefix", "path": path}

    def entry(self, path, *, libraries=(), resources=(), **kwargs):
        self.object(path, **kwargs)
        self.records[path] = {"file": self.ref(path), "format": "elf", "interpreter": None,
                              "libraries": [self.ref(value) for value in libraries],
                              "resources": [self.ref(value) for value in resources], "commands": []}

    def fallback(self, name, path):
        self.cache_rows.append((name, self.prefix / path))

    def finish(self):
        for path in self.prefix.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        put(self.config, "ld.so.cache", cache(self.cache_rows))
        self.document["executables"] = list(self.records.values())
        # The whole tiny owned prefix is an explicit library search root in this
        # graph fixture; Runtime rejects interpreter-prefix roots, so use its lib
        # children and close all actual platform default locations independently.
        directories = {"/usr/lib", "/usr/lib64", "/usr/local/lib", "/usr/local/lib64"}
        directories.update(str(self.prefix / name) for name in ("lib", "a", "b", "empty", "fallback", "resources"))
        self.document["loader"] = {"profile": "linux-x86_64-glibc-v1", "directories": sorted(directories),
                                   "cache": {"root": "config", "path": "ld.so.cache"}}
        super().save()

    def partition(self, name, keep, *, resources=None):
        document = copy.deepcopy(self.document)
        landmarks = {"bin/python3.12", "lib/python3.12/os.py", "lib/python3.12/site.py", "lib/python3.12/encodings/__init__.py"}
        document["roots"][0]["entries"] = sparse_rows(self.prefix, {*keep, *landmarks})
        if resources is not None:
            document["executables"][0]["resources"] = [self.ref(path) for path in resources]
        self.project.documents[name] = document
        self.project.definitions[name] = {"format": "frozen-tool-v1", "manifest": f"tool-locks/{name}.json"}
        self.project.save(name)
        self.project.configure()

    def capture(self, names=("native",)):
        self.sequence += 1
        directory = self.project.root.parent / ("capture-" + str(self.sequence))
        directory.mkdir()
        tools = tool_context.load_selected(self.project.root, self.project.definitions, list(names))
        return tool_capture.Capture(SimpleNamespace(root=self.project.root, tools=tools), directory, 30, None, sandbox=self.box)

    @staticmethod
    def targets(capture, entry="bin/python3.12", name="native"):
        runtime = capture.runtimes[name]
        return {runtime.graph.original(key) for key in runtime.dependencies[("prefix", entry)]}
