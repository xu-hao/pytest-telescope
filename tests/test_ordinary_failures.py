"""Partial ordinary results retain validated work and explicit remaining observations."""

import json
import os

import pytest
from test_ordinary_lifecycle import run_owned_lifecycle

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError, InfrastructureError


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def run_report(root, **kwargs):
    try:
        return controller.run(root, **kwargs)
    except (DeclarationError, InfrastructureError) as error:
        # Pre-report exceptions use the public refusal conversion. A later failure
        # must instead preserve prior validated rows in a returned kind=run report.
        return controller.error_result(error)


def suites(root, *, first_fails=False):
    config = ""
    for name in ("a", "b", "c"):
        config += f"""
[tool.pytest_telescope.suites.{name}]
root="tests/{name}"
cwd="."
config="pyproject.toml"
test_roots=["tests/{name}"]
"""
    write(root, "pyproject.toml", config)
    write(
        root,
        "tests/a/test_first.py",
        """
from pathlib import Path

def test_first():
    Path("a-ran").touch()
"""
        + ("    assert False\n" if first_fails else ""),
    )
    write(
        root,
        "tests/c/test_remaining.py",
        """
from pathlib import Path
Path("c-imported").touch()
def test_remaining():
    Path("c-ran").touch()
""",
    )


MUTATION = """
def pytest_collection_finish(session):
    # Real selection has completed; routing must reject this changed literal
    # before pytest dispatches any fixture or test body.
    session.items[0].own_markers[0].kwargs["cache"] = True
"""


DECLARED_TEST = """
from pathlib import Path
import pytest

@pytest.mark.telescope(cache=False)
def test_changed():
    Path("invalid-body-ran").touch()
"""


@pytest.mark.parametrize("failure", ["collection", "protocol"])
def test_later_suite_failure_keeps_prior_units_and_remaining_observations(tmp_path, failure):
    suites(tmp_path)
    if failure == "collection":
        source = 'raise RuntimeError("synthetic collection failure")\ndef test_bad(): pass\n'
    else:
        source = """
import os
from pathlib import Path

def test_bad():
    Path("b-ran").touch()
    os.write(1, b"TELESCOPE_RESULT {}\\n")
"""
    write(tmp_path, "tests/b/test_bad.py", source)
    report = run_report(tmp_path, arguments=("-s",) if failure == "protocol" else ())
    assert (tmp_path / "a-ran").exists()
    assert not (tmp_path / "c-imported").exists()
    assert report["kind"] == "run", json.dumps(report, indent=2)
    assert report["schema"] == 3
    assert report["status"] == ("failed" if failure == "collection" else "infrastructure_error")
    prior = [row for row in report["units"] if row["suite"] == "a"]
    assert len(prior) == 1 and prior[0]["outcome"] == "executed"
    assert prior[0]["route"] == "ordinary" and prior[0]["result_status"] == "passed"
    assert prior[0]["attempts"] == 1 and prior[0]["complete"] is True
    assert not any(row["suite"] in ("b", "c") for row in report["units"])
    assert report["inventory_complete"] is False and report["routing_complete"] is False
    observations = {(row["name"], row["route"]): row for row in report["collection"]["suites"]}
    assert set(observations) == {(name, "ordinary") for name in ("a", "b", "c")}
    assert observations["a", "ordinary"]["outcome"] == "executed"
    assert observations["b", "ordinary"]["outcome"] == (
        "failed" if failure == "collection" else "infrastructure_error"
    )
    remaining = observations["c", "ordinary"]
    assert remaining["files"] == ["tests/c/test_remaining.py"]
    assert remaining["outcome"] == "not-started" and remaining["complete"] is False
    assert remaining["collected"] is None and remaining["inventory_digest"] is None
    assert remaining["accounting_group_id"] is None
    assert remaining["worker_seconds"] is None and remaining["cpu_seconds"] is None
    assert not any(group["suite"] == "c" for group in report["groups"])
    assert report["counts"]["executed"] == 1 and report["counts"]["reused"] == 0


