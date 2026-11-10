"""Owned protected-link and mid-copy cancellation controls without any child process."""

import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import tool_capture, tool_context
from pytest_telescope.model import UnsupportedCapability
from pytest_telescope.sandbox import InvocationInterrupted


def capture_for(project, stage, cancellation=None):
    stage.mkdir()
    tools = tool_context.load_selected(project.root, project.definitions, ["owned"])
    return tool_capture.Capture(SimpleNamespace(root=project.root, tools=tools), stage, 10, cancellation)


@pytest.mark.parametrize("protected", [".ssh", ".aws"])
def test_link_to_owned_unselected_credential_tree_refuses_without_opening_target(
    tmp_path, monkeypatch, protected
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    public = project.add_tree("owned")
    private = put(project.external, protected + "/dummy", "synthetic private fixture\n")
    (public / "alias").symlink_to("../" + protected + "/dummy")
    project.refresh("owned")
    original = tool_capture._parent
    opened = []

    @contextmanager
    def parent(filename):
        filename = Path(filename)
        assert not filename.is_relative_to(private.parent), "unselected credential tree was opened"
        opened.append(filename)
        with original(filename) as pair:
            yield pair

    monkeypatch.setattr(tool_capture, "_parent", parent)
    capture = capture_for(project, tmp_path / "capture")
    with pytest.raises(UnsupportedCapability) as caught:
        with capture:
            pytest.fail("unselected protected link target admitted")
    assert caught.value.capability == "tool:layout"
    assert public / "alias" in opened and private not in opened
    assert not capture.directory.exists()


def test_cancellation_after_first_real_copy_does_not_retry_or_keep_material(tmp_path, monkeypatch):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    source = project.add_tree("owned", files={"first": "one", "second": "two"})
    cancellation = threading.Event()
    capture = capture_for(project, tmp_path / "capture", cancellation)
    original_measure, original_capture = tool_capture._measure, tool_capture.Capture._capture
    copied, attempts = [], []

    def measure(filename, expected, budget, destination=None, *, frozen=False):
        result = original_measure(filename, expected, budget, destination, frozen=frozen)
        if Path(filename) == source / "first" and destination is not None:
            assert destination.read_bytes() == b"one"
            copied.append(destination)
            cancellation.set()
        return result

    def attempt(self, deadline):
        attempts.append(self.directory)
        return original_capture(self, deadline)

    monkeypatch.setattr(tool_capture, "_measure", measure)
    monkeypatch.setattr(tool_capture.Capture, "_capture", attempt)
    with pytest.raises(InvocationInterrupted):
        with capture:
            pytest.fail("cancelled capture admitted")
    assert len(copied) == len(attempts) == 1
    assert not capture.directory.exists() and not capture.materialized
    assert (source / "first").read_bytes() == b"one"
    assert (source / "second").read_bytes() == b"two"
