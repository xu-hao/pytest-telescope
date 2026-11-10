"""Guard admission and drift controls against real Bubblewrap sessions.

Only the sandbox dispatch seam is observed or faulted; real fixtures and workers run.
"""

import copy
import hashlib
import json
from importlib.metadata import version

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.fixture_guard import (
    framework_definition_ids,
    require_capture_match,
    validate_capture,
    validate_inspection,
)
from pytest_telescope.model import DeclarationError, InfrastructureError, UnsupportedCapability


def put(root, name, contents):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


def different_digest(value):
    return ("1" if value[0] == "0" else "0") + value[1:]


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def distinct_groups(root, body_a="pass", body_b="pass", fixture=""):
    configure(root)
    put(root, "a.txt", "a")
    put(root, "b.txt", "b")
    put(
        root,
        "tests/test_groups.py",
        f"""import pytest
pytestmark = pytest.mark.telescope(inputs=[])
{fixture}
@pytest.mark.telescope(inputs=["a.txt"])
def test_a():
    {body_a}
@pytest.mark.telescope(inputs=["b.txt"])
def test_b():
    {body_b}
""",
    )


def test_e83_02_shared_fixture_refusal_has_no_guarded_body_dispatch(tmp_path, monkeypatch):
    distinct_groups(
        tmp_path, fixture='@pytest.fixture(scope="module")\ndef shared(): return object()'
    )
    events = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        events.append(("start", request["mode"]))
        result = original(*args, request=request, **kwargs)
        events.append(("complete", request["mode"]))
        return result

    monkeypatch.setattr(sandbox, "execute", observe)
    with pytest.raises(UnsupportedCapability) as error:
        controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert error.value.capability == "execution:fixture-sharing"
    assert any(event == ("complete", "guard-capture") for event in events)
    assert any(event == ("complete", "guard-inspection") for event in events)
    assert not any(mode in ("guard-batch", "batch", "execution") for _, mode in events)
    # The dispatch observation proves no guarded execution was submitted through
    # this seam. It is not inferred from error_result or a collection side-effect claim.


def test_e83_04_real_capture_inspection_bind_nonce_context_and_members(tmp_path, monkeypatch):
    distinct_groups(tmp_path)
    observed = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        result = original(*args, request=request, **kwargs)
        if request["mode"] in ("guard-capture", "guard-inspection"):
            observed.append((request["mode"], copy.deepcopy(request), copy.deepcopy(result)))
        return result

    monkeypatch.setattr(sandbox, "execute", observe)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert [mode for mode, _, _ in observed] == [
        "guard-capture",
        "guard-inspection",
        "guard-capture",
        "guard-inspection",
    ]
    framework_ids = framework_definition_ids(version("pytest"))
    for index in (0, 2):
        _, capture_request, capture = observed[index]
        _, inspection_request, inspection = observed[index + 1]
        assert len(capture["nonce"]) == len(inspection["nonce"]) == 64
        assert capture["base_context_id"] == capture_request["context_id"]
        assert inspection["group_id"] == inspection_request["context_id"]
        assert capture["case_ids"] == inspection["case_ids"]
        assert capture["registry_id"] == inspection["registry_id"]
        require_capture_match(capture, inspection)
        for key in ("nonce", "base_context_id", "case_ids", "content_id"):
            damaged = copy.deepcopy(capture)
            damaged[key] = [] if key == "case_ids" else different_digest(capture[key])
            with pytest.raises(InfrastructureError):
                validate_capture(
                    damaged,
                    nonce=capture["nonce"],
                    base_context_id=capture["base_context_id"],
                    case_ids=capture["case_ids"],
                    framework_ids=framework_ids,
                )
        for key in ("nonce", "context_id", "group_id", "registry_id", "graph_id", "case_ids"):
            damaged = copy.deepcopy(inspection)
            damaged[key] = [] if key == "case_ids" else different_digest(inspection[key])
            with pytest.raises(InfrastructureError):
                validate_inspection(
                    damaged,
                    nonce=inspection["nonce"],
                    group_id=inspection["group_id"],
                    case_ids=inspection["case_ids"],
                    registry_id=inspection["registry_id"],
                    framework_ids=framework_ids,
                )


def test_e83_04_late_graph_drift_retains_first_result_and_publishes_nothing(tmp_path, monkeypatch):
    distinct_groups(tmp_path)
    events = []
    original = sandbox.execute

    def drift(*args, request, **kwargs):
        if request["mode"] == "guard-batch":
            events.append("dispatch")
            if len(events) == 2:
                request = {**request, "graph_id": different_digest(request["graph_id"])}
            result = original(*args, request=request, **kwargs)
            if len(events) == 1:
                assert result["exit_code"] == 0
            return result
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", drift)
    with pytest.raises(DeclarationError) as caught:
        controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert events == ["dispatch", "dispatch"]
    report = controller.error_result(caught.value, fixture_guard="available-v1")
    assert report["schema"] == 5 and report["status"] == "declaration_error"
    assert report["fixture_guard"]["state"] == "admitted"
    observations = report["accounting"]["observations"]
    units = {unit["case_id"]: unit for unit in observations["units"]}
    first = units[case_id("tests/test_groups.py::test_a")]
    assert first["outcome"] == "executed" and first["result_status"] == "passed"
    assert first["attempts"] >= 1
    assert not any(group["cache_eligible"] for group in observations["groups"])
    monkeypatch.setattr(sandbox, "execute", original)
    fresh = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert fresh["status"] == "passed" and fresh["counts"]["reused"] == 0


def test_pre_admission_infrastructure_run_preserves_unavailable_observations(tmp_path, monkeypatch):
    distinct_groups(tmp_path)
    original = sandbox.execute
    observed = []

    def fail_capture(*args, request, **kwargs):
        observed.append(request["mode"])
        if request["mode"] == "guard-capture":
            raise InfrastructureError("synthetic capture failure")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", fail_capture)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert report["schema"] == 5 and report["kind"] == "run"
    assert report["status"] == "infrastructure_error"
    assert report["fixture_guard"] == {
        "profile": "available-v1",
        "state": "unavailable",
        "conditions": [],
    }
    assert report["inventory_complete"] and len(report["units"]) == 2
    assert observed.count("guard-capture") == 2
    assert "guard-batch" not in observed


def test_framework_mutable_state_is_group_local(tmp_path):
    distinct_groups(
        tmp_path,
        body_a='request.getfixturevalue("doctest_namespace")["synthetic"] = "first"; '
        'request.getfixturevalue("pytestconfig")._synthetic_group = "first"',
        body_b='assert "synthetic" not in request.getfixturevalue("doctest_namespace"); '
        'assert not hasattr(request.getfixturevalue("pytestconfig"), "_synthetic_group")',
    )
    path = tmp_path / "tests/test_groups.py"
    source = path.read_text(encoding="utf-8").replace("def test_a():", "def test_a(request):")
    source = source.replace("def test_b():", "def test_b(request):")
    path.write_text(source, encoding="utf-8")
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert report["counts"]["executed"] == 2 and len(report["groups"]) == 2
