"""Explicit frozen-tool declarations; parsing never executes an installed tool.

Source paths stay in the controller. Public plans expose only registration names and
project-relative manifests; worker requests and result records receive no source map.
"""

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model import DeclarationError, UnsupportedCapability, excluded_path, parent_fd, regular_bytes

FORMAT = "frozen-tool-v1"
MAX_TOOLS = 64
MAX_ROOTS = 32
MAX_ENTRIES = 100_000
MAX_VISITS = 200_000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOOL_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TOTAL_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_RELATIONS = 4096
MAX_LINK_HOPS = 32
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
ROOT_FIELDS = {"id", "source", "coverage", "preserve_source", "entries"}
MANIFEST_FIELDS = {"schema", "roots", "executables", "python", "links", "locks", "commands", "loader"}
PROTECTED_PARTS = {".git", ".hg", ".svn", ".ssh", ".aws", ".gnupg", ".codex", ".agents", ".pytest-telescope"}


def unsupported(kind, message):
    raise UnsupportedCapability("tool:" + kind, message)


def invalid():
    raise DeclarationError("invalid frozen-tool declaration")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def exact(value, fields):
    if type(value) is not dict or set(value) != fields:
        invalid()
    return value


def items(value, maximum):
    if type(value) is not list:
        invalid()
    if len(value) > maximum:
        unsupported("bounds", "frozen-tool declaration exceeds its item limit")
    return value


def text(value):
    if type(value) is not str or not value:
        invalid()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        invalid()
    if len(encoded) > 4096:
        unsupported("bounds", "frozen-tool path or text exceeds its byte limit")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        invalid()
    return value


def path(value, *, absolute=False, root=False):
    text(value)
    parsed = PurePosixPath(value)
    if len(parsed.parts) > 64:
        unsupported("bounds", "frozen-tool path exceeds its depth limit")
    if (
        parsed.is_absolute() != absolute
        or parsed.as_posix() != value
        or ".." in parsed.parts
        or "\\" in value
        or any(char in value for char in "*?[]")
        or value == "/"
        or (value == "." and not root)
    ):
        invalid()
    return value


def named(value):
    if type(value) is not str or NAME.fullmatch(value) is None:
        invalid()
    return value


def hashed(value):
    if type(value) is not str or DIGEST.fullmatch(value) is None:
        invalid()
    return value


def integer(value, maximum):
    if type(value) is not int or value < 0:
        invalid()
    if value > maximum:
        unsupported("bounds", "frozen-tool declaration exceeds its byte or count limit")
    return value


def mode(value):
    if type(value) is not int or not 0 <= value <= 0o777:
        invalid()
    return value


def unique(values):
    if len(values) != len(set(values)):
        invalid()


def registrations(value):
    if type(value) is not dict:
        invalid()
    if len(value) > MAX_TOOLS:
        unsupported("bounds", "too many registered frozen tools")
    result = {}
    for name, row in value.items():
        named(name)
        exact(row, {"format", "manifest"})
        if type(row["format"]) is not str:
            invalid()
        if row["format"] != FORMAT:
            unsupported("frozen-v1", "unsupported frozen-tool format")
        path(row["manifest"])
        if excluded_path(row["manifest"]):
            invalid()
        result[name] = dict(row)
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid()
        result[key] = value
    return result


def _number(_value):
    invalid()


@dataclass(frozen=True)
class Tool:
    name: str
    registration: dict
    raw: bytes
    document: dict

    @property
    def project_inputs(self):
        return sorted({self.registration["manifest"], *(row["path"] for row in self.document["locks"])})

    @property
    def roots(self):
        return {row["id"]: row for row in self.document["roots"]}


def _entry(value):
    if type(value) is not dict or type(value.get("kind")) is not str:
        invalid()
    kind = value["kind"]
    fields = {"path", "kind"}
    if kind == "file":
        exact(value, fields | {"mode", "size", "sha256"})
        mode(value["mode"])
        integer(value["size"], MAX_FILE_BYTES)
        hashed(value["sha256"])
    elif kind == "directory":
        exact(value, fields | {"mode"})
        mode(value["mode"])
    elif kind == "symlink":
        exact(value, fields | {"target"})
        text(value["target"])
    else:
        invalid()
    path(value["path"], root=True)
    if PROTECTED_PARTS.intersection(PurePosixPath(value["path"]).parts):
        unsupported("layout", "frozen-tool source contains a protected namespace")
    return dict(value)


