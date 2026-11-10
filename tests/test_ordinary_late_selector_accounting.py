"""Real all-ordinary sessions retain observations when a selector is rejected late."""

import json
import sys

import pytest

from pytest_telescope import controller, ordinary, sandbox
from pytest_telescope.model import DeclarationError
from pytest_telescope.result import validate_ordinary


def project(root):
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n', encoding="utf-8"
    )
    (root / "tests/test_owned.py").write_text(
        "from pathlib import Path\n"
        "def test_real():\n"
        "    with Path('owned-body-events.txt').open('a') as stream:\n"
        "        stream.write('body\\n')\n",
        encoding="utf-8",
    )


def observe_real_ordinary(monkeypatch):
    real_capture = ordinary.capture
    real_session = ordinary.session
    starts, sessions, frames = [], [], []

    def capture(args, timeout, **kwargs):
        assert args[0] == sys.executable
        assert timeout is None
        assert "on_start" not in kwargs
        kwargs["on_start"] = lambda: starts.append("real-child-started")
        code, output = real_capture(args, timeout, **kwargs)
        found = [line for line in output.splitlines() if line.startswith(b"TELESCOPE_RESULT ")]
        assert len(found) == 1
        frames.append((code, json.loads(found[0][len(b"TELESCOPE_RESULT ") :])))
        return code, output

    def session(*args, **kwargs):
        result = real_session(*args, **kwargs)
        assert validate_ordinary(result)
        sessions.append(result)
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("ordinary route invoked sandbox preparation", pytrace=False)

    monkeypatch.setattr(ordinary, "capture", capture)
    monkeypatch.setattr(ordinary, "session", session)
    monkeypatch.setattr(sandbox, "prepare", forbidden)
    return starts, sessions, frames


def assert_actual_pass(root, starts, sessions, frames):
    assert starts == ["real-child-started"] and len(sessions) == len(frames) == 1
    code, frame = frames[0]
    result = sessions[0]
    assert code == 0 and frame == result and result["protocol"] == 2
    assert result["declaration_error"] is False and result["inventory_complete"]
    assert result["passed"] == 1 and result["complete"]
    (member,) = result["case_results"]
    assert member["outcome"] == "passed" and member["complete"]
    assert [(row["phase"], row["outcome"]) for row in member["reports"]] == [
        ("setup", "passed"), ("call", "passed"), ("teardown", "passed")
    ]
    assert (root / "owned-body-events.txt").read_text() == "body\n"
    assert not (root / ".pytest-telescope").exists()
    return result, member


def test_ordinary_real_session_positive_control(tmp_path, monkeypatch):
    project(tmp_path)
    starts, sessions, frames = observe_real_ordinary(monkeypatch)
    report = controller.run(tmp_path, arguments=["tests/test_owned.py::test_real"])
    result, actual = assert_actual_pass(tmp_path, starts, sessions, frames)
    assert report["schema"] == 4 and report["kind"] == "run" and report["status"] == "passed"
    assert report["sandbox"]["required"] is False and report["counts"]["executed"] == 1
    (member,) = report["units"]
    assert member["case_id"] == actual["case_id"] and member["reports"] == actual["reports"]
    assert member["attempts"] == 1 and member["worker_seconds"] is None
    assert report["groups"][0]["attempts"] == 1 and not report["groups"][0]["cache_eligible"]
    assert result["exit_code"] == 0


def test_ordinary_unmatched_node_retains_completed_session_observations(tmp_path, monkeypatch):
    project(tmp_path)
    starts, sessions, frames = observe_real_ordinary(monkeypatch)
    error = None
    returned = None
    try:
        returned = controller.run(
            tmp_path,
            arguments=["tests/test_owned.py::test_real", "tests/test_owned.py::test_absent"],
        )
    except DeclarationError as exc:
        error = exc
    result, actual = assert_actual_pass(tmp_path, starts, sessions, frames)
    print("H5_ACTUAL_ORDINARY", len(starts), result["protocol"], result["passed"], actual["case_id"], "typed", type(error).__name__ if error else None)
    assert returned is None and type(error) is DeclarationError
    assert error.accounting is not None, "late real ordinary session lost retained accounting"
    retained = error.accounting.render()
    assert retained["version"] == 1 and retained["state"] == "available"
    observations = retained["observations"]
    assert observations["inventory_complete"] and observations["inventory_digest"]
    assert not observations["grouping_complete"]
    assert observations["counts"]["executed"] == 1 and observations["cases"]["passed"] == 1
    (member,) = observations["units"]
    (group,) = observations["groups"]
    assert member["case_id"] == actual["case_id"] and member["reports"] == actual["reports"]
    assert member["route"] == "ordinary" and member["result_status"] == "passed"
    assert member["attempts"] == 1 and member["complete"] and member["worker_seconds"] is None
    assert group["route"] == "ordinary" and group["profile"] == "ordinary-v2"
    assert group["attempts"] == 1 and group["case_ids"] == [member["case_id"]]
    assert not group["cache_eligible"] and group["passed"] == 1
    refusal = controller.error_result(error)
    assert refusal["schema"] == 4 and refusal["kind"] == "refusal"
    assert refusal["status"] == "declaration_error" and refusal["accounting"] == retained
    assert refusal["inventory_complete"] == observations["inventory_complete"]
    assert refusal["inventory_digest"] == observations["inventory_digest"]
    assert len(starts) == len(sessions) == 1 and (tmp_path / "owned-body-events.txt").read_text() == "body\n"


def test_ordinary_unknown_file_is_rejected_before_session(tmp_path, monkeypatch):
    project(tmp_path)
    starts, sessions, frames = observe_real_ordinary(monkeypatch)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, arguments=["tests/unknown.py::test_absent"])
    assert type(captured.value) is DeclarationError and captured.value.accounting is None
    assert not starts and not sessions and not frames
    assert not (tmp_path / "owned-body-events.txt").exists()
    assert not (tmp_path / ".pytest-telescope").exists()
