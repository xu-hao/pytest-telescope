"""Execution settings and independent pytest suite configuration."""

import configparser
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .model import (
    DeclarationError,
    UnsupportedCapability,
    excluded_namespace,
    mapping,
    regular_bytes,
    relative,
    sequence,
)

MAX_SUITES = 64
MAX_WORKERS = 32
REMOVED_EXECUTION = {
    "worker": "per-worker resource limits were removed; limit resources in the job that "
    "runs pytest and set execution.workers for parallel sandboxes",
    "budget": "aggregate resource budgets were removed; limit resources in the job that "
    "runs pytest and set execution.workers for parallel sandboxes",
}


def execution_config(config):
    config = mapping(config, "execution")
    for name, message in REMOVED_EXECUTION.items():
        if name in config:
            raise DeclarationError(message)
    return mapping(config, "execution", {"mode", "workers"})


def worker_count(config, override=None):
    """Concurrent scoped sandboxes; a count, never a resource budget."""
    config = execution_config(config)
    execution_mode(config)
    count = config.get("workers", 1) if override is None else override
    if type(count) is not int or not 1 <= count <= MAX_WORKERS:
        raise DeclarationError(f"workers must be an integer from 1 to {MAX_WORKERS}")
    return count


def execution_mode(config, override=None):
    mode = config.get("mode", "case") if override is None else override
    if type(mode) is not str or mode not in {"case", "batch"}:
        raise DeclarationError("execution mode must be case or batch")
    return mode


@dataclass
class Suite:
    name: str
    root: str
    cwd: str
    config: str
    test_roots: list
    include: list
    exclude: list
    args: list
    config_args: list
    plugins: list
    patterns: list
    raw: bytes
    inventory: list
    structural_files: list | None = None

    def identity(self):
        result = {
            key: getattr(self, key)
            for key in (
                "name",
                "root",
                "cwd",
                "config",
                "test_roots",
                "include",
                "exclude",
                "args",
                "config_args",
                "plugins",
            )
        }
        if self.structural_files is not None:
            result["structural_files"] = self.structural_files
        return result


def config_options(root, name):
    raw, _ = regular_bytes(root, name, 1048576)
    try:
        if name.endswith("pyproject.toml"):
            data = tomllib.loads(raw.decode())
            options = mapping(
                mapping(mapping(data.get("tool", {}), "tool").get("pytest", {}), "pytest").get(
                    "ini_options", {}
                ),
                "pytest ini_options",
            )
        elif Path(name).name in {"pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"}:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(raw.decode())
            section = "tool:pytest" if name.endswith("setup.cfg") else "pytest"
            if section not in parser:
                raise DeclarationError("suite configuration has no pytest section")
            options = dict(parser[section])
        else:
            raise UnsupportedCapability(
                "pytest:configuration", "unsupported pytest configuration format"
            )
        addopts = options.get("addopts", [])
        if isinstance(addopts, str):
            addopts = shlex.split(addopts)
        patterns = options.get("python_files", ["test_*.py", "*_test.py"])
        if isinstance(patterns, str):
            patterns = patterns.split()
        return raw, sequence(addopts, "pytest addopts"), sequence(patterns, "python_files")
    except (UnicodeError, ValueError, configparser.Error) as exc:
        if isinstance(exc, DeclarationError):
            raise
        raise DeclarationError("invalid suite pytest configuration") from exc


