"""Complete-inventory execution: ordinary sessions and cache-first Bubblewrap views."""

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

from . import sandbox
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

# Cache record format; records from earlier backends are never eligible.
SCHEMA = 5
RESULT_SCHEMA = 3
OUTCOMES = ("executed", "reused", "failed", "unsupported", "infrastructure_error", "not-selected")
ROUTES = ("ordinary", "scoped")


def suite_directories(snapshot, suite):
    """Create only structural directories needed for rootdir and cwd."""
    for name in (suite.root, suite.cwd):
        directory = snapshot / name
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            directory.chmod(0o755)
            os.utime(directory, ns=(0, 0))
            if directory == snapshot:
                break
            directory = directory.parent


@contextlib.contextmanager
def lock(path, timeout):
    if fcntl is None:
        raise InfrastructureError("scoped execution requires Linux with file locking")
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


def read_record(path, key, *, kind="execution", case_id=None, group_id=None, case_ids=None):
    limit = 64 * 1024 * 1024 if kind in {"collection", "batch"} else 1_048_576
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
        from .result import validate_batch, validate_collection, validate_result

        valid = (
            validate_collection(result, successful_only=True)
            if kind == "collection"
            else validate_batch(result, successful_only=True, group_id=group_id, case_ids=case_ids)
            if kind == "batch"
            else validate_result(result, successful_only=True, case_id=case_id)
        )
        if not valid:
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def write_record(path, key, result, *, kind="execution"):
    limit = 64 * 1024 * 1024 if kind == "collection" or result["protocol"] == 2 else 1_048_576
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
            raise sandbox.CleanupError(
                "pending result cleanup failed; no new records were eligible"
            ) from error


def runner_identity():
    digest = hashlib.sha256()
    for name in sorted(
        path.name for path in Path(__file__).parent.iterdir() if path.suffix in {".py", ".json"}
    ):
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


def _counts(rows):
    counts = {name: sum(row["outcome"] == name for row in rows) for name in OUTCOMES}
    counts["selected"] = sum(row["selected"] for row in rows)
    return counts


def _row(item, suite, route, outcome, reason=""):
    return dict(
        name=item["case_id"],
        case_id=item["case_id"],
        suite=suite.name,
        file=item["file"],
        route=route,
        selected=outcome != "not-selected",
        outcome=outcome,
        reason=reason,
        attempts=0,
        collected=0,
        passed=0,
        failed=0,
        skipped=0,
        worker_seconds=None if route == "ordinary" else 0,
        failures=[],
        group_id=None,
        result_status=None,
        complete=False,
    )


def _session_arguments(suite, options, files):
    """Arguments of one sandboxed pytest session, rooted at the frozen /project view."""
    return [
        "-c",
        "/project/" + suite.config,
        "--rootdir=/project/" + suite.root,
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        *[part for plugin in suite.plugins for part in ("-p", plugin)],
        *options,
        *files,
    ]


def _file_digest(inventory, filename):
    """Containing-file inventory that every scoped execution must collect again."""
    return fingerprint(
        sorted(
            (
                {"case_id": entry["case_id"], "origins": entry["origins"]}
                for entry in inventory
                if entry["file"] == filename
            ),
            key=lambda entry: entry["case_id"],
        )
    )


def _ordinary_group(suite, files, result, effective, attempt_seconds):
    """One ordinary suite session; never cache eligible and never retried."""
    complete, code = result["complete"], result["exit_code"]
    return dict(
        group_id=fingerprint(
            {
                "profile": "ordinary-v1",
                "suite": suite.identity(),
                "case_ids": result["case_ids"],
                "args": effective,
            }
        ),
        suite=suite.name,
        files=sorted(files),
        route="ordinary",
        profile="ordinary-v1",
        case_ids=result["case_ids"],
        outcome="executed"
        if complete and code in (0, 5)
        else "failed"
        if complete and code in (1, 2)
        else "incomplete"
        if code in (1, 2)
        else "infrastructure_error",
        complete=complete,
        cache_eligible=False,
        stop_reason=None if complete else "pytest-stop" if code in (1, 2) else "infrastructure",
        attempts=1,
        attempt_seconds=attempt_seconds,
        timing_complete=True,
        worker_seconds=result["worker_seconds"],
        passed=result["passed"],
        failed=result["failed"],
        skipped=result["skipped"],
        not_run=sum(row["outcome"] == "not-run" for row in result["case_results"]),
        unresolved=0,
        failures=result["failures"],
    )


