"""Ordinary pytest stays fresh and needs no prepared scoped runtime."""

import json
import socket
import sys

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError
from pytest_telescope.plan import make_plan


def put(root, name, source):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


@pytest.fixture
def project(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("p")
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots=["tests"]\n')

    def no_scoped_work(*args, **kwargs):
        pytest.fail("ordinary/static routing contacted scoped execution or its cache")

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


def passed(root, **kwargs):
    report = controller.run(root, **kwargs)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    assert report["counts"]["reused"] == 0
    assert all(unit["route"] == "ordinary" for unit in report["units"])
    assert all(group["cache_eligible"] is False for group in report["groups"])
    return report


@pytest.mark.parametrize("marker", ["", "@pytest.mark.telescope(cache=False)"])
def test_ordinary_bodies_execute_on_every_run_without_preparation(project, marker):
    put(
        project,
        "tests/test_counter.py",
        f"""
from pathlib import Path
import pytest
{marker}
def test_counter():
    path = Path("body-count")
    count = int(path.read_text()) if path.exists() else 0
    path.write_text(str(count + 1))
""",
    )
    for expected in (1, 2):
        result = passed(project)
        assert result["counts"]["executed"] == 1
        assert (project / "body-count").read_text() == str(expected)
    assert not list((project / ".pytest-telescope/cache").rglob("*.json"))


def test_session_fixture_spans_ordinary_files_and_finishes_before_return(project):
    put(
        project,
        "tests/conftest.py",
        """
from pathlib import Path
import pytest
@pytest.fixture(scope="session")
def shared():
    with Path("lifetime").open("a") as stream:
        stream.write("setup\\n")
    seen = []
    yield seen
    assert seen == ["a", "b"]
    with Path("lifetime").open("a") as stream:
        stream.write("teardown\\n")
""",
    )
    for letter in ("a", "b"):
        put(
            project,
            f"tests/test_{letter}.py",
            f"""
def test_{letter}(shared):
    assert shared == {[] if letter == "a" else ["a"]!r}
    shared.append({letter!r})
""",
        )
    for count in (1, 2):
        result = passed(project)
        assert result["counts"]["executed"] == 2
        assert len(result["groups"]) == 1
        assert (project / "lifetime").read_text().splitlines() == ["setup", "teardown"] * count


def test_ordinary_uses_invoking_interpreter_environment_and_host_files(project, monkeypatch):
    monkeypatch.setenv("TELESCOPE_ORDINARY_EXAMPLE", "inherited")
    put(project, "host-only.txt", "ordinary host input")
    put(
        project,
        "tests/test_context.py",
        f"""
import os
import sys
from pathlib import Path

def test_context(pytestconfig):
    assert sys.executable == {sys.executable!r}
    assert Path.cwd() == Path({str(project)!r})
    assert pytestconfig.rootpath == Path({str(project)!r})
    assert os.environ["TELESCOPE_ORDINARY_EXAMPLE"] == "inherited"
    assert Path("host-only.txt").read_text() == "ordinary host input"
""",
    )
    passed(project)


def test_ordinary_ignores_unrelated_socket_symlink_and_ignored_tree(project):
    put(project, "tests/test_one.py", "def test_one(): pass\n")
    put(project, ".gitignore", "ignored/\n")
    put(project, "ignored/not_a_test.py", "this is not Python source\n")
    with (project / "ignored/oversized.data").open("wb") as stream:
        stream.truncate(65 * 1024 * 1024)
    (project / "unrelated-link").symlink_to("ignored", target_is_directory=True)
    with socket.socket(socket.AF_UNIX) as owned:
        owned.bind(str(project / "sock"))
        for _ in range(2):
            assert passed(project)["counts"]["executed"] == 1


@pytest.mark.parametrize("field", ["inputs", "bundles", "distributions"])
def test_defaults_do_not_opt_ordinary_cases_into_scoped_execution(project, field):
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(f"\n[tool.pytest_telescope.defaults]\n{field}=[]\n")
    put(project, "tests/test_one.py", "def test_one(): pass\n")
    assert passed(project)["counts"]["executed"] == 1


def test_nonempty_defaults_remain_ordinary_without_a_literal_scope(project):
    put(project, "default.txt", "default input")
    (project / "unused-default-link").symlink_to("tests", target_is_directory=True)
    with (project / "pyproject.toml").open("a") as stream:
        stream.write(
            '\n[tool.pytest_telescope.defaults]\ninputs=["default.txt", "unused-default-link"]\n'
        )
    put(project, "tests/test_one.py", "def test_one(): pass\n")
    passed(project)


def test_absence_only_checks_exact_named_path_without_broad_snapshot(project):
    put(
        project,
        "tests/test_absence.py",
        """
from pathlib import Path
import pytest
@pytest.mark.telescope(absent=["missing.txt"])
def test_absence():
    Path("body-ran").touch()
""",
    )
    (project / "unrelated-link").symlink_to("tests", target_is_directory=True)
    passed(project)
    (project / "body-ran").unlink()
    put(project, "missing.txt", "present now")
    try:
        report = controller.run(project)
    except DeclarationError as error:
        report = controller.error_result(error)
    assert report["status"] == "declaration_error", json.dumps(report, indent=2)
    assert not (project / "body-ran").exists()


@pytest.mark.parametrize("field", ["inputs", "bundles", "distributions"])
@pytest.mark.parametrize("level", ["module", "class", "function", "parameter"])
def test_literal_empty_scope_is_kept_in_static_declared_union(project, field, level):
    mark = f"pytest.mark.telescope({field}=[])"
    bodies = {
        "module": f"pytestmark = {mark}\ndef test_one(): pass\n",
        "class": f"class TestCases:\n    pytestmark = {mark}\n    def test_one(self): pass\n",
        "function": f"@{mark}\ndef test_one(): pass\n",
        "parameter": f'@pytest.mark.parametrize("value", [pytest.param(1, marks={mark})])\ndef test_one(value): pass\n',
    }
    put(
        project,
        "tests/test_scope.py",
        'import pytest\nraise RuntimeError("must not import during plan")\n' + bodies[level],
    )
    put(project, "unrelated.txt", "never a declared input")
    (project / "unrelated-link").symlink_to("tests", target_is_directory=True)
    plan = make_plan(project)
    assert plan["inventory"] == ["tests/test_scope.py"]
    assert plan["execution"] == {"mode": "case", "profile": "case-v1"}
    assert "unrelated.txt" not in plan["inputs"]
    assert "unrelated-link" not in plan["inputs"]
    assert len(plan["declarations"]) == 1


def test_mixed_static_plan_does_not_import_or_expand_ordinary_inputs(project):
    put(
        project,
        "tests/test_mixed.py",
        """
import pytest
raise RuntimeError("static plan must not import mixed files")
@pytest.mark.telescope(inputs=[], cache=False)
def test_scoped(): pass
@pytest.mark.telescope(cache=False)
def test_ordinary(): pass
""",
    )
    (project / "unrelated-link").symlink_to("tests", target_is_directory=True)
    plan = make_plan(project)
    assert plan["inventory"] == ["tests/test_mixed.py"]
    assert plan["inputs"] == ["pyproject.toml", "tests/test_mixed.py"]
    assert len(plan["declarations"]) == 2
