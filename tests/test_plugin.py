"""Behavior of the built wheel in fresh pytest subprocesses."""

import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
    return tmp_path


def invoke(project, *args, extra_env=None):
    env = {"PATH": os.defpath, "HOME": str(project), "TMPDIR": str(project)}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-q", *args],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def forbid_imports(project):
    for filename in ("conftest.py", "test_subject.py"):
        (project / filename).write_text(
            "from pathlib import Path\n"
            f"Path({str(project / (filename + '.imported'))!r}).touch()\n"
            "raise AssertionError('unexpected import')\n"
        )


def assert_not_imported(project):
    assert not list(project.glob("*.imported"))


@pytest.mark.parametrize("disabled", [False, True])
def test_installed_entry_point_and_normal_fixture_lifecycle(project, disabled):
    (project / "test_subject.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        "from importlib.metadata import distribution\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    yield 7\n"
        "    Path('torn-down').touch()\n"
        "def test_normal(pytestconfig, resource):\n"
        "    assert resource == 7\n"
        f"    assert pytestconfig.pluginmanager.hasplugin('telescope') is {not disabled}\n"
        "    dist = distribution('pytest-telescope')\n"
        "    assert any(e.group == 'pytest11' and e.name == 'telescope' "
        "for e in dist.entry_points)\n"
    )
    args = ("-p", "no:telescope") if disabled else ()
    result = invoke(project, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert (project / "torn-down").is_file()


def test_inactive_plugin_preserves_test_failure(project):
    (project / "test_subject.py").write_text("def test_failure():\n    assert 1 == 2\n")
    result = invoke(project)
    assert result.returncode == 1
    assert "1 failed" in result.stdout


@pytest.mark.parametrize("source", ["argument", "config", "environment"])
def test_execution_request_refuses_before_imports(project, source):
    forbid_imports(project)
    args = ("--telescope",) if source == "argument" else ()
    env = {"PYTEST_ADDOPTS": "--telescope"} if source == "environment" else {}
    if source == "config":
        (project / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\naddopts = "--telescope"\n'
        )
    result = invoke(project, *args, extra_env=env)
    assert result.returncode == (3 if source == "argument" else 4)
    assert "error" in (result.stdout + result.stderr).lower()
    assert_not_imported(project)


def manifest(project):
    (project / "data.json").write_text('{"answer": 42}')
    (project / "helper.py").write_text("VALUE = 42\n")
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        '[tool.pytest_telescope.bundles.base]\ninputs = ["helper.py"]\n'
        '[tool.pytest_telescope.bundles.common]\nbundles = ["base"]\n'
        'inputs = ["conftest.py", "helper.py"]\n'
    )
    source = project / "test_subject.py"
    source.write_text(
        "import pytest\n"
        'pytestmark = pytest.mark.telescope(inputs=["data.json"], bundles=["common"])\n'
        + source.read_text()
    )


def test_plan_is_sorted_complete_and_does_not_import(project):
    forbid_imports(project)
    manifest(project)
    (project / "test_another.py").write_text((project / "test_subject.py").read_text())
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "mode": "plan-only",
        "runtime": {"profile": "python-v1", "required_capabilities": [], "identity": None},
        "execution": {"mode": "case", "profile": "case-v1"},
        "case_inventory": "requires isolated pytest collection",
        "inventory": ["test_another.py", "test_subject.py"],
        "discovery_scope": "declared-union",
        "distributions": [],
        "suites": [
            {
                "name": "default",
                "root": ".",
                "cwd": ".",
                "config": "pyproject.toml",
                "test_roots": ["."],
                "include": ["**"],
                "exclude": [],
                "args": [],
                "config_args": [],
                "plugins": [],
                "inventory": ["test_another.py", "test_subject.py"],
            }
        ],
        "inputs": [
            "conftest.py",
            "data.json",
            "helper.py",
            "pyproject.toml",
            "test_another.py",
            "test_subject.py",
        ],
        "declarations": [
            {
                "file": name,
                "line": 2,
                "scope": [],
                "inputs": ["conftest.py", "data.json", "helper.py"],
                "absent": [],
                "cache": True,
            }
            for name in ["test_another.py", "test_subject.py"]
        ],
    }
    repeated = invoke(project, "--telescope-plan")
    assert repeated.returncode == 0
    assert repeated.stdout == result.stdout
    assert_not_imported(project)


