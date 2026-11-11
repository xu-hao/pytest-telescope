"""Parallel functional workers reproduce the exact recorded membership before any body."""

import importlib.metadata
import json
import sys
import tomllib
import types
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from test_functional_gate import candidate as candidate
from test_functional_gate import gates as gates
from test_functional_gate import invocation as invocation
from test_gate_profiles import profile_session as profile_session
from test_workflow_gate_preflight import workflow_roles

import pytest_telescope

PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
ACTUAL = importlib.metadata.version("pytest")
VERIFIED = str(Path(pytest_telescope.__file__))
CORE = "tests/test_profile.py::test_core"
POSITIVE = "tests/test_profile.py::test_guard"
RECORD = {
    "schema": 1,
    "profile": "core",
    "pytest_version": ACTUAL,
    "selected": [CORE],
    "deselected": [POSITIVE],
}


def test_parallel_workers_record_the_serial_membership_and_run_selected_bodies(
    profile_session,
):
    value = profile_session
    assert value.run("core", ACTUAL, workers=2) == 0
    assert json.loads(value.membership.read_text()) == RECORD
    assert (value.root / "core-ran").exists()
    assert not (value.root / "guard-ran").exists()
    assert value.verified == [True, False]


def test_worker_importing_another_candidate_origin_refuses_before_any_body(profile_session):
    value = profile_session
    # Only parallel workers resolve the candidate to this owned stand-in package.
    stand_in = value.root / "stand-in"
    (stand_in / "pytest_telescope").mkdir(parents=True)
    (stand_in / "pytest_telescope/__init__.py").write_text("")
    (value.root / "tests/conftest.py").write_text(
        "import sys\nimport pytest\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_configure(config):\n"
        "    if hasattr(config, 'workerinput'):\n"
        "        for name in [name for name in sys.modules if name.split('.')[0] == 'pytest_telescope']:\n"
        "            del sys.modules[name]\n"
        f"        sys.path.insert(0, {str(stand_in)!r})\n"
    )
    assert value.run("core", ACTUAL, workers=2) != 0
    assert json.loads(value.membership.read_text()) == RECORD
    assert not (value.root / "core-ran").exists()
    assert not (value.root / "guard-ran").exists()


def test_worker_only_deselection_refuses_before_any_body(profile_session):
    value = profile_session
    # Unchanged in the recording collection; only parallel workers drop a selected case.
    (value.root / "tests/conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    if hasattr(config, 'workerinput'):\n"
        "        removed = [item for item in items if item.name == 'test_guard']\n"
        "        items[:] = [item for item in items if item not in removed]\n"
        "        config.hook.pytest_deselected(items=removed)\n"
    )
    assert value.run("core", ACTUAL, workers=2) != 0
    assert json.loads(value.membership.read_text()) == RECORD
    assert not (value.root / "core-ran").exists()
    assert not (value.root / "guard-ran").exists()


def test_worker_change_after_reported_collection_refuses_before_any_body(profile_session):
    value = profile_session
    # Runs after the worker reported its collection, so scheduling has already begun.
    (value.root / "tests/conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_collection_finish(session):\n"
        "    if hasattr(session.config, 'workerinput'):\n"
        "        session.items.extend(session.items)\n"
    )
    assert value.run("core", ACTUAL, workers=2) != 0
    assert json.loads(value.membership.read_text()) == RECORD
    assert not (value.root / "core-ran").exists()


def test_parallel_success_requires_every_worker_confirmation(profile_session, monkeypatch):
    value = profile_session
    actual = pytest.main
    calls = []

    def main(arguments, *, plugins):
        calls.append(arguments)
        if len(calls) == 1:
            return actual(arguments, plugins=plugins)
        # Explicit double: a parallel session reporting success without any worker.
        return 0

    monkeypatch.setattr(pytest, "main", main)
    with pytest.raises(ValueError, match="confirm"):
        value.run("core", ACTUAL, workers=2)
    assert "--collect-only" in calls[0] and "--tx" in calls[1]
    assert json.loads(value.membership.read_text()) == RECORD
    assert not (value.root / "core-ran").exists()


