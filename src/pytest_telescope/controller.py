"""Complete-inventory, cache-first execution without importing project code."""

import argparse
import contextlib
import hashlib
import math
import re
from importlib.metadata import version

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None
import json
import os
import time
import uuid
from pathlib import Path

from . import runtime
from .model import DeclarationError, InfrastructureError, fingerprint, load, snapshot

SCHEMA = 1


@contextlib.contextmanager
def lock(path, timeout):
    if fcntl is None:
        raise InfrastructureError("execution requires Linux with file locking and Docker")
    with path.open("a+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise InfrastructureError("cache lock deadline exceeded")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_record(path, key):
    try:
        with path.open("rb") as stream:
            data = stream.read(1_048_577)
        if len(data) > 1_048_576:
            return None
        record = json.loads(data)
        result = record["result"]
        if (
            type(record["schema"]) is not int
            or record["schema"] != SCHEMA
            or record["key"] != key
            or record["checksum"] != fingerprint(result)
        ):
            return None
        from .result import validate_result

        if not validate_result(result, successful_only=True):
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def write_record(path, key, result):
    record = {"schema": SCHEMA, "key": key, "result": result, "checksum": fingerprint(result)}
    content = json.dumps(record, sort_keys=True).encode()
    if len(content) > 1_048_576:
        raise InfrastructureError("successful result exceeds record size limit")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def collect_cache(state):
    """Bound only owned record files while the stable cache lock is held."""
    records = []
    visited = 0
    with os.scandir(state) as entries:
        for entry in entries:
            visited += 1
            if visited > 8192:
                raise InfrastructureError("cache directory entry limit exceeded")
            if re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{32}\.tmp", entry.name):
                Path(entry.path).unlink(missing_ok=True)
            elif re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise InfrastructureError("cache records must be regular owned files")
                records.append((metadata.st_mtime_ns, metadata.st_size, Path(entry.path)))
    records.sort(key=lambda item: (item[0], item[2].name))
    total = sum(size for _, size, _ in records)
    remaining = len(records)
    for _, size, path in records:
        if remaining <= 1024 and total <= 67_108_864:
            break
        path.unlink(missing_ok=True)
        remaining -= 1
        total -= size


def runner_identity():
    digest = hashlib.sha256()
    for name in ("controller.py", "model.py", "runtime.py", "result.py", "worker.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    return {"version": version("pytest-telescope"), "implementation": digest.hexdigest()}


def selected_arguments(arguments, unit, all_tests):
    options, selectors = [], []
    values = {"-k", "-m", "-o", "--override-ini", "-p", "--maxfail", "--tb", "--capture", "--color"}
    switches = {
        "-x",
        "--exitfirst",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-s",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
        "--runxfail",
        "--showlocals",
        "--no-header",
        "--no-summary",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in values:
            if index + 1 == len(arguments):
                raise DeclarationError("pytest option requires a value")
            value = arguments[index + 1]
            if argument in {"-o", "--override-ini"} and value.split("=", 1)[0] in {
                "python_files",
                "testpaths",
                "addopts",
            }:
                raise DeclarationError(
                    "collection overrides belong in the declared pyproject configuration"
                )
            options.extend([argument, value])
            index += 2
        elif argument in switches or (
            argument.startswith("-") and len(argument) > 2 and set(argument[1:]) <= {"v", "q"}
        ):
            options.append(argument)
            index += 1
        elif any(argument.startswith(option + "=") for option in values if option.startswith("--")):
            option, value = argument.split("=", 1)
            if option == "--override-ini" and value.split("=", 1)[0] in {
                "python_files",
                "testpaths",
                "addopts",
            }:
                raise DeclarationError("collection overrides belong in declared configuration")
            options.append(argument)
            index += 1
        elif argument.startswith("-"):
            raise DeclarationError(
                "unsupported pytest option; use documented execution/selection flags"
            )
        else:
            filename = argument.split("::", 1)[0].removeprefix("./")
            if filename not in all_tests:
                raise DeclarationError(
                    "selectors must name declared test files (optional ::node selectors)"
                )
            selectors.append((filename, argument))
            index += 1
    tests = (
        [selector for filename, selector in selectors if filename in unit.tests]
        if selectors
        else unit.tests
    )
    return [*options, *tests] if tests else None


def run(
    root, *, force=False, no_cache=False, explain=False, cache_dir=None, timeout=60, arguments=()
):
    started = time.monotonic()
    if not math.isfinite(timeout) or timeout < 0.1 or timeout > 3600:
        raise DeclarationError("timeout must be between 0.1 and 3600 seconds")
    if fcntl is None or os.name != "posix":
        raise InfrastructureError("execution requires Linux with file locking and Docker")
    manifest = load(root)
    preflight_error = None
    for _ in range(2):
        try:
            runtime.recover(manifest.root)
            identity = runtime.identity(manifest.root)
            preflight_error = None
            break
        except InfrastructureError as exc:
            preflight_error = str(exc)
            if isinstance(exc, runtime.CleanupError):
                break
    state = Path(cache_dir).resolve() if cache_dir else manifest.root / ".pytest-telescope/cache"
    environment = {name: os.environ.get(name) for name in manifest.env}
    all_tests = {name for unit in manifest.units for name in unit.tests}
    effective_arguments = [*manifest.pytest_args, *arguments]
    results = []
    blocked = preflight_error is not None
    blocked_reason = preflight_error
    runner = runner_identity()
    setup_seconds = time.monotonic() - started
    for unit in manifest.units:
        if blocked:
            results.append(
                {
                    "name": unit.name,
                    "outcome": "infrastructure_error",
                    "reason": blocked_reason,
                    "attempts": 0,
                    "collected": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "worker_seconds": 0,
                    "cpu_seconds": 0,
                    "failures": [],
                }
            )
            continue
        args = selected_arguments(effective_arguments, unit, all_tests)
        if args is None:
            results.append(
                {
                    "name": unit.name,
                    "outcome": "not-selected",
                    "reason": "explicit selector belongs to another target",
                    "collected": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "worker_seconds": 0,
                    "cpu_seconds": 0,
                }
            )
            continue
        retry_errors = []
        for attempt in range(2):
            try:
                setup_start = time.monotonic()
                state.mkdir(parents=True, exist_ok=True, mode=0o700)
                with lock(state / ".lock", timeout * 2 + 30):
                    with runtime.invocation(manifest.root) as directory:
                        frozen = Path(directory) / "project"
                        records = snapshot(manifest, unit, frozen)
                        key = fingerprint(
                            {
                                "schema": SCHEMA,
                                "unit": unit.name,
                                "inputs": records,
                                "absent": unit.absent,
                                "manifest": fingerprint(manifest.raw.decode()),
                                "runtime": identity["execution"],
                                "runner": runner,
                                "environment": environment,
                                "pytest_args": args,
                                "all_args": effective_arguments,
                                "inventory": manifest.inventory,
                                "snapshot_owner": [os.getuid(), os.getgid()],
                                "policy": runtime.POLICY,
                            }
                        )
                        setup_seconds += time.monotonic() - setup_start
                        record_path = state / (key + ".json")
                        enabled = unit.cache and not no_cache
                        collect_cache(state)
                        previous = read_record(record_path, key) if enabled and not force else None
                        if previous is not None:
                            result = {**previous, "worker_seconds": 0, "cpu_seconds": 0}
                            outcome, reason = (
                                "reused",
                                "complete successful record matches exact inputs, runtime and arguments",
                            )
                        else:
                            reason = (
                                "forced"
                                if force
                                else "non-cacheable"
                                if not enabled
                                else "no valid successful record for this identity"
                            )
                            result = runtime.execute(
                                identity["image"],
                                frozen,
                                ["-o", "addopts=", "-p", "no:cacheprovider", *args],
                                environment,
                                timeout,
                                root=manifest.root,
                            )
                            outcome = (
                                "executed"
                                if result["exit_code"] == 0
                                else "not-selected"
                                if result["exit_code"] == 5
                                else "failed"
                            )
                    if previous is None and enabled and result["exit_code"] == 0:
                        write_record(record_path, key, result)
                        collect_cache(state)
                    elif previous is None and enabled:
                        record_path.unlink(missing_ok=True)
                    results.append(
                        {
                            "name": unit.name,
                            "outcome": outcome,
                            "reason": reason if explain else "",
                            **{
                                name: result[name]
                                for name in (
                                    "collected",
                                    "passed",
                                    "failed",
                                    "skipped",
                                    "worker_seconds",
                                    "cpu_seconds",
                                )
                            },
                            "attempts": attempt + 1,
                            "failures": result["failures"],
                        }
                    )
                break
            except (InfrastructureError, OSError) as exc:
                retry_errors.append(
                    str(exc)
                    if isinstance(exc, InfrastructureError)
                    else "owned storage operation failed"
                )
                blocked = isinstance(exc, runtime.CleanupError)
                blocked_reason = "run stopped after owned-resource cleanup failure"
                if attempt == 1 or blocked:
                    results.append(
                        {
                            "name": unit.name,
                            "outcome": "infrastructure_error",
                            "reason": retry_errors[-1],
                            "attempts": attempt + 1,
                            "collected": 0,
                            "passed": 0,
                            "failed": 0,
                            "skipped": 0,
                            "worker_seconds": 0,
                            "cpu_seconds": 0,
                        }
                    )
                    break
    counts = {
        name: sum(unit["outcome"] == name for unit in results)
        for name in ("executed", "reused", "failed", "infrastructure_error", "not-selected")
    }
    cases = {
        name: sum(unit[name] for unit in results)
        for name in ("collected", "passed", "failed", "skipped")
    }
    status = (
        "infrastructure_error"
        if counts["infrastructure_error"]
        else "failed"
        if counts["failed"]
        else "passed"
    )
    if status == "passed" and cases["collected"] == 0:
        status = "no_tests"
    return {
        "status": status,
        "counts": counts,
        "cases": cases,
        "case_outcomes": {
            "fresh_passed": sum(
                unit["passed"] for unit in results if unit["outcome"] == "executed"
            ),
            "cached_passed": sum(unit["passed"] for unit in results if unit["outcome"] == "reused"),
            "fresh_skipped": sum(
                unit["skipped"] for unit in results if unit["outcome"] == "executed"
            ),
            "cached_skipped": sum(
                unit["skipped"] for unit in results if unit["outcome"] == "reused"
            ),
        },
        "units": results,
        "elapsed_seconds": time.monotonic() - started,
        "setup_seconds": setup_seconds,
        "worker_cpu_seconds": sum(unit["cpu_seconds"] for unit in results),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pytest-telescope")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preparation = subparsers.add_parser("runtime", help="build the clean Docker runtime")
    preparation.add_argument("--requirements", type=Path)
    subparsers.add_parser("plan", help="validate complete inventory and print target inputs")
    execution = subparsers.add_parser("run", help="execute or reuse every selected target")
    execution.add_argument("--force", action="store_true")
    execution.add_argument("--no-cache", action="store_true")
    execution.add_argument("--explain", action="store_true")
    execution.add_argument("--cache-dir", type=Path)
    execution.add_argument("--timeout", type=float, default=60)
    execution.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if options.command == "runtime":
            result = runtime.build_runtime(Path.cwd(), options.requirements)
        elif options.command == "plan":
            from .plan import make_plan

            result = make_plan(Path.cwd())
        else:
            arguments = (
                options.arguments[1:] if options.arguments[:1] == ["--"] else options.arguments
            )
            result = run(
                Path.cwd(),
                force=options.force,
                no_cache=options.no_cache,
                explain=options.explain,
                cache_dir=options.cache_dir,
                timeout=options.timeout,
                arguments=arguments,
            )
        print(json.dumps(result, sort_keys=True, indent=2))
        if result.get("status") == "infrastructure_error":
            return 3
        return (
            5
            if result.get("status") == "no_tests"
            else 1
            if result.get("status") == "failed"
            else 0
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "infrastructure_error", "error": "invocation interrupted"}))
        return 3
    except DeclarationError as exc:
        print(json.dumps({"status": "declaration_error", "error": str(exc)}))
        return 4
    except InfrastructureError as exc:
        print(json.dumps({"status": "infrastructure_error", "error": str(exc)}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
