"""Real ordinary-worker edges retain only the observations that actually exist."""

import hashlib
import json

import pytest
from frozen_tool_fixtures import ToolProject, cache_bytes, put
from test_late_policy_refusal_observations import (
    member,
    observe_ordinary,
    prerequisites,
    project_with_policy,
    retained,
)

from pytest_telescope import controller, model
from pytest_telescope.model import DeclarationError


def actual_frames(observed, count):
    assert (
        len(observed["frames"]) == len(observed["returned"]) == len(observed["processes"]) == count
    )
    assert observed["returned"] == observed["frames"]
    assert all(process.poll() is not None for process in observed["processes"])
    assert len({frame["nonce"] for frame in observed["frames"]}) == count
    assert len({frame["context_id"] for frame in observed["frames"]}) == count
    for frame in observed["frames"]:
        # observe_ordinary already checked each real frame against its original request.
        assert frame["protocol"] == 2 and type(frame["declaration_error"]) is bool
        assert frame["worker_seconds"] >= 0
    return observed["frames"]


def envelope(error):
    report = controller.error_result(error)
    assert report["kind"] == "refusal" and report["status"] == "declaration_error"
    assert report["inventory_complete"] is False and report["inventory_digest"] is None
    accounting = report["accounting"]
    assert accounting["version"] == 1 and accounting["state"] == "available"
    observed = accounting["observations"]
    assert "kind" not in observed and "status" not in observed
    assert observed["inventory_complete"] is False and observed["inventory_digest"] is None
    return report, observed


def test_predispatch_policy_refusal_keeps_selected_item_without_execution_membership(tmp_path):
    project, counter = project_with_policy(tmp_path, mutate=False)
    hook = tmp_path / "collection-finish-once"
    put(
        project.root,
        "tests/conftest.py",
        f"""from pathlib import Path
import pytest
@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    assert len(session.items) == 1
    p = Path({str(hook)!r})
    assert not p.exists()
    p.write_text('x')
    next(session.items[0].iter_markers('telescope')).kwargs['cache'] = True
""",
    )
    assert not model.load(project.root).scope_possible
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed), pytest.raises(DeclarationError) as caught:
        controller.run(project.root, timeout=10)
    (frame,) = actual_frames(observed, 1)
    assert hook.read_text() == "x" and not counter.exists()
    assert frame["declaration_error"] is True and frame["exit_code"] == 4
    assert frame["dispatch_started"] is False and frame["inventory_complete"] is False
    assert frame["complete"] is False
    (item,) = frame["items"]
    assert item["selected"] is True and item["order"] == 0
    assert frame["case_ids"] == frame["case_results"] == []
    assert frame["collected"] == frame["passed"] == frame["failed"] == frame["skipped"] == 0
    print(json.dumps({"dispatch_started": False, "observed_items": 1, "execution_members": 0}))
    _, retained_observations = envelope(caught.value)
    (unit,) = retained_observations["units"]
    (group,) = retained_observations["groups"]
    assert unit["case_id"] == item["case_id"] and unit["selected"] is True
    assert unit["group_id"] is None and unit["attempts"] == 0
    assert unit["result_status"] is None and unit["complete"] is False and unit["reports"] == []
    assert unit["outcome"] == "infrastructure_error"
    assert unit["collected"] == unit["passed"] == unit["failed"] == unit["skipped"] == 0
    assert group["group_id"] == frame["context_id"] and group["case_ids"] == []
    assert group["attempts"] == 1 and group["outcome"] == "infrastructure_error"
    assert group["complete"] is False and group["cache_eligible"] is False
    assert group["worker_seconds"] == frame["worker_seconds"]
    assert (
        group["passed"]
        == group["failed"]
        == group["skipped"]
        == group["not_run"]
        == group["unresolved"]
        == 0
    )
    assert not cache_bytes(project.root) and not (project.root / ".pytest-telescope").exists()


