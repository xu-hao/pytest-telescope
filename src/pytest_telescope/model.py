"""Pre-import declarations and immutable project snapshots."""

import fnmatch
import glob
import hashlib
import json
import os
import shlex
import stat
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class DeclarationError(ValueError):
    pass


class InfrastructureError(RuntimeError):
    pass


@dataclass
class Unit:
    name: str
    tests: list[str]
    inputs: list[str]
    absent: list[str]
    cache: bool


@dataclass
class Manifest:
    root: Path
    raw: bytes
    units: list[Unit]
    env: list[str]
    inventory: list[str]
    pytest_args: list[str]


def sequence(value, label):
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or any(not isinstance(item, str) or not item or len(item) > 512 for item in value)
    ):
        raise DeclarationError(f"{label} requires at most 4096 nonempty strings")
    return value


def mapping(value, label, allowed=None):
    if not isinstance(value, dict):
        raise DeclarationError(f"{label} must be a table")
    if allowed is not None and set(value) - allowed:
        raise DeclarationError(f"{label} contains unsupported fields")
    return value


def relative(name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise DeclarationError("paths must remain project-relative without traversal")
    return path.as_posix()


@contextmanager
def parent_fd(root, name):
    parts = Path(relative(name)).parts
    if not parts:
        raise DeclarationError("a regular input file is required")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def regular_bytes(root, name, limit):
    with parent_fd(root, name) as (directory, leaf):
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DeclarationError("declared file is not a regular file")
            content = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise InfrastructureError("input changed while freezing its snapshot")
            if len(content) > limit:
                raise DeclarationError("declared file exceeds its size limit")
            return content, before


def walk_bounded(root, start, *, prune=True, include_hidden=False):
    pending = [start]
    visited = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > 20000:
                    raise DeclarationError(
                        "filesystem discovery exceeds 20000 entries; narrow declarations"
                    )
                path = Path(entry.path)
                relative_path = path.relative_to(root)
                if len(relative_path.as_posix()) > 512 or len(relative_path.parts) > 64:
                    raise DeclarationError("discovered paths exceed 512 characters or 64 levels")
                if entry.name in {".git", ".pytest-telescope"}:
                    continue
                if (
                    prune
                    and entry.is_dir(follow_symlinks=False)
                    and (
                        (entry.name.startswith(".") and not include_hidden)
                        or entry.name in {"__pycache__", "venv", "node_modules", "build", "dist"}
                    )
                ):
                    continue
                yield path
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def glob_match(name, pattern):
    names, patterns = Path(name).parts, Path(pattern).parts

    @lru_cache(maxsize=4096)
    def match(i, j):
        if j == len(patterns):
            return i == len(names)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(names) and match(i + 1, j))
        return i < len(names) and fnmatch.fnmatchcase(names[i], patterns[j]) and match(i + 1, j + 1)

    return match(0, 0)


def expand(root, names, label, *, missing=False):
    found = set()
    for name in sequence(names, label):
        name = relative(name)
        if name == ".":
            raise DeclarationError("declare project subdirectories or files, not the checkout root")
        if missing:
            if glob.has_magic(name):
                raise DeclarationError("expected-absent paths cannot contain globs")
            if os.path.lexists(root / name):
                raise DeclarationError("an expected-absent input exists")
            if not (root / name).parent.resolve().is_relative_to(root):
                raise DeclarationError("expected-absent path escapes project root")
            found.add(name)
            continue
        if glob.has_magic(name):
            candidates = (
                path
                for path in walk_bounded(
                    root,
                    root,
                    include_hidden=any(part.startswith(".") for part in Path(name).parts),
                )
                if glob_match(path.relative_to(root).as_posix(), name)
            )
        else:
            path = root / name
            if not os.path.lexists(path):
                raise DeclarationError(f"missing {label}; declare expected absence explicitly")
            candidates = [path]
        for candidate in candidates:
            paths = (
                walk_bounded(root, candidate, prune=False)
                if candidate.is_dir() and not candidate.is_symlink()
                else ()
            )
            for selected in _with_first(candidate, paths):
                rel = selected.relative_to(root).as_posix()
                if Path(rel).parts and Path(rel).parts[0] in {".git", ".pytest-telescope"}:
                    raise DeclarationError("repository metadata and cache are not project inputs")
                if not selected.resolve(strict=False).is_relative_to(root):
                    raise DeclarationError("input symlink escapes project root")
                if any(
                    parent.is_symlink()
                    for parent in selected.parents
                    if parent != root and parent.is_relative_to(root)
                ):
                    raise DeclarationError("symlink directories are unsupported")
                if selected.is_symlink() and Path(os.readlink(selected)).is_absolute():
                    raise DeclarationError("leaf symlinks must use relative targets")
                if selected.is_symlink() and selected.is_dir():
                    raise DeclarationError("symlink directories are unsupported")
                if not (selected.is_file() or selected.is_dir() or selected.is_symlink()):
                    raise DeclarationError(
                        "inputs must be regular files, directories, or leaf symlinks"
                    )
                found.add(rel)
                if len(found) > 4096:
                    raise DeclarationError("expanded declarations exceed 4096 paths")
    return sorted(found)


