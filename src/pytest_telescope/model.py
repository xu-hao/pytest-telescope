"""Pre-import declarations and immutable project snapshots."""

import fnmatch
import glob
import hashlib
import io
import json
import os
import shlex
import stat
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path


class DeclarationError(ValueError):
    pass


class InfrastructureError(RuntimeError):
    pass


MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def render_json(value):
    """Encode bounded public output completely before writing any bytes."""
    output = io.StringIO()
    size = 0
    for chunk in json.JSONEncoder(sort_keys=True, indent=2, allow_nan=False).iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > MAX_OUTPUT_BYTES:
            raise InfrastructureError("structured result exceeds the 64 MiB output limit")
        output.write(chunk)
    return output.getvalue()


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
    registry: list[dict]
    declarations: dict[str, Unit]
    defaults: Unit
    broad_inputs: list[str]
    source_hashes: dict[str, str]

    @cached_property
    def registry_by_id(self):
        return {row["id"]: row for row in self.registry}


def sequence(value, label):
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in value)
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
    if len(name) > 4096 or len(path.parts) > 64:
        raise DeclarationError("paths exceed 4096 characters or 64 levels")
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


MAX_VISITS = 200_000

EXCLUDED = {
    ".git",
    ".hg",
    ".svn",
    ".pytest-telescope",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
}


def excluded_path(name, excluded=()):
    return bool(set(Path(name).parts) & EXCLUDED) or any(
        name == path or name.startswith(path + "/") for path in excluded
    )


def walk_bounded(root, start, *, prune=True, include_hidden=False, excluded=(), budget=None):
    pending = [start]
    if budget is None:
        budget = [0]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                budget[0] += 1
                if budget[0] > MAX_VISITS:
                    raise DeclarationError("filesystem discovery exceeds 200000 entries")
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                relative(name)
                if excluded_path(name, excluded):
                    continue
                if entry.is_dir(follow_symlinks=False) and os.path.lexists(path / "pyvenv.cfg"):
                    regular_bytes(root, name + "/pyvenv.cfg", 65536)
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


def expand(root, names, label, *, missing=False, excluded=(), budget=None):
    found = set()
    if budget is None:
        budget = [0]
    for name in sequence(names, label):
        name = relative(name)
        if excluded_path(name, excluded):
            raise DeclarationError("metadata, cache and host environments are not project inputs")
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
                    include_hidden=True,
                    excluded=excluded,
                    budget=budget,
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
                walk_bounded(root, candidate, prune=False, excluded=excluded, budget=budget)
                if candidate.is_dir() and not candidate.is_symlink()
                else ()
            )
            for selected in _with_first(candidate, paths):
                rel = selected.relative_to(root).as_posix()
                if excluded_path(rel, excluded):
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
                if len(found) > 100_000:
                    raise DeclarationError("expanded declarations exceed 100000 paths")
    return sorted(found)


def _with_first(first, rest):
    yield first
    yield from rest


