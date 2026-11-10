"""Ordinary declaration refusals retain genuine worker observations."""

import json
import sys
from pathlib import Path

import pytest

from pytest_telescope import controller, model, ordinary, sandbox
from pytest_telescope.model import DeclarationError
from pytest_telescope.result import validate_ordinary


def project(root, *, mutate=False, invalid=False, collection_change=False):
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n', encoding="utf-8"
    )
    marker = "'invalid'" if invalid else "False"
    change = (
        "    next(request.node.iter_markers('telescope')).kwargs['cache'] = True\n"
        if mutate
        else ""
    )
    (root / "tests/test_owned.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        f"@pytest.mark.telescope(cache={marker})\n"
        "def test_item(request):\n"
        "    p = Path('owned-body-event')\n"
        "    assert not p.exists()\n"
        "    p.write_text('body')\n" + change,
        encoding="utf-8",
    )
    if collection_change:
        (root / "tests/conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    for item in items:\n"
            "        item.get_closest_marker('telescope').kwargs['cache'] = True\n",
            encoding="utf-8",
        )


def observe_real_worker(monkeypatch):
    original_capture = ordinary.capture
    original_session = ordinary.session
    original_popen = sandbox.subprocess.Popen
    processes, frames, returned = [], [], []

    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        processes.append(child)
        return child

    def capture(argv, timeout, **kwargs):
        assert argv[0] == sys.executable and timeout is None
        path = Path(argv[-1])
        assert path.stat().st_size <= 65536
        request = json.loads(path.read_bytes())
        code, output = original_capture(argv, timeout, **kwargs)
        assert len(output) <= 1024 * 1024
        lines = [line for line in output.splitlines() if line.startswith(b"TELESCOPE_RESULT ")]
        assert len(lines) == 1
        result = json.loads(lines[0].removeprefix(b"TELESCOPE_RESULT "))
        assert validate_ordinary(result, request["nonce"], context_id=request["context_id"])
        assert result["exit_code"] == code
        frames.append(result)
        return code, output

    def session(*args, **kwargs):
        result = original_session(*args, **kwargs)
        returned.append(result)
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("ordinary route prepared a sandbox", pytrace=False)

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)
    monkeypatch.setattr(ordinary, "capture", capture)
    monkeypatch.setattr(ordinary, "session", session)
    monkeypatch.setattr(sandbox, "prepare", forbidden)
    return processes, frames, returned


def actual_frame(processes, frames, returned):
    assert len(processes) == len(frames) == len(returned) == 1
    assert processes[0].poll() is not None
    (frame,) = frames
    assert returned == [frame] and frame["protocol"] == 2
    return frame


def passed_member(frame):
    assert frame["collected"] == frame["passed"] == 1
    (member,) = frame["case_results"]
    assert member["outcome"] == "passed" and member["complete"]
    assert [(row["phase"], row["outcome"]) for row in member["reports"]] == [
        ("setup", "passed"),
        ("call", "passed"),
        ("teardown", "passed"),
    ]
    return member


def assert_no_cache(root):
    assert not (root / ".pytest-telescope").exists()


def test_real_ordinary_marker_without_mutation_passes(tmp_path, monkeypatch):
    project(tmp_path)
    assert not model.load(tmp_path).scope_possible
    processes, frames, returned = observe_real_worker(monkeypatch)
    report = controller.run(tmp_path, timeout=10)
    frame = actual_frame(processes, frames, returned)
    assert frame["declaration_error"] is False and frame["exit_code"] == 0
    assert frame["dispatch_started"] and frame["inventory_complete"] and frame["complete"]
    member = passed_member(frame)
    assert (tmp_path / "owned-body-event").read_text() == "body"
    assert report["schema"] == 4 and report["kind"] == "run" and report["status"] == "passed"
    assert report["units"][0]["case_id"] == member["case_id"]
    assert report["units"][0]["reports"] == member["reports"]
    assert report["units"][0]["attempts"] == 1 and report["units"][0]["worker_seconds"] is None
    assert_no_cache(tmp_path)


