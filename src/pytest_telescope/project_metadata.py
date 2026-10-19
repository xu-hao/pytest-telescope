"""Version 1 static PEP 621 projection; never imports or invokes build backends.

The complete frozen pyproject bytes, including build-system and inert tool tables,
are provenance. This intentionally supports only the documented static subset;
METADATA and entry_points.txt are discoverable metadata, not an installation.
"""

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

from .model import (
    DeclarationError,
    UnsupportedCapability,
    mapping,
    regular_bytes,
    relative,
    sequence,
)

MAX_DISTRIBUTIONS = 256
MAX_SOURCE_BYTES = 1024 * 1024
MAX_DISTRIBUTION_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 4096
FORMAT = "pep621-static-v1"
CAPABILITY = "metadata:pep621-static-v1"
_FIELDS = {
    "name",
    "version",
    "description",
    "requires-python",
    "dependencies",
    "optional-dependencies",
    "scripts",
    "gui-scripts",
    "entry-points",
    "dynamic",
}
_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_GROUP = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\Z")
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_OBJECT = re.compile(
    rf"(?P<module>{_IDENT}(?:\.{_IDENT})*)(?P<attr>:{_IDENT}(?:\.{_IDENT})*)?"
    r"(?:\s*\[(?P<extras>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)\])?\Z"
)


@dataclass(frozen=True)
class Distribution:
    name: str
    inputs: list[str]
    source_hashes: dict[str, str]
    files: dict[str, bytes]


def _unsupported(message):
    raise UnsupportedCapability(CAPABILITY, message)