def test_unmarked_plan_includes_broad_project_without_imports(project):
    forbid_imports(project)
    (project / ".hidden.json").write_text("{}")
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["inventory"] == ["test_subject.py"]
    assert plan["discovery_scope"] == "broad-potential"
    assert plan["inputs"] == [".hidden.json", "conftest.py", "pyproject.toml", "test_subject.py"]
    assert plan["declarations"] == []
    assert_not_imported(project)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[tool.pytest_telescope]\ntargets = {}", "migrate inputs"),
        ('[tool.pytest_telescope.defaults]\ninputs = "test_subject.py"', "nonempty strings"),
        ("[tool.pytest_telescope.defaults]\ncache = 1", "cache must be a boolean"),
        ("[tool.pytest_telescope.defaults]\nunknown = 1", "unsupported fields"),
        (
            '[tool.pytest_telescope.defaults]\nbundles = ["missing"]',
            "unknown dependency bundle",
        ),
        (
            '[tool.pytest_telescope.bundles.a]\nbundles = ["b"]\n'
            '[tool.pytest_telescope.bundles.b]\nbundles = ["a"]\n',
            "bundle cycle",
        ),
    ],
)
def test_invalid_declarations_refuse_before_imports(project, body, message):
    forbid_imports(project)
    (project / "pyproject.toml").write_text(body)
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 4
    assert message in result.stderr
    assert_not_imported(project)


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "../*.py", "missing", ".", "a\\b"])
def test_invalid_paths_refuse_without_importing(project, path):
    forbid_imports(project)
    (project / "pyproject.toml").write_text(
        f"[tool.pytest_telescope.defaults]\ninputs = [{json.dumps(path)}]\n"
    )
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 4
    assert_not_imported(project)


def test_escaping_symlink_refuses(project, tmp_path_factory):
    forbid_imports(project)
    outside = tmp_path_factory.mktemp("outside") / "data.json"
    outside.write_text("{}")
    (project / "data.json").symlink_to(outside)
    (project / "pyproject.toml").write_text(
        '[tool.pytest_telescope.defaults]\ninputs = ["data.json"]\n'
    )
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 4
    assert "escapes project root" in result.stderr
    assert_not_imported(project)


def test_execution_request_cannot_be_hidden_by_plan_flag(project):
    forbid_imports(project)
    manifest(project)
    result = invoke(project, "--telescope-plan", "--telescope")
    assert result.returncode == 4
    assert "error" in (result.stdout + result.stderr).lower()
    assert_not_imported(project)


@pytest.mark.parametrize("kind", ["nul", "large", "deep", "count", "entries", "path_length"])
def test_planner_limits_refuse_cleanly_before_imports(project, kind):
    forbid_imports(project)
    body = "[tool.pytest_telescope.defaults]\n"
    if kind == "nul":
        body += 'inputs = ["bad\\u0000name"]\n'
    elif kind == "large":
        body += "#" + "x" * 1_048_576
    elif kind == "deep":
        for index in range(34):
            body += f"[tool.pytest_telescope.bundles.b{index}]\n"
            if index < 33:
                body += f'bundles = ["b{index + 1}"]\n'
    elif kind == "count":
        for index in range(257):
            body += f"[tool.pytest_telescope.bundles.b{index}]\n"
    elif kind == "entries":
        body += "inputs = " + json.dumps(["test_subject.py"] * 4097)
    else:
        body += "inputs = " + json.dumps(["a" * 4097])
    (project / "pyproject.toml").write_text(body)
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 4
    assert "Traceback" not in result.stderr
    assert_not_imported(project)


def test_policy_marker_is_inert_during_ordinary_pytest(project):
    (project / "test_subject.py").write_text(
        "import pytest\n"
        '@pytest.mark.telescope(inputs=["missing.json"], cache=False)\n'
        "def test_normal():\n    assert True\n"
    )
    result = invoke(project, "--strict-markers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_fifo_declaration_refuses_cleanly_before_imports(project):
    forbid_imports(project)
    os.mkfifo(project / "pipe")
    (project / "pyproject.toml").write_text('[tool.pytest_telescope.defaults]\ninputs = ["pipe"]\n')
    result = invoke(project, "--telescope-plan")
    assert result.returncode == 4
    assert "regular files" in result.stderr
    assert "Traceback" not in result.stderr
    assert_not_imported(project)