def _with_first(first, rest):
    yield first
    yield from rest


def load(root):
    root = Path(root).resolve()
    try:
        manifest = root / "pyproject.toml"
        if not manifest.resolve().is_relative_to(root):
            raise DeclarationError("manifest escapes project root")
        raw, _ = regular_bytes(root, "pyproject.toml", 1_048_576)
        data = tomllib.loads(raw.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise DeclarationError("cannot read valid project pyproject.toml") from exc
    tool = mapping(data.get("tool", {}), "tool")
    spec = mapping(
        tool.get("pytest_telescope"),
        "pytest_telescope",
        {"targets", "bundles", "test_roots", "env"},
    )
    bundles = mapping(spec.get("bundles", {}), "bundles")
    targets = mapping(spec.get("targets"), "targets")
    list_entries = sum(
        len(value)
        for table in [spec, *bundles.values(), *targets.values()]
        if isinstance(table, dict)
        for value in table.values()
        if isinstance(value, list)
    )
    if list_entries > 4096:
        raise DeclarationError("declaration lists exceed 4096 total entries")
    if not targets or len(targets) + len(bundles) > 256:
        raise DeclarationError("declare 1 to 256 targets and bundles")
    if any(not name or len(name) > 128 for name in (*targets, *bundles)):
        raise DeclarationError("target and bundle names require 1 to 128 characters")
    resolved = {}
    active = set()
    depths = {}

    def bundle(name):
        if name not in bundles:
            raise DeclarationError("unknown dependency bundle")
        if name in active:
            raise DeclarationError("dependency bundle cycle")
        if len(active) >= 32:
            raise DeclarationError("bundle nesting exceeds 32 levels")
        if name not in resolved:
            active.add(name)
            item = mapping(bundles[name], "bundle", {"inputs", "bundles", "absent"})
            inputs = set(expand(root, item.get("inputs", []), "bundle inputs"))
            absent = set(expand(root, item.get("absent", []), "bundle absence", missing=True))
            depth = 1
            for dependency in sequence(item.get("bundles", []), "bundle references"):
                child_inputs, child_absent = bundle(dependency)
                inputs.update(child_inputs)
                absent.update(child_absent)
                depth = max(depth, 1 + depths[dependency])
            if depth > 32:
                raise DeclarationError("bundle nesting exceeds 32 levels")
            if len(inputs) + len(absent) > 4096:
                raise DeclarationError("effective bundle exceeds 4096 paths")
            depths[name] = depth
            active.remove(name)
            resolved[name] = inputs, absent
        return resolved[name]

    for name in sorted(bundles):
        bundle(name)
    config_files = {"pyproject.toml"}
    for name in ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"):
        if os.path.lexists(root / name):
            raise DeclarationError(
                "move pytest configuration into pyproject.toml; other pytest config files are unsupported"
            )
    pytest_table = mapping(tool.get("pytest", {}), "tool.pytest")
    pytest_config = mapping(pytest_table.get("ini_options", {}), "pytest ini_options")
    addopts = pytest_config.get("addopts", [])
    if isinstance(addopts, str):
        try:
            addopts = shlex.split(addopts)
        except ValueError as exc:
            raise DeclarationError("invalid pytest addopts quoting") from exc
    addopts = sequence(addopts, "pytest addopts")
    patterns = pytest_config.get("python_files", ["test_*.py", "*_test.py"])
    if isinstance(patterns, str):
        patterns = patterns.split()
    patterns = sequence(patterns, "python_files")
    inventory = set()
    for name in sequence(spec.get("test_roots", ["."]), "test_roots"):
        path = root / relative(name)
        if not path.is_dir() or not path.resolve().is_relative_to(root) or path.is_symlink():
            raise DeclarationError("test_roots must name existing project directories")
        for candidate in walk_bounded(root, path):
            if candidate.is_dir():
                continue
            if any(fnmatch.fnmatchcase(candidate.name, pattern) for pattern in patterns):
                if not candidate.resolve().is_relative_to(root):
                    raise DeclarationError("test inventory escapes project root")
                inventory.add(candidate.relative_to(root).as_posix())
                if len(inventory) > 4096:
                    raise DeclarationError("test inventory exceeds 4096 files")
    units = []
    covered = set()
    for name, item in sorted(targets.items()):
        item = mapping(item, "target", {"tests", "inputs", "bundles", "absent", "cache"})
        tests = []
        seen_tests = set()
        for pattern in sequence(item.get("tests"), "target tests"):
            for path in expand(root, [pattern], "target tests"):
                if path.endswith(".py") and not (root / path).is_dir() and path not in seen_tests:
                    seen_tests.add(path)
                    tests.append(path)
        if not tests:
            raise DeclarationError("target tests must not be empty")
        if covered.intersection(tests):
            raise DeclarationError("test files cannot belong to multiple execution targets")
        covered.update(tests)
        inputs = (
            set(expand(root, item.get("inputs", []), "target inputs")) | set(tests) | config_files
        )
        absent = set(expand(root, item.get("absent", []), "target absence", missing=True))
        for dependency in sequence(item.get("bundles", []), "target bundles"):
            child_inputs, child_absent = bundle(dependency)
            inputs.update(child_inputs)
            absent.update(child_absent)
        if len(inputs) + len(absent) > 4096:
            raise DeclarationError("effective target exceeds 4096 paths")
        if not isinstance(item.get("cache", True), bool):
            raise DeclarationError("target cache must be a boolean")
        for test in tests:
            for parent in (root / test).parents:
                if not parent.is_relative_to(root):
                    break
                conftest = parent / "conftest.py"
                if conftest.is_file() and conftest.relative_to(root).as_posix() not in inputs:
                    raise DeclarationError("declare every applicable conftest.py before execution")
        for path in inputs:
            candidate = root / path
            if candidate.is_symlink():
                target = candidate.resolve().relative_to(root).as_posix()
                if target not in inputs and target not in absent:
                    raise DeclarationError(
                        "declare a symlink's target or its expected absence explicitly"
                    )
        units.append(Unit(name, tests, sorted(inputs), sorted(absent), item.get("cache", True)))
    if inventory - covered:
        raise DeclarationError("undeclared test files found; add them to an execution target")
    env = sequence(spec.get("env", []), "env")
    forbidden = {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    }
    if any(
        not name.isidentifier() or name in forbidden or name.startswith("TELESCOPE_")
        for name in env
    ):
        raise DeclarationError(
            "environment names must not override the controlled Python/pytest runtime"
        )
    return Manifest(root, raw, units, sorted(set(env)), sorted(inventory), addopts)


def snapshot(manifest, unit, destination):
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    records = []
    total = 0
    for name in unit.inputs:
        source = manifest.root / name
        target = destination / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for parent in [target.parent, *target.parent.parents]:
                if parent.is_relative_to(destination):
                    parent.chmod(0o755)
            with parent_fd(manifest.root, name) as (directory, leaf):
                before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode):
                    text = os.readlink(leaf, dir_fd=directory)
                    if Path(text).is_absolute():
                        raise InfrastructureError("leaf symlink changed to an absolute target")
                    resolved = source.resolve(strict=False)
                    if not resolved.is_relative_to(manifest.root) or resolved.relative_to(
                        manifest.root
                    ).as_posix() not in {*unit.inputs, *unit.absent}:
                        raise InfrastructureError("symlink target changed outside its declaration")
                    target.symlink_to(text)
                    after = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    if (before.st_ino, before.st_mtime_ns, text) != (
                        after.st_ino,
                        after.st_mtime_ns,
                        os.readlink(leaf, dir_fd=directory),
                    ):
                        raise InfrastructureError("symlink changed during snapshot")
                    record = {"path": name, "kind": "symlink", "target": text}
                elif stat.S_ISDIR(before.st_mode):
                    target.mkdir(exist_ok=True)
                    target.chmod(0o755)
                    record = {"path": name, "kind": "directory", "mode": 0o755}
                elif stat.S_ISREG(before.st_mode):
                    content, measured = regular_bytes(manifest.root, name, 67_108_864)
                    total += len(content)
                    if total > 268_435_456:
                        raise DeclarationError("snapshot exceeds 256 MiB")
                    target.write_bytes(content)
                    target.chmod(0o444 | (stat.S_IMODE(measured.st_mode) & 0o111))
                    record = {
                        "path": name,
                        "kind": "file",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "mode": 0o444 | (stat.S_IMODE(measured.st_mode) & 0o111),
                    }
                else:
                    raise DeclarationError("input changed to an unsupported file type")
            records.append(record)
        except OSError as exc:
            raise InfrastructureError(
                "cannot freeze declared input without following replaced paths"
            ) from exc
    for path in [*destination.rglob("*"), destination]:
        os.utime(path, ns=(0, 0), follow_symlinks=False)
    if (destination / "pyproject.toml").read_bytes() != manifest.raw:
        raise InfrastructureError("manifest changed while freezing inputs")
    for name in unit.absent:
        if os.path.lexists(manifest.root / name):
            raise InfrastructureError("expected-absent input appeared during snapshot")
        records.append({"path": name, "kind": "absent"})
    return records


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
