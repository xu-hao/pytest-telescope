"""Real ordinary sessions keep final pytest verdicts distinct from member reports."""

import json
import os
import shutil
import signal
import subprocess
import sys

import pytest

from pytest_telescope import controller, ordinary

SCENARIOS = [
    pytest.param(0, "passed", 0, id="pass"),
    pytest.param(1, "failed", 1, id="member-failure"),
    pytest.param(3, "infrastructure_error", 3, id="final-3"),
    pytest.param(4, "infrastructure_error", 3, id="final-4"),
]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def write_session(project, session_code):
    override = {
        0: "None",
        1: "None",
        3: "pytest.ExitCode.INTERNAL_ERROR",
        4: "pytest.ExitCode.USAGE_ERROR",
    }[session_code]
    (project / "tests" / "conftest.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "EVENTS = Path(__file__).parents[1] / 'events.jsonl'\n"
        "\n"
        "def record(**event):\n"
        "    with EVENTS.open('a') as stream:\n"
        "        stream.write(json.dumps(event) + '\\n')\n"
        "\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    record(kind='fixture', phase='setup')\n"
        "    yield 7\n"
        "    record(kind='fixture', phase='teardown')\n"
        "\n"
        "def pytest_runtest_logreport(report):\n"
        "    record(kind='report', phase=report.when, outcome=report.outcome)\n"
        "\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    override = {override}\n"
        "    if override is not None:\n"
        "        session.exitstatus = override\n"
        "    record(kind='session', incoming=int(exitstatus), final=int(session.exitstatus))\n"
    )
    expected = 8 if session_code == 1 else 7
    (project / "tests" / "test_subject.py").write_text(
        f"def test_subject(resource):\n    assert resource == {expected}\n"
    )


def invoke(project, *arguments):
    """Bound owned subprocesses; interrupt first so the CLI cleans its ordinary child."""
    command = [sys.executable, "-m", *arguments]
    with subprocess.Popen(
        command,
        cwd=project,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
            print(json.dumps({"command": command, "stdout": stdout, "stderr": stderr}))
            pytest.fail("owned ordinary-session subprocess exceeded 20 seconds")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    print(
        json.dumps(
            {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    )
    return result


def assert_session_events(project, session_code):
    events = [json.loads(line) for line in (project / "events.jsonl").read_text().splitlines()]
    print(json.dumps({"events": events}))
    assert events == [
        {"kind": "fixture", "phase": "setup"},
        {"kind": "report", "phase": "setup", "outcome": "passed"},
        {
            "kind": "report",
            "phase": "call",
            "outcome": "failed" if session_code == 1 else "passed",
        },
        {"kind": "fixture", "phase": "teardown"},
        {"kind": "report", "phase": "teardown", "outcome": "passed"},
        {"kind": "session", "incoming": int(session_code == 1), "final": session_code},
    ]


def witness(project, session_code):
    """Independently establish real member reports and pytest's final process exit."""
    write_session(project, session_code)
    result = invoke(project, "pytest", "-q", "tests/test_subject.py")
    assert_session_events(project, session_code)
    assert result.returncode == session_code, result.stdout + result.stderr
    (project / "events.jsonl").unlink()


def assert_member_and_group_evidence(project, report, session_code):
    """Check causal prerequisites before the intentionally failing aggregate assertion."""
    print(json.dumps({"report": report}, sort_keys=True))
    assert_session_events(project, session_code)
    failed = int(session_code == 1)
    passed = 1 - failed
    assert report["kind"] == "run"
    assert report["inventory_complete"] is True
    assert report["grouping_complete"] is True
    assert report["collection"]["outcome"] == "executed"
    assert report["sandbox"]["required"] is False
    assert report["sandbox"]["identity"] is None
    assert report["counts"]["selected"] == 1
    assert report["counts"]["executed"] == passed
    assert report["counts"]["failed"] == failed
    assert report["counts"]["infrastructure_error"] == 0
    assert report["counts"]["reused"] == 0
    assert report["route_counts"]["ordinary"] == report["counts"]
    assert report["route_counts"]["scoped"]["selected"] == 0
    assert report["cases"] == {"collected": 1, "passed": passed, "failed": failed, "skipped": 0}
    (member,) = report["units"]
    assert member["route"] == "ordinary"
    assert member["result_status"] == ("failed" if failed else "passed")
    assert member["outcome"] == ("failed" if failed else "executed")
    assert member["complete"] is True
    assert member["attempts"] == 1
    assert member["passed"] == passed and member["failed"] == failed
    (group,) = report["groups"]
    assert group["route"] == "ordinary" and group["profile"] == "ordinary-v1"
    assert group["case_ids"] == [member["case_id"]]
    assert member["group_id"] == group["group_id"]
    assert (
        group["outcome"]
        == {
            0: "executed",
            1: "failed",
            3: "infrastructure_error",
            4: "infrastructure_error",
        }[session_code]
    )
    assert group["complete"] is True and group["stop_reason"] is None
    assert group["cache_eligible"] is False and group["attempts"] == 1
    assert group["passed"] == passed and group["failed"] == failed
    assert group["skipped"] == group["not_run"] == group["unresolved"] == 0
    assert not (project / ".pytest-telescope").exists()


@pytest.mark.parametrize("session_code,expected_status,cli_code", SCENARIOS)
def test_ordinary_session_verdict_api(
    project, monkeypatch, session_code, expected_status, cli_code
):
    witness(project, session_code)
    actual_capture = ordinary.capture
    observed = []

    def observe_capture(*args, **kwargs):
        result = actual_capture(*args, **kwargs)
        observed.append(result)
        print(json.dumps({"ordinary_exit": result[0], "output": result[1].decode()}))
        return result

    monkeypatch.setattr(ordinary, "capture", observe_capture)
    report = controller.run(project, explain=True)
    assert_member_and_group_evidence(project, report, session_code)
    assert len(observed) == 1
    assert observed[0][0] == session_code
    assert report["status"] == expected_status, report


@pytest.mark.parametrize("session_code,expected_status,cli_code", SCENARIOS)
def test_ordinary_session_verdict_cli(project, session_code, expected_status, cli_code):
    witness(project, session_code)
    result = invoke(project, "pytest_telescope", "run", "--explain")
    report = json.loads(result.stdout)
    assert_member_and_group_evidence(project, report, session_code)
    assert (report["status"], result.returncode) == (expected_status, cli_code), report