def load(root, *, cache_dir=None):
    from .markers import MAX_MARKERS, MAX_TOTAL_NODES, MAX_TOTAL_SOURCE, MarkerError, discover

    root = Path(root).resolve()
    excluded = set()
    budget = [0]
    if cache_dir is not None:
        cache = Path(cache_dir).resolve()
        if cache == root:
            raise DeclarationError("cache directory cannot be the project root")
        if cache.is_relative_to(root):
            excluded.add(cache.relative_to(root).as_posix())
    try:
        raw, _ = regular_bytes(root, "pyproject.toml", 1_048_576)
        data = tomllib.loads(raw.decode())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise DeclarationError("cannot read valid project pyproject.toml") from exc
    tool = mapping(data.get("tool", {}), "tool")
    spec = mapping(tool.get("pytest_telescope", {}), "pytest_telescope")
    if "targets" in spec:
        raise DeclarationError(
            "central targets are unsupported; migrate inputs to literal pytest.mark.telescope markers"
        )
    mapping(spec, "pytest_telescope", {"defaults", "bundles", "test_roots", "env"})
    bundles = mapping(spec.get("bundles", {}), "bundles")
    if len(bundles) > 256 or any(not name or len(name) > 128 for name in bundles):
        raise DeclarationError("declare at most 256 bundles with names of 1 to 128 characters")
    resolved, active, depths = {}, set(), {}
    expansions = {}

    def expanded(names, label, *, missing=False):
        names = sequence(names, label)
        key = (tuple(names), missing)
        if key not in expansions:
            expansions[key] = expand(
                root, names, label, missing=missing, excluded=excluded, budget=budget
            )
        return expansions[key]

    def declaration(item, label):
        item = mapping(item, label, {"inputs", "bundles", "absent", "cache"})
        inputs = set(expanded(item.get("inputs", []), label + " inputs"))
        absent = set(expanded(item.get("absent", []), label + " absence", missing=True))
        for dependency in sequence(item.get("bundles", []), "bundle references"):
            child = bundle(dependency)
            inputs.update(child.inputs)
            absent.update(child.absent)
        if type(item.get("cache", True)) is not bool:
            raise DeclarationError("cache must be a boolean")
        if len(inputs) + len(absent) > 100_000:
            raise DeclarationError("effective declaration exceeds 100000 paths")
        return Unit(label, [], sorted(inputs), sorted(absent), item.get("cache", True))

    def bundle(name):
        if name not in bundles:
            raise DeclarationError("unknown dependency bundle")
        if name in active:
            raise DeclarationError("dependency bundle cycle")
        if len(active) >= 32:
            raise DeclarationError("bundle nesting exceeds 32 levels")
        if name not in resolved:
            active.add(name)
            mapping(bundles[name], "bundle", {"inputs", "bundles", "absent"})
            resolved[name] = declaration(bundles[name], "bundle " + name)
            depth = 1 + max((depths[dep] for dep in bundles[name].get("bundles", [])), default=0)
            if depth > 32:
                raise DeclarationError("bundle nesting exceeds 32 levels")
            depths[name] = depth
            active.remove(name)
        return resolved[name]

    for name in bundles:
        bundle(name)
    defaults = declaration(spec.get("defaults", {}), "defaults")
    for name in ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"):
        if os.path.lexists(root / name):
            raise DeclarationError(
                "move pytest configuration into pyproject.toml; other pytest config files are unsupported"
            )
    pytest_config = mapping(
        mapping(tool.get("pytest", {}), "tool.pytest").get("ini_options", {}), "pytest ini_options"
    )
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
        for candidate in walk_bounded(root, path, excluded=excluded, budget=budget):
            if candidate.is_dir():
                continue
            if any(fnmatch.fnmatchcase(candidate.name, pattern) for pattern in patterns):
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                    raise DeclarationError("test source must be a regular project file")
                inventory.add(candidate.relative_to(root).as_posix())
                if len(inventory) > 100_000:
                    raise DeclarationError("test inventory exceeds 100000 files")
    registry, declarations, hashes = [], {}, {"pyproject.toml": hashlib.sha256(raw).hexdigest()}
    total_bytes = total_nodes = 0
    broad_possible = False
    for filename in sorted(inventory):
        content, _ = regular_bytes(root, filename, 4 * 1024 * 1024)
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_SOURCE:
            raise DeclarationError("test source exceeds the 64 MiB discovery budget")
        try:
            found, nodes = discover(content, filename)
        except MarkerError as exc:
            raise DeclarationError(str(exc)) from exc
        total_nodes += nodes
        if total_nodes > MAX_TOTAL_NODES or len(registry) + len(found) > MAX_MARKERS:
            raise DeclarationError("test declaration syntax exceeds total node/marker limits")
        hashes[filename] = hashlib.sha256(content).hexdigest()
        registry.extend(found)
        if not any(
            row["kind"] == "scope"
            and not row["owner"]
            and {"inputs", "bundles"}.intersection(row["kwargs"])
            for row in found
        ):
            broad_possible = True
        for row in found:
            declarations[row["id"]] = declaration(row["kwargs"], row["id"])
    broad = project_paths(root, excluded=excluded, budget=budget) if broad_possible else []
    inputs = set(broad) | set(inventory) | {"pyproject.toml"} | set(defaults.inputs)
    absent = set(defaults.absent)
    cache = defaults.cache
    for unit in declarations.values():
        inputs.update(unit.inputs)
        absent.update(unit.absent)
        cache = cache and unit.cache
    collection = Unit("collection", sorted(inventory), sorted(inputs), sorted(absent), cache)
    validate_scope(root, collection)
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
    return Manifest(
        root,
        raw,
        [collection],
        sorted(set(env)),
        sorted(inventory),
        addopts,
        registry,
        declarations,
        defaults,
        broad,
        hashes,
    )


