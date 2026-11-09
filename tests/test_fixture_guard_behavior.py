"""Required unreleased fixture-guard controls using owned Bubblewrap projects.

These tests exercise real public calls and are not qualified until the feature exists
and the namespace controls pass. Collection and test execution are deliberately separate.
"""

import hashlib
import json

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError, InfrastructureError, UnsupportedCapability


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\n',
    )


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def selected(report, node):
    return next(unit for unit in report["units"] if unit["case_id"] == case_id(node))


def admitted(report):
    assert report["schema"] == 5 and report["kind"] == "run"
    assert report["status"] == "passed", report
    assert report["inventory_complete"] and report["grouping_complete"]
    assert report["fixture_guard"] == {
        "profile": "available-v1",
        "state": "admitted",
        "conditions": [],
    }


def test_function_fixtures_with_distinct_inputs_execute_separately_and_reuse(tmp_path):
    configure(tmp_path)
    put(tmp_path, "left.txt", "left-v1")
    put(tmp_path, "right.txt", "right-v1")
    put(
        tmp_path,
        "tests/test_precise.py",
        '''from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture
def per_case(tmp_path):
    assert not (tmp_path / "state").exists()
    (tmp_path / "state").write_text("owned")
    yield tmp_path
    assert (tmp_path / "state").read_text() == "owned"
@pytest.mark.telescope(inputs=["left.txt"])
def test_left(per_case):
    assert Path("left.txt").read_text() in ("left-v1", "left-v2")
    assert not Path("right.txt").exists()
@pytest.mark.telescope(inputs=["right.txt"])
def test_right(per_case):
    assert Path("right.txt").read_text() == "right-v1"
    assert not Path("left.txt").exists()
''',
    )
    left = "tests/test_precise.py::test_left"
    right = "tests/test_precise.py::test_right"
    cold = controller.run(tmp_path, fixture_guard="available-v1")
    admitted(cold)
    assert cold["counts"]["executed"] == 2 and cold["counts"]["reused"] == 0
    assert selected(cold, left)["group_id"] != selected(cold, right)["group_id"]
    assert all(selected(cold, node)["result_status"] == "passed" for node in (left, right))
    warm = controller.run(tmp_path, fixture_guard="available-v1")
    admitted(warm)
    assert warm["counts"]["reused"] == 2
    put(tmp_path, "left.txt", "left-v2")
    changed = controller.run(tmp_path, fixture_guard="available-v1")
    admitted(changed)
    assert selected(changed, left)["outcome"] == "executed"
    assert selected(changed, right)["outcome"] == "reused"


def test_shared_user_fixture_is_one_ordered_atomic_group(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_shared.py",
        '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
created = []
@pytest.fixture(scope="module")
def shared():
    created.append("setup")
    values = []
    yield values
    assert values == [0, 1]
    assert created == ["setup"]
@pytest.mark.parametrize("number", [0, 1])
def test_shared(number, shared):
    assert shared == list(range(number))
    shared.append(number)
''',
    )
    cold = controller.run(tmp_path, fixture_guard="available-v1")
    admitted(cold)
    (group,) = cold["groups"]
    assert group["case_ids"] == [
        case_id("tests/test_shared.py::test_shared[0]"),
        case_id("tests/test_shared.py::test_shared[1]"),
    ]
    assert group["outcome"] == "executed" and group["complete"] and group["cache_eligible"]
    for row in cold["units"]:
        assert row["result_status"] == "passed"
        assert [(report["phase"], report["outcome"]) for report in row["reports"]] == [
            ("setup", "passed"), ("call", "passed"), ("teardown", "passed")
        ]
    warm = controller.run(tmp_path, fixture_guard="available-v1")
    admitted(warm)
    assert warm["counts"]["reused"] == 2
    assert warm["groups"][0]["outcome"] == "reused"


def test_split_possible_shared_user_fixture_refuses(tmp_path):
    configure(tmp_path)
    put(tmp_path, "left.txt", "left")
    put(tmp_path, "right.txt", "right")
    put(
        tmp_path,
        "tests/test_split.py",
        '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def shared():
    yield object()
@pytest.mark.telescope(inputs=["left.txt"])
def test_left(shared): pass
@pytest.mark.telescope(inputs=["right.txt"])
def test_right(shared): pass
''',
    )
    with pytest.raises(UnsupportedCapability) as captured:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert captured.value.capability == "execution:fixture-sharing"
    # Exception conversion is deliberately not evidence that no bodies started.
    refusal = controller.error_result(captured.value, fixture_guard="available-v1")
    assert refusal["schema"] == 5 and refusal["status"] == "unsupported"
    assert refusal["fixture_guard"] == {
        "profile": "available-v1", "state": "unavailable", "conditions": []
    }


