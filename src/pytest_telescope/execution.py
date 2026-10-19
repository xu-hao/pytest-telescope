"""Explicit worker envelopes and independent pytest suite configuration."""

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

DEFAULT_WORKER = dict(cpus=1, memory_bytes=536870912, processes=128, scratch_bytes=67108864)
MAX_SUITES = 64
MAX_WORKERS = 32


@dataclass(frozen=True)
class Envelope:
    worker: dict
    budget: dict
    concurrency: int


def envelope(config, override=None):
    config = mapping(config, "execution", {"worker", "budget"})

    def dimensions(value, label):
        value = mapping(value, label, set(DEFAULT_WORKER))
        if set(value) != set(DEFAULT_WORKER) or any(
            type(n) is not int or n <= 0 for n in value.values()
        ):
            raise DeclarationError(
                label + " requires positive integer cpus, memory_bytes, processes and scratch_bytes"
            )
        if (
            value["cpus"] > 1024
            or value["memory_bytes"] > 2**50
            or value["processes"] > 1_000_000
            or value["scratch_bytes"] > 2**50
        ):
            raise DeclarationError(label + " exceeds supported resource dimensions")
        return dict(value)

    worker = dimensions(config.get("worker", DEFAULT_WORKER), "execution.worker")
    budget = dimensions(
        override if override is not None else config.get("budget", worker), "execution.budget"
    )
    count = min(MAX_WORKERS, *(budget[key] // worker[key] for key in worker))
    if count < 1:
        raise DeclarationError("aggregate budget cannot fit one worker in every dimension")
    return Envelope(worker, budget, count)


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

    def identity(self):
        return {
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
                or (root / path).is_symlink()
                or not (root / path).resolve().is_relative_to(root)
            ):
                raise DeclarationError("suite root/cwd must be existing project directories")
        for capability in sequence(entry.get("capabilities", []), "capabilities"):
            if capability not in {"python", "project:readonly", "scratch:noexec", "network:none"}:
                raise UnsupportedCapability(
                    capability, "runtime profile does not qualify requested capability"
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
