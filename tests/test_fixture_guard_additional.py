"""Selection, parameter and lifecycle evidence in real guarded sessions."""

import hashlib
import json

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n')


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def test_direct_and_indirect_parameters_preserve_actual_fixture_lifetimes(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/test_params.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def indirect(request):
    state = []
    yield request.param, state
    assert state == ["used"]
@pytest.mark.parametrize("indirect", ["first", "second"], indirect=True, scope="module")
@pytest.mark.parametrize("direct", [1])
def test_item(indirect, direct):
    value, state = indirect
    assert value in ("first", "second") and direct == 1
    state.append("used")
''')
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert report["counts"]["executed"] == 2 and len(report["groups"]) == 1
    assert all(row["complete"] and row["result_status"] == "passed" for row in report["units"])
    warm = controller.run(tmp_path, fixture_guard="available-v1")
    assert warm["counts"]["reused"] == 2


def test_skipped_setup_and_call_keep_real_phase_reports_and_group_reuse(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/test_skip.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture
def skip_setup(): pytest.skip("synthetic setup skip")
def test_setup(skip_setup): pass
def test_call(): pytest.skip("synthetic call skip")
''')
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["counts"]["executed"] == 2
    assert all(row["result_status"] == "skipped" for row in report["units"])
    phases = {row["case_id"]: [phase["phase"] for phase in row["reports"] if phase["outcome"] == "skipped"] for row in report["units"]}
    assert phases[case_id("tests/test_skip.py::test_setup")] == ["setup"]
    assert phases[case_id("tests/test_skip.py::test_call")] == ["call"]
    assert controller.run(tmp_path, fixture_guard="available-v1")["counts"]["reused"] == 2


def test_session_nonzero_after_passing_report_cannot_publish(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/conftest.py", '''def pytest_sessionfinish(session, exitstatus):
    if getattr(session.config, "_synthetic_body_finished", False):
        session.exitstatus = 1
''')
    put(tmp_path, "tests/test_verdict.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])
def test_item(request): request.config._synthetic_body_finished = True
''')
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["fixture_guard"]["state"] == "admitted"
    assert report["status"] == "failed" and not report["groups"][0]["cache_eligible"]
    assert report["units"][0]["result_status"] == "passed"
    again = controller.run(tmp_path, fixture_guard="available-v1")
    assert again["counts"]["reused"] == 0


def test_condition_uses_selected_case_and_exact_absence_empty_presence(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.delenv("SYNTHETIC_ABSENT", raising=False)
    monkeypatch.setenv("SYNTHETIC_EMPTY", "")
    put(tmp_path, "tests/test_condition.py", '''import pytest
pytestmark = pytest.mark.telescope(env=["SYNTHETIC_ABSENT", "SYNTHETIC_EMPTY"])
def test_selected(): pass
def test_unselected(): pass
''')
    selected = case_id("tests/test_condition.py::test_selected")
    unselected = case_id("tests/test_condition.py::test_unselected")
    conditions = [
        {"case_id": selected, "name": "SYNTHETIC_ABSENT", "op": "absent"},
        {"case_id": selected, "name": "SYNTHETIC_EMPTY", "op": "present"},
        {"case_id": selected, "name": "SYNTHETIC_EMPTY", "op": "equals", "value": ""},
    ]
    report = controller.run(tmp_path, fixture_guard="available-v1", arguments=["-k", "selected and not unselected"], condition_checks=conditions)
    assert report["status"] == "passed" and report["counts"]["selected"] == 1
    rows = report["fixture_guard"]["conditions"]
    assert len(rows) == 3 and all(row["case_id"] == selected and row["matched"] for row in rows)
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["-k", "selected and not unselected"], condition_checks=[{**conditions[0], "case_id": unselected}])
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["-k", "selected and not unselected"], condition_checks=[{**conditions[0], "name": "UNDECLARED"}])