def test_default_unguarded_route_remains_schema4_and_reusable(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/test_default.py", 'import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_ok(): pass\n')
    first = controller.run(tmp_path)
    assert first["schema"] == 4 and first["status"] == "passed"
    assert "fixture_guard" not in first
    assert first["execution"]["profile"] == "batch-v2"
    second = controller.run(tmp_path)
    assert second["schema"] == 4 and second["counts"]["reused"] == 1


@pytest.mark.parametrize(
    "error", [DeclarationError("synthetic"), InfrastructureError("synthetic"),
              UnsupportedCapability("execution:fixture-sharing", "synthetic")]
)
def test_converter_has_no_phase_or_attempt_evidence(error):
    old = controller.error_result(error)
    guarded = controller.error_result(error, fixture_guard="available-v1")
    assert old["schema"] == 4 and "fixture_guard" not in old
    assert guarded == {
        **old,
        "schema": 5,
        "fixture_guard": {"profile": "available-v1", "state": "unavailable", "conditions": []},
    }
    assert "units" not in guarded and "groups" not in guarded
    with pytest.raises(DeclarationError):
        controller.error_result(error, fixture_guard="unknown")


@pytest.mark.parametrize("error", [KeyboardInterrupt(), OSError("synthetic")])
def test_cli_outer_exception_with_guard_reports_unknown_phase(monkeypatch, capsys, error):
    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(controller, "run", interrupted)
    assert controller.main(["run", "--fixture-guard", "available-v1"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == 5 and result["status"] == "infrastructure_error"
    assert result["fixture_guard"] == {
        "profile": "available-v1", "state": "unavailable", "conditions": []
    }
    assert "units" not in result and "groups" not in result


def test_cli_parser_failure_before_context_remains_schema4(capsys):
    assert controller.main(["run", "--fixture-guard", "invalid"]) == 4
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["schema"] == 4 and "fixture_guard" not in refusal


def test_conditions_bind_selected_case_and_current_captured_environment(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.setenv("SYNTHETIC_GUARD_OPTION", "fixture-guard-synthetic-value")
    put(
        tmp_path,
        "tests/test_condition.py",
        '''import os
import pytest
pytestmark = pytest.mark.telescope(env=["SYNTHETIC_GUARD_OPTION"])
def test_case():
    assert os.environ["SYNTHETIC_GUARD_OPTION"] == "fixture-guard-synthetic-value"
''',
    )
    node = "tests/test_condition.py::test_case"
    checks = [
        {"case_id": case_id(node), "name": "SYNTHETIC_GUARD_OPTION", "op": "present"},
        {"case_id": case_id(node), "name": "SYNTHETIC_GUARD_OPTION", "op": "equals",
         "value": "fixture-guard-synthetic-value"},
    ]
    cold = controller.run(tmp_path, fixture_guard="available-v1", condition_checks=checks)
    warm = controller.run(tmp_path, fixture_guard="available-v1", condition_checks=checks)
    for report in (cold, warm):
        assert report["schema"] == 5 and report["status"] == "passed"
        group_id = selected(report, node)["group_id"]
        assert report["fixture_guard"] == {
            "profile": "available-v1",
            "state": "admitted",
            "conditions": [
                {"case_id": case_id(node), "group_id": group_id, "matched": True},
                {"case_id": case_id(node), "group_id": group_id, "matched": True},
            ],
        }
        assert selected(report, node)["result_status"] == "passed"
        assert "fixture-guard-synthetic-value" not in json.dumps(report)
    assert cold["counts"]["executed"] == 1 and warm["counts"]["reused"] == 1
    bad = [{**checks[1], "value": "other-synthetic-value"}]
    with pytest.raises(UnsupportedCapability) as captured:
        controller.run(tmp_path, fixture_guard="available-v1", condition_checks=bad)
    assert captured.value.capability == "execution:condition-mismatch"
