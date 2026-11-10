"""Literal-call authentication from verified frozen inputs, never scope grants."""

import hashlib
import os
import posixpath
import re
from pathlib import PurePosixPath

from . import markers
from .model import DeclarationError, InfrastructureError, fingerprint, regular_bytes


class FrozenRegistry:
    """Parse each frozen Python import origin once, selecting each view separately."""

    def __init__(self, root, records, tool_links):
        self.root = root
        self.records = {}
        self.tool_links = {
            name: (row["target"], fingerprint(row)) for name, row in tool_links.items()
        }
        self.parsed = {}
        self.source_bytes = self.nodes = self.markers = 0
        fields = {
            "file": {"path", "kind", "sha256", "mode"},
            "directory": {"path", "kind", "mode"},
            "symlink": {"path", "kind", "target"},
            "absent": {"path", "kind"},
        }
        for record in records:
            if (
                type(record) is not dict
                or type(record.get("kind")) is not str
                or record["kind"] not in fields
            ):
                raise InfrastructureError("invalid frozen declaration source record")
            if set(record) != fields[record["kind"]]:
                raise InfrastructureError("invalid frozen declaration source record fields")
            name = record["path"]
            if not self.canonical_path(name) or name in self.records:
                raise InfrastructureError("invalid frozen declaration source path")
            if record["kind"] == "file" and (
                type(record["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
                or type(record["mode"]) is not int
                or record["mode"] not in {0o444, 0o445, 0o454, 0o455, 0o544, 0o545, 0o554, 0o555}
            ):
                raise InfrastructureError("invalid frozen declaration source identity")
            if record["kind"] == "directory" and (
                type(record["mode"]) is not int or record["mode"] != 0o755
            ):
                raise InfrastructureError("invalid frozen declaration directory")
            if record["kind"] == "symlink" and (
                type(record["target"]) is not str
                or not record["target"]
                or (record["target"].startswith("/") and name not in self.tool_links)
                or "\x00" in record["target"]
            ):
                raise InfrastructureError("invalid frozen declaration link")
            self.records[name] = dict(record)
        for name, (target, _) in self.tool_links.items():
            if self.records.get(name) != {"path": name, "kind": "symlink", "target": target}:
                raise InfrastructureError("frozen tool link disagrees with its selected binding")

    def verify_link(self, name):
        try:
            actual = os.readlink(self.root / name)
        except OSError as exc:
            raise InfrastructureError("cannot verify frozen declaration source link") from exc
        if actual != self.records[name]["target"]:
            raise InfrastructureError("frozen declaration source link changed")

    @staticmethod
    def canonical_path(name):
        return (
            type(name) is str
            and bool(name)
            and len(name) <= 4096
            and not name.startswith("/")
            and "\\" not in name
            and "\x00" not in name
            and len(PurePosixPath(name).parts) <= 64
            and all(part not in ("", ".", "..") for part in name.split("/"))
        )

    def for_view(self, unit):
        selected = set(unit.inputs)
        if any(name not in self.records for name in selected):
            raise InfrastructureError("declaration view has no frozen source record")
        sources = {}
        for origin in sorted(selected):
            if origin in self.tool_links:
                binding = unit.tool_links.get(origin)
                if binding is None or fingerprint(binding) != self.tool_links[origin][1]:
                    raise InfrastructureError("frozen tool link is not selected by this view")
                self.verify_link(origin)
                # Tool targets are captured separately, never parsed as project helper origins.
                continue
            if not origin.endswith(".py"):
                continue
            name = origin
            seen = set()
            while self.records[name]["kind"] == "symlink":
                if name in seen or len(seen) >= 64:
                    raise InfrastructureError("cyclic frozen declaration source link")
                if name in self.tool_links:
                    raise InfrastructureError("a tool link is not a project declaration source")
                seen.add(name)
                record = self.records[name]
                self.verify_link(name)
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), record["target"])
                )
                if not self.canonical_path(target) or target not in selected:
                    # A supported dangling leaf remains absent, never an authentication source.
                    if target in unit.absent and self.canonical_path(target):
                        name = None
                        break
                    raise InfrastructureError("frozen declaration source link escaped its view")
                name = target
            if name is not None and self.records[name]["kind"] == "file":
                sources[origin] = name
        registry = []
        for origin, name in sources.items():
            try:
                content, measured = regular_bytes(self.root, name, markers.MAX_SOURCE)
            except OSError as exc:
                raise InfrastructureError("cannot read frozen declaration source") from exc
            record = self.records[name]
            if (
                hashlib.sha256(content).hexdigest() != record["sha256"]
                or measured.st_mode & 0o777 != record["mode"]
            ):
                raise InfrastructureError("frozen declaration source disagrees with its record")
            if origin not in self.parsed:
                # Each readable import origin has its own call identities. Count aliases
                # against parsing budgets too: they must not multiply records unboundedly.
                self.source_bytes += len(content)
                if self.source_bytes > markers.MAX_TOTAL_SOURCE:
                    raise DeclarationError("frozen Python source exceeds the discovery budget")
                try:
                    found, nodes = markers.discover(content, origin)
                except markers.MarkerError as exc:
                    raise DeclarationError(str(exc)) from exc
                self.nodes += nodes
                self.markers += len(found)
                if self.nodes > markers.MAX_TOTAL_NODES or self.markers > markers.MAX_MARKERS:
                    raise DeclarationError(
                        "frozen declaration syntax exceeds total node/marker limits"
                    )
                self.parsed[origin] = found
            registry.extend(self.parsed[origin])
        return registry
