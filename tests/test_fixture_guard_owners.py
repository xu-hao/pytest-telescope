"""Real scoped sessions preserve user fixtures by actual pytest lifetime owner."""

import hashlib
import json

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import UnsupportedCapability


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure(root):
    put(root, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n')


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def verify_two_groups(report):
    assert report["schema"] == 5 and report["status"] == "passed", report
    assert report["fixture_guard"] == {"profile": "available-v1", "state": "admitted", "conditions": []}
    assert report["inventory_complete"] and report["grouping_complete"]
    assert report["counts"]["executed"] == 2 and len(report["groups"]) == 2
    expected = {case_id("tests/test_a.py::test_owner"), case_id("tests/test_b.py::test_owner")}
    assert {group["case_ids"][0] for group in report["groups"]} == expected
    assert all(len(group["case_ids"]) == 1 and group["complete"] for group in report["groups"])
    assert len({group["group_id"] for group in report["groups"]}) == 2
    assert all(unit["result_status"] == "passed" for unit in report["units"])


def test_same_module_owner_split_across_scopes_refuses_before_dispatch(tmp_path, monkeypatch):
    configure(tmp_path)
    put(tmp_path, "a.txt", "a")
    put(tmp_path, "b.txt", "b")
    put(tmp_path, "tests/test_split.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def owned():
    yield []
@pytest.mark.telescope(inputs=["a.txt"])
def test_a(owned): owned.append("a")
@pytest.mark.telescope(inputs=["b.txt"])
def test_b(owned): owned.append("b")
''')
    observed = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        observed.append(request["mode"])
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-sharing"
    assert "guard-capture" in observed and "guard-inspection" in observed
    assert "guard-batch" not in observed


def test_distinct_module_fixtures_have_independent_lifetime_owners(tmp_path):
    configure(tmp_path)
    for name in ("a", "b"):
        put(tmp_path, f"tests/test_{name}.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def owned():
    state = []
    yield state
    assert state == ["seen"]
def test_owner(owned):
    assert owned == []
    owned.append("seen")
''')
    report = controller.run(tmp_path, fixture_guard="available-v1")
    verify_two_groups(report)


def test_imported_fixture_definition_has_distinct_module_lifetime_owners(tmp_path):
    configure(tmp_path)
    put(tmp_path, "owned_helper.py", '''import pytest
@pytest.fixture(scope="module")
def owned():
    state = []
    yield state
    assert state == ["seen"]
''')
    for name in ("a", "b"):
        put(tmp_path, f"tests/test_{name}.py", '''import pytest
from owned_helper import owned
pytestmark = pytest.mark.telescope(inputs=["owned_helper.py"])
def test_owner(owned):
    assert owned == []
    owned.append("seen")
''')
    report = controller.run(tmp_path, fixture_guard="available-v1")
    verify_two_groups(report)
