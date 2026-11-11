"""Literal-call authentication from verified frozen inputs, never scope grants."""

import hashlib
import os
import re

from . import markers
from .model import (
    InfrastructureError,
    canonical_path,
    fingerprint,
    follow_source,
    regular_bytes,
)


class FrozenRegistry:
    """Label each frozen Python import origin from its static scan, per selected view.

    Static discovery already parsed and bounded every declared source of the pool; this
    registry only verifies the frozen bytes against that scan and labels each origin.
    """

    def __init__(self, root, records, tool_links, scans):
        self.root = root
        self.records = {}
        self.tool_links = {
            name: (row["target"], fingerprint(row)) for name, row in tool_links.items()
        }
        self.scans = scans
        self.labeled = {}
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
            if not canonical_path(name) or name in self.records:
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

    def entry(self, name):
        record = self.records[name]
        if record["kind"] == "symlink":
            self.verify_link(name)
            return "symlink", record["target"]
        return record["kind"], None

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
            name = follow_source(origin, self.entry, selected, set(unit.absent), self.tool_links)
            if name is not None:
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
            scan = self.scans.get(origin)
            if scan is None or scan.file != name or scan.sha256 != record["sha256"]:
                raise InfrastructureError(
                    "frozen declaration source disagrees with its static scan"
                )
            if origin not in self.labeled:
                # Each readable import origin has its own call identities, relabeled from
                # the one bounded parse of its file rather than parsed again.
                self.labeled[origin] = markers.label(scan.rows, origin)
            registry.extend(self.labeled[origin])
        return registry