def run(
    root,
    *,
    force=False,
    no_cache=False,
    explain=False,
    cache_dir=None,
    timeout=60,
    arguments=(),
    workers=None,
    suites=None,
    execution_mode=None,
    structural_files=None,
):
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from . import ordinary
    from .batching import group_jobs, project_result
    from .execution import effective_arguments as suite_arguments
    from .execution import execution_mode as choose_mode
    from .execution import worker_count
    from .model import Unit, scoped_item
    from .project_metadata import materialize_metadata, metadata_records

    started = time.monotonic()
    if not math.isfinite(timeout) or not 0.1 <= timeout <= 3600:
        raise DeclarationError("timeout must be between 0.1 and 3600 seconds")
    if os.name != "posix":
        raise InfrastructureError("execution requires Linux")
    state = (
        Path(cache_dir).resolve() if cache_dir else Path(root).resolve() / ".pytest-telescope/cache"
    )
    manifest = load(root, cache_dir=state, suite_names=suites, structural_files=structural_files)
    mode = choose_mode(manifest.execution, execution_mode)
    concurrency = worker_count(manifest.execution, workers)
    profile = mode + "-v1"
    configured = list(zip(manifest.suites, manifest.units))
    all_tests = set().union(*(set(suite.inventory) for suite, _ in configured))
    all_unit = Unit("all", sorted(all_tests), [], [], True)
    invocations, requested_selectors, seen_aliases = {}, set(), set()
    for suite, _ in configured:
        effective = suite_arguments(suite, arguments)
        parsed = selected_arguments(effective, all_unit, all_tests) or []
        options, selectors = split_arguments(parsed)
        own = [selector for selector in selectors if selector.split("::", 1)[0] in suite.inventory]
        hashed = [hashlib.sha256(selector.encode()).hexdigest() for selector in own]
        requested_selectors.update(hashed)
        invocations[suite.name] = (effective, options, hashed, bool(selectors and not own))
    environment = {name: os.environ.get(name) for name in manifest.env}
    observations = [
        dict(
            name=suite.name,
            route=route,
            files=files,
            outcome="not-started",
            collected=None,
            inventory_digest=None,
            worker_seconds=None if route == "ordinary" else 0,
        )
        for suite, _ in configured
        for route, files in (
            ("scoped", manifest.scoped_files[suite.name]),
            ("ordinary", manifest.ordinary_files[suite.name]),
        )
        if files
    ]
    collection = dict(outcome="executed", collected=0, worker_seconds=0, failures=[])
    sandbox_report = dict(
        backend=sandbox.BACKEND,
        policy=sandbox.POLICY,
        required=manifest.scope_possible,
        identity=None,
    )
    results, groups, eligible_groups = [], [], set()
    scoped = []  # (suite, item, containing collection inventory), in deterministic order
    progress = dict(grouping_complete=False, published=False, setup=None, scoped_rows=False)

    def observation(suite, route):
        return next(
            row for row in observations if row["name"] == suite.name and row["route"] == route
        )

    def stop(outcome, reason, failures=()):
        collection.update(outcome=outcome, reason=reason, failures=list(failures))

    def session_started():
        if progress["setup"] is None:
            progress["setup"] = time.monotonic() - started

    def summary():
        if not progress["scoped_rows"]:
            # Known scoped cases stay accounted when planning stopped before their groups.
            for suite, item, _ in scoped:
                results.append(
                    _row(item, suite, "scoped", "not-selected")
                    if not item["selected"]
                    else _row(
                        item, suite, "scoped", "infrastructure_error", "execution not started"
                    )
                )
            progress["scoped_rows"] = True
        if mode == "batch":
            for row in results:
                if row["route"] == "scoped":
                    row["worker_seconds"] = None
        counts = _counts(results)
        cases = {
            name: sum(row[name] for row in results)
            for name in ("collected", "passed", "failed", "skipped")
        }
        inventory_complete = all(row["outcome"] in {"reused", "executed"} for row in observations)
        if collection["outcome"] == "executed" and observations:
            if all(row["outcome"] == "reused" for row in observations):
                collection["outcome"] = "reused"
        status = "passed"
        if counts["infrastructure_error"] or collection["outcome"] == "infrastructure_error":
            status = "infrastructure_error"
        elif counts["failed"] or collection["outcome"] == "failed":
            status = "failed"
        elif not cases["collected"]:
            status = "no_tests"
        if (
            collection["outcome"] != "infrastructure_error"
            and any(
                group["outcome"] == "failed"
                or group["stop_reason"] in {"pytest-stop", "collection"}
                for group in groups
            )
            and not any(group["stop_reason"] in {"infrastructure", "cancelled"} for group in groups)
        ):
            status = "failed"
        if progress["published"]:
            for group in groups:
                if group["group_id"] in eligible_groups:
                    group["cache_eligible"] = True
        collection.update(
            collected=len(results),
            worker_seconds=sum(
                row["worker_seconds"] or 0 for row in observations if row["route"] == "scoped"
            ),
            suites=observations,
        )
        return dict(
            schema=RESULT_SCHEMA,
            kind="run",
            status=status,
            execution=dict(mode=mode, profile=profile, workers=concurrency),
            sandbox=sandbox_report,
            suites=[suite.name for suite, _ in configured],
            inventory_complete=inventory_complete,
            inventory_digest=fingerprint(
                sorted([row["suite"], row["case_id"], row["file"], row["route"]] for row in results)
            )
            if inventory_complete
            else None,
            grouping_complete=progress["grouping_complete"],
            counts=counts,
            route_counts={
                route: _counts([row for row in results if row["route"] == route])
                for route in ROUTES
            },
            cases=cases,
            case_outcomes={
                label: sum(row[field] for row in results if row["outcome"] == outcome)
                for label, field, outcome in (
                    ("fresh_passed", "passed", "executed"),
                    ("cached_passed", "passed", "reused"),
                    ("fresh_skipped", "skipped", "executed"),
                    ("cached_skipped", "skipped", "reused"),
                )
            },
            collection=collection,
            units=results,
            groups=groups,
            elapsed_seconds=time.monotonic() - started,
            setup_seconds=progress["setup"]
            if progress["setup"] is not None
            else time.monotonic() - started,
        )

    def ordinary_sessions(scoped):
        """Run each suite's ordinary session once; False when its inventory failed."""
        for suite, _ in configured:
            files = manifest.ordinary_files[suite.name]
            if not files:
                continue
            row = observation(suite, "ordinary")
            effective, options, selectors, disabled = invocations[suite.name]
            session_started()
            attempt_started = time.monotonic()
            try:
                result = ordinary.session(
                    manifest, suite, files, options, selectors, disabled, timeout
                )
            except InfrastructureError as exc:
                if isinstance(exc, sandbox.CleanupError):
                    raise
                row["outcome"] = "infrastructure_error"
                stop("infrastructure_error", str(exc))
                return False
            if result["declaration_error"]:
                raise DeclarationError(
                    "unsupported or changed declarations during ordinary collection"
                )
            if not result["inventory_complete"]:
                # Includes pytest's own startup errors, such as a failing conftest import.
                row["outcome"] = "failed"
                stop(
                    "failed",
                    "ordinary pytest collection failed",
                    [failure for failure in result["failures"] if failure["phase"] == "collect"],
                )
                return False
            if not inventory_matches_manifest(result, manifest):
                raise InfrastructureError("ordinary inventory disagrees with literal declarations")
            if len(results) + len(scoped) + len(result["items"]) > 100_000:
                raise DeclarationError("combined suite inventory exceeds 100000 cases")
            group = _ordinary_group(
                suite, files, result, effective, time.monotonic() - attempt_started
            )
            outcomes = {case["case_id"]: case for case in result["case_results"]}
            # Keep selected order unique within the suite across both collections.
            offset = 1 + max(
                (item["order"] for entry, item, _ in scoped if entry is suite and item["selected"]),
                default=-1,
            )
            for item in result["items"]:
                seen_aliases.update(item["aliases"])
                if scoped_item(manifest, item):
                    if item["selected"]:
                        item = {**item, "order": item["order"] + offset}
                    scoped.append((suite, item, result["items"]))
                    continue
                if not item["selected"]:
                    results.append(
                        _row(
                            item,
                            suite,
                            "ordinary",
                            "not-selected",
                            "not selected by pytest arguments" if explain else "",
                        )
                    )
                    continue
                status = outcomes[item["case_id"]]["outcome"]
                unit = _row(
                    item,
                    suite,
                    "ordinary",
                    "executed"
                    if status in ("passed", "skipped")
                    else "failed"
                    if status == "failed"
                    else "infrastructure_error",
                    "ordinary: always fresh" if explain else "",
                )
                unit.update(
                    attempts=1,
                    collected=1,
                    passed=int(status == "passed"),
                    failed=int(status == "failed"),
                    skipped=int(status == "skipped"),
                    failures=[f for f in result["failures"] if f["case_id"] == item["case_id"]],
                    group_id=group["group_id"],
                    result_status=status,
                    complete=outcomes[item["case_id"]]["complete"],
                )
                results.append(unit)
            groups.append(group)
            row.update(
                outcome="executed",
                collected=len(result["items"]),
                inventory_digest=fingerprint(
                    sorted([suite.name, item["case_id"], item["file"]] for item in result["items"])
                ),
            )
        return True

    if not manifest.scope_possible:
        # All-ordinary: no sandbox, frozen copy, cache directory or lock.
        if ordinary_sessions(scoped):
            if requested_selectors - seen_aliases:
                raise DeclarationError("selector does not identify a collected case")
            progress["grouping_complete"] = True
        return summary()

    try:
        box = sandbox.prepare(manifest.root)
        for attempt in range(2):
            try:
                sandbox.recover(manifest.root)
                break
            except InfrastructureError as exc:
                if attempt == 1 or isinstance(exc, sandbox.CleanupError):
                    raise
    except InfrastructureError as exc:
        stop("infrastructure_error", str(exc))
        return summary()
    sandbox_report["identity"] = box.identity
    common = {
        "schema": SCHEMA,
        "sandbox": box.identity,
        "runner": runner_identity(),
        "environment": environment,
        "snapshot_owner": [os.getuid(), os.getgid()],
        "profile": profile,
    }
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cancellation = threading.Event()
    blocked = None
    try:
        with lock(state / ".lock", timeout * 2 + 30):
            store = CacheStore(state)
            inventory_state = state / "inventory"
            inventory_state.mkdir(exist_ok=True, mode=0o700)
            inventory_store = CacheStore(
                inventory_state, count=max(8, len(configured)), size=64 * 1024 * 1024
            )
            with PendingRecords(store, inventory_store) as pending:
                with sandbox.invocation(manifest.root) as pool_directory:
                    pool = pool_directory / "project"
                    records = snapshot(manifest, manifest.pool, pool)
                    for suite, _ in configured:
                        suite_directories(pool, suite)
                    record_map = {record["path"]: record for record in records}
                    frozen_scope_hashes, metadata_hashes = {}, {}

                    def metadata_identity(names):
                        key = tuple(names)
                        if key not in metadata_hashes:
                            metadata_hashes[key] = metadata_records(manifest.distributions, names)
                        return metadata_hashes[key]

                    for suite, collection_unit in configured:
                        if collection_unit is None:
                            continue
                        row = observation(suite, "scoped")
                        effective, options, selectors, disabled = invocations[suite.name]
                        registry = [
                            entry
                            for entry in manifest.registry
                            if entry["file"] in collection_unit.tests
                        ]
                        inventory_key = fingerprint(
                            {
                                **common,
                                "suite": suite.identity(),
                                "all_args": effective,
                                "kind": "collection",
                                "inputs": [record_map[name] for name in collection_unit.inputs],
                                "absent": collection_unit.absent,
                                "metadata": metadata_identity(collection_unit.distributions),
                                "files": collection_unit.tests,
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
                        session_started()
                        for attempt in range(0 if reused else 2):
                            try:
                                with sandbox.invocation(manifest.root) as directory:
                                    frozen = directory / "project"
                                    materialize(pool, collection_unit, frozen, record_map)
                                    suite_directories(frozen, suite)
                                    metadata = directory / "metadata"
                                    materialize_metadata(
                                        manifest.distributions,
                                        collection_unit.distributions,
                                        metadata,
                                    )
                                    collected = sandbox.execute(
                                        box,
                                        frozen,
                                        _session_arguments(
                                            suite,
                                            options,
                                            ["/project/" + name for name in collection_unit.tests],
                                        ),
                                        environment,
                                        timeout,
                                        root=manifest.root,
                                        invocation_directory=directory,
                                        suite_root=suite.root,
                                        suite_cwd=suite.cwd,
                                        suite=suite.name,
                                        metadata_directory=metadata,
                                        selection_disabled=disabled,
                                        allow_unknown_selectors=True,
                                        request={
                                            "mode": "collection",
                                            "registry": registry,
                                            "selectors": selectors,
                                        },
                                    )
                                break
                            except InfrastructureError as exc:
                                if attempt == 1 or isinstance(exc, sandbox.CleanupError):
                                    row["outcome"] = "infrastructure_error"
                                    stop("infrastructure_error", str(exc))
                                    return summary()
                        if collected["exit_code"] == 4:
                            raise DeclarationError(
                                "unsupported or changed declarations during sandboxed collection"
                            )
                        if collected["exit_code"] not in (0, 5):
                            row.update(outcome="failed", worker_seconds=collected["worker_seconds"])
                            stop("failed", "sandboxed collection failed", collected["failures"])
                            return summary()
                        if not inventory_matches_manifest(collected, manifest) or not all(
                            scoped_item(manifest, item) for item in collected["items"]
                        ):
                            raise InfrastructureError(
                                "collected inventory disagrees with frozen declarations"
                            )
                        if enabled_inventory and collected["exit_code"] == 0 and not reused:
                            pending.stage(
                                inventory_path, inventory_key, collected, kind="collection"
                            )
                        row.update(
                            outcome="reused" if reused else "executed",
                            collected=len(collected["items"]),
                            worker_seconds=0 if reused else collected["worker_seconds"],
                            inventory_digest=fingerprint(
                                sorted(
                                    [suite.name, item["case_id"], item["file"]]
                                    for item in collected["items"]
                                )
                            ),
                        )
                        if len(results) + len(scoped) + len(collected["items"]) > 100_000:
                            raise DeclarationError("combined suite inventory exceeds 100000 cases")
                        for item in collected["items"]:
                            if item["file"] not in suite.inventory:
                                raise DeclarationError(
                                    "collected item is outside its structural suite"
                                )
                            seen_aliases.update(item["aliases"])
                            scoped.append((suite, item, collected["items"]))
                    if not ordinary_sessions(scoped):
                        return summary()
                    if requested_selectors - seen_aliases:
                        raise DeclarationError("selector does not identify a collected case")

                    jobs, scopes, digests = [], {}, {}
                    progress["scoped_rows"] = True
                    for suite, item, inventory in scoped:
                        index = len(results)
                        if not item["selected"]:
                            results.append(
                                _row(
                                    item,
                                    suite,
                                    "scoped",
                                    "not-selected",
                                    "not selected by pytest arguments" if explain else "",
                                )
                            )
                            continue
                        signature = (suite.name, item["file"], tuple(item["origins"]))
                        if signature not in scopes:
                            unit = case_unit(manifest, item, suite)
                            inputs = tuple(unit.inputs)
                            if inputs not in frozen_scope_hashes:
                                frozen_scope_hashes[inputs] = fingerprint(
                                    [record_map[name] for name in unit.inputs]
                                )
                            scopes[signature] = (
                                unit,
                                fingerprint(
                                    {
                                        "inputs": frozen_scope_hashes[inputs],
                                        "absent": unit.absent,
                                        "metadata": metadata_identity(unit.distributions),
                                        "cache": unit.cache,
                                    }
                                ),
                            )
                        unit, scope_key = scopes[signature]
                        if (suite.name, item["file"]) not in digests:
                            digests[suite.name, item["file"]] = _file_digest(
                                inventory, item["file"]
                            )
                        digest = digests[suite.name, item["file"]]
                        effective, options, _, _ = invocations[suite.name]
                        results.append(
                            _row(
                                item,
                                suite,
                                "scoped",
                                "infrastructure_error",
                                "execution not started",
                            )
                        )
                        jobs.append(
                            dict(
                                index=index,
                                item=item,
                                unit=unit,
                                suite=suite,
                                options=options,
                                registry=[
                                    entry
                                    for entry in manifest.registry
                                    if entry["file"] == item["file"]
                                ],
                                digest=digest,
                                scope=scope_key,
                                enabled=unit.cache and not no_cache,
                                identity={
                                    **common,
                                    "suite": suite.identity(),
                                    "all_args": effective,
                                    "kind": "execution",
                                    "scope": scope_key,
                                    "inventory": digest,
                                },
                            )
                        )

                    candidates = group_jobs(jobs, mode, state)
                    jobs = []
                    for candidate in candidates:
                        candidate["group_index"] = len(groups)
                        projected, group = project_result(candidate, None, mode)
                        groups.append(group)
                        for index, row in projected:
                            results[index] = row
                    progress["grouping_complete"] = True
                    for candidate in candidates:
                        previous = (
                            read_record(
                                candidate["path"],
                                candidate["key"],
                                kind="batch" if mode == "batch" else "execution",
                                case_id=candidate["case_ids"][0],
                                group_id=candidate["key"],
                                case_ids=candidate["case_ids"],
                            )
                            if candidate["enabled"] and not force
                            else None
                        )
                        if previous is None:
                            jobs.append(candidate)
                        else:
                            projected, group = project_result(
                                candidate, previous, mode, reused=True
                            )
                            groups[candidate["group_index"]] = group
                            for index, row in projected:
                                results[index] = row

                    def execute_job(job):
                        item, unit, suite = job["item"], job["unit"], job["suite"]
                        reason = (
                            "forced"
                            if force
                            else "non-cacheable"
                            if not job["enabled"]
                            else "no valid successful group record"
                        )
                        attempt_started = time.monotonic()
                        for attempt in range(2):
                            try:
                                if cancellation.is_set():
                                    raise sandbox.InvocationInterrupted(
                                        "run cancelled before sandbox start"
                                    )
                                with sandbox.invocation(manifest.root) as directory:
                                    frozen = directory / "project"
                                    materialize(pool, unit, frozen, record_map)
                                    suite_directories(frozen, suite)
                                    metadata = directory / "metadata"
                                    materialize_metadata(
                                        manifest.distributions, unit.distributions, metadata
                                    )
                                    request = dict(
                                        mode="batch" if mode == "batch" else "execution",
                                        registry=job["registry"],
                                        expected_digest=job["digest"],
                                    )
                                    if mode == "batch":
                                        request.update(
                                            group_id=job["key"], case_ids=job["case_ids"]
                                        )
                                    else:
                                        request["case_id"] = item["case_id"]
                                    result = sandbox.execute(
                                        box,
                                        frozen,
                                        _session_arguments(
                                            suite, job["options"], ["/project/" + item["file"]]
                                        ),
                                        environment,
                                        timeout,
                                        root=manifest.root,
                                        invocation_directory=directory,
                                        suite_root=suite.root,
                                        suite_cwd=suite.cwd,
                                        suite=suite.name,
                                        metadata_directory=metadata,
                                        cancellation=cancellation,
                                        request=request,
                                    )
                                if result["exit_code"] == 4:
                                    raise DeclarationError(
                                        "case collection or declarations differ from validated inventory"
                                    )
                                projected, group = project_result(
                                    job,
                                    result,
                                    mode,
                                    attempts=attempt + 1,
                                    reason=reason if explain else "",
                                    attempt_seconds=time.monotonic() - attempt_started,
                                )
                                return projected, group, result, False
                            except (InfrastructureError, OSError) as exc:
                                terminal = isinstance(exc, sandbox.CleanupError)
                                if terminal:
                                    cancellation.set()
                                if attempt == 1 or terminal:
                                    projected, group = project_result(
                                        job,
                                        None,
                                        mode,
                                        attempts=attempt + 1,
                                        reason=str(exc)
                                        if isinstance(exc, InfrastructureError)
                                        else "owned storage operation failed",
                                        attempt_seconds=time.monotonic() - attempt_started,
                                    )
                                    group["timing_complete"] = False
                                    if isinstance(exc, sandbox.InvocationInterrupted):
                                        group["stop_reason"] = "cancelled"
                                    return projected, group, None, terminal

                    def accept(job, projected, group):
                        groups[job["group_index"]] = group
                        for index, row in projected:
                            results[index] = row

                    active, cursor = {}, 0
                    executor = ThreadPoolExecutor(max_workers=concurrency)
                    try:
                        while cursor < len(jobs) or active:
                            while (
                                not cancellation.is_set()
                                and cursor < len(jobs)
                                and len(active) < concurrency
                            ):
                                job = jobs[cursor]
                                cursor += 1
                                active[executor.submit(execute_job, job)] = job
                            if not active:
                                break
                            completed, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                            for future in completed:
                                job = active.pop(future)
                                projected, group, result, terminal = future.result()
                                accept(job, projected, group)
                                if terminal:
                                    blocked = (
                                        "run stopped after owned cleanup failure or cancellation"
                                    )
                                if (
                                    result is not None
                                    and result["exit_code"] == 0
                                    and job["enabled"]
                                    and not cancellation.is_set()
                                ):
                                    pending.stage(job["path"], job["key"], result)
                                    eligible_groups.add(job["key"])
                    except (sandbox.InvocationInterrupted, KeyboardInterrupt):
                        blocked = (
                            "run cancelled; active sandboxes drained before result publication"
                        )
                        cancellation.set()
                    except BaseException:
                        cancellation.set()
                        raise
                    finally:
                        executor.shutdown(wait=True, cancel_futures=False)
                        for future, job in active.items():
                            if future.done() and not future.cancelled():
                                projected, group, _, terminal = future.result()
                                accept(job, projected, group)
                                if terminal:
                                    blocked = (
                                        "run stopped after owned cleanup failure or cancellation"
                                    )
                        if cancellation.is_set():
                            blocked = blocked or "run cancelled"
                            for job in jobs[cursor:]:
                                groups[job["group_index"]]["stop_reason"] = "cancelled"
                                for index, _ in job["members"]:
                                    results[index]["reason"] = blocked
                if blocked is None:
                    pending.publish()
                    progress["published"] = True
                else:
                    # A completed success is not reusable until every owned copy is removed.
                    stop("infrastructure_error", blocked)
    except (InfrastructureError, OSError) as exc:
        stop(
            "infrastructure_error",
            str(exc) if isinstance(exc, InfrastructureError) else "owned storage operation failed",
        )
        for row in results:
            if row["outcome"] == "infrastructure_error":
                row["reason"] = collection["reason"]
    return summary()


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
        "schema": RESULT_SCHEMA,
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
    subparsers.add_parser("plan", help="preview routes and literal declarations without collecting")
    execution = subparsers.add_parser(
        "run", help="run ordinary cases and execute or reuse each scoped case"
    )
    execution.add_argument("--force", action="store_true")
    execution.add_argument("--no-cache", action="store_true")
    execution.add_argument("--explain", action="store_true")
    execution.add_argument("--cache-dir", type=Path)
    execution.add_argument("--timeout", type=float, default=60)
    execution.add_argument("--execution-mode", choices=("case", "batch"))
    execution.add_argument("--workers", type=int)
    execution.add_argument("--suite", action="append", dest="suites")
    execution.add_argument("arguments", nargs=argparse.REMAINDER)
    try:
        options = parser.parse_args(argv)
        if options.command == "plan":
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
                workers=options.workers,
                suites=options.suites,
                execution_mode=options.execution_mode,
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
