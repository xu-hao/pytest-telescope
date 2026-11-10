"""Real scoped attempts and pending successes survive terminal typed refusal."""

import threading

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put
from test_frozen_tool_review_controller import mixed_project

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError


def scoped(project, name, *, refuse=False, inputs=()):
    project.module(
        name,
        f"""import pytest
pytestmark = pytest.mark.telescope(inputs={["conftest.py", *inputs]!r})
def test_body(request):
    request.config.owned_body_count = getattr(request.config, 'owned_body_count', 0) + 1
    assert request.config.owned_body_count == 1
    request.config.owned_refusal = {refuse!r}
""",
    )


@pytest.mark.parametrize("mode", ["case", "batch"])
@pytest.mark.parametrize("workers", [1, 2])
def test_valid_exit_four_fails_group_without_cancelling_siblings(
    tmp_path, monkeypatch, workers, mode
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    put(
        project.root,
        "conftest.py",
        """def pytest_sessionfinish(session):
    if getattr(session.config, 'owned_refusal', False):
        assert session.config.owned_body_count == 1
        session.exitstatus = 4
""",
    )
    put(project.root, "prior-input.txt", "first pinned input\n")
    scoped(project, "test_00_prior.py", inputs=("prior-input.txt",))
    assert_counts(project.run(execution_mode=mode), executed=1)
    previous = cache_bytes(project.root)
    assert previous
    # A different declared input requires a fresh prior attempt while its old
    # valid cache bytes remain available for the no-publication oracle below.
    put(project.root, "prior-input.txt", "second pinned input\n")
    scoped(project, "test_10_success.py")
    scoped(project, "test_20_refusal.py", refuse=True)
    scoped(project, "test_30_later.py")
    counter = tmp_path / "ordinary-once"
    project.module(
        "test_ordinary.py",
        f"""from pathlib import Path
def test_ordinary():
    p = Path({str(counter)!r})
    p.write_text(p.read_text() + 'x' if p.exists() else 'x')
""",
    )
    original_execute, original_stage = sandbox.execute, controller.PendingRecords.stage
    ready, refused = threading.Event(), threading.Event()
    calls, returned, staged = [], {}, []
    guard = threading.Lock()

    def execute(*args, **kwargs):
        request = kwargs["request"]
        if request["mode"] not in {"execution", "batch"}:
            return original_execute(*args, **kwargs)
        filename = request["registry"][0]["file"]
        if filename == "tests/test_20_refusal.py":
            assert ready.wait(10), "owned successful sibling was not staged"
        with guard:
            calls.append(filename)
        try:
            result = original_execute(*args, **kwargs)
            assert result["passed"] == 1 and result["complete"]
            with guard:
                returned[filename] = result
            if workers == 2 and filename == "tests/test_00_prior.py":
                assert refused.wait(10), "owned refusal worker did not finish"
            return result
        finally:
            if filename == "tests/test_20_refusal.py":
                refused.set()

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        original_stage(self, final, key, result, kind=kind, guarded=guarded)
        if kind == "execution":
            staged.append(key)
            with guard:
                success = returned.get("tests/test_10_success.py")
            if success is result:
                ready.set()

    monkeypatch.setattr(sandbox, "execute", execute)
    monkeypatch.setattr(controller.PendingRecords, "stage", stage)
    report = project.run(workers=workers, execution_mode=mode)
    assert counter.read_text() == "x"
    assert report["kind"] == "run" and report["status"] == "failed", report
    assert len(calls) == len(set(calls)) == 4 and len(returned) == 4
    assert returned["tests/test_20_refusal.py"]["exit_code"] == 4
    assert staged and ready.is_set() and refused.is_set()
    observed = report
    rows = {row["file"]: row for row in observed["units"]}
    for filename, result in returned.items():
        row = rows[filename]
        assert row["attempts"] == 1 and row["result_status"] == "passed", row
        assert row["complete"] and row["passed"] == 1
        assert row["reports"] == result["case_results"][0]["reports"]
        group = next(group for group in observed["groups"] if group["group_id"] == row["group_id"])
        assert group["attempts"] == 1 and group["unresolved"] == group["not_run"] == 0
        if filename == "tests/test_20_refusal.py":
            assert group["outcome"] == "failed" and not group["cache_eligible"]
        else:
            assert group["outcome"] == "executed" and group["cache_eligible"]
    current = cache_bytes(project.root)
    assert all(current.get(name) == content for name, content in previous.items())
    failed_group = rows["tests/test_20_refusal.py"]["group_id"]
    assert not (project.root / ".pytest-telescope/cache" / (failed_group + ".json")).exists()
    assert not list((project.root / ".pytest-telescope/cache").rglob("*.pending"))
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())

    calls.clear()
    returned.clear()
    warm = project.run(workers=workers, execution_mode=mode)
    assert warm["kind"] == "run" and warm["status"] == "failed"
    assert calls == ["tests/test_20_refusal.py"]
    assert warm["counts"]["reused"] == 3 and counter.read_text() == "xx"


