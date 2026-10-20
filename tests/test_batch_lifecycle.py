"""Atomic batch lifecycle and strict result controls with an explicit backend double."""

import contextlib
import copy
import hashlib
import shutil
import threading
from collections import Counter

import pytest

from pytest_telescope import controller
from pytest_telescope.execution import DEFAULT_WORKER
from pytest_telescope.model import InfrastructureError, fingerprint
from pytest_telescope.result import validate_batch, validate_collection

BUDGET = {key: value * 2 for key, value in DEFAULT_WORKER.items()}


def result_for(request):
    ids = request["case_ids"]
    return dict(
        protocol=2,
        nonce="a" * 32,
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
        cpu_seconds=0.1,
        failures=[],
        case_results=[dict(case_id=case, outcome="passed", complete=True) for case in ids],
    )


def backend(root, monkeypatch, behavior, *, count=3, fail_outer=False):
    (root / "pyproject.toml").write_text('[tool.pytest_telescope.execution]\nmode="batch"\n')
    rows = []
    for module in range(count):
        filename = f"test_{module}.py"
        (root / filename).write_text("def test_a(): pass\ndef test_b(): pass\n")
        for function in ("a", "b"):
            case = fingerprint(["default", f"{filename}::test_{function}"])
            rows.append(
                dict(
                    case_id=case,
                    file=filename,
                    origins=[],
                    selected=True,
                    order=len(rows),
                    aliases=sorted([case, hashlib.sha256(filename.encode()).hexdigest()]),
                )
            )
    ordinal = 0
    lock = threading.Lock()
    active = set()

    @contextlib.contextmanager
    def invocation(project):
        nonlocal ordinal
        with lock:
            ordinal += 1
            number = ordinal
        directory = project / ".pytest-telescope" / "synthetic" / str(number)
        directory.mkdir(parents=True)
        active.add(directory)
        try:
            yield directory
        finally:
            active.remove(directory)
            shutil.rmtree(directory)
        if fail_outer and number == 1:
            raise controller.runtime.CleanupError("synthetic outer cleanup failure")

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
                cpu_seconds=0.1,
                failures=[],
            )
        assert request["mode"] == "batch"
        result = result_for(request)
        behavior(request, kwargs, result)
        assert validate_batch(result, group_id=request["group_id"], case_ids=request["case_ids"])
        return result

    monkeypatch.setattr(controller.runtime, "recover", lambda root: None)
    monkeypatch.setattr(
        controller.runtime,
        "identity",
        lambda root, *, runtime_profile=None: {
            "image": "synthetic",
            "execution": {"fixture": True},
        },
    )
    monkeypatch.setattr(controller.runtime, "invocation", invocation)
    monkeypatch.setattr(controller.runtime, "execute", execute)
    return rows, active


@pytest.mark.parametrize("exhaust", [False, True])
def test_retry_is_bounded_once_per_group_and_never_counts_members_as_workers(
    tmp_path, monkeypatch, exhaust
):
    attempts = Counter()

    def behavior(request, kwargs, result):
        attempts[request["group_id"]] += 1
        if exhaust or attempts[request["group_id"]] == 1:
            raise InfrastructureError("synthetic unavailable worker")

    _, active = backend(tmp_path, monkeypatch, behavior)
    report = controller.run(tmp_path, budget=BUDGET)
    assert len(attempts) == 3 and set(attempts.values()) == {2}
    assert not active
    assert len(report["groups"]) == 3 and len(report["units"]) == 6
    assert all(
        group["attempts"] == 2 and not group["timing_complete"] for group in report["groups"]
    )
    assert all(row["attempts"] == 2 and row["worker_seconds"] is None for row in report["units"])
    if exhaust:
        assert report["status"] == "infrastructure_error"
        assert all(group["unresolved"] == 2 and group["not_run"] == 0 for group in report["groups"])
        assert all(row["result_status"] is None for row in report["units"])
        assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))
    else:
        assert report["counts"]["executed"] == 6
        assert report["worker_cpu_seconds"] == pytest.approx(0.4)
        warm = controller.run(tmp_path, budget=BUDGET)
        assert warm["counts"]["reused"] == 6
        assert all(group["attempts"] == 0 and group["cache_eligible"] for group in warm["groups"])


