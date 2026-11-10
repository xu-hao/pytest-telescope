"""Real post-body policy mutation retains actual worker observations on refusal."""

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from frozen_tool_fixtures import ToolProject, cache_bytes

import pytest_telescope
from pytest_telescope import controller, model, ordinary, sandbox
from pytest_telescope.model import DeclarationError, fingerprint
from pytest_telescope.result import validate_batch, validate_ordinary, validate_result


def project_with_policy(tmp_path, *, mutate=True, scoped=False):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    counter = tmp_path / "body-once"
    marker = "inputs=[], cache=False" if scoped else "cache=False"
    body = (
        (
            "    request.config.owned_calls = getattr(request.config, 'owned_calls', 0) + 1\n"
            "    assert request.config.owned_calls == 1\n"
        )
        if scoped
        else (f"    p = Path({str(counter)!r})\n    assert not p.exists()\n    p.write_text('x')\n")
    )
    mutation = (
        "    next(request.node.iter_markers('telescope')).kwargs['cache'] = True\n"
        if mutate
        else ""
    )
    project.module(
        "test_policy.py",
        "from pathlib import Path\nimport pytest\n"
        f"@pytest.mark.telescope({marker})\ndef test_body(request):\n" + body + mutation,
    )
    return project, counter


@contextmanager
def observe_ordinary(observed):
    original_capture, original_session, original_popen = (
        ordinary.capture,
        ordinary.session,
        sandbox.subprocess.Popen,
    )

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        observed["processes"].append(process)
        return process

    def capture(argv, *args, **kwargs):
        path = Path(argv[-1])
        assert path.stat().st_size <= 65536
        request = json.loads(path.read_bytes())
        result = original_capture(argv, *args, **kwargs)
        code, output = result
        assert len(output) <= 1024 * 1024
        frames = [
            json.loads(line.removeprefix(b"TELESCOPE_RESULT "))
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        assert len(frames) == 1
        frame = frames[0]
        assert validate_ordinary(frame, request["nonce"], context_id=request["context_id"])
        assert frame["exit_code"] == code
        observed["frames"].append(frame)
        return result

    def session(*args, **kwargs):
        result = original_session(*args, **kwargs)
        observed["returned"].append(result)
        return result

    def no_prepare(*args, **kwargs):
        raise AssertionError("policy-only ordinary route prepared a sandbox")

    with (
        patch.object(sandbox.subprocess, "Popen", popen),
        patch.object(ordinary, "capture", capture),
        patch.object(ordinary, "session", session),
        patch.object(sandbox, "prepare", no_prepare),
    ):
        yield


def prerequisites(observed, counter):
    assert len(observed["frames"]) == len(observed["returned"]) == len(observed["processes"]) == 1
    frame = observed["frames"][0]
    assert observed["returned"][0] == frame
    assert type(frame["declaration_error"]) is bool and frame["declaration_error"] is True
    assert frame["protocol"] == 2 and frame["exit_code"] == 4
    assert frame["complete"] is False and frame["inventory_complete"] is False
    assert frame["dispatch_started"] is True and frame["worker_seconds"] >= 0
    assert counter.read_text() == "x"
    assert observed["processes"][0].poll() is not None
    member(frame)
    return frame


def member(frame):
    assert frame["passed"] == frame["collected"] == 1
    (row,) = frame["case_results"]
    assert row["outcome"] == "passed" and row["complete"]
    assert [(entry["phase"], entry["outcome"]) for entry in row["reports"]] == [
        ("setup", "passed"),
        ("call", "passed"),
        ("teardown", "passed"),
    ]
    return row


def retained(report, frame, *, ordinary_route, empty_ordinary=None):
    assert report["kind"] == "refusal" and report["status"] == "declaration_error"
    accounting = report["accounting"]
    assert accounting["version"] == 1 and accounting["state"] == "available"
    observed = accounting["observations"]
    assert "status" not in observed and "kind" not in observed
    expected_member = member(frame)
    (unit,) = observed["units"]
    if ordinary_route:
        assert empty_ordinary is None
        (group,) = observed["groups"]
        assert group["route"] == "ordinary" and group["profile"] == "ordinary-v2"
    else:
        assert empty_ordinary is not None
        assert len(observed["groups"]) == 2
        by_context = {row["group_id"]: row for row in observed["groups"]}
        assert len(by_context) == 2
        assert frame["context_id"] != empty_ordinary["context_id"]
        assert set(by_context) == {frame["context_id"], empty_ordinary["context_id"]}
        group = by_context[frame["context_id"]]
        empty = by_context[empty_ordinary["context_id"]]
        assert group["route"] == "scoped"
        assert frame["protocol"] in (8, 9)
        assert group["profile"] == ("case-v2" if frame["protocol"] == 8 else "batch-v2")
        assert empty["route"] == "ordinary" and empty["profile"] == "ordinary-v2"
        assert empty["case_ids"] == empty_ordinary["case_ids"] == []
        assert empty["suite"] == group["suite"] == unit["suite"]
        assert empty["attempts"] == 1 and empty["outcome"] == "executed"
        assert empty["complete"] is True and empty["cache_eligible"] is False
        assert empty["stop_reason"] is None
        assert empty["worker_seconds"] == empty_ordinary["worker_seconds"]
        assert empty["passed"] == empty["failed"] == empty["skipped"] == 0
        assert empty["not_run"] == empty["unresolved"] == 0
        assert empty["failures"] == empty_ordinary["failures"] == []
        assert empty["collection_skips"] == empty_ordinary["collection_skips"] == []
    assert unit["group_id"] == group["group_id"] == frame["context_id"]
    assert group["case_ids"] == [unit["case_id"]]
    assert unit["route"] == group["route"]
    assert unit["case_id"] == expected_member["case_id"]
    assert unit["attempts"] == group["attempts"] == 1
    assert unit["complete"] and unit["result_status"] == "passed" and unit["passed"] == 1
    assert unit["reports"] == expected_member["reports"]
    assert group["outcome"] == ("infrastructure_error" if ordinary_route else "incomplete")
    assert group["complete"] is False and not group["cache_eligible"]
    assert group["worker_seconds"] == frame["worker_seconds"]
    assert report["inventory_complete"] is (not ordinary_route)
    assert observed["inventory_complete"] is report["inventory_complete"]
    assert observed["inventory_digest"] == report["inventory_digest"]
    if ordinary_route:
        assert report["inventory_digest"] is None
    else:
        assert report["inventory_digest"] is not None


@pytest.mark.parametrize("mutate", [False, True], ids=["unchanged", "late-refusal"])
def test_ordinary_real_policy_mark_preserves_body_observations(tmp_path, mutate):
    project, counter = project_with_policy(tmp_path, mutate=mutate)
    assert not model.load(project.root).scope_possible
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed):
        if mutate:
            with pytest.raises(DeclarationError) as caught:
                controller.run(project.root, timeout=10)
            frame = prerequisites(observed, counter)
            print(
                json.dumps(
                    {
                        "protocol": frame["protocol"],
                        "declaration_error": frame["declaration_error"],
                        "phases": member(frame)["reports"],
                        "worker_seconds": frame["worker_seconds"],
                    }
                )
            )
            retained(controller.error_result(caught.value), frame, ordinary_route=True)
        else:
            report = controller.run(project.root, timeout=10)
            assert report["status"] == "passed" and report["counts"]["executed"] == 1
            assert len(observed["processes"]) == 1 and counter.read_text() == "x"
            assert observed["frames"][0]["declaration_error"] is False
    assert not cache_bytes(project.root)


