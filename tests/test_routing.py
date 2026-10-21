"""Ordinary routing, sandbox prerequisites and removed settings; no sandbox launches."""

import json
import os
import stat

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError, UnsupportedCapability, load
from pytest_telescope.plan import make_plan

NAMESPACE_DENIAL = "bwrap: setting up uid map: Permission denied"


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def stand_in_bwrap(directory, message):
    """A stand-in for a host where bwrap exists but cannot create its namespaces."""
    directory.mkdir()
    script = directory / "bwrap"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "invoked" >> "{directory / "invocations"}"\n'
        f'echo "{message}" >&2\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return directory


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(root, "notes.txt", "undeclared project note\n")
    return root


def ordinary_tests(root):
    put(
        root,
        "tests/test_ordinary.py",
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def test_reads_undeclared_and_writes_project():\n"
        "    assert Path('notes.txt').read_text() == 'undeclared project note\\n'\n"
        "    with open('ran.log', 'a') as stream:\n"
        "        stream.write('ran\\n')\n"
        "\n"
        "def test_inherits_the_whole_environment():\n"
        "    assert os.environ['TELESCOPE_ROUTING_PROBE'] == 'inherited'\n"
        "\n"
        "@pytest.mark.telescope(cache=False)\n"
        "def test_policy_only_marker_is_ordinary():\n"
        "    assert Path('notes.txt').exists()\n"
        "\n"
        "@pytest.mark.telescope(absent=['missing.json'])\n"
        "def test_absence_only_marker_is_ordinary():\n"
        "    assert Path('notes.txt').exists()\n",
    )


