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
from .frozen_registry import FrozenRegistry
from .model import (
    DeclarationError,
    InfrastructureError,
    UnsupportedCapability,
    case_unit,
    environment_names,
    fingerprint,
    load,
    materialize,
    render_json,
    snapshot,
)

# Cache record format; records from earlier backends are never eligible.
SCHEMA = 8
RESULT_SCHEMA = 4
OUTCOMES = ("executed", "reused", "failed", "unsupported", "infrastructure_error", "not-selected")
ROUTES = ("ordinary", "scoped")


class _FixtureDrift(InfrastructureError):
    """A guarded execution cannot safely continue or publish results."""


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


def read_record(
    path,
    key,
    *,
    kind="execution",
    case_id=None,
    group_id=None,
    case_ids=None,
    registry_id=None,
    graph_id=None,
):
    limit = 64 * 1024 * 1024 if kind in {"collection", "batch", "guarded-batch"} else 1_048_576
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(limit + 1)
        if len(data) > limit:
            return None
        record = json.loads(data)
        if type(record) is not dict or set(record) != {"schema", "key", "result", "checksum"}:
            return None
        result = record["result"]
        if (
            type(record["schema"]) is not int
            or record["schema"] != SCHEMA
            or record["key"] != key
            or record["checksum"] != fingerprint(result)
        ):
            return None
        from .result import (
            validate_batch,
            validate_collection,
            validate_guarded_batch,
            validate_result,
        )

        valid = (
            validate_collection(result, successful_only=True, context_id=key)
            if kind == "collection"
            else validate_guarded_batch(
                result,
                successful_only=True,
                group_id=group_id,
                case_ids=case_ids,
                registry_id=registry_id,
                graph_id=graph_id,
            )
            if kind == "guarded-batch"
            else validate_batch(
                result, successful_only=True, group_id=group_id, case_ids=case_ids, context_id=key
            )
            if kind == "batch"
            else validate_result(result, successful_only=True, case_id=case_id, context_id=key)
        )
        if not valid:
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def write_record(path, key, result, *, kind="execution"):
    limit = (
        64 * 1024 * 1024
        if kind in {"collection", "guarded-batch"} or result["protocol"] == 10
        else 1_048_576
    )
    record = {
        "schema": SCHEMA,
        "key": key,
        "result": result,
        "checksum": fingerprint(result),
    }
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

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        path = final.with_name(key + "." + self.token + ".pending")
        write_record(path, key, result, kind="guarded-batch" if guarded else kind)
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
    from .result import ownership_matches

    files = set(manifest.inventory)
    if any(skip["file"] not in files for skip in result["collection_skips"]):
        return False
    for item in result["items"]:
        if item["file"] not in files or not ownership_matches(item, manifest.ownership_index):
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
        reports=[],
    )


def _session_arguments(root, suite, options, files):
    """Arguments of one sandboxed pytest session, rooted at the project's host path.

    The sandbox shows the frozen view there, so test files, configuration and the root
    directory have the same paths as an editable install of the project names.
    """
    return [
        "-c",
        str(root / suite.config),
        "--rootdir=" + str(root / suite.root),
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        *[part for plugin in suite.plugins for part in ("-p", plugin)],
        *options,
        *[str(root / name) for name in files],
    ]