def test_post_selection_declaration_failure_has_no_dispatch_claim(tmp_path):
    write(tmp_path, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    write(tmp_path, "tests/conftest.py", MUTATION)
    write(tmp_path, "tests/test_changed.py", DECLARED_TEST)
    report = run_report(tmp_path)
    assert report["kind"] == "run", json.dumps(report, indent=2)
    assert report["status"] == "declaration_error"
    assert not (tmp_path / "invalid-body-ran").exists()
    assert len(report["units"]) == 1
    unit = report["units"][0]
    assert unit["selected"] is True and unit["outcome"] == "unsupported"
    assert unit["route"] is None and unit["group_id"] is None, json.dumps(report, indent=2)
    assert unit["attempts"] == 0 and unit["reason"] == "declaration-invalid"
    assert unit["result_status"] is None and unit["complete"] is False
    assert unit["passed"] == unit["failed"] == unit["skipped"] == 0
    assert report["route_counts"]["unresolved"]["selected"] == 1
    assert report["route_counts"]["unresolved"]["unsupported"] == 1
    assert report["routing_complete"] is False
    assert not any(unit["case_id"] in group["case_ids"] for group in report["groups"])


@pytest.mark.parametrize("later", ["declaration", "protocol"])
def test_terminal_failure_precedes_an_earlier_test_failure(tmp_path, later):
    suites(tmp_path, first_fails=True)
    if later == "declaration":
        write(tmp_path, "tests/b/conftest.py", MUTATION)
        write(tmp_path, "tests/b/test_bad.py", DECLARED_TEST)
    else:
        write(
            tmp_path,
            "tests/b/test_bad.py",
            """
import os

def test_bad():
    os.write(1, b"TELESCOPE_RESULT {}\\n")
""",
        )
    report = run_report(tmp_path, arguments=("-s",) if later == "protocol" else ())
    assert report["status"] == (
        "declaration_error" if later == "declaration" else "infrastructure_error"
    ), json.dumps(report, indent=2)
    assert report["kind"] == "run"
    prior = [row for row in report["units"] if row["suite"] == "a"]
    assert len(prior) == 1 and prior[0]["outcome"] == "failed"
    assert prior[0]["result_status"] == "failed" and prior[0]["attempts"] == 1
    assert not (tmp_path / "invalid-body-ran").exists()
    assert not (tmp_path / "c-imported").exists()


def test_live_descendant_cleanup_failure_precedes_declaration_failure(tmp_path):
    sample = run_owned_lifecycle(tmp_path, "declaration")
    assert sample["status"] == "infrastructure_error", json.dumps(sample, indent=2)
    assert sample["exit_code"] == 3
    assert sample["child_exited_at_return"] is True
    assert sample["child_reaped_at_return"] is True


@pytest.mark.parametrize(
    "configuration",
    [
        "distributions=42\n",
        "[tool.pytest_telescope.distributions]\nmalformed=42\n"
        "[tool.pytest_telescope.distributions.unused]\n"
        'format="pep621-static-v1"\nmetadata="unused-link/missing.toml"\n',
    ],
)
def test_ordinary_rejects_distribution_syntax_without_expanding_unused_paths(
    tmp_path, monkeypatch, configuration
):
    write(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots=["tests"]\n' + configuration,
    )
    (tmp_path / "unused-link").symlink_to("does-not-exist", target_is_directory=True)
    write(
        tmp_path,
        "tests/test_ordinary.py",
        """
from pathlib import Path

def test_ordinary():
    Path("invalid-config-body-ran").touch()
""",
    )
    def guard(operation):
        def checked(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                assert "unused-link" not in os.fsdecode(path).split("/"), (
                    "ordinary configuration validation inspected an unused distribution path"
                )
            return operation(path, *args, **kwargs)

        return checked

    with monkeypatch.context() as unused_paths:
        for name in ("open", "stat", "lstat", "scandir"):
            unused_paths.setattr(os, name, guard(getattr(os, name)))
        report = run_report(tmp_path)
    assert report["status"] == "declaration_error", json.dumps(report, indent=2)
    assert report["schema"] == 3 and report["kind"] == "refusal"
    assert report["units"] == [] and report["groups"] == []
    assert not (tmp_path / "invalid-config-body-ran").exists()
