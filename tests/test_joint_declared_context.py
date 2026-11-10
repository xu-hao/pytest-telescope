"""Portable joint tool, helper, environment and fixture-guard behavior."""

import hashlib
import json

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError


def identity(name):
    return hashlib.sha256(
        json.dumps(["default", "tests/test_joint.py::" + name], separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture
def joint_project(tmp_path, monkeypatch):
    project = ToolProject(tmp_path / "project", tmp_path / "owned-tools")
    project.add_tree("left", files={"value.txt": "left-value"})
    project.add_tree("right", files={"value.txt": "right-value"})
    project.configure(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\n'
    )
    put(
        project.root,
        "helper.py",
        """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(env=["HELPER_ONLY"])
AVAILABLE_TOOLS = {name for name in ("left", "right") if Path("/tools", name).is_dir()}
assert AVAILABLE_TOOLS, "collection lost its declared tool view"
def read_tool(name):
    return Path("/tools", name, "install", "value.txt").read_text()
""",
    )
    project.module(
        "test_joint.py",
        """from pathlib import Path
import os
import pytest
import helper
pytestmark = pytest.mark.telescope(inputs=["helper.py"])
@pytest.fixture
def per_case():
    values = []
    yield values
    assert len(values) == 1
@pytest.mark.telescope(tools=["left"], env=["LEFT_FEATURE"])
def test_left(per_case):
    assert helper.AVAILABLE_TOOLS == {"left"}
    assert per_case == []
    per_case.append(helper.read_tool("left"))
    assert per_case == ["left-value"]
    assert os.environ["LEFT_FEATURE"] in ("left-v1", "left-v2")
    assert "RIGHT_FEATURE" not in os.environ
    assert "HELPER_ONLY" not in os.environ
    assert not Path("/tools/right").exists()
@pytest.mark.telescope(tools=["right"], env=["RIGHT_FEATURE"])
def test_right(per_case):
    assert helper.AVAILABLE_TOOLS == {"right"}
    assert per_case == []
    per_case.append(helper.read_tool("right"))
    assert per_case == ["right-value"]
    assert os.environ["RIGHT_FEATURE"] == "right-v1"
    assert "LEFT_FEATURE" not in os.environ
    assert "HELPER_ONLY" not in os.environ
    assert not Path("/tools/left").exists()
""",
    )
    monkeypatch.setenv("LEFT_FEATURE", "left-v1")
    monkeypatch.setenv("RIGHT_FEATURE", "right-v1")
    monkeypatch.setenv("HELPER_ONLY", "not-a-consumer-grant")
    return project


def guarded_run(project):
    result = controller.run(project.root, fixture_guard="available-v1", timeout=20)
    assert result["schema"] == 5 and result["status"] == "passed", result
    assert result["inventory_complete"] and result["grouping_complete"]
    assert result["fixture_guard"] == {
        "profile": "available-v1",
        "state": "admitted",
        "conditions": [],
    }
    assert len(result["groups"]) == 2
    assert len({row["group_id"] for row in result["groups"]}) == 2
    assert all(row["complete"] and row["cache_eligible"] for row in result["groups"])
    return {row["case_id"]: row for row in result["units"]}


def test_guard_revalidates_exact_tool_helper_and_environment_scopes(joint_project, monkeypatch):
    baseline = controller.run(joint_project.root, timeout=20)
    assert baseline["schema"] == 4 and baseline["status"] == "passed", baseline
    assert "fixture_guard" not in baseline
    assert baseline["counts"]["executed"] == 2
    cold = guarded_run(joint_project)
    assert {row["outcome"] for row in cold.values()} == {"executed"}
    warm = guarded_run(joint_project)
    assert {row["outcome"] for row in warm.values()} == {"reused"}
    monkeypatch.setenv("LEFT_FEATURE", "left-v2")
    changed = guarded_run(joint_project)
    assert changed[identity("test_left")]["outcome"] == "executed"
    assert changed[identity("test_right")]["outcome"] == "reused"


def test_authenticated_helper_marker_cannot_be_borrowed_by_a_guarded_case(
    joint_project, monkeypatch
):
    module = joint_project.root / "tests/test_joint.py"
    module.write_text(module.read_text() + "\ntest_left.pytestmark.append(helper.pytestmark)\n")
    modes = []
    execute = sandbox.execute

    def observed(*args, request, **kwargs):
        modes.append(request["mode"])
        return execute(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observed)
    with pytest.raises(DeclarationError):
        controller.run(joint_project.root, fixture_guard="available-v1", timeout=20)
    assert modes and "collection" in modes
    assert all(mode not in modes for mode in ("batch", "guard-batch", "execution"))