def _text(value, label, *, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > 4096:
        raise DeclarationError(f"{label} requires a nonempty string of at most 4096 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DeclarationError(f"{label} cannot contain control characters")
    return value


def _normalized_name(value, label):
    value = _text(value, label)
    try:
        return canonicalize_name(value, validate=True)
    except InvalidName as error:
        raise DeclarationError(f"invalid {label}") from error


def _requirement(value, extra=None):
    value = _text(value, "dependency")
    try:
        requirement = Requirement(value)
    except InvalidRequirement as error:
        raise DeclarationError("invalid dependency requirement") from error
    if extra is not None:
        marker = f'extra == "{extra}"'
        if requirement.marker is not None:
            marker = f"({requirement.marker}) and {marker}"
        requirement.marker = Marker(marker)
    return str(requirement)


def _files(project):
    if set(project) - _FIELDS:
        _unsupported("project contains fields outside the static metadata subset")
    if sequence(project.get("dynamic", []), "project.dynamic"):
        _unsupported("dynamic project metadata requires a build backend")
    if "version" not in project:
        _unsupported("project requires a static version")
    name = _normalized_name(project.get("name"), "project.name")
    try:
        version = str(Version(_text(project["version"], "project.version")))
    except InvalidVersion as error:
        raise DeclarationError("invalid project.version") from error
    lines = ["Metadata-Version: 2.3", f"Name: {name}", f"Version: {version}"]
    if "description" in project:
        lines.append(
            "Summary: " + _text(project["description"], "project.description", allow_empty=True)
        )
    if "requires-python" in project:
        try:
            specifier = SpecifierSet(
                _text(project["requires-python"], "project.requires-python", allow_empty=True)
            )
        except InvalidSpecifier as error:
            raise DeclarationError("invalid project.requires-python") from error
        lines.append("Requires-Python: " + str(specifier))
    requirements = sequence(project.get("dependencies", []), "project.dependencies")
    count = len(requirements)
    lines.extend("Requires-Dist: " + _requirement(value) for value in requirements)
    extras = mapping(project.get("optional-dependencies", {}), "project.optional-dependencies")
    if len(extras) > MAX_ITEMS:
        raise DeclarationError("too many optional dependency groups")
    normalized_extras = set()
    for extra, values in sorted(extras.items()):
        normalized = _normalized_name(extra, "extra")
        if normalized in normalized_extras:
            raise DeclarationError("duplicate normalized extra name")
        normalized_extras.add(normalized)
        values = sequence(values, "optional dependencies")
        count += len(values)
        if count > MAX_ITEMS:
            raise DeclarationError("too many dependencies and entry points")
        lines.append("Provides-Extra: " + normalized)
        lines.extend("Requires-Dist: " + _requirement(value, normalized) for value in values)
    groups = dict(mapping(project.get("entry-points", {}), "project.entry-points"))
    if len(groups) > MAX_ITEMS:
        raise DeclarationError("too many entry point groups")
    if {"console_scripts", "gui_scripts"} & groups.keys():
        raise DeclarationError(
            "declare console and GUI scripts through project.scripts and gui-scripts"
        )
    for field, group in (("scripts", "console_scripts"), ("gui-scripts", "gui_scripts")):
        if field in project:
            groups[group] = project[field]
    entry_lines = []
    for group, entries in sorted(groups.items()):
        if not isinstance(group, str) or not _GROUP.fullmatch(group):
            raise DeclarationError("invalid entry point group")
        entries = mapping(entries, "entry point group")
        count += len(entries)
        if count > MAX_ITEMS:
            raise DeclarationError("too many dependencies and entry points")
        entry_lines.append(f"[{group}]")
        for entry, value in sorted(entries.items()):
            if not isinstance(entry, str) or not _NAME.fullmatch(entry):
                raise DeclarationError("invalid entry point name")
            value = _text(value, "entry point object reference")
            match = _OBJECT.fullmatch(value)
            if match is None or (group in {"console_scripts", "gui_scripts"} and not match["attr"]):
                raise DeclarationError("invalid entry point object reference")
            if match["extras"]:
                for extra in match["extras"].split(","):
                    _normalized_name(extra.strip(), "entry point extra")
            entry_lines.append(f"{entry} = {value}")
        entry_lines.append("")
    directory = f"{name.replace('-', '_')}-{version}.dist-info"
    files = {f"{directory}/METADATA": ("\n".join(lines) + "\n\n").encode("utf-8")}
    if entry_lines:
        files[f"{directory}/entry_points.txt"] = ("\n".join(entry_lines) + "\n").encode("utf-8")
    if sum(map(len, files.values())) > MAX_DISTRIBUTION_BYTES:
        raise DeclarationError("generated distribution metadata exceeds its size limit")
    return name, files


def load_distributions(root: Path, entries: dict, expand_inputs) -> dict[str, Distribution]:
    """Freeze sources and translate configured aliases without running project code."""
    entries = mapping(entries, "distributions")
    if len(entries) > MAX_DISTRIBUTIONS:
        raise DeclarationError("at most 256 distributions are supported")
    result = {}
    names = set()
    total = 0
    for alias, entry in sorted(entries.items()):
        _text(alias, "distribution alias")
        entry = mapping(entry, "distribution", {"format", "metadata", "build_inputs"})
        if entry.get("format") != FORMAT:
            _unsupported("distribution requires format pep621-static-v1")
        source = relative(_text(entry.get("metadata"), "distribution.metadata"))
        if Path(source).name != "pyproject.toml":
            _unsupported("static metadata requires a pyproject.toml source")
        try:
            raw, _ = regular_bytes(root, source, MAX_SOURCE_BYTES)
        except OSError as error:
            raise DeclarationError("cannot read regular distribution metadata source") from error
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise DeclarationError("invalid distribution metadata TOML") from error
        build = mapping(document.get("build-system", {}), "build-system")
        if "backend-path" in build:
            _unsupported("in-tree build backends are not supported")
        if "project" not in document:
            _unsupported("static metadata requires a project table")
        name, files = _files(mapping(document["project"], "project"))
        if name in names:
            raise DeclarationError("duplicate normalized distribution name")
        names.add(name)
        total += sum(map(len, files.values()))
        if total > MAX_TOTAL_BYTES:
            raise DeclarationError("generated metadata exceeds the total size limit")
        build_inputs = sequence(entry.get("build_inputs", []), "distribution.build_inputs")
        inputs = sorted({source, *expand_inputs(build_inputs, "distribution build inputs")})
        result[alias] = Distribution(name, inputs, {source: hashlib.sha256(raw).hexdigest()}, files)
    return result


def _selected_files(distributions, names):
    selected = sequence(names, "selected distributions")
    if len(set(selected)) > MAX_DISTRIBUTIONS:
        raise DeclarationError("too many selected distributions")
    files = {}
    total = 0
    for name in sorted(set(selected)):
        if name not in distributions:
            raise DeclarationError("unknown selected distribution")
        distribution = distributions[name]
        size = 0
        if len(distribution.files) > 2:
            raise DeclarationError("invalid generated metadata files")
        for path, data in sorted(distribution.files.items()):
            parts = Path(relative(path)).parts
            if (
                len(parts) != 2
                or not parts[0].endswith(".dist-info")
                or parts[1] not in {"METADATA", "entry_points.txt"}
                or not isinstance(data, bytes)
                or path in files
            ):
                raise DeclarationError("invalid generated metadata file")
            size += len(data)
            total += len(data)
            if size > MAX_DISTRIBUTION_BYTES or total > MAX_TOTAL_BYTES:
                raise DeclarationError("generated metadata exceeds its size limit")
            files[path] = data
    return dict(sorted(files.items()))


def metadata_records(distributions, names):
    """Return deterministic selected representation identity for case keys."""
    return [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in _selected_files(distributions, names).items()
    ]


def materialize_metadata(distributions, names, destination: Path) -> None:
    """Create a new metadata-only tree with normalized read-only modes and epoch times."""
    files = _selected_files(distributions, names)
    destination.mkdir(mode=0o700)
    directories = {destination}
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        directories.add(target.parent)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o444)
        os.utime(target, ns=(0, 0))
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        directory.chmod(0o555)
        os.utime(directory, ns=(0, 0))