def test_parallel_workers_require_their_runner_before_collection(profile_session, monkeypatch):
    value = profile_session
    monkeypatch.setitem(sys.modules, "xdist", None)
    with pytest.raises(ValueError, match="pytest-xdist"):
        value.run("core", ACTUAL, workers=2)
    assert not value.membership.exists()
    assert not (value.root / "core-ran").exists()


@pytest.mark.parametrize(
    ("mode", "count"), [("tests", "0"), ("tests", "-1"), ("tests", "two"), ("harness", "2")]
)
def test_invalid_worker_counts_refuse_before_dispatch(invocation, monkeypatch, mode, count):
    value = invocation
    monkeypatch.setattr(sys, "argv", value.arguments(mode) + ["--workers", count])
    with pytest.raises(SystemExit) as caught:
        value.functional.main()
    assert caught.value.code == 2
    assert value.calls == []


def check_dispatch(gates, tmp_path, monkeypatch, arguments, environment=None):
    """Run check.py with named stage doubles; return each stage's arguments and timeout."""
    output = Path(arguments[arguments.index("--artifacts") + 1])
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "environment-double"))
    if environment is not None:
        monkeypatch.setattr("os.environ", dict(environment))
    else:
        monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(sys, "argv", arguments)
    source = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(source))
    stages = {}

    class StageDouble:
        def __init__(self, directory, root):
            assert directory == output.resolve()

        def run(self, label, *argv, timeout=300, **kwargs):
            stages[label] = (argv, timeout)
            if label == "build":
                destination = Path(argv[argv.index("--outdir") + 1])
                destination.mkdir()
                (destination / "candidate.whl").write_bytes(b"owned wheel double")

    def binding(root, wheel):
        return {
            "schema": 2,
            "root": str(root),
            "source": dict(source),
            "wheel_path": str(wheel),
            "wheel": {"sha256": "c" * 64},
            "interpreter": {"unit_double": "not executed"},
        }

    def forbidden(*args, **kwargs):
        pytest.fail("dispatch unit controls must not build, install or launch a process")

    monkeypatch.setattr(gates.check, "Stages", StageDouble)
    monkeypatch.setattr(gates.check, "candidate_binding", binding)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden)
    gates.check.main()
    return stages


@pytest.mark.parametrize(("requested", "expected"), [(None, "1"), ("1", "1"), ("4", "4")])
def test_check_forwards_one_explicit_worker_count_to_functional_tests_only(
    gates, tmp_path, monkeypatch, requested, expected
):
    arguments = ["check.py", "--artifacts", str(tmp_path / "artifacts")]
    if requested is not None:
        arguments += ["--functional-workers", requested]
    stages = check_dispatch(gates, tmp_path, monkeypatch, arguments)
    functional, timeout = stages["functional"]
    assert functional[functional.index("--workers") + 1] == expected
    assert functional.count("--workers") == 1 and timeout == 1200
    assert "--workers" not in stages["harness"][0]


@pytest.mark.parametrize("count", ["0", "-2"])
def test_check_refuses_a_nonpositive_worker_count_before_any_stage(
    gates, tmp_path, monkeypatch, count
):
    arguments = ["check.py", "--artifacts", str(tmp_path / "artifacts")]
    with pytest.raises(SystemExit) as caught:
        check_dispatch(gates, tmp_path, monkeypatch, arguments + ["--functional-workers", count])
    assert caught.value.code == 2
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_hosted_workflows_request_one_functional_worker_per_runner_cpu(
    gates, tmp_path, monkeypatch, workflow
):
    runner = tmp_path / "runner"
    runner.mkdir()
    roles = workflow_roles(workflow, runner)
    assert roles
    for number, role in enumerate(roles):
        arguments = list(role["arguments"])
        arguments[arguments.index("--artifacts") + 1] = str(tmp_path / f"artifacts-{number}")
        stages = check_dispatch(gates, tmp_path, monkeypatch, arguments, role["environment"])
        functional, _ = stages["functional"]
        assert functional[functional.index("--workers") + 1] == "4"