def _root(value, project):
    exact(value, ROOT_FIELDS)
    named(value["id"])
    if type(value["coverage"]) is not str or value["coverage"] not in {"tree", "files"}:
        invalid()
    if type(value["preserve_source"]) is not bool:
        invalid()
    if value["source"] == "/":
        unsupported("layout", "the filesystem root is not a frozen-tool source")
    source = PurePosixPath(path(value["source"], absolute=True))
    if (
        source == project
        or source.is_relative_to(project)
        or project.is_relative_to(source)
        or PROTECTED_PARTS.intersection(source.parts)
        or source.name in {"site-packages", "dist-packages"}
        or source == PurePosixPath(Path.home())
        or (value["coverage"] == "tree" and source in {PurePosixPath(sys.prefix), PurePosixPath(sys.base_prefix)})
        or source in map(PurePosixPath, ("/home", "/root", "/tmp", "/dev/shm"))
        or any(source.is_relative_to(prefix) for prefix in ("/proc", "/dev", "/run", "/sys", "/tools", "/telescope", "/metadata"))
        or (value["coverage"] == "tree" and source in map(PurePosixPath, ("/usr", "/etc", "/opt", "/var", "/usr/local", "/usr/lib", "/usr/lib64", "/usr/local/lib", "/lib", "/lib64")))
    ):
        unsupported("layout", "frozen-tool source is not a supported narrow input")
    rows = [_entry(row) for row in items(value["entries"], MAX_ENTRIES)]
    names = [row["path"] for row in rows]
    unique(names)
    mapping = {row["path"]: row for row in rows}
    if "." not in mapping or mapping["."]["kind"] == "symlink":
        invalid()
    for name in names:
        if name == ".":
            continue
        for parent in PurePosixPath(name).parents:
            if mapping.get(str(parent), {}).get("kind") != "directory":
                invalid()
    return {**value, "entries": sorted(rows, key=lambda row: row["path"])}


def _reference(value, roots):
    exact(value, {"root", "path"})
    named(value["root"])
    path(value["path"], root=True)
    if value["root"] not in roots or value["path"] not in roots[value["root"]]:
        invalid()
    return value["root"], value["path"]


def _references(value, roots):
    refs = [_reference(row, roots) for row in items(value, MAX_RELATIONS)]
    unique(refs)
    return refs


def parse(project, name, registration):
    """Read only declared project JSON and validate its exact supported structure."""
    try:
        raw, _ = regular_bytes(project, registration["manifest"], MAX_MANIFEST_BYTES)
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_float=_number, parse_constant=_number)
    except (OSError, ValueError, UnicodeError, RecursionError, MemoryError) as exc:
        if isinstance(exc, DeclarationError):
            raise
        raise DeclarationError("cannot read a valid bounded frozen-tool manifest") from exc
    exact(document, MANIFEST_FIELDS)
    if type(document["schema"]) is not int:
        invalid()
    if document["schema"] != 1:
        unsupported("frozen-v1", "unsupported frozen-tool manifest version")
    roots = [_root(row, PurePosixPath(project)) for row in items(document["roots"], MAX_ROOTS)]
    unique([row["id"] for row in roots])
    root_map = {row["id"]: row for row in roots}
    entries = {row["id"]: {entry["path"]: entry for entry in row["entries"]} for row in roots}
    if sum(len(row["entries"]) for row in roots) > MAX_ENTRIES:
        unsupported("bounds", "too many frozen-tool entries")
    if sum(entry["size"] for row in roots for entry in row["entries"] if entry["kind"] == "file") > MAX_TOOL_BYTES:
        unsupported("bounds", "frozen-tool declared bytes exceed the tool limit")
    executable_refs = []
    relations = 0
    for row in items(document["executables"], MAX_RELATIONS):
        exact(row, {"file", "format", "interpreter", "libraries", "resources", "commands"})
        executable_refs.append(_reference(row["file"], entries))
        if type(row["format"]) is not str or row["format"] not in {"elf", "script", "env"}:
            invalid()
        if row["interpreter"] is not None:
            _reference(row["interpreter"], entries)
        for field in ("libraries", "resources", "commands"):
            relations += len(_references(row[field], entries))
    unique(executable_refs)
    if relations > MAX_RELATIONS:
        unsupported("bounds", "too many frozen-tool runtime relations")
    for row in document["executables"]:
        if any(ref not in executable_refs for ref in _references(row["commands"], entries)):
            invalid()
    names = []
    for row in items(document["commands"], 256):
        exact(row, {"name", "file"})
        if type(row["name"]) is not str or COMMAND.fullmatch(row["name"]) is None:
            invalid()
        names.append(row["name"])
        if _reference(row["file"], entries) not in executable_refs:
            invalid()
    unique(names)
    names = []
    for row in items(document["python"], 64):
        exact(row, {"name", "version", "metadata", "packages", "dependencies", "runtime"})
        text(row["name"])
        text(row["version"])
        names.append(row["name"])
        _reference(row["metadata"], entries)
        _references(row["packages"], entries)
        _references(row["runtime"], entries)
        unique([text(item) for item in items(row["dependencies"], 64)])
    unique(names)
    if any(set(row["dependencies"]) - set(names) for row in document["python"]):
        invalid()
    names = []
    for row in items(document["links"], MAX_RELATIONS):
        exact(row, {"path", "target", "root", "subpath"})
        names.append(path(row["path"]))
        if excluded_path(row["path"]):
            invalid()
        text(row["target"])
        _reference({"root": row["root"], "path": row["subpath"]}, entries)
        if not root_map[row["root"]]["preserve_source"]:
            invalid()
    unique(names)
    names = []
    for row in items(document["locks"], MAX_RELATIONS):
        exact(row, {"path", "sha256", "roots", "tree_sha256"})
        names.append(path(row["path"]))
        if excluded_path(row["path"]):
            invalid()
        hashed(row["sha256"])
        hashed(row["tree_sha256"])
        selected = [named(item) for item in items(row["roots"], MAX_ROOTS)]
        if not selected or selected != sorted(set(selected)):
            invalid()
        if any(item not in root_map or root_map[item]["coverage"] != "tree" for item in selected):
            invalid()
    unique(names)
    loader = document["loader"]
    if loader is not None:
        exact(loader, {"profile", "directories", "cache"})
        if loader["profile"] != "linux-x86_64-glibc-v1":
            unsupported("layout", "unsupported frozen-tool native loader profile")
        directories = [path(item, absolute=True) for item in items(loader["directories"], 64)]
        if directories != sorted(set(directories)):
            invalid()
        if loader["cache"] is not None:
            _reference(loader["cache"], entries)
    return Tool(name, dict(registration), raw, {**document, "roots": sorted(roots, key=lambda row: row["id"])})


