"""One fresh ordinary pytest session per suite, without scoped cache authority."""

import hashlib
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import process, python_source
from .execution import effective_arguments
from .model import DeclarationError, InfrastructureError, Unit, fingerprint
from .result import validate_ordinary

COUNT_KEYS = (
    "executed",
    "reused",
    "failed",
    "unsupported",
    "infrastructure_error",
    "not-selected",
    "selected",
)


def session(manifest, suite, files, arguments, timeout, *, expected_items=(), expected_files=()):
    from .controller import selected_arguments, split_arguments

    effective = effective_arguments(suite, arguments)
    parsed = (
        selected_arguments(
            effective, Unit("all", manifest.inventory, [], [], False), set(manifest.inventory)
        )
        or []
    )
    options, selectors = split_arguments(parsed)
    own = [value for value in selectors if value.split("::", 1)[0] in suite.inventory]
    nonce = uuid.uuid4().hex
    request = dict(
        mode="ordinary",
        nonce=nonce,
        project_root=str(manifest.root),
        suite=suite.name,
        suite_root=suite.root,
        registry=[row for row in manifest.registry if row["file"] in files],
        selectors=[hashlib.sha256(value.encode()).hexdigest() for value in own],
        selection_disabled=bool(selectors and not own),
        allow_unknown_selectors=True,
        expected_items=list(expected_items),
        expected_files=list(expected_files),
        source_projection=None,
        metadata=False,
        python_paths=[],
        args=[
            "-c",
            str(manifest.root / suite.config),
            "--rootdir=" + str(manifest.root / suite.root),
            "-o",
            "addopts=",
            *[part for plugin in suite.plugins for part in ("-p", plugin)],
            *options,
            *[str(manifest.root / name) for name in files],
        ],
    )
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 64 * 1024 * 1024:
        raise InfrastructureError("ordinary request exceeds the bounded protocol")
    # Bootstrap this installed producer, preserving the invoking interpreter/environment.
    entry = "import sys;sys.path.insert(0,sys.argv.pop(1));from pytest_telescope.ordinary import child_main;raise SystemExit(child_main())"
    with tempfile.TemporaryDirectory(prefix="pytest-telescope-ordinary-") as directory:
        request_path = Path(directory) / "request.json"
        request_path.write_bytes(encoded)
        code, output = process.capture(
            [sys.executable, "-c", entry, str(Path(__file__).parent.parent), str(request_path)],
            timeout,
            cwd=manifest.root / suite.cwd,
        )
    try:
        frames = [
            line.removeprefix(b"TELESCOPE_RESULT ")
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        if len(frames) != 1:
            raise ValueError("ordinary result frame count")
        result = json.loads(frames[0])
        if not validate_ordinary(result, nonce) or result["exit_code"] != code:
            raise ValueError("ordinary result protocol")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InfrastructureError("ordinary pytest returned an invalid bounded result") from exc
    return result, own


def child_main():
    from .worker import main

    if len(sys.argv) != 2:
        raise ValueError("ordinary request requires one owned file")
    with open(sys.argv[1], "rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("ordinary request too large")
    request = json.loads(raw)
    return main(request, ordinary=True)


def run(manifest, arguments, timeout, *, mode, resources, controller, runtime_selection, backend):
    from .controller import inventory_matches_manifest

    started = time.monotonic()
    units, groups, observations = [], [], []
    status = "passed"
    selected_aliases, requested = set(), set()
    complete = True
    for suite in manifest.suites:
        files = manifest.ordinary_files[suite.name]
        if not files:
            continue
        result, selectors = session(manifest, suite, files, arguments, timeout)
        if not inventory_matches_manifest(result, manifest):
            raise InfrastructureError("ordinary inventory disagrees with literal declarations")
        requested.update(hashlib.sha256(value.encode()).hexdigest() for value in selectors)
        selected_aliases.update(alias for item in result["items"] for alias in item["aliases"])
        if len(units) + len(result["items"]) > 100_000:
            raise DeclarationError("combined suite inventory exceeds 100000 cases")
        group_id = fingerprint(
            {
                "profile": "ordinary-v1",
                "suite": suite.identity(),
                "case_ids": result["case_ids"],
                "args": effective_arguments(suite, arguments),
            }
        )
        case_results = {row["case_id"]: row for row in result["case_results"]}
        for item in result["items"]:
            case = case_results.get(item["case_id"])
            selected = item["selected"]
            result_status = case["outcome"] if case else None
            outcome = (
                "not-selected"
                if not selected
                else "executed"
                if result_status in ("passed", "skipped")
                else "failed"
                if result_status == "failed"
                else "unsupported"
                if result["exit_code"] == 4
                else "infrastructure_error"
            )
            units.append(
                dict(
                    name=item["case_id"],
                    case_id=item["case_id"],
                    selected=selected,
                    file=item["file"],
                    suite=suite.name,
                    route=None if result["exit_code"] == 4 else "ordinary",
                    outcome=outcome,
                    reason="ordinary-fresh" if selected else "not-selected",
                    attempts=1 if selected else 0,
                    collected=1,
                    passed=int(result_status == "passed"),
                    failed=int(result_status == "failed"),
                    skipped=int(result_status == "skipped"),
                    worker_seconds=None,
                    cpu_seconds=None,
                    failures=[
                        failure
                        for failure in result["failures"]
                        if failure["case_id"] == item["case_id"]
                    ],
                    group_id=group_id if selected else None,
                    result_status=result_status,
                    complete=case["complete"] if case else False,
                )
            )
        stop = None
        if not result["inventory_complete"]:
            stop = "collection"
        elif not result["complete"]:
            stop = "pytest-stop" if result["exit_code"] in (1, 2) else "infrastructure"
        groups.append(
            dict(
                group_id=group_id,
                suite=suite.name,
                files=sorted(files),
                route="ordinary",
                profile="ordinary-v1",
                case_ids=result["case_ids"],
                outcome="executed"
                if result["exit_code"] in (0, 5)
                else "failed"
                if result["exit_code"] in (1, 2)
                else "infrastructure_error",
                complete=result["complete"],
                cache_eligible=False,
                attempts=1,
                attempt_seconds=[result["worker_seconds"]],
                worker_seconds=result["worker_seconds"],
                cpu_seconds=None,
                timing_complete=False,
                passed=result["passed"],
                failed=result["failed"],
                skipped=result["skipped"],
                not_run=sum(row["outcome"] == "not-run" for row in case_results.values()),
                unresolved=0,
                stop_reason=stop,
                failures=result["failures"],
            )
        )
        observations.append(
            dict(
                name=suite.name,
                route="ordinary",
                files=files,
                outcome="executed" if result["inventory_complete"] else "failed",
                complete=result["inventory_complete"],
                collected=len(result["items"]),
                inventory_digest=fingerprint(
                    [[item["case_id"], item["file"]] for item in result["items"]]
                )
                if result["inventory_complete"]
                else None,
                accounting_group_id=group_id,
                worker_seconds=None,
                cpu_seconds=None,
                failures=[f for f in result["failures"] if f["phase"] == "collect"],
            )
        )
        complete = complete and result["inventory_complete"]
        if result["exit_code"] == 4:
            status = "declaration_error"
        elif result["exit_code"] == 3:
            status = "infrastructure_error"
        elif result["exit_code"] in (1, 2) and status == "passed":
            status = "failed"
        if not result["inventory_complete"]:
            break
    if requested - selected_aliases:
        raise DeclarationError("selector does not identify a collected case")
    counts = {key: 0 for key in COUNT_KEYS}
    routes = {route: dict(counts) for route in ("ordinary", "scoped", "unresolved")}
    for unit in units:
        route = unit["route"] or "unresolved"
        counts[unit["outcome"]] += 1
        routes[route][unit["outcome"]] += 1
        counts["selected"] += unit["selected"]
        routes[route]["selected"] += unit["selected"]
    if status == "passed" and not counts["selected"]:
        status = "no_tests"
    report = dict(
        schema=3,
        kind="run",
        status=status,
        inventory_complete=complete,
        routing_complete=complete,
        inventory_digest=fingerprint(
            [[u["suite"], u["case_id"], u["file"], u["route"]] for u in units]
        )
        if complete
        else None,
        grouping_complete=complete,
        suites=[{"name": suite.name, "files": suite.inventory} for suite in manifest.suites],
        execution={"mode": mode, "profile": mode + "-v1"},
        backend=backend,
        runtime=runtime_selection,
        python=python_source.envelope(manifest.suites),
        resources={
            "scoped": {
                "required": False,
                "validated": False,
                "configured": {"worker": resources.worker, "budget": resources.budget},
                "concurrency": None,
                "enforced": None,
            },
            "controller": {"configured": controller, "enforced": None},
            "ordinary": {
                "enforcement": "inherited-host",
                "slot_limit": 1,
                "peak_sessions": int(bool(groups)),
            },
        },
        counts=counts,
        route_counts=routes,
        units=units,
        groups=groups,
        cases={
            "collected": len(units),
            **{key: sum(u[key] for u in units) for key in ("passed", "failed", "skipped")},
        },
        case_outcomes={
            "fresh_passed": sum(u["passed"] for u in units),
            "cached_passed": 0,
            "fresh_skipped": sum(u["skipped"] for u in units),
            "cached_skipped": 0,
        },
        collection={
            "outcome": "executed" if complete else "failed",
            "collected": len(units),
            "worker_seconds": None,
            "cpu_seconds": None,
            "failures": [f for o in observations for f in o["failures"]],
            "suites": observations,
        },
        elapsed_seconds=time.monotonic() - started,
        setup_seconds=0,
        worker_cpu_seconds=None if groups else 0,
        known_scoped_cpu_seconds=0,
    )
    if status == "declaration_error":
        report["error"] = "Unsupported or changed declarations during ordinary collection."
    return report
