"""Required real-Bubblewrap mixed-route control; refusals must fail, never skip."""

import json
import shutil

import pytest
from test_ordinary_session_verdict import write_session

from pytest_telescope import controller, ordinary


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    configuration = []
    for name in ("ordinary", "scoped"):
        (root / name / "tests").mkdir(parents=True)
        (root / name / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
        configuration.append(
            f"[tool.pytest_telescope.suites.{name}]\n"
            f'root = "{name}"\n'
            f'config = "{name}/pyproject.toml"\n'
            f'test_roots = ["{name}/tests"]\n'
        )
    (root / "pyproject.toml").write_text("\n".join(configuration))
    (root / "scoped" / "declared.txt").write_text("declared input\n")
    (root / "scoped" / "undeclared.txt").write_text("must stay outside the view\n")
    (root / "scoped" / "tests" / "test_view.py").write_text(
        "import errno\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.telescope(inputs=['scoped/declared.txt'], cache=False)\n"
        "\n"
        "def test_selected_scope(tmp_path):\n"
        "    assert Path.cwd() == Path('/project/scoped')\n"
        f"    for root in (Path('/project'), Path({str(root)!r})):\n"
        "        declared = root / 'scoped/declared.txt'\n"
        "        assert declared.read_text() == 'declared input\\n'\n"
        "        for relative in ('scoped/undeclared.txt', 'ordinary/tests/conftest.py'):\n"
        "            with pytest.raises(FileNotFoundError):\n"
        "                (root / relative).read_text()\n"
        "        with pytest.raises(OSError) as refused:\n"
        "            declared.write_text('must be read-only')\n"
        "        assert refused.value.errno in (errno.EROFS, errno.EACCES)\n"
        "    scratch = tmp_path / 'owned.txt'\n"
        "    scratch.write_text('private scratch')\n"
        "    assert scratch.read_text() == 'private scratch'\n"
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def assert_ordinary_events(project, session_codes):
    events = [
        json.loads(line)
        for line in (project / "ordinary" / "events.jsonl").read_text().splitlines()
    ]
    print(json.dumps({"ordinary_events": events}))
    expected = []
    for code in session_codes:
        expected.extend(
            [
                {"kind": "fixture", "phase": "setup"},
                {"kind": "report", "phase": "setup", "outcome": "passed"},
                {"kind": "report", "phase": "call", "outcome": "passed"},
                {"kind": "fixture", "phase": "teardown"},
                {"kind": "report", "phase": "teardown", "outcome": "passed"},
                {"kind": "session", "incoming": 0, "final": code},
            ]
        )
    assert events == expected


def assert_mixed_evidence(project, report, session_code):
    assert report["kind"] == "run"
    assert report["sandbox"]["required"] is True
    assert len(report["sandbox"]["identity"]) == 64
    assert report["inventory_complete"] is True and report["grouping_complete"] is True
    assert report["collection"]["outcome"] == "executed"
    assert {(row["name"], row["route"]) for row in report["collection"]["suites"]} == {
        ("ordinary", "ordinary"),
        ("scoped", "scoped"),
    }
    assert report["counts"]["selected"] == report["counts"]["executed"] == 2
    assert report["counts"]["reused"] == report["counts"]["failed"] == 0
    assert report["counts"]["infrastructure_error"] == 0
    assert report["cases"] == {"collected": 2, "passed": 2, "failed": 0, "skipped": 0}
    assert len(report["groups"]) == len(report["units"]) == 2
    for route in ("ordinary", "scoped"):
        counts = report["route_counts"][route]
        assert counts["selected"] == counts["executed"] == 1
        assert counts["reused"] == counts["failed"] == counts["infrastructure_error"] == 0
        member = next(row for row in report["units"] if row["route"] == route)
        group = next(row for row in report["groups"] if row["route"] == route)
        assert member["suite"] == group["suite"] == route
        assert member["result_status"] == "passed" and member["outcome"] == "executed"
        assert member["complete"] is True and member["attempts"] == 1
        assert member["group_id"] == group["group_id"]
        assert group["case_ids"] == [member["case_id"]]
        assert group["complete"] is True and group["stop_reason"] is None
        assert group["attempts"] == 1 and group["cache_eligible"] is False
        assert group["passed"] == 1 and group["failed"] == group["skipped"] == 0
        assert group["not_run"] == group["unresolved"] == 0
        expected = "infrastructure_error" if route == "ordinary" and session_code else "executed"
        assert group["outcome"] == expected
    state = project / ".pytest-telescope"
    assert list((state / "scratch").iterdir()) == []
    assert not list((state / "cache").rglob("*.json"))
    assert not list((state / "cache").rglob("*.pending"))
    assert (project / "scoped" / "declared.txt").read_text() == "declared input\n"
    assert report["status"] == ("infrastructure_error" if session_code else "passed")


@pytest.mark.parametrize("session_code", [3, 4])
def test_real_scoped_execution_preserves_ordinary_session_failure(
    project, monkeypatch, session_code
):
    actual_capture = ordinary.capture
    observed = []

    def observe_capture(*args, **kwargs):
        result = actual_capture(*args, **kwargs)
        observed.append(result[0])
        print(json.dumps({"ordinary_exit": result[0], "output": result[1].decode()}))
        return result

    monkeypatch.setattr(ordinary, "capture", observe_capture)
    write_session(project / "ordinary", 0)
    witness = controller.run(project, explain=True, workers=1, timeout=10)
    print(json.dumps({"witness": witness}, sort_keys=True))
    assert_ordinary_events(project, [0])
    assert observed == [0]
    assert_mixed_evidence(project, witness, 0)

    write_session(project / "ordinary", session_code)
    for repetition in (1, 2):
        report = controller.run(project, explain=True, workers=1, timeout=10)
        print(json.dumps({"repetition": repetition, "report": report}, sort_keys=True))
        expected_codes = [0] + [session_code] * repetition
        assert_ordinary_events(project, expected_codes)
        assert observed == expected_codes
        assert_mixed_evidence(project, report, session_code)
