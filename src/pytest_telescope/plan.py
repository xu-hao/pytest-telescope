"""Static declaration validation; no execution or security boundary."""

import json
import tomllib
from pathlib import Path
from typing import Any


class PlanError(ValueError):
    """A declaration cannot describe a supported static plan."""


def table(value: Any, label: str, fields: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be a table")
    if fields is not None and set(value) - fields:
        raise PlanError(f"{label} contains unsupported fields")
    return value


def strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise PlanError(f"{label} must be a list of nonempty strings")
    return value


def make_plan(root: Path) -> dict:
    root = root.resolve()
    manifest = root / "pyproject.toml"
    max_bytes = 1_048_576
    try:
        if not manifest.resolve().is_relative_to(root):
            raise PlanError("manifest must remain inside the project root")
        with manifest.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise PlanError("manifest exceeds the 1 MiB limit")
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise PlanError("cannot read a valid pyproject.toml in the current directory") from exc
    tool = table(document.get("tool", {}), "tool")
    config = table(tool.get("pytest_telescope"), "tool.pytest_telescope", {"bundles", "targets"})
    bundles = table(config.get("bundles", {}), "bundles")
    targets = table(config.get("targets"), "targets")
    if len(bundles) + len(targets) > 256:
        raise PlanError("at most 256 targets and bundles are supported")
    if any(not name or len(name) > 128 for name in (*bundles, *targets)):
        raise PlanError("target and bundle names require 1 to 128 characters")
    if not targets:
        raise PlanError("targets must contain at least one target")

    entry_count = 0

    def entries(value: Any, label: str) -> list[str]:
        nonlocal entry_count
        result = strings(value, label)
        entry_count += len(result)
        if entry_count > 4096:
            raise PlanError("at most 4096 declaration list entries are supported")
        return result

    def paths(value: Any, label: str) -> set[str]:
        result = set()
        for name in entries(value, label):
            path = Path(name)
            if (
                len(name) > 512
                or "\x00" in name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or any(char in name for char in "*?[]")
                or not path.parts
            ):
                raise PlanError(f"{label} requires explicit project-relative file paths")
            candidate = root / path
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise PlanError(f"{label} contains a missing or invalid file") from exc
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise PlanError(f"{label} must name files inside the project root")
            result.add(path.as_posix())
        return result

    resolved_bundles: dict[str, set[str]] = {}
    visiting: set[str] = set()
    bundle_depths: dict[str, int] = {}

    def bundle(name: str) -> set[str]:
        if name not in bundles:
            raise PlanError("unknown dependency bundle")
        if name in visiting:
            raise PlanError("dependency bundle cycle")
        if name not in resolved_bundles:
            if len(visiting) >= 32:
                raise PlanError("bundle nesting exceeds 32 levels")
            visiting.add(name)
            spec = table(bundles[name], "bundle", {"inputs", "bundles"})
            inputs = paths(spec.get("inputs", []), "bundle inputs")
            depth = 1
            for dependency in entries(spec.get("bundles", []), "bundle references"):
                inputs.update(bundle(dependency))
                depth = max(depth, 1 + bundle_depths[dependency])
            if depth > 32:
                raise PlanError("bundle nesting exceeds 32 levels")
            bundle_depths[name] = depth
            visiting.remove(name)
            resolved_bundles[name] = inputs
        return resolved_bundles[name]

    for name in sorted(bundles):
        bundle(name)
    units = []
    output_bytes = 0
    for name in sorted(targets):
        if not name:
            raise PlanError("target names must be nonempty")
        spec = table(targets[name], "target", {"tests", "inputs", "bundles"})
        tests = paths(spec.get("tests"), "target tests")
        if not tests:
            raise PlanError("target tests must not be empty")
        inputs = paths(spec.get("inputs", []), "target inputs") | tests | {"pyproject.toml"}
        for dependency in entries(spec.get("bundles", []), "target bundles"):
            inputs.update(bundle(dependency))
        unit = {"name": name, "tests": sorted(tests), "inputs": sorted(inputs)}
        output_bytes += len(json.dumps(unit, indent=2).encode("utf-8"))
        if output_bytes > max_bytes:
            raise PlanError("plan exceeds the 1 MiB output limit")
        units.append(unit)
    plan = {"mode": "plan-only", "targets": units}
    if len(json.dumps(plan, indent=2, sort_keys=True).encode("utf-8")) > max_bytes:
        raise PlanError("plan exceeds the 1 MiB output limit")
    return plan
