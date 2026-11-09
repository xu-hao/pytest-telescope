"""Required real-Bubblewrap controls for literal declarations in imported helpers."""

from pytest_telescope import controller


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_declared_marked_helper_keeps_consumer_scope(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    put(
        project,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    put(project, "helpers/__init__.py", "")
    put(
        project,
        "helpers/support.py",
        """
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])

def answer():
    return Path("data/answer.txt").read_text().strip()

def test_helper_must_stay_uncollected():
    raise AssertionError("the imported helper is outside the selected test roots")
""",
    )
    put(project, "data/answer.txt", "42\n")
    put(project, "helper-private.txt", "helper's own input\n")
    put(
        project,
        "tests/test_consumer.py",
        f"""
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(
    inputs=["helpers/__init__.py", "helpers/support.py", "data/answer.txt"],
    absent=["not-created.txt"],
)

from helpers.support import answer

def test_answer():
    assert answer() == "42"
    assert not Path("helper-private.txt").exists()
    assert not Path("not-created.txt").exists()
    assert not Path({str(project / "helper-private.txt")!r}).exists()
""",
    )

    report = controller.run(project, explain=True, workers=1)

    assert report["schema"] == 4 and report["kind"] == "run"
    assert report["status"] == "passed", report
    assert report["inventory_complete"] is True
    assert report["grouping_complete"] is True
    assert report["counts"] == {
        "selected": 1,
        "executed": 1,
        "reused": 0,
        "failed": 0,
        "unsupported": 0,
        "infrastructure_error": 0,
        "not-selected": 0,
    }
    assert report["cases"] == {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}
    assert report["route_counts"]["scoped"] == report["counts"]
    assert not any(report["route_counts"]["ordinary"].values())
    assert report["sandbox"]["required"] is True
    assert report["sandbox"]["identity"] is not None
    assert report["execution"] == {"mode": "case", "profile": "case-v2", "workers": 1}
    assert report["collection"]["outcome"] == "executed"
    assert report["collection"]["collected"] == 1
    collection_files = [(row["route"], row["files"]) for row in report["collection"]["suites"]]
    assert collection_files == [("scoped", ["tests/test_consumer.py"])]
    (unit,) = report["units"]
    assert unit["file"] == "tests/test_consumer.py"
    assert unit["route"] == "scoped"
    assert unit["selected"] is True
    assert unit["outcome"] == "executed"
    (group,) = report["groups"]
    assert group["case_ids"] == [unit["case_id"]]
    assert group["profile"] == "case-v2"
    assert group["attempts"] == 1
    assert group["cache_eligible"] is True
    assert not list((project / ".pytest-telescope/scratch").iterdir())
    assert (project / "helper-private.txt").read_text() == "helper's own input\n"