def _file_digest(inventory, filename):
    """Containing-file inventory that every scoped execution must collect again."""
    return fingerprint(
        sorted(
            (
                {key: entry[key] for key in ("case_id", "origins", "owners", "parameters")}
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
        group_id=result["context_id"],
        suite=suite.name,
        files=sorted(files),
        route="ordinary",
        profile="ordinary-v2",
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
        collection_skips=[{**skip, "phase": "collect"} for skip in result["collection_skips"]],
    )


def _guard_conditions(fixture_guard, condition_checks):
    """Validate transient caller predicates without rendering their values."""
    if fixture_guard not in (None, "available-v1") or (
        fixture_guard is not None and type(fixture_guard) is not str
    ):
        raise DeclarationError("invalid fixture guard")
    if type(condition_checks) not in (tuple, list) or len(condition_checks) > 4096:
        raise DeclarationError("invalid fixture guard conditions")
    if fixture_guard is None and condition_checks:
        raise DeclarationError("fixture guard conditions require the fixture guard")
    seen, total = set(), 0
    for row in condition_checks:
        if type(row) is not dict:
            raise DeclarationError("invalid fixture guard condition")
        op = row.get("op")
        keys = {"case_id", "name", "op", "value"} if op == "equals" else {"case_id", "name", "op"}
        if set(row) != keys or type(op) is not str or op not in ("absent", "present", "equals"):
            raise DeclarationError("invalid fixture guard condition")
        case, name = row["case_id"], row["name"]
        if type(case) is not str or not re.fullmatch(r"[0-9a-f]{64}", case):
            raise DeclarationError("invalid fixture guard condition")
        if type(name) is not str or environment_names([name]) != [name]:
            raise DeclarationError("invalid fixture guard condition")
        if (case, name, op) in seen:
            raise DeclarationError("duplicate fixture guard condition")
        seen.add((case, name, op))
        if op == "equals":
            value = row["value"]
            if type(value) is not str:
                raise DeclarationError("invalid fixture guard condition")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                size = None
            if size is None:
                raise DeclarationError("invalid fixture guard condition")
            total += size
            if size > 4096 or total > 1_048_576:
                raise DeclarationError("fixture guard condition limit exceeded")
    return condition_checks


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
    fixture_guard=None,
    condition_checks=(),
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
    from .tool_capture import Capture
    from .tool_context import validate_environment

    condition_checks = _guard_conditions(fixture_guard, condition_checks)
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
    if fixture_guard is not None:
        if mode != "batch" or any(manifest.ordinary_files[suite.name] for suite in manifest.suites):
            raise UnsupportedCapability(
                "execution:fixture-profile", "fixture guard requires scoped batch execution"
            )
        from .fixture_guard import framework_definition_ids

        framework_definition_ids(version("pytest"))
    concurrency = worker_count(manifest.execution, workers)
    profile = "batch-available-v1" if fixture_guard else mode + "-v2"
    configured = list(zip(manifest.suites, manifest.units))
    all_tests = set().union(*(set(suite.inventory) for suite, _ in configured))
    all_unit = Unit("all", sorted(all_tests), [], [], True)
    invocations, requested_selectors, seen_aliases, seen_files = {}, set(), set(), set()
    for suite, _ in configured:
        effective = suite_arguments(suite, arguments)
        selected_arguments(effective, all_unit, all_tests)
        # Structural membership is not an explicit request for a collected case.
        options, selectors = split_arguments(effective)
        own = [selector for selector in selectors if selector.split("::", 1)[0] in suite.inventory]
        hashed = [hashlib.sha256(selector.encode()).hexdigest() for selector in own]
        requested_selectors.update(hashed)
        invocations[suite.name] = (effective, options, hashed, bool(selectors and not own))
    declared_environment = set(manifest.env) | set(manifest.defaults.env)
    for declaration in manifest.declarations.values():
        declared_environment.update(declaration.env)
    environment = {name: os.environ.get(name) for name in sorted(declared_environment)}

    def unit_environment(unit):
        validate_environment(unit.env, unit.tools)
        return {name: environment[name] for name in unit.env}

    observations = [
        dict(
            name=suite.name,
            route=route,
            files=files,
            outcome="not-started",
            collected=None,
            inventory_digest=None,
            worker_seconds=None if route == "ordinary" else 0,
            skips=[],
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
    progress = dict(
        grouping_complete=False, published=False, setup=None, scoped_rows=False, parked=0
    )
    guard_state = {"admitted": False, "conditions": []}

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
        represented = {(row["suite"], row["case_id"]) for row in results}
        for suite, item, _ in scoped:
            if (suite.name, item["case_id"]) not in represented:
                results.append(
                    _row(item, suite, "scoped", "not-selected")
                    if not item["selected"]
                    else _row(
                        item, suite, "scoped", "infrastructure_error", "execution not started"
                    )
                )
        progress["parked"] = sum(
            row["selected"]
            and row["group_id"] is None
            and row["attempts"] == 0
            and row["outcome"] == "infrastructure_error"
            for row in results
        )
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
        # Parked cases share the infrastructure_error count but are not an outage: the
        # run stopped for the collection outcome that parked them.
        outages = counts["infrastructure_error"] - progress["parked"]
        if (
            outages
            or collection["outcome"] == "infrastructure_error"
            or any(group["outcome"] == "infrastructure_error" for group in groups)
        ):
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
            and not any(
                group["outcome"] == "infrastructure_error"
                or group["stop_reason"] in {"infrastructure", "cancelled"}
                for group in groups
            )
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
            skips=[
                dict(suite=row["name"], route=row["route"], **skip)
                for row in observations
                for skip in row["skips"]
            ],
        )
        report = dict(
            schema=5 if fixture_guard else RESULT_SCHEMA,
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
        if fixture_guard:
            report["fixture_guard"] = {
                "profile": fixture_guard,
                "state": "admitted" if guard_state["admitted"] else "unavailable",
                "conditions": guard_state["conditions"],
            }
        return report

    def retain_refusal(error):
        if progress["setup"] is not None:
            from . import model
            from .accounting import FIELDS, RefusalAccounting

            observed = summary()
            metadata = error_result(error, fixture_guard=fixture_guard)
            fields = FIELDS
            if fixture_guard:
                metadata["fixture_guard"] = observed["fixture_guard"]
                fields = fields | {"fixture_guard"}
            try:
                accounting = RefusalAccounting(
                    {name: observed[name] for name in fields},
                    metadata=metadata,
                    maximum=model.MAX_OUTPUT_BYTES,
                )
                error.attach_accounting(accounting)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                raise InfrastructureError("cannot retain valid bounded refusal accounting") from exc

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
                result = ordinary.session(manifest, suite, files, options, selectors, disabled)
            except InfrastructureError as exc:
                if isinstance(exc, sandbox.CleanupError):
                    raise
                row["outcome"] = "infrastructure_error"
                stop("infrastructure_error", str(exc))
                return False
            row["skips"] = [{**skip, "phase": "collect"} for skip in result["collection_skips"]]
            if not result["inventory_complete"] and not result["declaration_error"]:
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
            seen_files.update(hashlib.sha256(name.encode()).hexdigest() for name in files)
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
                if item["case_id"] not in outcomes:
                    if result["declaration_error"] and not result["dispatch_started"]:
                        # Collection observed this selection, but no execution
                        # membership was established before its typed refusal.
                        results.append(_row(item, suite, "ordinary", "infrastructure_error"))
                        continue
                    raise InfrastructureError("ordinary result omitted an execution member")
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
                    reports=outcomes[item["case_id"]]["reports"],
                )
                results.append(unit)
            groups.append(group)
            row.update(
                outcome="failed" if result["declaration_error"] else "executed",
                collected=len(result["items"]),
                inventory_digest=None
                if result["declaration_error"]
                else fingerprint(
                    sorted([suite.name, item["case_id"], item["file"]] for item in result["items"])
                ),
            )
            if result["declaration_error"]:
                # A strict incomplete inventory can still carry genuine member
                # reports. Retain them before raising; never plan or replay it.
                raise DeclarationError(
                    "unsupported or changed declarations during ordinary collection"
                )
        return True

    if not manifest.scope_possible:
        if fixture_guard and condition_checks:
            from .fixture_guard import evaluate_conditions

            evaluate_conditions(condition_checks, [])
        # All-ordinary: no sandbox, frozen copy, cache directory or lock.
        try:
            if ordinary_sessions(scoped):
                if requested_selectors - seen_aliases - seen_files:
                    raise DeclarationError("selector does not identify a collected case")
                progress["grouping_complete"] = True
            return summary()
        except DeclarationError as error:
            retain_refusal(error)
            raise

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
                with (
                    sandbox.invocation(manifest.root) as pool_directory,
                    Capture(manifest, pool_directory, timeout, cancellation, sandbox=box) as tools,
                ):
                    pool = pool_directory / "project"
                    records = snapshot(manifest, manifest.pool, pool)
                    frozen_registry = FrozenRegistry(
                        pool, records, manifest.pool.tool_links, manifest.scans
                    )
                    for suite, _ in configured:
                        suite_directories(pool, suite)
                    record_map = {record["path"]: record for record in records}
                    tools.verify_project(record_map)
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
                        registry = frozen_registry.for_view(collection_unit)
                        collection_tools = tools.view(collection_unit.tools)
                        collection_environment = unit_environment(collection_unit)
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
                                **(
                                    {"tools": collection_tools.identity}
                                    if collection_unit.tools
                                    else {}
                                ),
                                "environment": fingerprint(collection_environment),
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
                                    if frozen_registry.for_view(collection_unit) != registry:
                                        raise InfrastructureError(
                                            "frozen collection registry changed"
                                        )
                                    collected = sandbox.execute(
                                        box,
                                        frozen,
                                        _session_arguments(
                                            manifest.root, suite, options, collection_unit.tests
                                        ),
                                        collection_environment,
                                        timeout,
                                        root=manifest.root,
                                        invocation_directory=directory,
                                        suite_root=suite.root,
                                        suite_cwd=suite.cwd,
                                        suite=suite.name,
                                        metadata_directory=metadata,
                                        tool_view=tools.view(collection_unit.tools),
                                        selection_disabled=disabled,
                                        allow_unknown_selectors=True,
                                        request={
                                            "mode": "collection",
                                            "registry": registry,
                                            "selectors": selectors,
                                            "context_id": inventory_key,
                                        },
                                    )
                                break
                            except InfrastructureError as exc:
                                if attempt == 1 or isinstance(exc, sandbox.CleanupError):
                                    row["outcome"] = "infrastructure_error"
                                    stop("infrastructure_error", str(exc))
                                    return summary()
                        row["skips"] = [
                            {**skip, "phase": "collect"} for skip in collected["collection_skips"]
                        ]
                        if collected["declaration_error"]:
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
                        seen_files.update(
                            hashlib.sha256(name.encode()).hexdigest()
                            for name in collection_unit.tests
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
                    if requested_selectors - seen_aliases - seen_files:
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
                            tool_view = tools.view(unit.tools)
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
                                        "registry": frozen_registry.for_view(unit),
                                        **({"tools": tool_view.identity} if unit.tools else {}),
                                        "environment": fingerprint(unit_environment(unit)),
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
                                environment=unit_environment(unit),
                                suite=suite,
                                options=options,
                                registry=frozen_registry.for_view(unit),
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
                    if fixture_guard:
                        from .fixture_guard import (
                            evaluate_conditions,
                            guarded_group_id,
                            require_capture_match,
                            require_conservation,
                        )

                        def inspect_candidate(candidate, inspection_mode):
                            suite, unit = candidate["suite"], candidate["unit"]
                            for attempt in range(2):
                                try:
                                    with sandbox.invocation(manifest.root) as directory:
                                        frozen = directory / "project"
                                        materialize(pool, unit, frozen, record_map)
                                        suite_directories(frozen, suite)
                                        metadata = directory / "metadata"
                                        materialize_metadata(
                                            manifest.distributions, unit.distributions, metadata
                                        )
                                        if frozen_registry.for_view(unit) != candidate["registry"]:
                                            raise InfrastructureError(
                                                "frozen fixture inspection registry changed"
                                            )
                                        request = {
                                            "mode": inspection_mode,
                                            "registry": candidate["registry"],
                                            "expected_digest": candidate["digest"],
                                            "group_id": candidate["key"],
                                            "case_ids": candidate["case_ids"],
                                            "context_id": candidate["key"],
                                        }
                                        if inspection_mode == "guard-inspection":
                                            request["registry_id"] = candidate["capture"][
                                                "registry_id"
                                            ]
                                        return sandbox.execute(
                                            box,
                                            frozen,
                                            _session_arguments(
                                                manifest.root,
                                                suite,
                                                candidate["options"],
                                                [candidate["item"]["file"]],
                                            ),
                                            candidate["environment"],
                                            timeout,
                                            root=manifest.root,
                                            invocation_directory=directory,
                                            suite_root=suite.root,
                                            suite_cwd=suite.cwd,
                                            suite=suite.name,
                                            metadata_directory=metadata,
                                            tool_view=tools.view(unit.tools),
                                            request=request,
                                        )
                                except (InfrastructureError, OSError) as exc:
                                    if attempt == 1 or isinstance(exc, sandbox.CleanupError):
                                        if isinstance(exc, OSError):
                                            raise InfrastructureError(
                                                "fixture inspection storage failed"
                                            ) from exc
                                        raise

                        inspections = []
                        for candidate in candidates:
                            candidate["capture"] = inspect_candidate(candidate, "guard-capture")
                            candidate["key"] = guarded_group_id(
                                candidate["key"],
                                candidate["capture"]["registry_id"],
                                candidate["capture"]["content_id"],
                            )
                            candidate["path"] = state / (candidate["key"] + ".json")
                            candidate["profile"] = profile
                            candidate["inspection"] = inspect_candidate(
                                candidate, "guard-inspection"
                            )
                            require_capture_match(candidate["capture"], candidate["inspection"])
                            inspections.append(candidate["inspection"])
                        require_conservation(inspections, candidates)
                        guard_state["conditions"] = evaluate_conditions(
                            condition_checks, candidates
                        )
                        guard_state["admitted"] = True
                    jobs = []
                    for candidate in candidates:
                        candidate["group_index"] = len(groups)
                        projected, group = project_result(candidate, None, mode)
                        groups.append(group)
                        for index, row in projected:
                            results[index] = row
                    progress["grouping_complete"] = True
                    for candidate in candidates:
                        tools.verify(candidate["unit"].tools)
                        previous = (
                            read_record(
                                candidate["path"],
                                candidate["key"],
                                kind="guarded-batch"
                                if fixture_guard
                                else "batch"
                                if mode == "batch"
                                else "execution",
                                case_id=candidate["case_ids"][0],
                                group_id=candidate["key"],
                                case_ids=candidate["case_ids"],
                                registry_id=candidate["inspection"]["registry_id"]
                                if fixture_guard
                                else None,
                                graph_id=candidate["inspection"]["graph_id"]
                                if fixture_guard
                                else None,
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
                        sessions_started = 0

                        def session_started():
                            nonlocal sessions_started
                            sessions_started += 1

                        for attempt in range(2):
                            result = None
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
                                    if frozen_registry.for_view(unit) != job["registry"]:
                                        raise InfrastructureError("frozen case registry changed")
                                    request = dict(
                                        mode="guard-batch"
                                        if fixture_guard
                                        else "batch"
                                        if mode == "batch"
                                        else "execution",
                                        registry=job["registry"],
                                        expected_digest=job["digest"],
                                        context_id=job["key"],
                                    )
                                    if mode == "batch":
                                        request.update(
                                            group_id=job["key"], case_ids=job["case_ids"]
                                        )
                                        if fixture_guard:
                                            request.update(
                                                guard_protocol="batch-guard-v1",
                                                registry_id=job["inspection"]["registry_id"],
                                                graph_id=job["inspection"]["graph_id"],
                                            )
                                    else:
                                        request["case_id"] = item["case_id"]
                                    result = sandbox.execute(
                                        box,
                                        frozen,
                                        _session_arguments(
                                            manifest.root, suite, job["options"], [item["file"]]
                                        ),
                                        job["environment"],
                                        timeout,
                                        root=manifest.root,
                                        invocation_directory=directory,
                                        suite_root=suite.root,
                                        suite_cwd=suite.cwd,
                                        suite=suite.name,
                                        metadata_directory=metadata,
                                        tool_view=tools.view(unit.tools),
                                        cancellation=cancellation,
                                        on_session_start=session_started,
                                        request=request,
                                    )
                                if result["declaration_error"]:
                                    raise DeclarationError(
                                        "case collection or declarations differ from validated inventory"
                                    )
                                projected, group = project_result(
                                    job,
                                    result,
                                    mode,
                                    attempts=sessions_started,
                                    reason=reason if explain else "",
                                    attempt_seconds=time.monotonic() - attempt_started,
                                )
                                return projected, group, result, False, None
                            except DeclarationError as exc:
                                cancellation.set()
                                projected, group = project_result(
                                    job,
                                    result,
                                    mode,
                                    attempts=sessions_started,
                                    reason=str(exc),
                                    attempt_seconds=time.monotonic() - attempt_started,
                                )
                                if result is None:
                                    group["timing_complete"] = False
                                return projected, group, None, False, exc
                            except (InfrastructureError, OSError) as exc:
                                terminal = isinstance(
                                    exc, (sandbox.CleanupError, _FixtureDrift)
                                ) or (bool(fixture_guard) and attempt == 1)
                                if terminal:
                                    cancellation.set()
                                if attempt == 1 or terminal:
                                    projected, group = project_result(
                                        job,
                                        None,
                                        mode,
                                        attempts=sessions_started,
                                        reason=str(exc)
                                        if isinstance(exc, InfrastructureError)
                                        else "owned storage operation failed",
                                        attempt_seconds=time.monotonic() - attempt_started,
                                    )
                                    group["timing_complete"] = False
                                    if isinstance(exc, sandbox.InvocationInterrupted):
                                        group["stop_reason"] = "cancelled"
                                    return projected, group, None, terminal, None

                    refusal = None

                    def accept(job, projected, group, terminal, error):
                        nonlocal blocked, refusal
                        groups[job["group_index"]] = group
                        for index, row in projected:
                            results[index] = row
                        if error is not None and refusal is None:
                            refusal = error
                            cancellation.set()
                        if terminal:
                            blocked = (
                                "run stopped after infrastructure failure or cancellation"
                                if fixture_guard
                                else "run stopped after owned cleanup failure or cancellation"
                            )

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
                                projected, group, result, terminal, error = future.result()
                                accept(job, projected, group, terminal, error)
                                if (
                                    result is not None
                                    and result["exit_code"] == 0
                                    and job["enabled"]
                                    and not cancellation.is_set()
                                ):
                                    pending.stage(
                                        job["path"], job["key"], result, guarded=bool(fixture_guard)
                                    )
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
                                projected, group, _, terminal, error = future.result()
                                accept(job, projected, group, terminal, error)
                        if cancellation.is_set():
                            blocked = blocked or "run cancelled"
                            for job in jobs[cursor:]:
                                groups[job["group_index"]]["stop_reason"] = "cancelled"
                                for index, _ in job["members"]:
                                    results[index]["reason"] = blocked
                    if refusal is not None:
                        raise refusal
                    tools.verify(tuple(manifest.tools))
                if blocked is None:
                    pending.publish()
                    progress["published"] = True
                else:
                    # A completed success is not reusable until every owned copy is removed.
                    stop("infrastructure_error", blocked)
    except DeclarationError as error:
        retain_refusal(error)
        raise
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


def error_result(error, *, fixture_guard=None):
    """Versioned typed refusal, retaining established inventory and observations."""
    if fixture_guard is not None and (
        type(fixture_guard) is not str or fixture_guard != "available-v1"
    ):
        raise DeclarationError("invalid fixture guard")
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
    if fixture_guard is not None:
        result["schema"] = 5
        result["fixture_guard"] = {
            "profile": "available-v1",
            "state": "unavailable",
            "conditions": [],
        }
    if isinstance(error, DeclarationError) and error.accounting is not None:
        result["inventory_complete"] = error.accounting.inventory_complete
        result["inventory_digest"] = error.accounting.inventory_digest
        result["accounting"] = error.accounting.render()
        if fixture_guard is not None:
            if error.accounting.fixture_guard is None:
                raise InfrastructureError("guarded refusal accounting profile mismatch")
            result["fixture_guard"] = error.accounting.fixture_guard
        elif error.accounting.fixture_guard is not None:
            raise InfrastructureError("unguarded refusal accounting profile mismatch")
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
    execution.add_argument("--fixture-guard", choices=("available-v1",))
    execution.add_argument("--workers", type=int)
    execution.add_argument("--suite", action="append", dest="suites")
    execution.add_argument("arguments", nargs=argparse.REMAINDER)
    guard_context = None
    try:
        options = parser.parse_args(argv)
        if options.command == "run":
            guard_context = options.fixture_guard
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
                fixture_guard=options.fixture_guard,
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
        print(
            render_json(
                error_result(
                    InfrastructureError("invocation interrupted"), fixture_guard=guard_context
                )
            )
        )
        return 3
    except DeclarationError as exc:
        print(render_json(error_result(exc, fixture_guard=guard_context)))
        return 4
    except InfrastructureError as exc:
        print(render_json(error_result(exc, fixture_guard=guard_context)))
        return 3
    except (OSError, RecursionError, MemoryError):
        print(
            render_json(
                error_result(
                    InfrastructureError("bounded project storage or parsing operation failed"),
                    fixture_guard=guard_context,
                )
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