def test_real_ordinary_post_body_declaration_refusal_retains_reports(tmp_path, monkeypatch):
    project(tmp_path, mutate=True)
    assert not model.load(tmp_path).scope_possible
    processes, frames, returned = observe_real_worker(monkeypatch)
    refusal = None
    try:
        controller.run(tmp_path, timeout=10)
    except DeclarationError as error:
        refusal = error
    frame = actual_frame(processes, frames, returned)
    assert frame["declaration_error"] is True and frame["exit_code"] == 4
    assert not frame["complete"] and not frame["inventory_complete"] and frame["dispatch_started"]
    member = passed_member(frame)
    assert (tmp_path / "owned-body-event").read_text() == "body"
    assert_no_cache(tmp_path)
    print(
        "W2_ACTUAL",
        frame["protocol"],
        frame["declaration_error"],
        frame["exit_code"],
        member["case_id"],
    )
    assert type(refusal) is DeclarationError and refusal.accounting is not None
    retained = refusal.accounting.render()
    assert retained["version"] == 1 and retained["state"] == "available"
    observed = retained["observations"]
    assert observed["inventory_complete"] is False and observed["inventory_digest"] is None
    assert observed["counts"]["executed"] == 1 and observed["cases"]["passed"] == 1
    (unit,) = observed["units"]
    (group,) = observed["groups"]
    assert unit["case_id"] == member["case_id"] == group["case_ids"][0]
    assert unit["group_id"] == group["group_id"]
    assert (
        unit["result_status"] == "passed"
        and unit["complete"]
        and unit["reports"] == member["reports"]
    )
    assert unit["attempts"] == group["attempts"] == 1 and unit["worker_seconds"] is None
    assert group["profile"] == "ordinary-v2" and group["route"] == "ordinary"
    assert not group["complete"] and not group["cache_eligible"]
    assert group["worker_seconds"] == frame["worker_seconds"]
    envelope = controller.error_result(refusal)
    assert envelope["schema"] == 4 and envelope["status"] == "declaration_error"
    assert envelope["inventory_complete"] is False and envelope["inventory_digest"] is None
    assert envelope["accounting"] == retained
    retained["observations"]["units"].clear()
    assert len(refusal.accounting.render()["observations"]["units"]) == 1
    assert (tmp_path / "owned-body-event").read_text() == "body" and len(processes) == 1


def test_invalid_literal_cache_refuses_before_any_worker(tmp_path, monkeypatch):
    project(tmp_path, invalid=True)
    processes, frames, returned = observe_real_worker(monkeypatch)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, timeout=10)
    assert type(captured.value) is DeclarationError and captured.value.accounting is None
    assert not processes and not frames and not returned
    assert not (tmp_path / "owned-body-event").exists()
    assert_no_cache(tmp_path)


def test_real_collection_change_refuses_before_dispatch(tmp_path, monkeypatch):
    project(tmp_path, collection_change=True)
    assert not model.load(tmp_path).scope_possible
    processes, frames, returned = observe_real_worker(monkeypatch)
    refusal = None
    try:
        controller.run(tmp_path, timeout=10)
    except DeclarationError as error:
        refusal = error
    frame = actual_frame(processes, frames, returned)
    assert frame["declaration_error"] is True and frame["exit_code"] == 4
    assert not frame["dispatch_started"] and not frame["inventory_complete"]
    assert not frame["case_results"] and not (tmp_path / "owned-body-event").exists()
    assert_no_cache(tmp_path)
    assert type(refusal) is DeclarationError and refusal.accounting is not None
    retained = refusal.accounting.render()
    assert retained["version"] == 1 and retained["state"] == "available"
    observed = retained["observations"]
    assert observed["inventory_complete"] is False and observed["inventory_digest"] is None
    assert all(not group["case_ids"] for group in observed["groups"])
    assert all(unit["group_id"] is None for unit in observed["units"])
    assert observed["cases"]["passed"] == 0 and observed["counts"]["executed"] == 0
