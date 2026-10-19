"""Complete-inventory, cache-first execution without importing project code."""

import argparse
import contextlib
import hashlib
import heapq
import math
import re
import stat
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
from .model import (
    DeclarationError,
    InfrastructureError,
    case_unit,
    fingerprint,
    load,
    materialize,
    render_json,
    snapshot,
)

SCHEMA = 2


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


def read_record(path, key, *, kind="execution", case_id=None):
    limit = 64 * 1024 * 1024 if kind == "collection" else 1_048_576
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(limit + 1)
        if len(data) > limit:
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
        from .result import validate_collection, validate_result

        valid = (
            validate_collection(result, successful_only=True)
            if kind == "collection"
            else validate_result(result, successful_only=True, case_id=case_id)
        )
        if not valid:
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def write_record(path, key, result, *, kind="execution"):
    limit = 64 * 1024 * 1024 if kind == "collection" else 1_048_576
    record = {"schema": SCHEMA, "key": key, "result": result, "checksum": fingerprint(result)}
    content = json.dumps(record, sort_keys=True).encode()
    if len(content) > limit:
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


class CacheStore:
    """One bounded scan per invocation, then incremental owned-record accounting."""

    def __init__(self, state, *, count=100_000, size=256 * 1024 * 1024):
        self.state = state
        self.count, self.size = count, size
        self.records, self.heap, self.total = {}, [], 0
        with os.scandir(state) as entries:
            for visited, entry in enumerate(entries, 1):
                if visited > 200_000:
                    raise InfrastructureError("cache directory entry limit exceeded")
                if re.fullmatch(
                    r"[0-9a-f]{64}\.[0-9a-f]{32}\.(?:tmp|pending)(?:\.[0-9a-f]{32}\.tmp)?",
                    entry.name,
                ):
                    Path(entry.path).unlink(missing_ok=True)
                elif re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                    self.add(Path(entry.path), trim=False)
        self.trim()

    def add(self, path, *, trim=True):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise InfrastructureError("cache records must be regular owned files")
        previous = self.records.get(path)
        if previous is not None:
            self.total -= previous[1]
        self.records[path] = (metadata.st_mtime_ns, metadata.st_size)
        self.total += metadata.st_size
        heapq.heappush(self.heap, (metadata.st_mtime_ns, path))
        if trim:
            self.trim()

    def trim(self):
        while len(self.records) > self.count or self.total > self.size:
            timestamp, path = heapq.heappop(self.heap)
            current = self.records.get(path)
            if current is None or current[0] != timestamp:
                continue
            path.unlink(missing_ok=True)
            self.total -= current[1]
            del self.records[path]


def collect_cache(state):
    return CacheStore(state)


