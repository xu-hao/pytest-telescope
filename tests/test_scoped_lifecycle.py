"""Deterministic controller controls without reaching a configured backend."""

import contextlib
import hashlib
import threading
import time
from collections import Counter

import pytest

from pytest_telescope import controller
from pytest_telescope.execution import DEFAULT_WORKER, envelope
from pytest_telescope.model import DeclarationError, InfrastructureError, fingerprint

BUDGET = {key: value * 2 for key, value in DEFAULT_WORKER.items()}


def synthetic_backend(root, monkeypatch, behavior, *, count=4, fail_outer=False):
    (root / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = []
    for number in range(count):
        filename = f"test_{number}.py"
        (root / filename).write_text("def test_case(): pass\n")
        case = fingerprint(["default", f"{filename}::test_case"])
        rows.append(
            dict(
                case_id=case,
                file=filename,
                origins=[],
                selected=True,
                aliases=sorted([case, hashlib.sha256(filename.encode()).hexdigest()]),
            )
        )
    lock = threading.Lock()
    ordinal = 0
    active_directories = set()

    @contextlib.contextmanager
    def invocation(project):
        nonlocal ordinal
        with lock:
            ordinal += 1
            number = ordinal
        directory = project / ".pytest-telescope/scratch" / str(number)
        directory.mkdir(parents=True)
        active_directories.add(directory)
        try:
            yield directory
        finally:
            active_directories.remove(directory)
        if fail_outer and number == 1:
            raise controller.runtime.CleanupError("synthetic outer cleanup failure")

    def execute(*args, request, **kwargs):
        if request["mode"] == "collection":
            return dict(
                protocol=3,
                kind="collection",
                nonce="a" * 32,
                exit_code=0,
                complete=True,
                leaked_descendants=False,
                items=rows,
                worker_seconds=0.1,
                cpu_seconds=0.1,
                failures=[],
            )
        behavior(request["case_id"], kwargs)
        return dict(
            protocol=1,
            nonce="a" * 32,
            exit_code=0,
            complete=True,
            leaked_descendants=False,
            collected=1,
            passed=1,
            failed=0,
            skipped=0,
            case_ids=[request["case_id"]],
            worker_seconds=0.1,
            cpu_seconds=0.1,
            failures=[],
        )

    monkeypatch.setattr(controller.runtime, "recover", lambda root: None)
    monkeypatch.setattr(
        controller.runtime,
        "identity",
        lambda root: {"image": "fixture", "execution": {"fixture": True}},
    )
    monkeypatch.setattr(controller.runtime, "invocation", invocation)
    monkeypatch.setattr(controller.runtime, "execute", execute)
    return rows, active_directories


def test_parallel_dispatch_is_bounded_and_summary_order_is_stable(tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = peak = 0

    def behavior(case, kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        assert kwargs["limits"] == DEFAULT_WORKER
        barrier.wait()
        time.sleep(0.01)
        with lock:
            active -= 1

    rows, directories = synthetic_backend(tmp_path, monkeypatch, behavior)
    report = controller.run(tmp_path, budget=BUDGET)
    assert peak == 2 and active == 0 and not directories
    assert report["status"] == "passed"
    assert report["inventory_complete"] is True
    assert [row["case_id"] for row in report["units"]] == [row["case_id"] for row in rows]
    assert report["counts"]["selected"] == report["counts"]["executed"] == 4
    warm = controller.run(tmp_path, budget=BUDGET)
    assert warm["counts"]["reused"] == 4


@pytest.mark.parametrize("exhaust", [False, True])
def test_infrastructure_retry_is_bounded_per_case(tmp_path, monkeypatch, exhaust):
    attempts = Counter()

    def behavior(case, kwargs):
        attempts[case] += 1
        if exhaust or attempts[case] == 1:
            raise InfrastructureError("synthetic unavailable worker")

    _, directories = synthetic_backend(tmp_path, monkeypatch, behavior)
    report = controller.run(tmp_path, budget=BUDGET)
    assert len(attempts) == 4 and set(attempts.values()) == {2}
    assert not directories
    outcome = "infrastructure_error" if exhaust else "executed"
    assert report["counts"][outcome] == 4
    assert all(row["attempts"] == 2 for row in report["units"])
    if exhaust:
        assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))


def test_terminal_cleanup_failure_stops_dispatch_drains_workers_and_parks_remainder(
    tmp_path, monkeypatch
):
    entered = threading.Barrier(2, timeout=5)
    attempts = Counter()
    lock = threading.Lock()

    def behavior(case, kwargs):
        with lock:
            attempts[case] += 1
            first = len(attempts) == 1
        entered.wait()
        if first:
            raise controller.runtime.CleanupError("synthetic worker cleanup failure")
        assert kwargs["cancellation"].wait(5)
        raise controller.runtime.InvocationInterrupted("synthetic sibling cancellation")

    _, directories = synthetic_backend(tmp_path, monkeypatch, behavior)
    report = controller.run(tmp_path, budget=BUDGET)
    assert len(attempts) == 2 and set(attempts.values()) == {1}
    assert not directories
    assert report["status"] == "infrastructure_error"
    assert report["inventory_complete"] is True
    assert len(report["units"]) == report["counts"]["selected"] == 4
    assert report["counts"]["infrastructure_error"] == 4
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_outer_cleanup_failure_preserves_inventory_but_discards_pending_success(
    tmp_path, monkeypatch
):
    rows, directories = synthetic_backend(
        tmp_path, monkeypatch, lambda *args: None, fail_outer=True
    )
    report = controller.run(tmp_path, budget=BUDGET)
    assert not directories
    assert report["status"] == "infrastructure_error"
    assert report["inventory_complete"] is True
    assert len(report["units"]) == len(rows)
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


@pytest.mark.parametrize("dimension", list(DEFAULT_WORKER))
def test_every_budget_dimension_limits_concurrency(dimension):
    budget = {key: value * 4 for key, value in DEFAULT_WORKER.items()}
    budget[dimension] = DEFAULT_WORKER[dimension] * 2
    assert envelope({}, budget).concurrency == 2
    budget[dimension] = DEFAULT_WORKER[dimension] - 1
    with pytest.raises(DeclarationError):
        envelope({}, budget)


def test_resource_bounds_are_validated_before_backend_access(tmp_path, monkeypatch):
    def backend(root):
        pytest.fail("backend must not be reached for invalid resource declarations")

    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    (tmp_path / "test_one.py").write_text("def test_one(): pass\n")
    monkeypatch.setattr(controller.runtime, "identity", backend)
    for value in (True, 0, -1, 1.5):
        with pytest.raises(DeclarationError):
            controller.run(tmp_path, budget=BUDGET | {"cpus": value})


def test_worker_count_is_capped_independently_of_large_aggregate_budget():
    assert (
        envelope({}, {key: value * 64 for key, value in DEFAULT_WORKER.items()}).concurrency == 32
    )