def suites(root, spec, selected=None):
    entries = spec.get("suites")
    if entries is None:
        for name in ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"):
            if (root / name).exists():
                raise DeclarationError(
                    "declare independent suites for non-pyproject pytest configuration"
                )
        entries = {
            "default": {
                "root": ".",
                "config": "pyproject.toml",
                "test_roots": spec.get("test_roots", ["."]),
            }
        }
    elif "test_roots" in spec:
        raise DeclarationError("explicit suites cannot also set top-level test_roots")
    entries = mapping(entries, "suites")
    if not entries or len(entries) > MAX_SUITES:
        raise DeclarationError("declare between 1 and 64 suites")
    if selected is not None:
        selected = set(sequence(list(selected), "suite selection"))
        if not selected or selected - entries.keys():
            raise DeclarationError("suite selection must name configured suites")
        entries = {name: entry for name, entry in entries.items() if name in selected}
    result = []
    for name, entry in sorted(entries.items()):
        if not name or len(name) > 128 or any(not (c.isalnum() or c in "_-") for c in name):
            raise DeclarationError(
                "suite names require 1 to 128 alphanumeric, underscore or hyphen characters"
            )
        entry = mapping(entry, "suite")
        if "python" in entry:
            raise DeclarationError(
                "suite python source lookup was removed; scoped tests use the invoking "
                "interpreter and child interpreters need an explicit PYTHONPATH"
            )
        entry = mapping(
            entry,
            "suite",
            {
                "root",
                "cwd",
                "config",
                "test_roots",
                "include",
                "exclude",
                "args",
                "plugins",
                "capabilities",
            },
        )
        sequence(
            [
                entry.get("root", "."),
                entry.get("cwd", entry.get("root", ".")),
                entry.get("config", "pyproject.toml"),
            ],
            "suite root/cwd/config",
        )
        directory, cwd, configuration = (
            relative(entry.get("root", ".")),
            relative(entry.get("cwd", entry.get("root", "."))),
            relative(entry.get("config", "pyproject.toml")),
        )
        if any(excluded_namespace(root, path) for path in (directory, cwd, configuration)):
            raise DeclarationError("suite root/cwd/config cannot expose excluded namespaces")
        for path in (directory, cwd):
            if (
                not (root / path).is_dir()
                or any(
                    (root / Path(*Path(path).parts[:depth])).is_symlink()
                    for depth in range(1, len(Path(path).parts) + 1)
                )
                or not (root / path).resolve().is_relative_to(root)
            ):
                raise DeclarationError("suite root/cwd must be existing project directories")
        for capability in sequence(entry.get("capabilities", []), "capabilities"):
            if capability not in {"python", "project:readonly"}:
                raise UnsupportedCapability(
                    capability,
                    "the Bubblewrap sandbox controls only the filesystem view and does not "
                    "provide this capability",
                )
        raw, args, patterns = config_options(root, configuration)
        roots = [
            relative(value)
            for value in sequence(entry.get("test_roots", [directory]), "suite test_roots")
        ]
        includes = [
            relative(value) for value in sequence(entry.get("include", ["**"]), "suite include")
        ]
        excludes = [
            relative(value) for value in sequence(entry.get("exclude", []), "suite exclude")
        ]
        plugins = sequence(entry.get("plugins", []), "suite plugins")
        if len(plugins) > 128 or len(set(plugins)) != len(plugins):
            raise DeclarationError("declare at most 128 distinct explicit plugins per suite")
        for plugin in plugins:
            if not all(part.isidentifier() for part in plugin.split(".")):
                raise DeclarationError("explicit plugins require Python module names")
            if plugin == "xdist" or plugin.startswith("xdist."):
                raise UnsupportedCapability(
                    "pytest:distributed-scheduling", "nested distributed scheduling is unsupported"
                )
        result.append(
            Suite(
                name,
                directory,
                cwd,
                configuration,
                roots,
                includes,
                excludes,
                sequence(entry.get("args", []), "suite args"),
                args,
                plugins,
                patterns,
                raw,
                [],
            )
        )
    return result


def effective_arguments(suite, caller):
    """Apply pytest's last addopts override before scheduler validation."""
    arguments = [*suite.args, *caller]
    clean, replacement, index = [], None, 0
    while index < len(arguments):
        arg = arguments[index]
        value, consumed = None, 1
        if arg in {"-o", "--override-ini"} and index + 1 < len(arguments):
            value, consumed = arguments[index + 1], 2
        elif arg.startswith("--override-ini="):
            value = arg.split("=", 1)[1]
        if value is not None and value.startswith("addopts="):
            try:
                replacement = shlex.split(value.split("=", 1)[1])
            except ValueError as exc:
                raise DeclarationError("invalid addopts override quoting") from exc
        else:
            clean.extend(arguments[index : index + consumed])
        index += consumed
    effective = [*(suite.config_args if replacement is None else replacement), *clean]
    for index, arg in enumerate(effective):
        if (
            arg in {"-n", "--numprocesses", "--dist", "--tx", "--rsyncdir", "--looponfail"}
            or arg.startswith(("--numprocesses=", "--dist=", "--tx=", "--rsyncdir="))
            or (arg.startswith("-n") and len(arg) > 2)
        ):
            raise UnsupportedCapability(
                "pytest:distributed-scheduling", "effective distributed scheduling is unsupported"
            )
        plugin = (
            effective[index + 1]
            if arg == "-p" and index + 1 < len(effective)
            else arg.removeprefix("-p")
            if arg.startswith("-p")
            else ""
        )
        if plugin == "xdist" or plugin.startswith("xdist."):
            raise UnsupportedCapability(
                "pytest:distributed-scheduling",
                "nested distributed scheduler plugin is unsupported",
            )
    return effective
