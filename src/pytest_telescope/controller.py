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
    UnsupportedCapability,
    case_unit,
    fingerprint,
    load,
    materialize,
    render_json,
    snapshot,
)

SCHEMA = 3


def suite_directories(snapshot, suite):
    """Create only structural directories needed for rootdir and cwd."""
    for name in (suite.root, suite.cwd):
        directory = snapshot / name
        directory.mkdir(parents=True, exist_ok=True)
        while directory != snapshot:
            directory.chmod(0o755)
            directory = directory.parent


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
    for name in sorted(path.name for path in Path(__file__).parent.glob("*.py")):
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
            required.setdefault(hashlib.sha256(owner.encode()).hexdigest(), set()).add(row["id"])
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
        "name": item["case_id"],
        "case_id": item["case_id"],
        "selected": outcome != "not-selected",
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
        for name in (
            "executed",
            "reused",
            "failed",
            "unsupported",
            "infrastructure_error",
            "not-selected",
        )
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
    root,
    *,
    force=False,
    no_cache=False,
    explain=False,
    cache_dir=None,
    timeout=60,
    arguments=(),
    budget=None,
    suites=None,
):
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from .execution import effective_arguments as suite_arguments
    from .execution import envelope
    from .model import Unit
    from .project_metadata import materialize_metadata, metadata_records

    started = time.monotonic()
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 3600:
        raise DeclarationError("timeout must be between 0.1 and 3600 seconds")
    if fcntl is None or os.name != "posix":
        raise InfrastructureError("execution requires Linux with file locking and Docker")
    state = (
        Path(cache_dir).resolve() if cache_dir else Path(root).resolve() / ".pytest-telescope/cache"
    )
    manifest = load(root, cache_dir=state, suite_names=suites)
    resources = envelope(manifest.execution, budget)
    chosen_suites = set(suites) if suites is not None else {suite.name for suite in manifest.suites}
    if not chosen_suites or chosen_suites - {suite.name for suite in manifest.suites}:
        raise DeclarationError("suite selection must name configured suites")
    configured = [
        (suite, unit)
        for suite, unit in zip(manifest.suites, manifest.units)
        if suite.name in chosen_suites
    ]
    all_tests = set().union(*(set(suite.inventory) for suite, _ in configured))
    all_unit = Unit("all", sorted(all_tests), [], [], True)
    invocations = []
    for suite, unit in configured:
        effective = suite_arguments(suite, arguments)
        parsed = selected_arguments(effective, all_unit, all_tests) or []
        options, selectors = split_arguments(parsed)
        own_selectors = [
            selector for selector in selectors if selector.split("::", 1)[0] in suite.inventory
        ]
        invocations.append(
            (suite, unit, effective, options, own_selectors, bool(selectors and not own_selectors))
        )
    environment = {name: os.environ.get(name) for name in manifest.env}
    collection = dict(
        outcome="infrastructure_error",
        collected=0,
        worker_seconds=0,
        cpu_seconds=0,
        failures=[],
        suites=[
            dict(
                name=suite.name,
                files=suite.inventory,
                outcome="not-started",
                collected=None,
                inventory_digest=None,
                worker_seconds=0,
                cpu_seconds=0,
            )
            for suite, _ in configured
        ],
    )
    results = []

    def summary(setup):
        report = _summary(results, collection, started, setup)
        report["schema"] = 1
        report["kind"] = "run"
        report["inventory_complete"] = all(
            row["outcome"] in {"reused", "executed"} for row in collection["suites"]
        )
        report["inventory_digest"] = (
            fingerprint(sorted([row["suite"], row["case_id"], row["file"]] for row in results))
            if report["inventory_complete"]
            else None
        )
        report["counts"]["selected"] = sum(row["selected"] for row in results)
        report["resources"] = {
            "worker": resources.worker,
            "budget": resources.budget,
            "concurrency": resources.concurrency,
        }
        report["suites"] = [suite.name for suite, _ in configured]
        return report

    for attempt in range(2):
        try:
            runtime.recover(manifest.root)
            identity = runtime.identity(manifest.root)
            break
        except InfrastructureError as exc:
            if attempt == 1 or isinstance(exc, runtime.CleanupError):
                collection["reason"] = str(exc)
                return summary(time.monotonic() - started)
    common = {
        "schema": SCHEMA,
        "runtime": identity["execution"],
        "runner": runner_identity(),
        "environment": environment,
        "snapshot_owner": [os.getuid(), os.getgid()],
        "policy": runtime.POLICY,
        "worker_limits": resources.worker,
    }
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cancellation = threading.Event()
    blocked = None
    setup_seconds = time.monotonic() - started
    try:
        with lock(state / ".lock", timeout * 2 + 30):
            store = CacheStore(state)
            inventory_state = state / "inventory"
            inventory_state.mkdir(exist_ok=True, mode=0o700)
            inventory_store = CacheStore(
                inventory_state, count=max(8, len(configured)), size=64 * 1024 * 1024
            )
            with PendingRecords(store, inventory_store) as pending:
                with runtime.invocation(manifest.root) as pool_directory:
                    pool = pool_directory / "project"
                    pool_unit = Unit(
                        "pool",
                        sorted(all_tests),
                        sorted(set().union(*(set(unit.inputs) for _, unit in configured))),
                        sorted(set().union(*(set(unit.absent) for _, unit in configured))),
                        True,
                    )
                    records = snapshot(manifest, pool_unit, pool)
                    for suite, _ in configured:
                        suite_directories(pool, suite)
                    record_map = {record["path"]: record for record in records}
                    pool_is_broad = pool_unit.inputs == manifest.broad_inputs
                    frozen_scope_hashes = {}
                    metadata_hashes = {}

                    def metadata_identity(names):
                        key = tuple(names)
                        if key not in metadata_hashes:
                            metadata_hashes[key] = metadata_records(manifest.distributions, names)
                        return metadata_hashes[key]

                    jobs = []
                    all_selected_aliases = set()
                    requested_selectors = set()
                    for (
                        suite,
                        collection_unit,
                        effective,
                        options,
                        selectors,
                        selection_disabled,
                    ) in invocations:
                        active_collection = next(
                            row for row in collection["suites"] if row["name"] == suite.name
                        )
                        active_collection["outcome"] = "infrastructure_error"
                        requested_selectors.update(
                            hashlib.sha256(selector.encode()).hexdigest() for selector in selectors
                        )
                        registry = [
                            row for row in manifest.registry if row["file"] in suite.inventory
                        ]
                        suite_common = {**common, "suite": suite.identity(), "all_args": effective}
                        inventory_key = fingerprint(
                            {
                                **suite_common,
                                "kind": "collection",
                                "inputs": [record_map[name] for name in collection_unit.inputs],
                                "absent": collection_unit.absent,
                                "metadata": metadata_identity(collection_unit.distributions),
                                "files": suite.inventory,
                                "registry": registry,
                            }
                        )
                        inventory_path = inventory_state / (inventory_key + ".json")
                        enabled_inventory = collection_unit.cache and not no_cache
                        collected = (
                            read_record(inventory_path, inventory_key, kind="collection")
                            if enabled_inventory and not force
                            else None
                        )
                        if collected is not None and not inventory_matches_manifest(
                            collected, manifest
                        ):
                            collected = None
                        reused = collected is not None
                        if collected is None and not suite.inventory:
                            collected = dict(
                                protocol=3,
                                kind="collection",
                                nonce="0" * 32,
                                items=[],
                                complete=True,
                                exit_code=5,
                                leaked_descendants=False,
                                worker_seconds=0,
                                cpu_seconds=0,
                                failures=[],
                            )
                        if collected is None:
                            for attempt in range(2):
                                try:
                                    with runtime.invocation(manifest.root) as directory:
                                        frozen = directory / "project"
                                        materialize(pool, collection_unit, frozen, record_map)
                                        suite_directories(frozen, suite)
                                        metadata = directory / "metadata"
                                        materialize_metadata(
                                            manifest.distributions,
                                            collection_unit.distributions,
                                            metadata,
                                        )
                                        collected = runtime.execute(
                                            identity["image"],
                                            frozen,
                                            [
                                                "-c",
                                                "/project/" + suite.config,
                                                "--rootdir=/project/" + suite.root,
                                                "-o",
                                                "addopts=",
                                                "-p",
                                                "no:cacheprovider",
                                                *[
                                                    part
                                                    for plugin in suite.plugins
                                                    for part in ("-p", plugin)
                                                ],
                                                *options,
                                                *["/project/" + name for name in suite.inventory],
                                            ],
                                            environment,
                                            timeout,
                                            root=manifest.root,
                                            invocation_directory=directory,
                                            suite_root=suite.root,
                                            suite_cwd=suite.cwd,
                                            suite=suite.name,
                                            metadata_directory=metadata,
                                            limits=resources.worker,
                                            selection_disabled=selection_disabled,
                                            allow_unknown_selectors=True,
                                            request={
                                                "mode": "collection",
                                                "registry": registry,
                                                "selectors": [
                                                    hashlib.sha256(selector.encode()).hexdigest()
                                                    for selector in selectors
                                                ],
                                            },
                                        )
                                    break
                                except InfrastructureError as exc:
                                    if attempt == 1 or isinstance(exc, runtime.CleanupError):
                                        collection["reason"] = str(exc)
                                        return summary(time.monotonic() - started)
                            if collected["exit_code"] == 4:
                                raise DeclarationError(
                                    "unsupported or changed declarations during isolated collection"
                                )
                            if collected["exit_code"] not in (0, 5):
                                active_collection["outcome"] = "failed"
                                collection.update(
                                    outcome="failed",
                                    failures=collected["failures"],
                                    worker_seconds=collected["worker_seconds"],
                                    cpu_seconds=collected["cpu_seconds"],
                                )
                                return summary(time.monotonic() - started)
                            if not inventory_matches_manifest(collected, manifest):
                                raise InfrastructureError(
                                    "collected inventory disagrees with frozen declarations"
                                )
                            if enabled_inventory and collected["exit_code"] == 0:
                                pending.stage(
                                    inventory_path, inventory_key, collected, kind="collection"
                                )
                        suite_collection = dict(
                            name=suite.name,
                            outcome="reused" if reused else "executed",
                            collected=len(collected["items"]),
                            worker_seconds=0 if reused else collected["worker_seconds"],
                            cpu_seconds=0 if reused else collected["cpu_seconds"],
                        )
                        suite_collection["inventory_digest"] = fingerprint(
                            sorted(
                                [suite.name, item["case_id"], item["file"]]
                                for item in collected["items"]
                            )
                        )
                        active_collection.update(suite_collection)
                        if len(results) + len(collected["items"]) > 100_000:
                            raise DeclarationError("combined suite inventory exceeds 100000 cases")
                        for key in ("collected", "worker_seconds", "cpu_seconds"):
                            collection[key] += suite_collection[key]
                        files = {}
                        for item in collected["items"]:
                            if item["file"] not in suite.inventory:
                                raise DeclarationError(
                                    "collected item is outside its structural suite"
                                )
                            all_selected_aliases.update(item["aliases"])
                            files.setdefault(item["file"], []).append(
                                {"case_id": item["case_id"], "origins": item["origins"]}
                            )
                        digests = {
                            name: fingerprint(sorted(rows, key=lambda row: row["case_id"]))
                            for name, rows in files.items()
                        }
                        registry_files = {}
                        for row in registry:
                            registry_files.setdefault(row["file"], []).append(row)
                        scopes = {}
                        for item in collected["items"]:
                            index = len(results)

                            def row(outcome, reason="", result=None, attempts=0):
                                return {
                                    **_unit_result(item, outcome, reason, result, attempts),
                                    "suite": suite.name,
                                    "name": item["case_id"],
                                    "case_id": item["case_id"],
                                }

                            if not item["selected"]:
                                results.append(
                                    row(
                                        "not-selected",
                                        "not selected by pytest arguments" if explain else "",
                                    )
                                )
                                continue
                            signature = (item["file"], tuple(item["origins"]))
                            if signature not in scopes:
                                unit = case_unit(manifest, item, suite)
                                file_scope = (
                                    "broad"
                                    if unit.inputs is manifest.broad_inputs
                                    else tuple(unit.inputs)
                                )
                                if file_scope not in frozen_scope_hashes:
                                    frozen_scope_hashes[file_scope] = fingerprint(
                                        [record_map[name] for name in unit.inputs]
                                    )
                                scope_key = fingerprint(
                                    {
                                        "inputs": frozen_scope_hashes[file_scope],
                                        "absent": unit.absent,
                                        "metadata": metadata_identity(unit.distributions),
                                        "cache": unit.cache,
                                    }
                                )
                                scopes[signature] = unit, scope_key
                            unit, scope_key = scopes[signature]
                            key = fingerprint(
                                {
                                    **suite_common,
                                    "kind": "execution",
                                    "case": item["case_id"],
                                    "scope": scope_key,
                                    "inventory": digests[item["file"]],
                                }
                            )
                            path = state / (key + ".json")
                            enabled = unit.cache and not no_cache
                            previous = (
                                read_record(
                                    path,
                                    key,
                                    case_id=item["case_id"],
                                )
                                if enabled and not force
                                else None
                            )
                            if previous is not None:
                                results.append(
                                    row(
                                        "reused",
                                        "complete successful case record matches"
                                        if explain
                                        else "",
                                        {**previous, "worker_seconds": 0, "cpu_seconds": 0},
                                    )
                                )
                                continue
                            results.append(row("infrastructure_error", "execution not started"))
                            jobs.append(
                                (
                                    index,
                                    item,
                                    unit,
                                    suite,
                                    options,
                                    registry_files.get(item["file"], []),
                                    digests[item["file"]],
                                    path,
                                    key,
                                    enabled,
                                )
                            )
                    if requested_selectors - all_selected_aliases:
                        raise DeclarationError("selector does not identify a collected case")
                    collection["outcome"] = (
                        "reused"
                        if all(row["outcome"] == "reused" for row in collection["suites"])
                        else "executed"
                    )
                    setup_seconds = time.monotonic() - started - collection["worker_seconds"]

                    def execute_job(job):
                        index, item, unit, suite, options, registry, digest, path, key, enabled = (
                            job
                        )
                        reason = (
                            "forced"
                            if force
                            else "non-cacheable"
                            if not enabled
                            else "no valid successful case record"
                        )

                        def outcome(status, message="", result=None, attempts=0):
                            return {
                                **_unit_result(item, status, message, result, attempts),
                                "suite": suite.name,
                                "name": item["case_id"],
                                "case_id": item["case_id"],
                            }

                        for attempt in range(2):
                            try:
                                if cancellation.is_set():
                                    raise runtime.InvocationInterrupted(
                                        "run cancelled before worker start"
                                    )
                                with runtime.invocation(manifest.root) as directory:
                                    if unit.inputs is manifest.broad_inputs and pool_is_broad:
                                        frozen = pool
                                    else:
                                        frozen = directory / "project"
                                        materialize(pool, unit, frozen, record_map)
                                        suite_directories(frozen, suite)
                                    metadata = directory / "metadata"
                                    materialize_metadata(
                                        manifest.distributions, unit.distributions, metadata
                                    )
                                    result = runtime.execute(
                                        identity["image"],
                                        frozen,
                                        [
                                            "-c",
                                            "/project/" + suite.config,
                                            "--rootdir=/project/" + suite.root,
                                            "-o",
                                            "addopts=",
                                            "-p",
                                            "no:cacheprovider",
                                            *[
                                                part
                                                for plugin in suite.plugins
                                                for part in ("-p", plugin)
                                            ],
                                            *options,
                                            "/project/" + item["file"],
                                        ],
                                        environment,
                                        timeout,
                                        root=manifest.root,
                                        invocation_directory=directory,
                                        suite_root=suite.root,
                                        suite_cwd=suite.cwd,
                                        suite=suite.name,
                                        metadata_directory=metadata,
                                        limits=resources.worker,
                                        cancellation=cancellation,
                                        request={
                                            "mode": "execution",
                                            "registry": registry,
                                            "case_id": item["case_id"],
                                            "expected_digest": digest,
                                        },
                                    )
                                if result["exit_code"] == 4:
                                    raise DeclarationError(
                                        "case collection or declarations differ from validated inventory"
                                    )
                                return (
                                    outcome(
                                        "executed" if result["exit_code"] == 0 else "failed",
                                        reason if explain else "",
                                        result,
                                        attempt + 1,
                                    ),
                                    result,
                                    False,
                                )
                            except (InfrastructureError, OSError) as exc:
                                terminal = isinstance(exc, runtime.CleanupError)
                                if terminal:
                                    cancellation.set()
                                if attempt == 1 or terminal:
                                    return (
                                        outcome(
                                            "infrastructure_error",
                                            str(exc)
                                            if isinstance(exc, InfrastructureError)
                                            else "owned storage operation failed",
                                            attempts=attempt + 1,
                                        ),
                                        None,
                                        terminal,
                                    )

                    active, cursor = {}, 0
                    executor = ThreadPoolExecutor(max_workers=resources.concurrency)
                    try:
                        while cursor < len(jobs) or active:
                            while (
                                not cancellation.is_set()
                                and cursor < len(jobs)
                                and len(active) < resources.concurrency
                            ):
                                job = jobs[cursor]
                                cursor += 1
                                active[executor.submit(execute_job, job)] = job
                            if not active:
                                break
                            completed, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                            for future in completed:
                                job = active.pop(future)
                                row, result, terminal = future.result()
                                results[job[0]] = row
                                if terminal:
                                    blocked = "run stopped after owned-resource cleanup failure or cancellation"
                                if (
                                    result is not None
                                    and result["exit_code"] == 0
                                    and job[-1]
                                    and not cancellation.is_set()
                                ):
                                    pending.stage(job[-3], job[-2], result)
                    except (runtime.InvocationInterrupted, KeyboardInterrupt):
                        blocked = "run cancelled; active workers drained before result publication"
                        cancellation.set()
                    except BaseException:
                        cancellation.set()
                        raise
                    finally:
                        executor.shutdown(wait=True, cancel_futures=False)
                        for future, job in active.items():
                            if future.done() and not future.cancelled():
                                row, _, terminal = future.result()
                                results[job[0]] = row
                                if terminal:
                                    blocked = "run stopped after owned-resource cleanup failure or cancellation"
                        if cancellation.is_set():
                            blocked = blocked or "run cancelled"
                            for job in jobs[cursor:]:
                                results[job[0]]["reason"] = blocked
                if blocked is None:
                    pending.publish()
                else:
                    # Even a completed successful worker is not a reusable result until outer cleanup.
                    collection["reason"] = blocked
                    collection["outcome"] = "infrastructure_error"
    except (InfrastructureError, OSError) as exc:
        collection["outcome"] = "infrastructure_error"
        collection["reason"] = (
            str(exc) if isinstance(exc, InfrastructureError) else "owned storage operation failed"
        )
        for row in results:
            if row["outcome"] == "infrastructure_error":
                row["reason"] = collection["reason"]
    return summary(setup_seconds)


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