@pytest.mark.parametrize("workers", [1, 2])
def test_typed_boundary_drains_started_siblings_and_discards_pending(
    tmp_path, monkeypatch, workers
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    put(
        project.root,
        "conftest.py",
        """def pytest_sessionfinish(session):
    if getattr(session.config, 'owned_refusal', False):
        assert session.config.owned_body_count == 1
        session.exitstatus = 4
""",
    )
    put(project.root, "prior-input.txt", "first pinned input\n")
    scoped(project, "test_00_prior.py", inputs=("prior-input.txt",))
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    assert previous
    # A different declared input requires a fresh prior attempt while its old
    # valid cache bytes remain available for the no-publication oracle below.
    put(project.root, "prior-input.txt", "second pinned input\n")
    scoped(project, "test_10_success.py")
    scoped(project, "test_20_refusal.py")
    if workers == 1:
        scoped(project, "test_30_never.py")
    counter = tmp_path / "ordinary-once"
    project.module(
        "test_ordinary.py",
        f"""from pathlib import Path
def test_ordinary():
    p = Path({str(counter)!r})
    p.write_text(p.read_text() + 'x' if p.exists() else 'x')
""",
    )
    original_execute, original_stage = sandbox.execute, controller.PendingRecords.stage
    ready, refused = threading.Event(), threading.Event()
    calls, returned, staged = [], {}, []
    guard = threading.Lock()

    def execute(*args, **kwargs):
        request = kwargs["request"]
        if request["mode"] != "execution":
            return original_execute(*args, **kwargs)
        filename = request["registry"][0]["file"]
        if filename == "tests/test_20_refusal.py":
            assert ready.wait(10), "owned successful sibling was not staged"
        with guard:
            calls.append(filename)
        try:
            result = original_execute(*args, **kwargs)
            assert result["passed"] == 1 and result["complete"]
            with guard:
                returned[filename] = result
            if workers == 2 and filename == "tests/test_00_prior.py":
                assert refused.wait(10), "owned refusal worker did not finish"
            if filename == "tests/test_20_refusal.py":
                # Actual worker completed; this explicit typed boundary returns no observations.
                raise DeclarationError("owned typed refusal after successful worker")
            return result
        finally:
            if filename == "tests/test_20_refusal.py":
                refused.set()

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        original_stage(self, final, key, result, kind=kind, guarded=guarded)
        if kind == "execution":
            staged.append(key)
            with guard:
                success = returned.get("tests/test_10_success.py")
            if success is result:
                ready.set()

    monkeypatch.setattr(sandbox, "execute", execute)
    monkeypatch.setattr(controller.PendingRecords, "stage", stage)
    report = project.run(workers=workers)
    assert counter.read_text() == "x"
    assert len(calls) == len(set(calls)) == 3 and len(returned) == 3
    assert returned["tests/test_20_refusal.py"]["exit_code"] == 0
    assert staged and ready.is_set() and refused.is_set()
    assert report["kind"] == "refusal" and report["status"] == "declaration_error", report
    observed = report["accounting"]["observations"]
    rows = {row["file"]: row for row in observed["units"]}
    for filename, result in returned.items():
        row = rows[filename]
        if filename == "tests/test_20_refusal.py":
            assert row["attempts"] == 1 and row["result_status"] is None, row
            assert row["reports"] == [] and not row["complete"]
            group = next(
                group for group in observed["groups"] if group["group_id"] == row["group_id"]
            )
            assert group["attempts"] == group["unresolved"] == 1 and group["not_run"] == 0
            assert not group["cache_eligible"]
            continue
        assert row["attempts"] == 1 and row["result_status"] == "passed", row
        assert row["complete"] and row["passed"] == 1
        assert row["reports"] == result["case_results"][0]["reports"]
        group = next(group for group in observed["groups"] if group["group_id"] == row["group_id"])
        assert group["attempts"] == 1 and group["unresolved"] == group["not_run"] == 0
        assert not group["cache_eligible"]
    if workers == 1:
        never = rows["tests/test_30_never.py"]
        assert never["attempts"] == 0 and never["result_status"] == "not-run"
    assert cache_bytes(project.root) == previous
    assert not list((project.root / ".pytest-telescope/cache").rglob("*.pending"))
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())


def test_attempted_worker_without_returned_observations_is_unresolved_not_unrun(
    tmp_path, monkeypatch
):
    project, counter = mixed_project(tmp_path)
    original = sandbox.execute
    observed_attempts = []

    def unavailable(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["request"]["mode"] == "execution":
            assert result["passed"] == 1 and result["complete"]
            observed_attempts.append(result)
            # Explicit boundary double: actual child ran, but this call supplies
            # no result to its caller. The controller cannot claim those passes.
            raise DeclarationError("owned typed boundary lost its worker observations")
        return result

    monkeypatch.setattr(sandbox, "execute", unavailable)
    report = project.run()
    assert len(observed_attempts) == 1 and counter.read_text() == "x"
    assert report["kind"] == "refusal" and report["status"] == "declaration_error", report
    observed = report["accounting"]["observations"]
    scoped_row = next(row for row in observed["units"] if row["route"] == "scoped")
    group = next(row for row in observed["groups"] if row["group_id"] == scoped_row["group_id"])
    assert scoped_row["attempts"] == group["attempts"] == 1
    assert scoped_row["result_status"] is None and scoped_row["reports"] == []
    assert not scoped_row["complete"] and not group["complete"]
    assert group["unresolved"] == 1 and group["not_run"] == 0
    assert not group["cache_eligible"]
    assert not cache_bytes(project.root)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())