def test_group_cleanup_failure_cancels_drains_and_accounts_unscheduled_members(
    tmp_path, monkeypatch
):
    barrier = threading.Barrier(2, timeout=5)
    attempts = Counter()
    lock = threading.Lock()

    def behavior(request, kwargs, result):
        with lock:
            attempts[request["group_id"]] += 1
            first = len(attempts) == 1
        barrier.wait()
        if first:
            raise controller.runtime.CleanupError("synthetic cleanup failure")
        assert kwargs["cancellation"].wait(5)
        raise controller.runtime.InvocationInterrupted("synthetic cancellation")

    _, active = backend(tmp_path, monkeypatch, behavior)
    report = controller.run(tmp_path, budget=BUDGET)
    assert len(attempts) == 2 and set(attempts.values()) == {1}
    assert not active
    assert report["inventory_complete"] is True
    assert report["counts"]["infrastructure_error"] == report["counts"]["selected"] == 6
    assert sum(group["attempts"] for group in report["groups"]) == 2
    assert sum(group["unresolved"] for group in report["groups"]) == 4
    assert sum(group["not_run"] for group in report["groups"]) == 2
    assert not any(group["cache_eligible"] for group in report["groups"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_outer_cleanup_does_not_publish_otherwise_successful_groups(tmp_path, monkeypatch):
    _, active = backend(tmp_path, monkeypatch, lambda *args: None, fail_outer=True)
    report = controller.run(tmp_path, budget=BUDGET)
    assert not active
    assert report["status"] == "infrastructure_error"
    assert report["counts"]["executed"] == 6
    assert all(group["complete"] and not group["cache_eligible"] for group in report["groups"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def test_execution_collection_failure_remains_distinct_from_pytest_stop(tmp_path, monkeypatch):
    def behavior(request, kwargs, result):
        result.update(
            exit_code=2,
            complete=False,
            passed=0,
            failures=[
                dict(
                    case_id=request["case_ids"][0],
                    phase="collect",
                    file="test_0.py",
                    line=0,
                    exception_type="CollectionError",
                )
            ],
        )
        result["case_results"] = [
            dict(case_id=case, outcome="not-run", complete=False) for case in request["case_ids"]
        ]

    _, active = backend(tmp_path, monkeypatch, behavior, count=1)
    report = controller.run(tmp_path)
    assert not active and report["status"] == "failed"
    assert report["inventory_complete"] is True
    assert report["groups"][0]["stop_reason"] == "collection"
    assert report["groups"][0]["not_run"] == 2
    assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))


@pytest.fixture
def batch_result():
    return result_for(dict(group_id="a" * 64, case_ids=["b" * 64, "c" * 64]))


def test_batch_result_requires_exact_ordered_group_identity(batch_result):
    assert validate_batch(
        batch_result, successful_only=True, group_id="a" * 64, case_ids=["b" * 64, "c" * 64]
    )
    assert not validate_batch(batch_result, group_id="d" * 64)
    assert not validate_batch(batch_result, case_ids=["c" * 64, "b" * 64])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(protocol=1),
        lambda row: row.update(collected=1),
        lambda row: row.update(passed=1),
        lambda row: row.update(complete=False),
        lambda row: row.update(case_ids=["b" * 64, "b" * 64]),
        lambda row: row["case_results"].reverse(),
        lambda row: row["case_results"].pop(),
        lambda row: row["case_results"][0].update(outcome="not-run"),
        lambda row: row["case_results"][0].update(complete=False),
        lambda row: row.update(leaked_descendants=True),
        lambda row: row.update(worker_seconds=float("nan")),
        lambda row: row.update(unrecognized=True),
    ],
)
def test_malformed_atomic_evidence_cannot_become_a_success(batch_result, mutation):
    changed = copy.deepcopy(batch_result)
    mutation(changed)
    assert not validate_batch(changed, successful_only=True)


def test_partial_batch_retains_observed_outcomes_but_is_never_reusable(batch_result):
    batch_result.update(exit_code=2, complete=False, passed=1)
    batch_result["case_results"][1].update(outcome="not-run", complete=False)
    assert validate_batch(batch_result)
    assert not validate_batch(batch_result, successful_only=True)


def test_collection_order_is_validated_as_actual_unique_selected_positions():
    ids = ["a" * 64, "b" * 64]
    items = [
        dict(
            case_id=case,
            file="test_one.py",
            origins=[],
            selected=True,
            order=index,
            aliases=sorted([case, hashlib.sha256(b"test_one.py").hexdigest()]),
        )
        for index, case in enumerate(ids)
    ]
    report = dict(
        protocol=4,
        kind="collection",
        nonce="a" * 32,
        exit_code=0,
        complete=True,
        leaked_descendants=False,
        worker_seconds=0,
        cpu_seconds=0,
        failures=[],
        items=items,
    )
    assert validate_collection(report)
    report["items"][0]["order"] = 1
    assert not validate_collection(report)


def test_later_suite_collection_failure_preserves_known_cases_without_claiming_a_plan(
    tmp_path, monkeypatch
):
    backend(tmp_path, monkeypatch, lambda *args: None, count=1)
    (tmp_path / "pyproject.toml").write_text("""[tool.pytest_telescope.execution]
mode="batch"
[tool.pytest_telescope.suites.first]
test_roots=["test_0.py"]
[tool.pytest_telescope.suites.second]
test_roots=["test_0.py"]
""")
    original = controller.runtime.execute

    def execute(*args, request, **kwargs):
        assert request["mode"] == "collection"
        result = original(*args, request=request, **kwargs)
        if kwargs["suite"] == "second":
            result.update(exit_code=2, complete=False, items=[])
        return result

    monkeypatch.setattr(controller.runtime, "execute", execute)
    report = controller.run(tmp_path)
    assert report["inventory_complete"] is False
    assert report["grouping_complete"] is False
    assert len(report["units"]) == 2 and report["groups"] == []
    assert all(row["group_id"] is None and row["result_status"] is None for row in report["units"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
