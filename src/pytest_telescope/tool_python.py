"""Installed-distribution validation over pinned bytes, without importing packages."""

import base64
import csv
import hashlib
import importlib.util
import io
import os
import re
import struct
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .tool_graph import layout


def validate(graph, budget):
    records = graph.tool.document["python"]
    declared = {row["name"]: row for row in records}
    for record in records:
        name = record["name"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or canonicalize_name(name) != name:
            layout("installed tool distribution name is not normalized")
        try:
            version = Version(record["version"])
        except InvalidVersion:
            layout("installed tool distribution version is invalid")
        metadata = graph.reference(record["metadata"], kind="directory")
        graph.preserve(metadata)
        root = graph.roots[metadata[0]]
        if root["coverage"] != "tree" or metadata[1] != "." or not Path(root["source"]).name.endswith(".dist-info"):
            layout("installed metadata requires one complete dist-info tree")
        package_keys = [graph.reference(row, kind="directory") for row in record["packages"]]
        if not package_keys:
            layout("installed distribution requires its complete package trees")
        for key in package_keys:
            graph.preserve(key)
            if graph.roots[key[0]]["coverage"] != "tree" or key[1] != ".":
                layout("installed packages require complete declared trees")
            if graph.resolve(graph.original(key) + "/__init__.py", required=False) is None:
                layout("namespace and import-hook package layouts are unsupported")
        runtime = [graph.reference(row) for row in record["runtime"]]
        for key in runtime:
            graph.preserve(key)
        worker = graph.resolve(os.path.normpath(sys.executable))
        if not any(graph.original(worker) == graph.original(key) or (graph.nodes[key]["kind"] == "directory" and Path(graph.original(worker)).is_relative_to(graph.original(key))) for key in runtime):
            layout("installed distribution runtime does not pin the invoking interpreter")
        base = graph.original(metadata)
        meta_key = graph.resolve(base + "/METADATA")
        record_key = graph.resolve(base + "/RECORD")
        try:
            message = BytesParser(policy=policy.default).parsebytes(graph.bytes(meta_key))
            if message.defects or len(message.get_all("Name", [])) != 1 or len(message.get_all("Version", [])) != 1 or canonicalize_name(message["Name"]) != name or Version(message["Version"]) != version:
                layout("installed metadata disagrees with its distribution record")
            required = set()
            for encoded in message.get_all("Requires-Dist", []):
                dependency = Requirement(encoded)
                if dependency.url or dependency.extras:
                    layout("direct and extra-enabled installed dependencies are unsupported")
                if dependency.marker is not None and not dependency.marker.evaluate({"extra": ""}):
                    continue
                dependency_name = canonicalize_name(dependency.name)
                if dependency_name not in declared or Version(declared[dependency_name]["version"]) not in dependency.specifier:
                    layout("installed dependency version does not satisfy metadata")
                required.add(dependency_name)
            if required != set(record["dependencies"]):
                layout("installed dependencies disagree with active metadata requirements")
        except (ValueError, InvalidRequirement, InvalidVersion, TypeError):
            layout("unsupported installed distribution metadata")
        # Editable direct-url metadata and executable import startup machinery cannot
        # claim the ordinary installed-package profile.
        direct = graph.resolve(base + "/direct_url.json", required=False)
        if direct is not None:
            import json

            try:
                value = json.loads(graph.bytes(direct))
                if type(value) is not dict or value.get("dir_info", {}).get("editable") is True:
                    layout("editable installed tools are unsupported")
            except (ValueError, TypeError, AttributeError):
                layout("invalid installed direct-url metadata")
        allowed = [base, *(graph.original(key) for key in package_keys)]
        parent = str(Path(base).parent)
        seen = set()
        try:
            rows = csv.reader(io.StringIO(graph.bytes(record_key).decode("utf-8")), strict=True)
            for index, row in enumerate(rows):
                budget.tick(visit=True)
                if index >= 100_000 or len(row) != 3:
                    layout("installed RECORD exceeds its supported shape or bound")
                filename, encoded, size = row
                if not filename or filename.startswith("/") or "\\" in filename or any(ord(char) < 32 for char in filename):
                    layout("invalid installed RECORD path")
                normalized = os.path.normpath(os.path.join(parent, filename))
                if normalized in seen or not any(Path(normalized).is_relative_to(directory) for directory in allowed):
                    layout("installed RECORD escapes its selected distribution roots")
                seen.add(normalized)
                key = graph.resolve(normalized)
                if graph.nodes[key]["kind"] != "file":
                    layout("installed RECORD does not name a regular pinned file")
                if Path(normalized).suffix in (".pth", ".egg-link"):
                    layout("installed import hooks are unsupported")
                if encoded:
                    algorithm, separator, expected = encoded.partition("=")
                    if not separator or algorithm not in ("sha256", "sha384", "sha512"):
                        layout("unsupported installed RECORD hash")
                    actual = hashlib.new(algorithm)
                    with graph.open(key) as stream:
                        while chunk := stream.read(1024 * 1024):
                            budget.tick(size=len(chunk))
                            actual.update(chunk)
                    if base64.urlsafe_b64encode(actual.digest()).rstrip(b"=").decode() != expected or not size.isdecimal() or int(size) != graph.nodes[key]["size"]:
                        layout("installed RECORD does not match its pinned file")
                elif key != record_key and not normalized.endswith(".pyc"):
                    layout("installed RECORD lacks a required source or resource hash")
                elif size:
                    layout("hashless installed RECORD row has an unsupported size")
                if normalized.endswith(".pyc"):
                    bytecode(graph, key)
        except (UnicodeError, csv.Error):
            layout("invalid installed RECORD encoding")
        expected = {
            graph.original(key) for key, row in graph.nodes.items()
            if row["kind"] == "file" and any(Path(graph.original(key)).is_relative_to(directory) for directory in allowed)
        }
        # An optional generated cache may not be recorded by installers, but still
        # needs a pinned source/runtime relation. Every other file is in RECORD.
        for filename in expected - seen:
            if not filename.endswith(".pyc"):
                layout("installed file is absent from its pinned RECORD")
            bytecode(graph, graph.resolve(filename))
        if graph.original(record_key) not in seen:
            layout("installed RECORD omits its own normal self row")


def bytecode(graph, key):
    filename = Path(graph.original(key))
    try:
        source_name = importlib.util.source_from_cache(str(filename))
    except ValueError:
        layout("unsupported installed bytecode layout")
    source = graph.resolve(source_name)
    if graph.nodes[source]["kind"] != "file":
        layout("installed bytecode lacks its pinned source")
    with graph.open(key) as stream:
        header = stream.read(16)
    if len(header) != 16 or header[:4] != importlib.util.MAGIC_NUMBER:
        layout("installed bytecode does not match the invoking interpreter")
    flags = struct.unpack_from("<I", header, 4)[0]
    if flags not in (0, 1, 3):
        layout("unsupported installed bytecode flags")
    if flags & 1 and header[8:16] != importlib.util.source_hash(graph.bytes(source)):
        layout("installed bytecode does not match its pinned source")
    if flags == 0 and struct.unpack_from("<I", header, 12)[0] != graph.nodes[source]["size"] & 0xffffffff:
        layout("installed bytecode source size does not match")