def test_collection_skip_and_late_policy_refusal_keep_digest_and_passed_phases(tmp_path):
    project, counter = project_with_policy(tmp_path)
    reason = "owned optional component"
    project.module(
        "test_skip.py",
        f"""import pytest
pytest.skip({reason!r}, allow_module_level=True)
def test_unreached():
    raise AssertionError('collection skip body ran')
""",
    )
    assert not model.load(project.root).scope_possible
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed), pytest.raises(DeclarationError) as caught:
        controller.run(project.root, timeout=10)
    frame = prerequisites(observed, counter)
    actual_frames(observed, 1)
    expected = {
        "file": "tests/test_skip.py",
        "reason": {
            "profile": "pytest-reason-sha256-v1",
            "kind": "skip",
            "sha256": hashlib.sha256(("Skipped: " + reason).encode()).hexdigest(),
        },
    }
    assert frame["collection_skips"] == [expected]
    report, observed_report = envelope(caught.value)
    retained(report, frame, ordinary_route=True)
    (group,) = observed_report["groups"]
    assert group["collection_skips"] == [{**expected, "phase": "collect"}]
    assert observed_report["collection"]["skips"] == [
        {"suite": "default", "route": "ordinary", **expected, "phase": "collect"},
    ]
    assert reason not in json.dumps(report)
    assert not cache_bytes(project.root) and not (project.root / ".pytest-telescope").exists()


def test_prior_ordinary_suite_observations_survive_later_policy_refusal(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    configuration = []
    counters = {}
    for name, mutate in (("a_prior", False), ("z_later", True)):
        counters[name] = tmp_path / (name + "-once")
        put(project.root, name + "/pyproject.toml", '[tool.pytest.ini_options]\naddopts = ""\n')
        mutation = (
            "    next(request.node.iter_markers('telescope')).kwargs['cache'] = True\n"
            if mutate
            else ""
        )
        put(
            project.root,
            name + "/tests/test_" + name + ".py",
            f"""from pathlib import Path
import pytest
@pytest.mark.telescope(cache=False)
def test_body(request):
    p = Path({str(counters[name])!r})
    assert not p.exists()
    p.write_text('x')
"""
            + mutation,
        )
        configuration.append(
            f'[tool.pytest_telescope.suites.{name}]\nroot = "{name}"\n'
            f'config = "{name}/pyproject.toml"\ntest_roots = ["{name}/tests"]\n'
        )
    put(project.root, "pyproject.toml", "\n".join(configuration))
    assert not model.load(project.root).scope_possible
    observed = {"frames": [], "returned": [], "processes": []}
    with observe_ordinary(observed), pytest.raises(DeclarationError) as caught:
        controller.run(project.root, timeout=10)
    prior, later = actual_frames(observed, 2)
    assert all(counter.read_text() == "x" for counter in counters.values())
    assert prior["declaration_error"] is False and prior["exit_code"] == 0
    assert prior["complete"] is True and prior["inventory_complete"] is True
    assert later["declaration_error"] is True and later["exit_code"] == 4
    assert later["complete"] is False and later["inventory_complete"] is False
    assert prior["dispatch_started"] is later["dispatch_started"] is True
    members = [member(prior), member(later)]
    _, observed_report = envelope(caught.value)
    assert observed_report["suites"] == ["a_prior", "z_later"]
    assert observed_report["cases"] == {"collected": 2, "passed": 2, "failed": 0, "skipped": 0}
    assert len(observed_report["units"]) == len(observed_report["groups"]) == 2
    for name, frame, expected_member, complete, outcome in (
        ("a_prior", prior, members[0], True, "executed"),
        ("z_later", later, members[1], False, "infrastructure_error"),
    ):
        unit = next(row for row in observed_report["units"] if row["suite"] == name)
        group = next(row for row in observed_report["groups"] if row["suite"] == name)
        assert (
            unit["case_id"] == expected_member["case_id"]
            and unit["reports"] == expected_member["reports"]
        )
        assert (
            unit["outcome"] == "executed"
            and unit["complete"] is True
            and unit["result_status"] == "passed"
        )
        assert unit["attempts"] == group["attempts"] == 1 and unit["passed"] == 1
        assert unit["group_id"] == group["group_id"] == frame["context_id"]
        assert group["case_ids"] == [unit["case_id"]] and group["outcome"] == outcome
        assert group["complete"] is complete and group["cache_eligible"] is False
        assert group["worker_seconds"] == frame["worker_seconds"]
    prior_inventory, later_inventory = observed_report["collection"]["suites"]
    assert prior_inventory["name"] == "a_prior" and prior_inventory["outcome"] == "executed"
    assert prior_inventory["inventory_digest"] is not None
    assert later_inventory["name"] == "z_later" and later_inventory["outcome"] == "failed"
    assert later_inventory["inventory_digest"] is None
    assert not cache_bytes(project.root) and not (project.root / ".pytest-telescope").exists()