def test_invalid_literal_policy_refuses_before_any_worker_start(tmp_path):
    project, counter = project_with_policy(tmp_path)
    project.module(
        "test_policy.py",
        "import pytest\n@pytest.mark.telescope(cache='invalid')\ndef test_body(): pass\n",
    )
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed), pytest.raises(DeclarationError) as caught:
        controller.run(project.root, timeout=10)
    assert caught.value.accounting is None and "accounting" not in controller.error_result(
        caught.value
    )
    assert not any(observed.values()) and not counter.exists() and not cache_bytes(project.root)


def cli_observed(evidence):
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed):
        code = controller.main(["run", "--timeout", "10"])
    value = {
        "frames": observed["frames"],
        "returned": observed["returned"],
        "started": len(observed["processes"]),
        "exited": all(process.poll() is not None for process in observed["processes"]),
    }
    encoded = json.dumps(value).encode()
    assert len(encoded) <= 1024 * 1024
    with evidence.open("xb") as stream:
        stream.write(encoded)
    return code


def test_cli_late_ordinary_refusal_preserves_actual_worker_evidence(tmp_path):
    project, counter = project_with_policy(tmp_path)
    assert not model.load(project.root).scope_possible
    evidence = tmp_path / "worker-observation.json"
    paths = [str(Path(pytest_telescope.__file__).parent.parent), str(Path(__file__).parent)]
    code = (
        "import sys; from pathlib import Path; sys.path[:0]=" + repr(paths) + "; "
        "from test_late_policy_refusal_observations import cli_observed; "
        "raise SystemExit(cli_observed(Path(sys.argv[1])))"
    )
    status, output = sandbox.capture(
        [sys.executable, "-I", "-B", "-c", code, str(evidence)],
        10,
        env=dict(os.environ),
        cwd=project.root,
        limit=1024 * 1024,
    )
    assert status == 4 and counter.read_text() == "x"
    assert evidence.stat().st_size <= 1024 * 1024
    actual = json.loads(evidence.read_bytes())
    assert actual["started"] == 1 and actual["exited"]
    (frame,) = actual["frames"]
    assert actual["returned"] == [frame] and validate_ordinary(frame)
    assert frame["declaration_error"] is True and frame["exit_code"] == 4
    assert not frame["complete"] and not frame["inventory_complete"] and frame["dispatch_started"]
    member(frame)
    report = json.loads(output)
    assert str(project.root).encode() not in output and b"TELESCOPE_RESULT" not in output
    retained(report, frame, ordinary_route=True)
    assert not cache_bytes(project.root)


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_scoped_late_policy_refusal_retains_real_member_reports(tmp_path, monkeypatch, mode):
    project, counter = project_with_policy(tmp_path, scoped=True)
    prefix = Path(sys.prefix)
    box = sandbox.Sandbox(
        "/usr/bin/bwrap",
        sys.executable,
        ((str(prefix), str(prefix)),),
        str(prefix / "bin") + ":/usr/bin:/bin",
        fingerprint(["owned-default-worker", sys.executable, str(prefix)]),
        {},
    )
    monkeypatch.setattr(sandbox, "prepare", lambda root: box)
    original = sandbox.execute
    returned, started = [], []
    original_session, original_popen = ordinary.session, sandbox.subprocess.Popen
    empty_sessions, ordinary_processes = [], []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        ordinary_processes.append(process)
        return process

    def ordinary_session(*args, **kwargs):
        with patch.object(sandbox.subprocess, "Popen", popen):
            value = original_session(*args, **kwargs)
        assert validate_ordinary(value)
        empty_sessions.append(value)
        return value

    def execute(*args, **kwargs):
        request = kwargs["request"]
        if request["mode"] != "collection":
            callback = kwargs["on_session_start"]

            def on_start():
                started.append(request["mode"])
                callback()

            kwargs["on_session_start"] = on_start
        value = original(*args, **kwargs)
        if request["mode"] != "collection":
            assert value["context_id"] == request["context_id"]
            validator = validate_result if mode == "case" else validate_batch
            assert validator(value)
            returned.append(value)
        return value

    monkeypatch.setattr(sandbox, "execute", execute)
    monkeypatch.setattr(ordinary, "session", ordinary_session)
    with pytest.raises(DeclarationError) as caught:
        controller.run(project.root, timeout=10, execution_mode=mode)
    assert len(returned) == len(started) == 1 and not counter.exists()
    frame = returned[0]
    assert frame["declaration_error"] is True and frame["exit_code"] == 4 and not frame["complete"]
    assert frame["protocol"] == (8 if mode == "case" else 9)
    member(frame)
    (empty_ordinary,) = empty_sessions
    assert len(ordinary_processes) == 1 and ordinary_processes[0].poll() is not None
    assert empty_ordinary["protocol"] == 2 and empty_ordinary["exit_code"] == 5
    assert empty_ordinary["declaration_error"] is False
    assert empty_ordinary["complete"] is True and empty_ordinary["inventory_complete"] is True
    assert empty_ordinary["dispatch_started"] is True
    assert empty_ordinary["case_ids"] == empty_ordinary["case_results"] == []
    assert (
        empty_ordinary["collected"]
        == empty_ordinary["passed"]
        == empty_ordinary["failed"]
        == empty_ordinary["skipped"]
        == 0
    )
    (observed_item,) = empty_ordinary["items"]
    assert observed_item["selected"] is True and observed_item["case_id"] == frame["case_ids"][0]
    retained(
        controller.error_result(caught.value),
        frame,
        ordinary_route=False,
        empty_ordinary=empty_ordinary,
    )
    assert not cache_bytes(project.root)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())
