"""Fixture-guard capture and inspection share the bounded worker slots.

An explicit sandbox stand-in observes every dispatched session, so concurrency, order
and cancellation are measured at the dispatch seam; one control repeats the comparison
across worker counts with real Bubblewrap sessions.
"""

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.fixture_guard import (
    guarded_group_id,
    validate_capture,
    validate_inspection,
)
from pytest_telescope.model import (
    DeclarationError,
    InfrastructureError,
    UnsupportedCapability,
    fingerprint,
)
from pytest_telescope.result import validate_guarded_batch

pytestmark = pytest.mark.requires_fixture_guard

INSPECTION_MODES = ("guard-capture", "guard-inspection")
NONCE = "a" * 64


def capture_for(request):
    ids = request["case_ids"]
    items = [dict(case_id=case, fixtures=[], packages=[]) for case in ids]
    registry = fingerprint({"synthetic-registry": ids})
    result = dict(
        protocol="fixture-capture-v1",
        nonce=NONCE,
        base_context_id=request["context_id"],
        case_ids=ids,
        registry_id=registry,
        content_id=fingerprint(
            dict(protocol="fixture-content-v1", registry_id=registry, case_ids=ids, items=items)
        ),
        items=items,
    )
    return validate_capture(
        result,
        nonce=NONCE,
        base_context_id=request["context_id"],
        case_ids=ids,
        framework_ids=frozenset(),
    )


def inspection_for(request):
    committed = dict(
        protocol="fixture-inspection-v1",
        context_id=request["group_id"],
        group_id=request["group_id"],
        case_ids=request["case_ids"],
        registry_id=request["registry_id"],
        items=[dict(case_id=case, fixtures=[], packages=[]) for case in request["case_ids"]],
    )
    return validate_inspection(
        dict(committed, nonce=NONCE, graph_id=fingerprint(committed)),
        nonce=NONCE,
        group_id=request["group_id"],
        case_ids=request["case_ids"],
        registry_id=request["registry_id"],
        framework_ids=frozenset(),
    )


def batch_for(request):
    ids = request["case_ids"]
    result = dict(
        protocol=10,
        declaration_error=False,
        context_id=request["group_id"],
        collection_skips=[],
        nonce=NONCE,
        group_id=request["group_id"],
        case_ids=ids,
        collected=len(ids),
        passed=len(ids),
        failed=0,
        skipped=0,
        exit_code=0,
        complete=True,
        leaked_descendants=False,
        worker_seconds=0.2,
        failures=[],
        case_results=[
            dict(
                case_id=case,
                outcome="passed",
                complete=True,
                reports=[
                    dict(phase=phase, outcome="passed", skip_reason=None)
                    for phase in ("setup", "call", "teardown")
                ],
            )
            for case in ids
        ],
        guard_protocol=request["guard_protocol"],
        registry_id=request["registry_id"],
        graph_id=request["graph_id"],
    )
    assert validate_guarded_batch(
        result,
        nonce=NONCE,
        group_id=request["group_id"],
        case_ids=ids,
        registry_id=request["registry_id"],
        graph_id=request["graph_id"],
    )
    return result


class Seam:
    """Everything the stand-in observed at the sandbox dispatch boundary."""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []  # (candidate, mode, request) in start order
        self.results = {}  # (candidate, mode) -> returned result
        self.running = Counter()
        self.peak = Counter()
        self.reached = {}  # concurrent inspection sandboxes -> event
        self.active = None

    def modes(self, candidate=None):
        return [mode for index, mode, _ in self.events if candidate in (None, index)]

    def started(self):
        return {index for index, _, _ in self.events}

    def concurrent_inspections(self, count):
        """An event set once `count` inspection sandboxes are active together."""
        with self.lock:
            event = self.reached.setdefault(count, threading.Event())
            if self.running["inspection"] >= count:
                event.set()
            return event