def load_selected(project, definitions, names):
    """Unreferenced registrations never cause external or manifest I/O."""
    if set(names) - definitions.keys():
        invalid()
    result = {name: parse(project, name, definitions[name]) for name in sorted(set(names))}
    if sum(len(tool.raw) for tool in result.values()) > MAX_TOTAL_MANIFEST_BYTES:
        unsupported("bounds", "selected frozen-tool manifests exceed their aggregate limit")
    if sum(len(root["entries"]) for tool in result.values() for root in tool.document["roots"]) > MAX_ENTRIES:
        unsupported("bounds", "selected frozen-tool entries exceed their aggregate limit")
    if sum(entry["size"] for tool in result.values() for root in tool.document["roots"] for entry in root["entries"] if entry["kind"] == "file") > MAX_TOTAL_BYTES:
        unsupported("bounds", "selected frozen-tool bytes exceed their aggregate limit")
    return result


def project_links(tools, names):
    result = {}
    for name in names:
        tool = tools[name]
        for row in tool.document["links"]:
            root = tool.roots[row["root"]]
            record = {**row, "source": root["source"], "entries": root["entries"]}
            existing = result.get(row["path"])
            if existing is not None and existing != record:
                unsupported("layout", "selected frozen tools have conflicting project links")
            result[row["path"]] = record
    return result


def project_inputs(tools, names):
    return sorted({entry for name in names for entry in tools[name].project_inputs} | set(project_links(tools, names)))


def linked_input(name, links):
    """Resolve an explicit project input only through declared immutable records."""
    for link, row in links.items():
        if name == link:
            return link
        if name.startswith(link + "/"):
            from .model import glob_match

            suffix = str(PurePosixPath(row["subpath"]) / name[len(link) + 1 :])
            if not any(glob_match(entry["path"], suffix) for entry in row["entries"]):
                raise DeclarationError("tool-linked input has no declared target")
            return link
    return None


def validate_project_link(project, row):
    """The project link is preserved, never followed while opening its ancestors."""
    try:
        with parent_fd(project, row["path"]) as (directory, leaf):
            before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISLNK(before.st_mode) or os.readlink(leaf, dir_fd=directory) != row["target"]:
                raise DeclarationError("frozen-tool project link differs from its declaration")
        target = Path(row["source"]) / row["subpath"]
        # Literal normalization is sufficient here: actual target links are verified
        # against pinned records during capture, never followed as an ambient input.
        for alias in (Path(project), Path("/project")):
            resolved = os.path.normpath(alias / row["path"] / ".." / row["target"]) if not Path(row["target"]).is_absolute() else os.path.normpath(row["target"])
            if resolved != os.path.normpath(target):
                unsupported("layout", "project link aliases do not resolve to their pinned target")
    except OSError as exc:
        raise DeclarationError("cannot verify the declared frozen-tool project link") from exc


def validate_environment(names, tools):
    if tools and any(name in {"PATH", "HOME", "TMPDIR"} or name.startswith(("LD_", "PYTHON")) for name in names):
        raise DeclarationError("declared environment conflicts with the selected frozen-tool runtime")
