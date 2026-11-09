"""Public ordinary-session controls for selection, precedence, collection and freshness."""

import json
import shutil

import pytest
from test_ordinary_session_verdict import (
    assert_group_identity,
    assert_phase_reports,
    assert_session_events,
    invoke,
    write_session,
)

from pytest_telescope import controller


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


def read_events(project):
    events = [json.loads(line) for line in (project / "events.jsonl").read_text().splitlines()]
    print(json.dumps({"events": events}))
    return events


def run_endpoint(project, endpoint, arguments=()):
    if endpoint == "api":
        report = controller.run(project, arguments=arguments, explain=True)
        code = None
    else:
        assert endpoint == "cli"
        command = ["pytest_telescope", "run", "--explain"]
        if arguments:
            command.extend(["--", *arguments])
        result = invoke(project, *command)
        report, code = json.loads(result.stdout), result.returncode
    print(json.dumps({"report": report, "cli_exit": code}, sort_keys=True))
    return report, code


@pytest.mark.parametrize("endpoint", ["api", "cli"])
@pytest.mark.parametrize("session_code", [5, 3, 4])
def test_empty_ordinary_selection_keeps_final_session_verdict(project, endpoint, session_code):
    write_session(project, 0 if session_code == 5 else session_code)
    arguments = ["-k", "no_matching_case"]
    expected_events = [{"kind": "session", "incoming": 5, "final": session_code}]
    witness = invoke(project, "pytest", "-q", *arguments, "tests/test_subject.py")
    assert read_events(project) == expected_events
    assert witness.returncode == session_code, witness.stdout + witness.stderr
    (project / "events.jsonl").unlink()

    report, code = run_endpoint(project, endpoint, arguments)
    assert read_events(project) == expected_events
    assert report["schema"] == 4 and report["kind"] == "run"
    assert report["inventory_complete"] is True and report["grouping_complete"] is True
    assert report["collection"]["outcome"] == "executed"
    assert report["collection"]["collected"] == 1
    assert report["counts"]["selected"] == 0 and report["counts"]["not-selected"] == 1
    assert report["counts"]["executed"] == report["counts"]["reused"] == 0
    assert report["counts"]["failed"] == report["counts"]["infrastructure_error"] == 0
    assert report["cases"] == {"collected": 0, "passed": 0, "failed": 0, "skipped": 0}
    (member,) = report["units"]
    assert member["route"] == "ordinary" and member["selected"] is False
    assert member["outcome"] == "not-selected" and member["result_status"] is None
    assert member["group_id"] is None and member["complete"] is False
    assert member["reports"] == []
    (group,) = report["groups"]
    assert group["route"] == "ordinary" and group["case_ids"] == []
    assert_group_identity(group, "ordinary-v2")
    assert group["complete"] is True and group["stop_reason"] is None
    assert group["attempts"] == 1 and group["cache_eligible"] is False
    assert group["passed"] == group["failed"] == group["skipped"] == 0
    assert group["not_run"] == group["unresolved"] == 0
    assert group["outcome"] == ("executed" if session_code == 5 else "infrastructure_error")
    assert report["sandbox"]["required"] is False
    assert not (project / ".pytest-telescope").exists()
    assert report["status"] == ("no_tests" if session_code == 5 else "infrastructure_error")
    if endpoint == "cli":
        assert code == (5 if session_code == 5 else 3)


