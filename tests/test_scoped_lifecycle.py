"""Deterministic scheduling and cleanup controls with an explicit sandbox stand-in."""

import threading
import time
from collections import Counter

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import InfrastructureError


def install(root, sandbox_double, module_rows, behavior, *, count=4, fail_outer=False):
    (root / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = module_rows(root, [(f"test_{number}.py", ["case"]) for number in range(count)])

    def execute(*args, request, **kwargs):
        if request["mode"] == "collection":
            return dict(
                protocol=4,
                kind="collection",
                nonce="a" * 32,
                exit_code=0,
                complete=True,
                leaked_descendants=False,
                items=rows,
                worker_seconds=0.1,
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
            failures=[],
        )

    return rows, sandbox_double(execute, fail_outer=fail_outer)


def test_parallel_dispatch_is_bounded_by_workers_and_summary_order_is_stable(
    tmp_path, sandbox_double, module_rows
):
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = peak = 0

    def behavior(case, kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        time.sleep(0.01)
        with lock:
            active -= 1

    rows, directories = install(tmp_path, sandbox_double, module_rows, behavior)
    report = controller.run(tmp_path, workers=2)
    assert peak == 2 and active == 0 and not directories
    assert report["status"] == "passed"
    assert report["inventory_complete"] is True
    assert report["execution"]["workers"] == 2
    assert [row["case_id"] for row in report["units"]] == [row["case_id"] for row in rows]
    assert report["counts"]["selected"] == report["counts"]["executed"] == 4
    assert report["route_counts"]["scoped"]["executed"] == 4
    warm = controller.run(tmp_path, workers=2)
    assert warm["counts"]["reused"] == 4


def test_one_worker_runs_scoped_cases_one_at_a_time(tmp_path, sandbox_double, module_rows):
    lock = threading.Lock()
    active = peak = 0

    def behavior(case, kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    install(tmp_path, sandbox_double, module_rows, behavior)
    assert controller.run(tmp_path)["counts"]["executed"] == 4
    assert peak == 1


@pytest.mark.parametrize("exhaust", [False, True])
def test_infrastructure_retry_is_bounded_per_case(tmp_path, sandbox_double, module_rows, exhaust):
    attempts = Counter()

    def behavior(case, kwargs):
        attempts[case] += 1
        if exhaust or attempts[case] == 1:
            raise InfrastructureError("synthetic unavailable worker")

    _, directories = install(tmp_path, sandbox_double, module_rows, behavior)
    report = controller.run(tmp_path, workers=2)
    assert len(attempts) == 4 and set(attempts.values()) == {2}
    assert not directories
    outcome = "infrastructure_error" if exhaust else "executed"
    assert report["counts"][outcome] == 4
    assert all(row["attempts"] == 2 for row in report["units"])
    if exhaust:
        assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))


def test_terminal_cleanup_failure_stops_dispatch_drains_workers_and_parks_remainder(
    tmp_path, sandbox_double, module_rows
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
            raise sandbox.CleanupError("synthetic worker cleanup failure")
        assert kwargs["cancellation"].wait(5)
        raise sandbox.InvocationInterrupted("synthetic sibling cancellation")

    _, directories = install(tmp_path, sandbox_double, module_rows, behavior)
    report = controller.run(tmp_path, workers=2)
    assert len(attempts) == 2 and set(attempts.values()) == {1}
    assert not directories
    assert report["status"] == "infrastructure_error"
    assert report["inventory_complete"] is True
    assert len(report["units"]) == report["counts"]["selected"] == 4
    assert report["counts"]["infrastructure_error"] == 4
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_outer_cleanup_failure_preserves_inventory_but_discards_pending_success(
    tmp_path, sandbox_double, module_rows
):
    rows, directories = install(
        tmp_path, sandbox_double, module_rows, lambda *args: None, fail_outer=True
    )
    report = controller.run(tmp_path, workers=2)
    assert not directories
    assert report["status"] == "infrastructure_error"
    assert report["inventory_complete"] is True
    assert len(report["units"]) == len(rows)
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_workers_are_validated_before_the_sandbox_is_prepared(tmp_path, monkeypatch, module_rows):
    def prepare(root):
        pytest.fail("the sandbox must not be prepared for an invalid worker count")

    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    module_rows(tmp_path, [("test_one.py", ["one"])])
    monkeypatch.setattr(sandbox, "prepare", prepare)
    for value in (True, 0, -1, 1.5, 33):
        with pytest.raises(controller.DeclarationError):
            controller.run(tmp_path, workers=value)


def test_failed_collection_is_a_failure_even_with_parked_scoped_cases(
    tmp_path, sandbox_double, module_rows
):
    """Known scoped cases left unexecuted by a failed collection are not an outage."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = module_rows(tmp_path, [("test_scoped.py", ["scoped"])])
    (tmp_path / "test_broken.py").write_text('raise RuntimeError("synthetic import failure")\n')

    def execute(*args, request, **kwargs):
        assert request["mode"] == "collection", "no scoped case may run after a failed collection"
        return dict(
            protocol=4,
            kind="collection",
            nonce="a" * 32,
            exit_code=0,
            complete=True,
            leaked_descendants=False,
            items=rows,
            worker_seconds=0.1,
            failures=[],
        )

    sandbox_double(execute)
    report = controller.run(tmp_path)
    assert report["status"] == "failed", report
    assert report["collection"]["outcome"] == "failed"
    assert report["inventory_complete"] is False
    (parked,) = report["units"]
    assert parked["route"] == "scoped" and parked["reason"] == "execution not started"
    assert parked["result_status"] is None and parked["group_id"] is None