def guarded_project(root, sandbox_double, module_rows, behavior=None, *, count=6):
    """A guarded batch project with one candidate per module, in module order."""
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text('[tool.pytest_telescope.execution]\nmode="batch"\n')
    rows = module_rows(root, [(f"test_{module}.py", ["a", "b"]) for module in range(count)])
    candidate = {row["case_id"]: int(row["file"][5:-3]) for row in rows}
    seam = Seam()

    def execute(*args, request, **kwargs):
        if request["mode"] == "collection":
            return dict(
                protocol=11,
                declaration_error=False,
                context_id=request["context_id"],
                collection_skips=[],
                kind="collection",
                nonce="a" * 32,
                exit_code=0,
                complete=True,
                leaked_descendants=False,
                items=rows,
                worker_seconds=0.1,
                failures=[],
            )
        index, mode = candidate[request["case_ids"][0]], request["mode"]
        phase = "inspection" if mode in INSPECTION_MODES else "execution"
        with seam.lock:
            seam.events.append((index, mode, copy.deepcopy(request)))
            seam.running[phase] += 1
            seam.peak[phase] = max(seam.peak[phase], seam.running[phase])
            for needed, event in seam.reached.items():
                if phase == "inspection" and seam.running[phase] >= needed:
                    event.set()
        try:
            if behavior is not None:
                behavior(index, request, kwargs)
            if mode == "guard-capture":
                result = capture_for(request)
            elif mode == "guard-inspection":
                result = inspection_for(request)
            else:
                kwargs["on_session_start"]()
                result = batch_for(request)
            with seam.lock:
                seam.results[index, mode] = result
            return result
        finally:
            with seam.lock:
                seam.running[phase] -= 1

    seam.active = sandbox_double(execute)
    return seam


def stable(report):
    """A report without wall-clock measurements or the requested worker count."""
    report = copy.deepcopy(report)
    del report["elapsed_seconds"], report["setup_seconds"], report["execution"]["workers"]
    for group in report["groups"]:
        del group["attempt_seconds"]
    return report


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_inspection_sandboxes_fill_and_never_exceed_the_worker_slots(
    tmp_path, sandbox_double, module_rows, workers
):
    seam = None

    def slow(index, request, kwargs):
        if request["mode"] == "guard-capture":
            # Hold every slot until all of them are occupied at the same moment.
            assert seam.concurrent_inspections(workers).wait(10)
        time.sleep(0.02)

    seam = guarded_project(tmp_path, sandbox_double, module_rows, slow)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=workers)
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert report["counts"]["executed"] == 12 and not seam.active
    assert seam.peak["inspection"] == workers
    assert 1 <= seam.peak["execution"] <= workers
    for index in range(6):
        # Capture strictly precedes the inspection of the key that capture produced.
        assert seam.modes(index) == ["guard-capture", "guard-inspection", "guard-batch"]
        (capture_request, inspection_request, batch_request) = (
            request for candidate, _, request in seam.events if candidate == index
        )
        capture = seam.results[index, "guard-capture"]
        inspection = seam.results[index, "guard-inspection"]
        key = guarded_group_id(
            capture_request["context_id"], capture["registry_id"], capture["content_id"]
        )
        assert inspection_request["group_id"] == inspection_request["context_id"] == key
        assert inspection_request["registry_id"] == capture["registry_id"]
        assert batch_request["group_id"] == key
        assert batch_request["graph_id"] == inspection["graph_id"]
    assert [group["group_id"] for group in report["groups"]] == [
        seam.results[index, "guard-inspection"]["group_id"] for index in range(6)
    ]


def test_one_worker_inspects_candidates_one_at_a_time_in_order(
    tmp_path, sandbox_double, module_rows
):
    seam = guarded_project(tmp_path, sandbox_double, module_rows, count=3)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1, collect_only=True)
    assert report["fixture_guard"]["state"] == "admitted" and seam.peak["inspection"] == 1
    assert [(index, mode) for index, mode, _ in seam.events] == [
        (index, mode) for index in range(3) for mode in INSPECTION_MODES
    ]


def test_worker_count_and_completion_order_change_no_result_or_key(
    tmp_path, sandbox_double, module_rows
):
    root = tmp_path / "project"

    def reversed_completion(index, request, kwargs):
        if request["mode"] in INSPECTION_MODES:
            time.sleep(0.03 * (5 - index))

    seam = guarded_project(root, sandbox_double, module_rows, reversed_completion)
    reports = {
        workers: controller.run(
            root,
            fixture_guard="available-v1",
            workers=workers,
            cache_dir=tmp_path / f"cache-{workers}",
        )
        for workers in (1, 4)
    }
    assert seam.peak["inspection"] > 1 and not seam.active
    assert reports[1]["status"] == "passed" and reports[1]["counts"]["executed"] == 12
    assert stable(reports[4]) == stable(reports[1])
    assert len({group["group_id"] for group in reports[1]["groups"]}) == 6
    # Records published after pooled inspection are the ones a one-worker run reuses.
    warm = controller.run(
        root, fixture_guard="available-v1", workers=1, cache_dir=tmp_path / "cache-4"
    )
    assert warm["counts"]["reused"] == 12
    assert [group["group_id"] for group in warm["groups"]] == [
        group["group_id"] for group in reports[1]["groups"]
    ]
    assert seam.modes().count("guard-batch") == 12
    preflight = {
        workers: controller.run(
            root,
            fixture_guard="available-v1",
            workers=workers,
            collect_only=True,
            cache_dir=tmp_path / f"cache-{workers}",
        )
        for workers in (1, 4)
    }
    assert stable(preflight[4]) == stable(preflight[1])
    assert preflight[1]["fixture_guard"]["state"] == "admitted"