def test_unmarked_cases_run_as_fresh_ordinary_pytest_without_bubblewrap(
    project, tmp_path, monkeypatch
):
    ordinary_tests(project)
    put(
        project,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.defaults]\ninputs = ["notes.txt"]\n',
    )
    stand_in = stand_in_bwrap(tmp_path / "bin", NAMESPACE_DENIAL)
    monkeypatch.setenv("PATH", str(stand_in) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TELESCOPE_ROUTING_PROBE", "inherited")
    first = controller.run(project, explain=True)
    second = controller.run(project, explain=True)
    for report in (first, second):
        assert report["schema"] == 3 and report["kind"] == "run", report
        assert report["status"] == "passed", report
        assert report["sandbox"]["required"] is False
        assert report["sandbox"]["identity"] is None
        assert report["counts"]["executed"] == 4 and report["counts"]["reused"] == 0
        assert report["route_counts"]["ordinary"]["executed"] == 4
        assert report["route_counts"]["scoped"]["selected"] == 0
        assert {row["route"] for row in report["units"]} == {"ordinary"}
        assert [group["profile"] for group in report["groups"]] == ["ordinary-v1"]
        assert report["groups"][0]["cache_eligible"] is False
        assert report["inventory_complete"] is True and report["grouping_complete"] is True
    assert (project / "ran.log").read_text() == "ran\nran\n"
    assert not (stand_in / "invocations").exists()
    assert not (project / ".pytest-telescope").exists()


def test_ordinary_cases_share_one_session_per_suite(project):
    put(
        project,
        "tests/conftest.py",
        "import pytest\n"
        "@pytest.fixture(scope='session')\n"
        "def session_token():\n"
        "    with open('session-setups.log', 'a') as stream:\n"
        "        stream.write('setup\\n')\n"
        "    return object()\n",
    )
    for name in ("first", "second"):
        put(
            project,
            f"tests/test_{name}.py",
            f"def test_{name}(session_token):\n    assert session_token is not None\n",
        )
    report = controller.run(project)
    assert report["status"] == "passed", report
    assert report["counts"]["executed"] == 2
    assert len(report["groups"]) == 1
    assert report["groups"][0]["files"] == ["tests/test_first.py", "tests/test_second.py"]
    assert (project / "session-setups.log").read_text() == "setup\n"


def test_ordinary_failures_selection_and_exit_status(project, capsys, monkeypatch):
    put(
        project,
        "tests/test_mixed_outcomes.py",
        "def test_passes():\n    pass\n\ndef test_fails():\n    assert False\n",
    )
    report = controller.run(project, arguments=["-k", "fails"])
    assert report["status"] == "failed"
    assert report["counts"]["failed"] == 1 and report["counts"]["not-selected"] == 1
    failed = next(row for row in report["units"] if row["outcome"] == "failed")
    assert failed["failures"][0]["exception_type"] == "AssertionError"
    monkeypatch.chdir(project)
    assert controller.main(["run", "--", "-k", "passes"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["executed"] == 1


@pytest.mark.parametrize("missing", [True, False])
def test_scoped_cases_refuse_before_any_test_runs_without_usable_bubblewrap(
    project, tmp_path, monkeypatch, capsys, missing
):
    ordinary_tests(project)
    put(
        project,
        "tests/test_scoped.py",
        "import pytest\n@pytest.mark.telescope(inputs=[])\ndef test_scoped():\n    pass\n",
    )
    monkeypatch.setenv("TELESCOPE_ROUTING_PROBE", "inherited")
    if missing:
        empty = tmp_path / "no-bwrap"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        capability = "sandbox:bubblewrap"
    else:
        stand_in = stand_in_bwrap(tmp_path / "bin", NAMESPACE_DENIAL)
        monkeypatch.setenv("PATH", str(stand_in) + os.pathsep + os.environ["PATH"])
        capability = "sandbox:namespaces"
    with pytest.raises(UnsupportedCapability) as refused:
        controller.run(project)
    assert refused.value.capability == capability
    if not missing:
        assert NAMESPACE_DENIAL in str(refused.value)
    assert not (project / "ran.log").exists()
    monkeypatch.chdir(project)
    assert controller.main(["run"]) == 4
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == 3 and output["kind"] == "refusal"
    assert output["status"] == "unsupported" and output["capability"] == capability
    assert not (project / "ran.log").exists()


def test_empty_and_inherited_declarations_are_scoped_in_the_plan(project):
    ordinary_tests(project)
    put(
        project,
        "tests/test_module_scope.py",
        "import pytest\n"
        "pytestmark = pytest.mark.telescope(bundles=[])\n"
        "def test_inherits_module_scope():\n"
        "    pass\n",
    )
    put(
        project,
        "tests/test_function_scope.py",
        "import pytest\n"
        "def test_plain():\n"
        "    pass\n"
        "@pytest.mark.telescope(inputs=[], cache=False)\n"
        "def test_empty_scope():\n"
        "    pass\n",
    )
    plan = make_plan(project)
    assert plan["sandbox"]["required"] is True
    suite = plan["suites"][0]
    assert suite["scoped_files"] == ["tests/test_module_scope.py"]
    assert suite["ordinary_files"] == ["tests/test_function_scope.py", "tests/test_ordinary.py"]
    routes = {(row["file"], row["line"]): row["route"] for row in plan["declarations"]}
    assert routes[("tests/test_module_scope.py", 2)] == "scoped"
    assert routes[("tests/test_function_scope.py", 4)] == "scoped"
    assert routes[("tests/test_ordinary.py", 13)] == "ordinary"
    assert routes[("tests/test_ordinary.py", 17)] == "ordinary"
    assert "notes.txt" not in plan["inputs"]


@pytest.mark.parametrize("workers", [0, 33, True, "2"])
def test_workers_is_a_bounded_count(project, workers):
    ordinary_tests(project)
    with pytest.raises(DeclarationError):
        controller.run(project, workers=workers)


def test_configured_workers_appear_in_plan_and_report(project):
    put(
        project,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 3\n',
    )
    put(project, "tests/test_plain.py", "def test_plain():\n    pass\n")
    assert make_plan(project)["execution"] == {"mode": "batch", "profile": "batch-v1", "workers": 3}
    report = controller.run(project, workers=2)
    assert report["execution"] == {"mode": "batch", "profile": "batch-v1", "workers": 2}


@pytest.mark.parametrize(
    "configuration,expected",
    [
        ("[tool.pytest_telescope.execution.budget]\ncpus = 2\n", "execution.workers"),
        ("[tool.pytest_telescope.execution.worker]\ncpus = 1\n", "execution.workers"),
        ('[tool.pytest_telescope.runtime]\nprofile = "python-v1"\n', "invoking interpreter"),
        ('[tool.pytest_telescope.python]\nprofile = "default"\n', "PYTHONPATH"),
    ],
)
def test_removed_settings_refuse_with_their_replacement(project, configuration, expected):
    put(
        project,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n' + configuration,
    )
    put(project, "tests/test_plain.py", "def test_plain():\n    pass\n")
    with pytest.raises(DeclarationError, match=expected):
        load(project)


@pytest.mark.parametrize("capability", ["network:none", "scratch:noexec"])
def test_isolation_the_sandbox_does_not_provide_is_refused(project, capability):
    put(
        project,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.core]\n"
        'config = "pyproject.toml"\n'
        'test_roots = ["tests"]\n'
        f'capabilities = ["{capability}"]\n',
    )
    put(project, "tests/test_plain.py", "def test_plain():\n    pass\n")
    with pytest.raises(UnsupportedCapability) as refused:
        load(project)
    assert refused.value.capability == capability


def test_removed_cli_command_and_flags_are_rejected(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    for arguments in (
        ["runtime"],
        ["run", "--budget-cpus", "2"],
        ["plan", "--runtime-profile", "x"],
    ):
        assert controller.main(arguments) == 4
        assert json.loads(capsys.readouterr().out)["status"] == "declaration_error"
