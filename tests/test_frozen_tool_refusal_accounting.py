"""Actual ordinary effects survive a typed deferred refusal without replay."""

import json

import pytest
from frozen_tool_fixtures import cache_bytes
from test_frozen_tool_review_controller import mixed_project

from pytest_telescope import controller, model, tool_capture
from pytest_telescope.model import DeclarationError, UnsupportedCapability

FIELDS = {
    "execution",
    "suites",
    "inventory_complete",
    "inventory_digest",
    "grouping_complete",
    "counts",
    "route_counts",
    "cases",
    "case_outcomes",
    "collection",
    "units",
    "groups",
    "elapsed_seconds",
    "setup_seconds",
}


def deferred_project(tmp_path, monkeypatch, *, after=1, extra_case=False):
    project, counter = mixed_project(tmp_path)
    if extra_case:
        module = project.root / "tests/test_mixed.py"
        module.write_text(
            module.read_text()
            + "\n@pytest.mark.telescope(tools=['first'])\ndef test_second_scoped(): pass\n"
        )
    original = tool_capture.Capture.view
    requests = []

    def deferred(self, names):
        if counter.exists():
            requests.append(tuple(names))
            if len(requests) == after:
                raise UnsupportedCapability("tool:layout", "owned deferred view refusal")
        return original(self, names)

    monkeypatch.setattr(tool_capture.Capture, "view", deferred)
    return project, counter, requests


def retained(report, known):
    assert report["kind"] == "refusal" and report["status"] == "unsupported", report
    assert report["capability"] == "tool:layout"
    accounting = report["accounting"]
    assert set(accounting) == {"version", "state", "observations"}
    assert type(accounting["version"]) is int and accounting["version"] == 1
    assert accounting["state"] == "available"
    observed = accounting["observations"]
    assert set(observed) == FIELDS
    assert observed["inventory_complete"] is True and report["inventory_complete"] is True
    assert observed["inventory_digest"] == report["inventory_digest"]
    assert len(report["inventory_digest"]) == 64
    assert len(observed["units"]) == known
    assert observed["counts"]["selected"] == known
    for key, total in observed["counts"].items():
        assert sum(counts[key] for counts in observed["route_counts"].values()) == total
    ordinary = [row for row in observed["units"] if row["route"] == "ordinary"]
    scoped = [row for row in observed["units"] if row["route"] == "scoped"]
    assert len(ordinary) == 1 and ordinary[0]["outcome"] == "executed"
    assert ordinary[0]["attempts"] == 1 and ordinary[0]["passed"] == 1
    assert len(scoped) == known - 1
    assert all(row["attempts"] == 0 and row["group_id"] is None for row in scoped)
    assert all(not row["complete"] and row["passed"] == 0 for row in scoped)
    assert not observed["grouping_complete"]
    assert observed["route_counts"]["ordinary"]["executed"] == 1
    return observed


@pytest.mark.parametrize(
    "error",
    [
        DeclarationError("owned early declaration"),
        UnsupportedCapability("tool:layout", "owned early layout"),
    ],
)
def test_early_typed_exception_declares_no_started_accounting(error):
    assert error.accounting is None
    report = controller.error_result(error)
    assert "accounting" not in report
    assert report["inventory_complete"] is False and report["inventory_digest"] is None


def test_late_python_exception_retains_immutable_real_observations(tmp_path, monkeypatch):
    project, counter, requests = deferred_project(tmp_path, monkeypatch)
    with pytest.raises(UnsupportedCapability) as caught:
        controller.run(project.root, timeout=20, explain=True)
    assert counter.read_text() == "x" and len(requests) == 1
    error = caught.value
    assert error.capability == "tool:layout" and error.accounting is not None
    report = controller.error_result(error)
    observed = retained(report, 2)
    observed["route_counts"]["ordinary"]["executed"] = 99
    observed["units"].clear()
    retained(controller.error_result(error), 2)
    assert not cache_bytes(project.root)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())


def test_late_cli_refusal_keeps_exit_four_and_actual_accounting(tmp_path, monkeypatch, capsys):
    project, counter, requests = deferred_project(tmp_path, monkeypatch)
    monkeypatch.chdir(project.root)
    status = controller.main(["run", "--timeout", "20", "--explain"])
    output = capsys.readouterr()
    assert status == 4 and not output.err
    retained(json.loads(output.out), 2)
    assert counter.read_text() == "x" and len(requests) == 1
    assert not cache_bytes(project.root)


def test_partial_planning_retains_every_known_unstarted_case(tmp_path, monkeypatch):
    project, counter, requests = deferred_project(tmp_path, monkeypatch, after=2, extra_case=True)
    report = project.run()
    assert counter.read_text() == "x" and len(requests) == 2
    retained(report, 3)
    assert not cache_bytes(project.root)


def test_bounded_unavailable_keeps_late_session_and_inventory(tmp_path, monkeypatch, capsys):
    project, counter, requests = deferred_project(tmp_path, monkeypatch)
    monkeypatch.setattr(model, "MAX_OUTPUT_BYTES", 2048)
    monkeypatch.chdir(project.root)
    assert controller.main(["run", "--timeout", "20", "--explain"]) == 4
    output = capsys.readouterr()
    assert len(output.out.encode()) <= 2048 and not output.err
    report = json.loads(output.out)
    assert report["kind"] == "refusal" and report["status"] == "unsupported"
    assert report["accounting"] == {"version": 1, "state": "unavailable", "reason": "bounds"}
    assert report["inventory_complete"] is True and len(report["inventory_digest"]) == 64
    assert counter.read_text() == "x" and len(requests) == 1
    assert not cache_bytes(project.root)