def test_refusal_cancels_unstarted_candidates_and_drains_active_ones(
    tmp_path, sandbox_double, module_rows
):
    both_active = threading.Barrier(2, timeout=10)

    def refuse_first(index, request, kwargs):
        both_active.wait()
        if index == 0:
            raise UnsupportedCapability(
                "execution:fixture-metadata", "fixture metadata cannot be validated"
            )
        # The later candidate's sandbox is stopped; the pool waits for its cleanup.
        assert kwargs["cancellation"].wait(10)
        raise sandbox.InvocationInterrupted("synthetic cancellation")

    seam = guarded_project(tmp_path, sandbox_double, module_rows, refuse_first)
    with pytest.raises(UnsupportedCapability) as caught:
        controller.run(tmp_path, fixture_guard="available-v1", workers=2)
    assert caught.value.capability == "execution:fixture-metadata"
    assert seam.modes() == ["guard-capture", "guard-capture"] and seam.started() == {0, 1}
    assert not seam.active
    refusal = controller.error_result(caught.value, fixture_guard="available-v1")
    assert refusal["fixture_guard"]["state"] == "unavailable"
    units = refusal["accounting"]["observations"]["units"]
    assert len(units) == 12 and all(unit["attempts"] == 0 for unit in units)
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_first_refusal_in_candidate_order_is_reported_for_every_worker_count(
    tmp_path, sandbox_double, module_rows, workers
):
    earlier_active, later_refused = threading.Event(), threading.Event()
    earlier_interrupted = []

    def refuse_out_of_order(index, request, kwargs):
        if index == 0:
            earlier_active.set()
            if workers > 1:
                # The later candidate refuses first; this one keeps its sandbox.
                assert later_refused.wait(10)
                time.sleep(0.3)
            earlier_interrupted.append(kwargs["cancellation"].is_set())
            raise DeclarationError("synthetic earlier refusal")
        if index == 1:
            assert earlier_active.wait(10)
            later_refused.set()
            raise UnsupportedCapability("execution:fixture-sharing", "synthetic later refusal")
        assert kwargs["cancellation"].wait(10)
        raise sandbox.InvocationInterrupted("synthetic cancellation")

    seam = guarded_project(tmp_path, sandbox_double, module_rows, refuse_out_of_order)
    with pytest.raises(DeclarationError) as caught:
        controller.run(tmp_path, fixture_guard="available-v1", workers=workers)
    assert type(caught.value) is DeclarationError
    assert str(caught.value) == "synthetic earlier refusal"
    assert earlier_interrupted == [False]
    assert seam.started() <= set(range(workers)) and not seam.active
    assert set(seam.modes()) == {"guard-capture"}


def test_inspection_outage_retries_in_its_slot_and_reports_like_one_worker(
    tmp_path, sandbox_double, module_rows
):
    root = tmp_path / "project"
    attempts = Counter()

    def outage(index, request, kwargs):
        if index == 2 and request["mode"] == "guard-inspection":
            attempts[request["group_id"], current["workers"]] += 1
            raise InfrastructureError("synthetic inspection outage")

    seam = guarded_project(root, sandbox_double, module_rows, outage)
    current, reports = {}, {}
    for workers in (1, 4):
        current["workers"] = workers
        reports[workers] = controller.run(
            root,
            fixture_guard="available-v1",
            workers=workers,
            cache_dir=tmp_path / f"cache-{workers}",
        )
    # Two bounded attempts for the failing candidate in each run, and no body dispatch.
    assert len(attempts) == 2 and set(attempts.values()) == {2}
    assert "guard-batch" not in seam.modes() and not seam.active
    assert reports[1]["status"] == "infrastructure_error"
    assert reports[1]["collection"]["reason"] == "synthetic inspection outage"
    assert reports[1]["fixture_guard"]["state"] == "unavailable"
    assert reports[1]["counts"]["infrastructure_error"] == 12 and not reports[1]["groups"]
    assert stable(reports[4]) == stable(reports[1])
    for workers in (1, 4):
        assert not list((tmp_path / f"cache-{workers}").rglob("*.json"))