@pytest.mark.parametrize("infrastructure_first", [True, False], ids=["first", "last"])
@pytest.mark.parametrize("session_code", [3, 4])
def test_ordinary_session_infrastructure_takes_precedence_over_member_failure(
    project, infrastructure_first, session_code
):
    suite_codes = dict(
        zip(("first", "second"), (session_code, 1) if infrastructure_first else (1, session_code))
    )
    configuration = []
    for name, final_code in suite_codes.items():
        suite = project / name
        (suite / "tests").mkdir(parents=True)
        (suite / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
        write_session(suite, final_code)
        configuration.append(
            f"[tool.pytest_telescope.suites.{name}]\n"
            f'root = "{name}"\n'
            f'config = "{name}/pyproject.toml"\n'
            f'test_roots = ["{name}/tests"]\n'
        )
    (project / "pyproject.toml").write_text("\n".join(configuration))

    report = controller.run(project, explain=True)
    print(json.dumps({"report": report}, sort_keys=True))
    for name, final_code in suite_codes.items():
        assert_session_events(project / name, final_code)
    assert report["schema"] == 4 and report["kind"] == "run"
    assert report["inventory_complete"] is True and report["grouping_complete"] is True
    assert report["collection"]["outcome"] == "executed"
    assert report["suites"] == ["first", "second"]
    assert [group["suite"] for group in report["groups"]] == ["first", "second"]
    assert report["cases"] == {"collected": 2, "passed": 1, "failed": 1, "skipped": 0}
    assert report["counts"]["selected"] == 2
    assert report["counts"]["executed"] == report["counts"]["failed"] == 1
    assert report["counts"]["infrastructure_error"] == report["counts"]["reused"] == 0
    assert report["route_counts"]["ordinary"] == report["counts"]
    assert report["route_counts"]["scoped"]["selected"] == 0
    assert len(report["units"]) == 2
    for group in report["groups"]:
        failed = int(suite_codes[group["suite"]] == 1)
        member = next(row for row in report["units"] if row["suite"] == group["suite"])
        assert member["route"] == "ordinary" and member["complete"] is True
        assert member["result_status"] == ("failed" if failed else "passed")
        assert_phase_reports(member, "failed" if failed else "passed")
        assert_group_identity(group, "ordinary-v2")
        assert member["outcome"] == ("failed" if failed else "executed")
        assert member["group_id"] == group["group_id"]
        assert member["attempts"] == 1
        assert group["case_ids"] == [member["case_id"]]
        assert group["outcome"] == ("failed" if failed else "infrastructure_error")
        assert group["route"] == "ordinary" and group["complete"] is True
        assert group["stop_reason"] is None and group["cache_eligible"] is False
        assert group["attempts"] == 1
        assert group["passed"] == 1 - failed and group["failed"] == failed
        assert group["skipped"] == group["not_run"] == group["unresolved"] == 0
    assert report["sandbox"]["required"] is False
    assert not (project / ".pytest-telescope").exists()
    assert report["status"] == "infrastructure_error"


@pytest.mark.parametrize("endpoint", ["api", "cli"])
def test_ordinary_collection_failure_is_not_an_empty_or_infrastructure_run(project, endpoint):
    write_session(project, 0)
    (project / "tests" / "test_subject.py").write_text(
        'raise RuntimeError("synthetic ordinary collection failure")\n'
    )
    report, code = run_endpoint(project, endpoint)
    assert read_events(project) == [{"kind": "session", "incoming": 2, "final": 2}]
    assert report["schema"] == 4 and report["kind"] == "run"
    assert report["inventory_complete"] is False and report["grouping_complete"] is False
    assert report["collection"]["outcome"] == "failed"
    assert report["collection"]["failures"]
    assert all(
        failure["phase"] == "collect" and failure["exception_type"] == "CollectionError"
        for failure in report["collection"]["failures"]
    )
    assert report["units"] == report["groups"] == []
    assert report["counts"]["selected"] == report["counts"]["executed"] == 0
    assert report["counts"]["failed"] == report["counts"]["infrastructure_error"] == 0
    assert report["counts"]["reused"] == 0
    assert report["sandbox"]["required"] is False
    assert not (project / ".pytest-telescope").exists()
    assert report["status"] == "failed"
    if endpoint == "cli":
        assert code == 1


@pytest.mark.parametrize("session_code", [0, 3, 4])
def test_ordinary_sessions_repeat_fresh_even_after_final_session_failure(project, session_code):
    write_session(project, session_code)
    first_events = None
    first_group_id = None
    for invocation in (1, 2):
        report = controller.run(project, explain=True)
        print(json.dumps({"invocation": invocation, "report": report}, sort_keys=True))
        events = read_events(project)
        if invocation == 1:
            assert_session_events(project, session_code)
            first_events = events
        else:
            assert events == first_events * 2
        assert report["schema"] == 4 and report["kind"] == "run"
        assert report["inventory_complete"] is True and report["grouping_complete"] is True
        assert report["cases"] == {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}
        assert report["counts"]["executed"] == report["counts"]["selected"] == 1
        assert report["counts"]["reused"] == report["counts"]["failed"] == 0
        assert report["counts"]["infrastructure_error"] == 0
        assert report["route_counts"]["ordinary"] == report["counts"]
        (member,) = report["units"]
        assert member["route"] == "ordinary" and member["outcome"] == "executed"
        assert member["result_status"] == "passed" and member["complete"] is True
        assert_phase_reports(member)
        assert member["attempts"] == 1
        (group,) = report["groups"]
        assert group["route"] == "ordinary" and group["complete"] is True
        assert_group_identity(group, "ordinary-v2")
        assert member["group_id"] == group["group_id"]
        if first_group_id is None:
            first_group_id = group["group_id"]
        else:
            assert group["group_id"] != first_group_id
        assert group["outcome"] == ("executed" if session_code == 0 else "infrastructure_error")
        assert group["attempts"] == 1 and group["cache_eligible"] is False
        assert group["case_ids"] == [member["case_id"]]
        assert report["sandbox"]["required"] is False
        assert not (project / ".pytest-telescope").exists()
        assert report["status"] == ("passed" if session_code == 0 else "infrastructure_error")
