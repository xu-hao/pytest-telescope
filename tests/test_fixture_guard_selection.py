"""Guard condition capture, selection, inherited fixtures and bounds."""

import hashlib
import json

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\nmarkers = ["chosen: synthetic selector"]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


@pytest.mark.requires_fixture_guard
def test_inherited_and_overridden_fixture_chain_runs_and_tears_down(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/conftest.py",
        """import pytest
@pytest.fixture(scope="module")
def shared():
    values = []
    yield values
    assert values == ["base", "body"]
""",
    )
    put(
        tmp_path,
        "tests/test_override.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])
@pytest.fixture(scope="module")
def shared(shared):
    shared.append("base")
    yield shared
    assert shared == ["base", "body"]
def test_item(shared): shared.append("body")
""",
    )
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert report["units"][0]["reports"][-1]["phase"] == "teardown"
    assert report["units"][0]["reports"][-1]["outcome"] == "passed"
    assert controller.run(tmp_path, fixture_guard="available-v1")["counts"]["reused"] == 1


@pytest.mark.requires_fixture_guard
@pytest.mark.parametrize("selector", [["-m", "chosen"], ["tests/test_select.py::test_chosen"]])
def test_marker_and_node_selectors_bind_exact_guarded_membership(tmp_path, selector):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_select.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.chosen
def test_chosen(): pass
def test_other(): pass
""",
    )
    report = controller.run(tmp_path, fixture_guard="available-v1", arguments=selector)
    assert report["status"] == "passed" and report["counts"]["selected"] == 1
    assert report["groups"][0]["case_ids"] == [case_id("tests/test_select.py::test_chosen")]
    assert sum(row["selected"] for row in report["units"]) == 1


@pytest.mark.requires_fixture_guard
def test_captured_condition_and_worker_environment_survive_ambient_change(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.setenv("GUARD_SNAPSHOT", "before")
    put(
        tmp_path,
        "tests/test_capture.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(env=["GUARD_SNAPSHOT"])
def test_item(): assert os.environ["GUARD_SNAPSHOT"] == "before"
""",
    )
    original = sandbox.execute
    changed = []

    def mutate_after_collection(*args, request, **kwargs):
        result = original(*args, request=request, **kwargs)
        if request["mode"] == "collection" and not changed:
            monkeypatch.setenv("GUARD_SNAPSHOT", "after")
            changed.append(True)
        return result

    monkeypatch.setattr(sandbox, "execute", mutate_after_collection)
    case = case_id("tests/test_capture.py::test_item")
    check = {"case_id": case, "name": "GUARD_SNAPSHOT", "op": "equals", "value": "before"}
    report = controller.run(tmp_path, fixture_guard="available-v1", condition_checks=[check])
    assert changed and report["status"] == "passed"
    assert report["fixture_guard"]["conditions"] == [
        {"case_id": case, "group_id": report["units"][0]["group_id"], "matched": True}
    ]
    assert report["units"][0]["result_status"] == "passed"


def test_condition_cardinality_and_total_value_limits_are_strict(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_case.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    case = case_id("tests/test_case.py::test_item")
    many = [{"case_id": case, "name": f"SYN_{index}", "op": "present"} for index in range(4097)]
    total = [
        {"case_id": case, "name": f"SYN_{index}", "op": "equals", "value": "x" * 4096}
        for index in range(257)
    ]
    for invalid in (many, total):
        with pytest.raises(DeclarationError):
            controller.run(tmp_path, fixture_guard="available-v1", condition_checks=invalid)
