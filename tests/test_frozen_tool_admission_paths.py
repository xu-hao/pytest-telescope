"""Owned ancestor replacement exercises actual no-follow capture and bounded retry."""

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import controller, model, tool_capture
from pytest_telescope.model import InfrastructureError


@pytest.mark.parametrize(
    "fault", ["stable", "link-once", "link-always", "file-once", "file-always"]
)
def test_capture_reopens_owned_ancestors_without_following_replacement(
    tmp_path, monkeypatch, fault
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    source = project.add_tree("owned", files={"value.txt": "pinned bytes\n"})
    project.module(
        "test_owned.py",
        """import pytest
pytestmark = pytest.mark.telescope(tools=["owned"])
def test_owned(): pass
""",
    )
    declaration = model.load(project.root)
    stage = tmp_path / "stage"
    stage.mkdir()
    capture = tool_capture.Capture(declaration, stage, 20, None)
    original = tool_capture.Capture._capture
    attempts = []
    retired = tmp_path / "retired"

    def replace_ancestor(self, deadline):
        attempts.append(len(attempts) + 1)
        if fault == "stable" or (fault.endswith("once") and len(attempts) > 1):
            return original(self, deadline)
        project.external.rename(retired)
        try:
            if fault.startswith("link"):
                project.external.symlink_to(retired, target_is_directory=True)
            else:
                project.external.write_bytes(b"owned non-directory ancestor")
            return original(self, deadline)
        finally:
            project.external.unlink()
            retired.rename(project.external)

    monkeypatch.setattr(tool_capture.Capture, "_capture", replace_ancestor)
    if fault.endswith("always"):
        with pytest.raises(InfrastructureError):
            with capture:
                pytest.fail("replaced ancestor was admitted")
    else:
        with capture:
            assert capture.graphs["owned"].bytes(("install", "value.txt")) == b"pinned bytes\n"
    assert attempts == ([1] if fault == "stable" else [1, 2])
    assert (source / "value.txt").read_bytes() == b"pinned bytes\n"
    assert not project.external.is_symlink() and not retired.exists()
    assert not capture.directory.exists() and not capture.materialized


@pytest.mark.parametrize("filename", ["startup.py", "protocol.json"])
def test_runner_identity_binds_owned_producer_bytes(tmp_path, monkeypatch, filename):
    anchor = put(tmp_path, "controller.py", "owned controller bytes")
    changed = put(tmp_path, filename, "before")
    unrelated = put(tmp_path, "notes.txt", "unrelated")
    monkeypatch.setattr(controller, "__file__", str(anchor))
    monkeypatch.setattr(controller, "version", lambda name: "owned-version")
    before = controller.runner_identity()
    unrelated.write_text("changed unrelated bytes")
    assert controller.runner_identity() == before
    changed.write_text("after")
    after = controller.runner_identity()
    assert after["version"] == before["version"] == "owned-version"
    assert after["implementation"] != before["implementation"]