def test_controller_refuses_a_worker_collection_that_differs_before_scheduling(gates):
    controller = gates.functional.parallel_plugin(pytest, dict(RECORD), VERIFIED)
    node = types.SimpleNamespace(gateway=types.SimpleNamespace(id="gw0"), workerinput={})
    controller.pytest_configure_node(node)
    assert node.workerinput[gates.functional.RECEIPT] == RECORD
    assert node.workerinput[gates.functional.ORIGIN] == VERIFIED
    for ids in ([], [CORE, CORE], [POSITIVE], [CORE, POSITIVE]):
        with pytest.raises(pytest.UsageError, match="differs"):
            controller.pytest_xdist_node_collection_finished(node, ids)
    assert controller.collected == set()
    controller.pytest_xdist_node_collection_finished(node, [CORE])
    assert controller.collected == {"gw0"}


@pytest.mark.parametrize("receipt", [None, {"selected": [CORE]}, [CORE]])
def test_worker_without_an_exact_receipt_refuses_at_configuration(gates, receipt):
    workerinput = {} if receipt is None else {gates.functional.RECEIPT: receipt}
    registered = []
    config = types.SimpleNamespace(
        workerinput=workerinput,
        pluginmanager=types.SimpleNamespace(register=lambda *args: registered.append(args)),
    )
    with pytest.raises(pytest.UsageError, match="receipt"):
        gates.functional.pytest_configure(config)
    assert registered == []
    # Outside a parallel worker the same module is inert.
    gates.functional.pytest_configure(types.SimpleNamespace())


def worker_configuration(gates, origin):
    workerinput = {gates.functional.RECEIPT: dict(RECORD)}
    if origin is not None:
        workerinput[gates.functional.ORIGIN] = origin
    registered = []
    config = types.SimpleNamespace(
        workerinput=workerinput,
        pluginmanager=types.SimpleNamespace(register=lambda *args: registered.append(args)),
    )
    return config, registered


@pytest.mark.parametrize("origin", [None, "elsewhere", ["list"], "parent"])
def test_worker_refuses_at_configuration_unless_it_imports_the_verified_origin(
    gates, tmp_path, origin
):
    if origin == "elsewhere":
        origin = str(tmp_path / "pytest_telescope/__init__.py")
    elif origin == "parent":
        origin = str(Path(VERIFIED).parent)
    elif origin is not None:
        origin = [VERIFIED]
    config, registered = worker_configuration(gates, origin)
    with pytest.raises(pytest.UsageError, match="candidate origin"):
        gates.functional.pytest_configure(config)
    assert registered == []


def test_worker_importing_the_verified_origin_registers_its_membership_check(gates):
    config, registered = worker_configuration(gates, VERIFIED)
    gates.functional.pytest_configure(config)
    assert [name for _, name in registered] == ["telescope-functional-selection"]


@pytest.mark.parametrize(
    ("name", "pinned", "others"),
    [
        ("pytest-xdist", "3.8.0", ("3.7.0", "3.8.1", "3.9.0", "4.0.0")),
        ("execnet", "2.1.2", ("2.1.1", "2.1.3", "2.2.0", "3.0.0")),
    ],
)
def test_development_extra_admits_exactly_one_parallel_runner_version(name, pinned, others):
    declared = tomllib.loads(PROJECT.read_text())["project"]["optional-dependencies"]["dev"]
    (requirement,) = [
        requirement
        for requirement in map(Requirement, declared)
        if canonicalize_name(requirement.name) == name
    ]
    assert requirement.marker is None
    assert requirement.specifier.contains(pinned)
    assert not [version for version in others if requirement.specifier.contains(version)]
