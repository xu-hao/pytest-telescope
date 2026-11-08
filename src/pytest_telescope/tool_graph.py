"""Exact combined mapping of the roots belonging to one frozen tool."""

import os
import posixpath
import stat
from contextlib import contextmanager
from pathlib import Path

from . import tool_context as declaration
from .model import InfrastructureError

PREFIXES = frozenset({"/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"})


def layout(message="frozen-tool paths do not form a closed declared view"):
    declaration.unsupported("layout", message)


class Graph:
    def __init__(self, tool, directory):
        self.tool = tool
        self.directory = directory
        self.nodes = {}
        self.mapping = {}
        self.prefixes = {}
        self.roots = tool.roots
        roots = list(self.roots.values())
        for index, root in enumerate(roots):
            for other in roots[:index]:
                left, right = Path(root["source"]), Path(other["source"])
                if left == right:
                    layout("redundant frozen-tool root anchors")
                if left.is_relative_to(right) and other["coverage"] != "files":
                    layout("overlapping frozen-tool roots require an explicit files anchor")
                if right.is_relative_to(left) and root["coverage"] != "files":
                    layout("overlapping frozen-tool roots require an explicit files anchor")
            for row in root["entries"]:
                key = (root["id"], row["path"])
                self.nodes[key] = row
                self._map(self.logical(key), key)
                if root["preserve_source"]:
                    self._map(self.original(key), key)

    def original(self, key):
        return posixpath.normpath(posixpath.join(self.roots[key[0]]["source"], key[1]))

    def logical(self, key):
        return posixpath.normpath(posixpath.join("/tools", self.tool.name, *key))

    def token(self, key):
        return self.original(key), self.nodes[key]

    def _map(self, filename, key):
        previous = self.mapping.get(filename)
        if previous is not None and self.token(previous) != self.token(key):
            layout("overlapping frozen-tool entries disagree")
        self.mapping[filename] = key

    def normalize_prefix(self, filename):
        filename = posixpath.normpath(filename)
        first = "/" + filename.lstrip("/").split("/", 1)[0]
        if first not in PREFIXES:
            return filename
        if first not in self.prefixes:
            try:
                before = os.lstat(first)
                if stat.S_ISLNK(before.st_mode):
                    target = os.readlink(first)
                    mapped = posixpath.normpath(posixpath.join("/", target))
                    if mapped in PREFIXES or mapped == "/" or not mapped.startswith("/"):
                        layout("unsupported frozen-tool baseline prefix mapping")
                    row = {"kind": "symlink", "target": target, "mapping": mapped}
                elif stat.S_ISDIR(before.st_mode):
                    row = {"kind": "directory", "mode": stat.S_IMODE(before.st_mode), "mapping": first}
                else:
                    layout("unsupported frozen-tool baseline prefix type")
                after = os.lstat(first)
                if self._signature(before) != self._signature(after):
                    raise InfrastructureError("frozen-tool baseline prefix changed during capture")
                self.prefixes[first] = (row, self._signature(before))
            except FileNotFoundError:
                self.prefixes[first] = ({"kind": "absent", "mapping": first}, None)
            except OSError as exc:
                raise InfrastructureError("cannot capture frozen-tool baseline prefix") from exc
        row, _ = self.prefixes[first]
        return row["mapping"] + filename[len(first):]

    @staticmethod
    def _signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def verify_prefixes(self):
        for filename, (row, signature) in self.prefixes.items():
            try:
                current = os.lstat(filename)
                if self._signature(current) != signature or (row["kind"] == "symlink" and os.readlink(filename) != row["target"]):
                    raise InfrastructureError("frozen-tool baseline prefix changed")
            except FileNotFoundError as exc:
                if row["kind"] != "absent":
                    raise InfrastructureError("frozen-tool baseline prefix disappeared") from exc
            except OSError as exc:
                raise InfrastructureError("cannot revalidate frozen-tool baseline prefix") from exc

    def resolved_path(self, filename):
        seen = set()
        for _ in range(declaration.MAX_LINK_HOPS):
            filename = self.normalize_prefix(filename)
            if filename in seen:
                layout("frozen-tool link graph contains a cycle")
            seen.add(filename)
            parts = Path(filename).parts
            for end in range(2, len(parts) + 1):
                prefix = str(Path(*parts[:end]))
                key = self.mapping.get(prefix)
                if key is not None and self.nodes[key]["kind"] == "symlink":
                    filename = posixpath.join(posixpath.dirname(prefix), self.nodes[key]["target"], *parts[end:])
                    break
            else:
                return filename
        layout("frozen-tool link resolution exceeds its hop limit")

    def resolve(self, filename, *, required=True):
        key = self.mapping.get(self.resolved_path(filename))
        if key is None and required:
            layout()
        return key

    def reference(self, row, *, kind=None):
        key = self.resolve(self.logical((row["root"], row["path"])))
        if kind is not None and self.nodes[key]["kind"] != kind:
            layout("frozen-tool runtime reference has the wrong file type")
        return key

    def validate_links(self):
        for key, row in self.nodes.items():
            if row["kind"] != "symlink":
                continue
            logical = self.resolve(self.logical(key))
            if self.roots[key[0]]["preserve_source"]:
                original = self.resolve(self.original(key))
                if self.token(logical) != self.token(original):
                    layout("logical and original tool links resolve differently")

    @contextmanager
    def open(self, key):
        from .tool_capture import _parent

        if self.nodes[key]["kind"] != "file":
            layout()
        try:
            with _parent(self.directory / key[0] / key[1]) as (parent, leaf):
                descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InfrastructureError("owned frozen-tool runtime changed type")
                yield stream
        except OSError as exc:
            raise InfrastructureError("cannot read owned frozen-tool runtime") from exc

    def bytes(self, key, maximum=16 * 1024 * 1024):
        if self.nodes[key].get("size", maximum + 1) > maximum:
            layout("frozen-tool runtime metadata exceeds its decoding bound")
        with self.open(key) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            layout("frozen-tool runtime metadata exceeds its decoding bound")
        return content

    def preserve(self, key):
        if not self.roots[key[0]]["preserve_source"]:
            layout("native and installed runtime files require their original paths")
        if self.token(self.resolve(self.original(key))) != self.token(key):
            layout()

    def prefix_identity(self):
        return [[name, row] for name, (row, _) in sorted(self.prefixes.items())]
