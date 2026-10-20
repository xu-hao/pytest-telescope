"""Schema 3 reports actual ordinary sessions without scoped accounting claims."""

import json
import re

import pytest

from pytest_telescope import controller, runtime

COUNT_KEYS = {
    "executed",
    "reused",
    "failed",
    "unsupported",
    "infrastructure_error",
    "not-selected",
    "selected",
}


@pytest.fixture
def project(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("r")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots=["tests"]\n')

    def no_scoped_work(*args, **kwargs):
        pytest.fail("ordinary reporting contacted a scoped runtime or result cache")

    for name in (
        "build_runtime",
        "inspect_runtime",
        "identity",
        "_inspect",
        "execute",
        "recover",
        "command",
    ):
        monkeypatch.setattr(runtime, name, no_scoped_work)
    monkeypatch.setattr(controller, "CacheStore", no_scoped_work)
    return root


def assert_partition(report):
    assert report["schema"] == 3, json.dumps(report, indent=2)
    assert report["kind"] == "run"
    assert report["inventory_complete"] is True
    assert report["routing_complete"] is True
    assert report["grouping_complete"] is True
    assert re.fullmatch("[0-9a-f]{64}", report["inventory_digest"])
    assert set(report["counts"]) == COUNT_KEYS
    assert set(report["route_counts"]) == {"ordinary", "scoped", "unresolved"}
    for route, counts in report["route_counts"].items():
        assert set(counts) == COUNT_KEYS
        rows = [
            row
            for row in report["units"]
            if row["route"] == (None if route == "unresolved" else route)
        ]
        assert counts["selected"] == sum(row["selected"] for row in rows)
        for key in COUNT_KEYS - {"selected"}:
            assert counts[key] == sum(row["outcome"] == key for row in rows)
    for key in COUNT_KEYS:
        assert report["counts"][key] == sum(row[key] for row in report["route_counts"].values())
    assert report["counts"]["selected"] + report["counts"]["not-selected"] == len(report["units"])
    assert sum(report["counts"][key] for key in COUNT_KEYS - {"selected"}) == len(report["units"])
    assert all(row["route"] == "ordinary" for row in report["units"])
    assert report["counts"]["reused"] == 0
    for row in report["units"]:
        assert re.fullmatch("[0-9a-f]{64}", row["case_id"])
        assert row["name"] == row["case_id"]
        assert row["worker_seconds"] is None and row["cpu_seconds"] is None
        if row["selected"]:
            assert row["group_id"] in {group["group_id"] for group in report["groups"]}
        else:
            assert row["group_id"] is None
    for group in report["groups"]:
        assert group["route"] == "ordinary" and group["profile"] == "ordinary-v1"
        assert group["cache_eligible"] is False and group["attempts"] == 1
        assert group["files"] == sorted(set(group["files"]))
        assert "file" not in group
        assert set(group["case_ids"]) == {
            row["case_id"] for row in report["units"] if row["group_id"] == group["group_id"]
        }
        assert len(group["case_ids"]) == len(set(group["case_ids"]))
        assert group["cpu_seconds"] is None and group["timing_complete"] is False
        assert isinstance(group["worker_seconds"], (int, float)) and group["worker_seconds"] >= 0
    assert report["worker_cpu_seconds"] is None
    assert report["known_scoped_cpu_seconds"] == 0


def assert_no_scoped_claim(report):
    assert report["backend"] == {"name": "bubblewrap", "profile": "bubblewrap-v1", "identity": None}
    assert report["runtime"] == {
        "profile": "python-v2",
        "required_capabilities": [],
        "identity": None,
    }
    scoped = report["resources"]["scoped"]
    assert scoped["required"] is False and scoped["validated"] is False
    assert scoped["concurrency"] is None and scoped["enforced"] is None
    assert report["resources"]["controller"]["enforced"] is None
    assert report["resources"]["ordinary"] == {
        "enforcement": "inherited-host",
        "slot_limit": 1,
        "peak_sessions": 1,
    }
    assert set(report["execution"]) == {"mode", "profile"}


@pytest.mark.parametrize("mode", [None, "batch"])
def test_ordinary_group_spans_files_while_execution_keeps_scoped_policy(project, mode):
    for letter in ("a", "b"):
        (project / f"tests/test_{letter}.py").write_text(f"def test_{letter}(): pass\n")
    kwargs = {} if mode is None else {"execution_mode": mode}
    report = controller.run(project, **kwargs)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    assert_partition(report)
    assert_no_scoped_claim(report)
    selected_mode = mode or "case"
    assert report["execution"] == {"mode": selected_mode, "profile": selected_mode + "-v1"}
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["files"] == ["tests/test_a.py", "tests/test_b.py"]
    cases_by_file = {row["file"]: row["case_id"] for row in report["units"]}
    assert group["case_ids"] == [cases_by_file[name] for name in group["files"]]
    assert report["suites"] == [{"name": "default", "files": group["files"]}]
    assert report["counts"]["executed"] == report["counts"]["selected"] == 2
    collection = report["collection"]
    assert collection["outcome"] == "executed" and collection["collected"] == 2
    assert collection["worker_seconds"] is None and collection["cpu_seconds"] is None
    assert len(collection["suites"]) == 1
    observed = collection["suites"][0]
    assert observed["name"] == "default" and observed["route"] == "ordinary"
    assert observed["files"] == group["files"] and observed["collected"] == 2
    assert observed["accounting_group_id"] == group["group_id"]
    assert observed["worker_seconds"] is None and observed["cpu_seconds"] is None
    assert "error" not in report and "capability" not in report


@pytest.mark.parametrize("selector", [None, "keep", "matches_nothing"])
def test_ordinary_route_counts_conserve_failed_skipped_and_unselected_cases(project, selector):
    (project / "tests/test_outcomes.py").write_text("""
import pytest

def test_keep_pass(): pass
@pytest.mark.skip(reason="synthetic skip")
def test_keep_skip(): pass
def test_fail(): assert False
""")
    arguments = () if selector is None else ("-k", selector)
    report = controller.run(project, arguments=arguments)
    expected = "failed" if selector is None else "passed" if selector == "keep" else "no_tests"
    assert report["status"] == expected, json.dumps(report, indent=2)
    assert_partition(report)
    assert_no_scoped_claim(report)
    assert report["collection"]["collected"] == 3
    assert len(report["groups"]) == 1
    if selector is None:
        assert report["counts"]["executed"] == 2 and report["counts"]["failed"] == 1
        assert report["case_outcomes"]["fresh_passed"] == 1
        assert report["case_outcomes"]["fresh_skipped"] == 1
    elif selector == "keep":
        assert report["counts"]["executed"] == 2 and report["counts"]["not-selected"] == 1
    else:
        assert report["counts"]["selected"] == 0 and report["counts"]["not-selected"] == 3
        assert report["groups"][0]["case_ids"] == []


def test_ordinary_does_not_require_worker_budget_to_fit_scoped_worker(project):
    (project / "tests/test_one.py").write_text("def test_one(): pass\n")
    budget = {"cpus": 1, "memory_bytes": 4096, "processes": 1, "scratch_bytes": 4096}
    report = controller.run(project, budget=budget)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    assert_partition(report)
    assert_no_scoped_claim(report)
    assert report["resources"]["scoped"]["configured"]["budget"] == budget