class PendingRecords:
    """Keep new records ineligible until the complete frozen-pool invocation cleans."""

    def __init__(self, execution_store, inventory_store):
        self.stores = {"execution": execution_store, "collection": inventory_store}
        self.paths = {}
        self.queues = {"execution": [], "collection": []}
        self.sizes = {"execution": 0, "collection": 0}
        self.counts = {"execution": 0, "collection": 0}
        self.sequence = 0
        self.token = uuid.uuid4().hex

    def __enter__(self):
        return self

    def stage(self, final, key, result, *, kind="execution"):
        path = final.with_name(key + "." + self.token + ".pending")
        write_record(path, key, result, kind=kind)
        size = path.stat().st_size
        self.paths[path] = (final, kind, size)
        self.sequence += 1
        heapq.heappush(self.queues[kind], (self.sequence, path))
        self.sizes[kind] += size
        self.counts[kind] += 1
        store = self.stores[kind]
        while self.sizes[kind] > store.size or self.counts[kind] > store.count:
            _, old = heapq.heappop(self.queues[kind])
            _, _, removed = self.paths.pop(old)
            old.unlink()
            self.sizes[kind] -= removed
            self.counts[kind] -= 1

    def publish(self):
        while self.paths:
            path = next(iter(self.paths))
            final, kind, _ = self.paths[path]
            os.replace(path, final)
            del self.paths[path]
            self.stores[kind].add(final)
        for store in self.stores.values():
            descriptor = os.open(store.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def __exit__(self, *exc):
        try:
            for path in self.paths:
                path.unlink(missing_ok=True)
        except OSError as error:
            raise runtime.CleanupError(
                "pending result cleanup failed; no new records were eligible"
            ) from error


def runner_identity():
    digest = hashlib.sha256()
    for name in ("controller.py", "model.py", "runtime.py", "result.py", "worker.py", "markers.py"):
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


def inventory_matches_manifest(result, manifest):
    files = set(manifest.inventory)
    registry = manifest.registry_by_id
    required = {}
    for row in manifest.registry:
        if row["kind"] in {"scope", "decorator"}:
            owner = "::".join([row["file"], *row["owner"]])
            required.setdefault(owner, set()).add(row["id"])
    for item in result["items"]:
        if item["file"] not in files:
            return False
        origins = set(item["origins"])
        if any(
            origin not in registry or registry[origin]["file"] != item["file"] for origin in origins
        ):
            return False
        aliases = set(item["aliases"])
        for alias in aliases:
            if not required.get(alias, set()).issubset(origins):
                return False
    return True


def _unit_result(item, outcome, reason="", result=None, attempts=0):
    row = {
        "name": hashlib.sha256(item["nodeid"].encode()).hexdigest(),
        "file": item["file"],
        "outcome": outcome,
        "reason": reason,
        "attempts": attempts,
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "worker_seconds": 0,
        "cpu_seconds": 0,
        "failures": [],
    }
    if result is not None:
        for name in (
            "collected",
            "passed",
            "failed",
            "skipped",
            "worker_seconds",
            "cpu_seconds",
            "failures",
        ):
            row[name] = result[name]
    return row


def _summary(results, collection, started, setup_seconds):
    counts = {
        name: sum(row["outcome"] == name for row in results)
        for name in ("executed", "reused", "failed", "infrastructure_error", "not-selected")
    }
    cases = {
        name: sum(row[name] for row in results)
        for name in ("collected", "passed", "failed", "skipped")
    }
    status = "passed"
    if counts["infrastructure_error"] or collection["outcome"] == "infrastructure_error":
        status = "infrastructure_error"
    elif counts["failed"] or collection["outcome"] == "failed":
        status = "failed"
    elif not cases["collected"]:
        status = "no_tests"
    return {
        "status": status,
        "counts": counts,
        "cases": cases,
        "collection": collection,
        "case_outcomes": {
            label: sum(row[field] for row in results if row["outcome"] == outcome)
            for label, field, outcome in (
                ("fresh_passed", "passed", "executed"),
                ("cached_passed", "passed", "reused"),
                ("fresh_skipped", "skipped", "executed"),
                ("cached_skipped", "skipped", "reused"),
            )
        },
        "units": results,
        "elapsed_seconds": time.monotonic() - started,
        "setup_seconds": setup_seconds,
        "worker_cpu_seconds": collection["cpu_seconds"]
        + sum(row["cpu_seconds"] for row in results),
    }


def run(
    root, *, force=False, no_cache=False, explain=False, cache_dir=None, timeout=60, arguments=()
):
    started = time.monotonic()
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 3600:
        raise DeclarationError("timeout must be between 0.1 and 3600 seconds")
    if fcntl is None or os.name != "posix":
        raise InfrastructureError("execution requires Linux with file locking and Docker")
    state = (
        Path(cache_dir).resolve() if cache_dir else Path(root).resolve() / ".pytest-telescope/cache"
    )
    manifest = load(root, cache_dir=state)
    collection_unit = manifest.units[0]
    effective_arguments = [*manifest.pytest_args, *arguments]
    parsed = selected_arguments(effective_arguments, collection_unit, set(manifest.inventory))
    parsed = parsed or []
    options, selectors = split_arguments(parsed)
    environment = {name: os.environ.get(name) for name in manifest.env}
    collection = {
        "outcome": "infrastructure_error",
        "collected": 0,
        "worker_seconds": 0,
        "cpu_seconds": 0,
        "failures": [],
    }
    for attempt in range(2):
        try:
            runtime.recover(manifest.root)
            identity = runtime.identity(manifest.root)
            break
        except InfrastructureError as exc:
            if attempt == 1 or isinstance(exc, runtime.CleanupError):
                collection["reason"] = str(exc)
                return _summary([], collection, started, time.monotonic() - started)
    common = {
        "schema": SCHEMA,
        "runtime": identity["execution"],
        "runner": runner_identity(),
        "environment": environment,
        "all_args": effective_arguments,
        "snapshot_owner": [os.getuid(), os.getgid()],
        "policy": runtime.POLICY,
    }
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    with lock(state / ".lock", timeout * 2 + 30):
        store = CacheStore(state)
        inventory_state = state / "inventory"
        inventory_state.mkdir(exist_ok=True, mode=0o700)
        inventory_store = CacheStore(inventory_state, count=8, size=64 * 1024 * 1024)
        with PendingRecords(store, inventory_store) as pending:
            with runtime.invocation(manifest.root) as pool_directory:
                pool = pool_directory / "project"
                records = snapshot(manifest, collection_unit, pool)
                record_map = {record["path"]: record for record in records}
                inventory_key = fingerprint(
                    {
                        **common,
                        "kind": "collection",
                        "inputs": records,
                        "files": manifest.inventory,
                        "registry": manifest.registry,
                    }
                )
                inventory_path = inventory_state / (inventory_key + ".json")
                inventory_enabled = collection_unit.cache and not no_cache
                collected = (
                    read_record(inventory_path, inventory_key, kind="collection")
                    if inventory_enabled and not force
                    else None
                )
                if collected is not None and not inventory_matches_manifest(collected, manifest):
                    collected = None
                reused_inventory = collected is not None
                if collected is None:
                    for attempt in range(2):
                        try:
                            with runtime.invocation(manifest.root) as directory:
                                collected = runtime.execute(
                                    identity["image"],
                                    pool,
                                    [
                                        "-o",
                                        "addopts=",
                                        "-p",
                                        "no:cacheprovider",
                                        *options,
                                        *manifest.inventory,
                                    ],
                                    environment,
                                    timeout,
                                    root=manifest.root,
                                    invocation_directory=directory,
                                    request={
                                        "mode": "collection",
                                        "registry": manifest.registry,
                                        "selectors": selectors,
                                    },
                                )
                            break
                        except InfrastructureError as exc:
                            if attempt == 1 or isinstance(exc, runtime.CleanupError):
                                collection["reason"] = str(exc)
                                return _summary([], collection, started, time.monotonic() - started)
                    if collected["exit_code"] == 4:
                        raise DeclarationError(
                            "unsupported or changed telescope declarations during isolated collection"
                        )
                    if collected["exit_code"] not in (0, 5):
                        collection.update(
                            outcome="failed",
                            failures=collected["failures"],
                            worker_seconds=collected["worker_seconds"],
                            cpu_seconds=collected["cpu_seconds"],
                        )
                        return _summary([], collection, started, time.monotonic() - started)
                    if not inventory_matches_manifest(collected, manifest):
                        raise InfrastructureError(
                            "collected inventory disagrees with the frozen declaration registry"
                        )
                    if inventory_enabled and collected["exit_code"] == 0:
                        pending.stage(inventory_path, inventory_key, collected, kind="collection")
                items = collected["items"]
                collection.update(
                    outcome="reused" if reused_inventory else "executed",
                    collected=len(items),
                    worker_seconds=0 if reused_inventory else collected["worker_seconds"],
                    cpu_seconds=0 if reused_inventory else collected["cpu_seconds"],
                )
                files = {}
                for item in items:
                    if item["file"] not in manifest.inventory:
                        raise DeclarationError(
                            "custom collected item is outside the planned test source inventory"
                        )
                    files.setdefault(item["file"], []).append(
                        {"nodeid": item["nodeid"], "origins": item["origins"]}
                    )
                file_digests = {
                    name: fingerprint(sorted(rows, key=lambda row: row["nodeid"]))
                    for name, rows in files.items()
                }
                registry_files = {}
                for row in manifest.registry:
                    registry_files.setdefault(row["file"], []).append(row)
                scopes = {}
                frozen_scope_hashes = {}
                blocked = None
                setup_seconds = time.monotonic() - started - collection["worker_seconds"]
                for item in items:
                    if not item["selected"]:
                        results.append(
                            _unit_result(
                                item,
                                "not-selected",
                                "not selected by pytest arguments" if explain else "",
                            )
                        )
                        continue
                    if blocked is not None:
                        results.append(_unit_result(item, "infrastructure_error", blocked))
                        continue
                    signature = (item["file"], tuple(item["origins"]))
                    if signature not in scopes:
                        unit = case_unit(manifest, item)
                        file_scope = (
                            "broad" if unit.inputs is manifest.broad_inputs else tuple(unit.inputs)
                        )
                        if file_scope not in frozen_scope_hashes:
                            frozen_scope_hashes[file_scope] = fingerprint(
                                [record_map[name] for name in unit.inputs]
                            )
                        scope_key = fingerprint(
                            {
                                "inputs": frozen_scope_hashes[file_scope],
                                "absent": unit.absent,
                                "cache": unit.cache,
                            }
                        )
                        scopes[signature] = unit, scope_key
                    unit, scope_key = scopes[signature]
                    key = fingerprint(
                        {
                            **common,
                            "kind": "execution",
                            "case": item["nodeid"],
                            "scope": scope_key,
                            "inventory": file_digests[item["file"]],
                        }
                    )
                    path = state / (key + ".json")
                    enabled = unit.cache and not no_cache
                    previous = (
                        read_record(
                            path, key, case_id=hashlib.sha256(item["nodeid"].encode()).hexdigest()
                        )
                        if enabled and not force
                        else None
                    )
                    if previous is not None:
                        results.append(
                            _unit_result(
                                item,
                                "reused",
                                "complete successful case record matches" if explain else "",
                                {**previous, "worker_seconds": 0, "cpu_seconds": 0},
                            )
                        )
                        continue
                    reason = (
                        "forced"
                        if force
                        else "non-cacheable"
                        if not enabled
                        else "no valid successful case record"
                    )
                    for attempt in range(2):
                        try:
                            with runtime.invocation(manifest.root) as directory:
                                if unit.inputs is manifest.broad_inputs:
                                    frozen = pool
                                else:
                                    frozen = directory / "project"
                                    materialize(pool, unit, frozen, record_map)
                                result = runtime.execute(
                                    identity["image"],
                                    frozen,
                                    [
                                        "-o",
                                        "addopts=",
                                        "-p",
                                        "no:cacheprovider",
                                        *options,
                                        item["file"],
                                    ],
                                    environment,
                                    timeout,
                                    root=manifest.root,
                                    invocation_directory=directory,
                                    request={
                                        "mode": "execution",
                                        "registry": registry_files.get(item["file"], []),
                                        "case_nodeid": item["nodeid"],
                                        "expected_digest": file_digests[item["file"]],
                                    },
                                )
                            if result["exit_code"] == 4:
                                raise DeclarationError(
                                    "case collection or telescope declarations differ from validated inventory"
                                )
                            outcome = "executed" if result["exit_code"] == 0 else "failed"
                            if enabled and result["exit_code"] == 0:
                                pending.stage(path, key, result)
                            results.append(
                                _unit_result(
                                    item, outcome, reason if explain else "", result, attempt + 1
                                )
                            )
                            break
                        except (InfrastructureError, OSError) as exc:
                            message = (
                                str(exc)
                                if isinstance(exc, InfrastructureError)
                                else "owned storage operation failed"
                            )
                            if isinstance(exc, runtime.CleanupError):
                                blocked = "run stopped after owned-resource cleanup failure"
                            if attempt == 1 or blocked:
                                results.append(
                                    _unit_result(
                                        item, "infrastructure_error", message, attempts=attempt + 1
                                    )
                                )
                                break
            if blocked is None:
                pending.publish()
    return _summary(results, collection, started, setup_seconds)


def split_arguments(arguments):
    """Split already-validated options from real pytest node selectors."""
    values = {"-k", "-m", "-o", "--override-ini", "-p", "--maxfail", "--tb", "--capture", "--color"}
    options, selectors = [], []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in values:
            options.extend(arguments[index : index + 2])
            index += 2
        elif value.startswith("-"):
            options.append(value)
            index += 1
        else:
            selectors.append(value.removeprefix("./"))
            index += 1
    return options, selectors


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pytest-telescope")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preparation = subparsers.add_parser("runtime", help="build the clean Docker runtime")
    preparation.add_argument("--requirements", type=Path)
    subparsers.add_parser("plan", help="preview literal declarations without collecting tests")
    execution = subparsers.add_parser("run", help="execute or reuse each selected pytest case")
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
        print(render_json(result))
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
    except (OSError, RecursionError, MemoryError):
        print(
            json.dumps(
                {
                    "status": "infrastructure_error",
                    "error": "bounded project storage or parsing operation failed",
                }
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