def error_result(error):
    """Versioned refusal envelope when no complete inventory can be established."""
    result = {
        "schema": 1,
        "kind": "refusal",
        "inventory_complete": False,
        "inventory_digest": None,
        "status": "unsupported"
        if isinstance(error, UnsupportedCapability)
        else "declaration_error"
        if isinstance(error, DeclarationError)
        else "infrastructure_error",
        "error": str(error),
    }
    if isinstance(error, UnsupportedCapability):
        result["capability"] = error.capability
    return result


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise DeclarationError("invalid command-line arguments; consult --help")

    parser = Parser(prog="pytest-telescope")
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
    execution.add_argument("--suite", action="append", dest="suites")
    for dimension in ("cpus", "memory-bytes", "processes", "scratch-bytes"):
        execution.add_argument("--budget-" + dimension, type=int)
    execution.add_argument("arguments", nargs=argparse.REMAINDER)
    try:
        options = parser.parse_args(argv)
        if options.command == "runtime":
            result = runtime.build_runtime(Path.cwd(), options.requirements)
        elif options.command == "plan":
            from .plan import make_plan

            result = make_plan(Path.cwd())
        else:
            arguments = (
                options.arguments[1:] if options.arguments[:1] == ["--"] else options.arguments
            )
            dimensions = ("cpus", "memory_bytes", "processes", "scratch_bytes")
            budget = {name: getattr(options, "budget_" + name) for name in dimensions}
            budget = budget if any(value is not None for value in budget.values()) else None
            result = run(
                Path.cwd(),
                force=options.force,
                no_cache=options.no_cache,
                explain=options.explain,
                cache_dir=options.cache_dir,
                timeout=options.timeout,
                arguments=arguments,
                budget=budget,
                suites=options.suites,
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
        print(json.dumps(error_result(InfrastructureError("invocation interrupted"))))
        return 3
    except UnsupportedCapability as exc:
        print(json.dumps(error_result(exc)))
        return 4
    except DeclarationError as exc:
        print(json.dumps(error_result(exc)))
        return 4
    except InfrastructureError as exc:
        print(json.dumps(error_result(exc)))
        return 3
    except (OSError, RecursionError, MemoryError):
        print(
            json.dumps(
                error_result(
                    InfrastructureError("bounded project storage or parsing operation failed")
                )
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
