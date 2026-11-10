"""Actual partial projections, atomic reservations and bounded owned cleanup."""

import threading
from types import SimpleNamespace

import pytest
from test_frozen_tool_materialization_bounds import owned_paths, two_views
from test_frozen_tool_materialization_bounds import sparse_pair as sparse_pair

from pytest_telescope import controller, model, sandbox, tool_capture, tool_context, tool_view
from pytest_telescope.model import InfrastructureError, UnsupportedCapability


def test_partial_projection_keeps_reservation_until_cleanup_and_fresh_capture(
    sparse_pair, monkeypatch
):
    project, sources, stage = sparse_pair
    monkeypatch.setattr(tool_context, "MAX_VISITS", 64)
    capture = tool_capture.Capture(model.load(project.root), stage, 10, None)
    original = tool_view.Projection.materialize
    failures = []

    def partial(self, graph, key, destination):
        original(self, graph, key, destination)
        if len(self.names) == 2 and key[1] == "bin/value1" and not failures:
            failures.append(destination)
            raise OSError("owned partial projection fault")

    with capture:
        capture.view(("first",))
        assert len(owned_paths(capture.directory)) == 37
        with monkeypatch.context() as fault:
            fault.setattr(tool_view.Projection, "materialize", partial)
            with pytest.raises(InfrastructureError):
                capture.view(("first", "second"))
        assert len(failures) == 1 and failures[0].exists()
        physical = owned_paths(capture.directory)
        assert 37 < len(physical) < 58
        with pytest.raises(UnsupportedCapability) as caught:
            capture.view(("second",))
        assert caught.value.capability == "tool:bounds"
        assert owned_paths(capture.directory) == physical
    assert not capture.directory.exists()
    with capture:
        two_views(capture, sources)
    assert not capture.directory.exists()


@pytest.mark.parametrize("boundary", [0, 1], ids=["before-first-path", "during-reservation"])
@pytest.mark.parametrize("failure", ["cancel", "deadline"])
def test_interrupted_reservation_creates_no_members_and_consumes_no_capacity(
    sparse_pair, monkeypatch, boundary, failure
):
    project, _, stage = sparse_pair
    cancellation = threading.Event()
    monkeypatch.setattr(tool_context, "MAX_VISITS", 64)
    original = tool_view.Projection.planned_paths

    def interrupted(self, originals):
        for index, path in enumerate(original(self, originals)):
            if index == boundary:
                if failure == "cancel":
                    cancellation.set()
                else:
                    self.budget.deadline = float("-inf")
            yield path

    with tool_capture.Capture(model.load(project.root), stage, 10, cancellation) as capture:
        before = owned_paths(capture.directory)
        expected = sandbox.InvocationInterrupted if failure == "cancel" else InfrastructureError
        with monkeypatch.context() as fault:
            fault.setattr(tool_view.Projection, "planned_paths", interrupted)
            with pytest.raises(expected):
                capture.view(("first",))
        assert owned_paths(capture.directory) == before
        assert len(before) == 26
    assert not capture.directory.exists()
    with tool_capture.Capture(model.load(project.root), stage, 10, threading.Event()) as capture:
        capture.view(("first",))
        assert len(owned_paths(capture.directory)) == 37


def test_cancellation_after_materialized_members_unwinds_without_source_changes(
    sparse_pair, monkeypatch
):
    project, _, stage = sparse_pair
    cancellation = threading.Event()
    monkeypatch.setattr(tool_context, "MAX_VISITS", 64)
    original = tool_view.Projection.materialize
    created = []

    def cancel(self, graph, key, destination):
        original(self, graph, key, destination)
        if key[1] == "bin/value0":
            created.append(destination)
            cancellation.set()

    with monkeypatch.context() as fault:
        fault.setattr(tool_view.Projection, "materialize", cancel)
        with pytest.raises(sandbox.InvocationInterrupted):
            with tool_capture.Capture(model.load(project.root), stage, 10, cancellation) as capture:
                capture.view(("first",))
    assert len(created) == 1 and not (stage / "tools").exists()


@pytest.mark.parametrize("expiry_check", [1, 10], ids=["before-removal", "during-removal"])
def test_cleanup_deadline_is_terminal_then_abandoned_recovery_uses_same_bound(
    sparse_pair, monkeypatch, expiry_check
):
    project, sources, _ = sparse_pair
    monkeypatch.setattr(tool_context, "MAX_VISITS", 69)
    manifest = model.load(project.root)
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return 61 if calls > expiry_check else 0

    directory = None
    cleanup_errors, preserved = [], []

    def preserve_abandoned(path):
        # A labelled outer-boundary double preserves the failed invocation and
        # its real owner.lock; recovery itself remains the production operation.
        assert path == directory
        preserved.append(path)
        raise sandbox.CleanupError("owned fixture preserves abandoned invocation")

    try:
        with monkeypatch.context() as abandonment:
            abandonment.setattr(sandbox, "_remove", preserve_abandoned)
            with pytest.raises(sandbox.CleanupError):
                with sandbox.invocation(project.root) as directory:
                    capture = tool_capture.Capture(manifest, directory, 10, None)
                    capture.__enter__()
                    two_views(capture, sources)
                    capture.view(("first", "second"))
                    assert len(owned_paths(capture.directory)) == 69
                    with monkeypatch.context() as fault:
                        fault.setattr(tool_capture, "time", SimpleNamespace(monotonic=clock))
                        with pytest.raises(sandbox.CleanupError) as caught:
                            capture.__exit__(None, None, None)
                        cleanup_errors.append(caught.value)
        assert calls > expiry_check
        assert len(cleanup_errors) == len(preserved) == 1
        assert directory is not None and directory.exists()
        with controller.lock(project.root / ".pytest-telescope/recovery-test.lock", 10):
            sandbox.recover(project.root)
        assert not directory.exists()
    finally:
        if directory is not None and directory.exists():
            if (directory / "tools").exists():
                tool_capture._remove(directory / "tools")
            sandbox._remove(directory)