def test_cleanup_failure_stops_every_active_inspection(tmp_path, sandbox_double, module_rows):
    both_active = threading.Barrier(2, timeout=10)

    def cleanup_failure(index, request, kwargs):
        both_active.wait()
        if index == 1:
            raise sandbox.CleanupError("synthetic cleanup failure")
        # An owned-resource failure also stops candidates that precede it.
        assert kwargs["cancellation"].wait(10)
        raise sandbox.InvocationInterrupted("synthetic cancellation")

    seam = guarded_project(tmp_path, sandbox_double, module_rows, cleanup_failure)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=2)
    assert report["status"] == "infrastructure_error"
    assert report["collection"]["reason"] == "synthetic cleanup failure"
    assert report["fixture_guard"]["state"] == "unavailable"
    assert seam.modes() == ["guard-capture", "guard-capture"] and not seam.active
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_real_sessions_agree_across_worker_counts(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\n'
    )
    for name in ("a", "b", "c", "d"):
        (root / f"{name}.txt").write_text(name)
        (root / "tests" / f"test_{name}.py").write_text(
            "import pathlib\nimport pytest\n"
            f'pytestmark = pytest.mark.telescope(inputs=["{name}.txt"])\n'
            '@pytest.fixture(scope="module")\n'
            f'def content(): return pathlib.Path("{name}.txt").read_text()\n'
            f'def test_first(content): assert content == "{name}"\n'
            f'def test_second(content): assert content == "{name}"\n'
        )
    original = sandbox.execute
    lock = threading.Lock()
    running, peak, overlapped = Counter(), Counter(), threading.Event()
    current = {}

    def observe(*args, request, **kwargs):
        inspecting = request["mode"] in INSPECTION_MODES
        workers = current["workers"]
        if inspecting:
            with lock:
                running[workers] += 1
                peak[workers] = max(peak[workers], running[workers])
                if running[workers] > 1:
                    overlapped.set()
            if workers > 1 and request["mode"] == "guard-capture":
                overlapped.wait(60)
        try:
            return original(*args, request=request, **kwargs)
        finally:
            if inspecting:
                with lock:
                    running[workers] -= 1

    monkeypatch.setattr(sandbox, "execute", observe)
    reports = {}
    for workers in (1, 4):
        current["workers"] = workers
        reports[workers] = controller.run(
            root,
            fixture_guard="available-v1",
            workers=workers,
            cache_dir=tmp_path / f"cache-{workers}",
        )
    assert reports[1]["status"] == "passed" and reports[1]["counts"]["executed"] == 8
    assert reports[1]["fixture_guard"]["state"] == "admitted"
    assert peak[1] == 1 and 1 < peak[4] <= 4

    def comparable(report):
        report = stable(report)
        report["collection"]["worker_seconds"] = None
        for row in report["collection"]["suites"]:
            row["worker_seconds"] = None
        for group in report["groups"]:
            group["worker_seconds"] = None
        return report

    assert comparable(reports[4]) == comparable(reports[1])
    current["workers"] = 1
    warm = controller.run(
        root, fixture_guard="available-v1", workers=1, cache_dir=tmp_path / "cache-4"
    )
    assert warm["counts"]["reused"] == 8


def test_interrupt_stops_every_active_inspection_and_cleans_up(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\n'
    )
    for name in ("a", "b", "c", "d", "e", "f"):
        # Collection imports the module at once; a capture session holds its slot.
        (root / "tests" / f"test_{name}.py").write_text(
            "import json\nimport time\nimport pytest\n"
            "pytestmark = pytest.mark.telescope(inputs=[])\n"
            f'if json.load(open("{sandbox.REQUEST}"))["mode"] == "guard-capture":\n'
            "    time.sleep(300)\n"
            "def test_item(): pass\n"
        )
    scratch = root / ".pytest-telescope/scratch"
    command = [sys.executable, "-m", "pytest_telescope", "run"]
    command += ["--fixture-guard", "available-v1", "--workers", "4", "--timeout", "240"]
    with subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            deadline = time.monotonic() + 120
            # The frozen project copy plus one invocation for each occupied worker slot.
            while len(list(scratch.glob("invocation-*"))) < 5:
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(2)  # The four capture sessions reach their held imports.
            assert len(list(scratch.glob("invocation-*"))) == 5
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
    report = json.loads(stdout)
    assert process.returncode == 3 and report["status"] == "infrastructure_error"
    assert report["collection"]["reason"] == "controller interrupted; owned sandboxes stopped"
    assert report["fixture_guard"]["state"] == "unavailable"
    assert report["counts"]["infrastructure_error"] == 6 and not report["groups"]
    assert not list(scratch.iterdir())
    assert not list((root / ".pytest-telescope/cache").rglob("*.json"))