def validate_scope(root, unit):
    inputs, absent = set(unit.inputs), set(unit.absent)
    for test in unit.tests:
        for parent in (root / test).parents:
            if not parent.is_relative_to(root):
                break
            conftest = parent / "conftest.py"
            if conftest.is_file() and conftest.relative_to(root).as_posix() not in inputs:
                raise DeclarationError("declare every applicable conftest.py before execution")
    for name in inputs:
        path = root / name
        if path.is_symlink():
            target = path.resolve().relative_to(root).as_posix()
            if target not in inputs and target not in absent:
                raise DeclarationError(
                    "declare a symlink's target or its expected absence explicitly"
                )


def case_unit(manifest, item):
    origins = item["origins"]
    registry = manifest.registry_by_id
    if any(
        origin not in registry or registry[origin]["file"] != item["file"] for origin in origins
    ):
        raise InfrastructureError("collected marker origin disagrees with static declarations")
    narrow = any(
        {"inputs", "bundles"}.intersection(registry[origin]["kwargs"]) for origin in origins
    )
    if not narrow and not manifest.broad_inputs:
        raise DeclarationError("collected item invalidates the module's declared narrow scope")
    inputs = set(manifest.defaults.inputs) | {item["file"], "pyproject.toml"}
    absent = set(manifest.defaults.absent)
    cache = manifest.defaults.cache
    for origin in origins:
        declaration = manifest.declarations[origin]
        inputs.update(declaration.inputs)
        absent.update(declaration.absent)
        cache = cache and declaration.cache
    effective_inputs = sorted(inputs) if narrow else manifest.broad_inputs
    unit = Unit(
        hashlib.sha256(item["nodeid"].encode()).hexdigest(),
        [item["file"]],
        effective_inputs,
        sorted(absent),
        cache,
    )
    if narrow:
        validate_scope(manifest.root, unit)
    return unit


def project_paths(root, *, excluded=(), budget=None):
    paths = []
    for path in walk_bounded(root, root, excluded=excluded, budget=budget):
        if path.is_symlink():
            if (
                path.is_dir()
                or Path(os.readlink(path)).is_absolute()
                or not path.resolve().is_relative_to(root)
            ):
                raise DeclarationError("broad project scope contains an unsupported symlink")
        elif not path.is_file() and not path.is_dir():
            raise DeclarationError("broad project scope contains a special file")
        paths.append(path.relative_to(root).as_posix())
        if len(paths) > 100_000:
            raise DeclarationError("broad project scope exceeds 100000 paths")
    return sorted(paths)


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
                    if total > 2_147_483_648:
                        raise DeclarationError("snapshot exceeds 2 GiB")
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
    for path in _with_first(destination, walk_bounded(destination, destination)):
        os.utime(path, ns=(0, 0), follow_symlinks=False)
    if (destination / "pyproject.toml").read_bytes() != manifest.raw:
        raise InfrastructureError("manifest changed while freezing inputs")
    for name in unit.absent:
        if os.path.lexists(manifest.root / name):
            raise InfrastructureError("expected-absent input appeared during snapshot")
        records.append({"path": name, "kind": "absent"})
    measured_hashes = {
        record["path"]: record["sha256"] for record in records if record["kind"] == "file"
    }
    for name, expected in manifest.source_hashes.items():
        if name in unit.inputs and measured_hashes.get(name) != expected:
            raise InfrastructureError("test declarations changed while freezing inputs")
    return records


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def materialize(pool, unit, destination, records):
    """Build a narrow view of already-frozen immutable bytes, without host reads."""
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    selected = set(unit.inputs)
    for name in unit.inputs:
        record = records[name]
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in [target.parent, *target.parent.parents]:
            if parent.is_relative_to(destination):
                parent.chmod(0o755)
        if record["kind"] == "file":
            os.link(pool / name, target, follow_symlinks=False)
        elif record["kind"] == "symlink":
            target.symlink_to(record["target"])
        elif record["kind"] == "directory":
            target.mkdir(exist_ok=True)
            target.chmod(0o755)
        else:
            raise InfrastructureError("frozen scope contains an invalid file record")
    for path in _with_first(destination, walk_bounded(destination, destination)):
        if path.is_dir() or path.is_symlink():
            os.utime(path, ns=(0, 0), follow_symlinks=False)
    if any(name not in records for name in selected):
        raise InfrastructureError("case scope was not part of the frozen discovery union")
