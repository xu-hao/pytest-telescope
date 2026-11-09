"""Actual workflow commands meet real preflight; all execution stages are doubles."""

import os
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml
from test_functional_gate import gates as gates
from test_functional_gate import write

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def invocation(name, runner):
    """Read command and inherited environment, without running setup/action steps."""
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    found = []
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                tokens = shlex.split(line)
                if len(tokens) > 1 and tokens[1] == "scripts/check.py":
                    environment = {"RUNNER_TEMP": str(runner)}
                    for source in (document, job, step):
                        environment.update(source.get("env", {}))

                    def expand(value):
                        value = re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", str(runner), value)
                        for key, content in environment.items():
                            value = value.replace("${" + key + "}", content)
                            value = value.replace("$" + key, content)
                        if "$" in value:
                            raise AssertionError("unsupported workflow expression in gate input")
                        return value

                    found.append((tokens[0], [expand(token) for token in tokens[1:]],
                                  {key: expand(value) for key, value in environment.items()}))
    assert len(found) == 1, "one gate entry is required per workflow"
    return found[0]


class StageReached(Exception):
    pass


@pytest.fixture
def entry(tmp_path, monkeypatch, gates):
    runner = tmp_path / "runner-temp"
    runner.mkdir()
    prefix = tmp_path / "owned-venv"
    # Neither stand-in is executed. The selected executable is really hashed.
    interpreter = write(prefix / "bin/python", b"owned interpreter stand-in\n", 0o755)
    node = write(tmp_path / "node", b"owned selected Node stand-in\n", 0o755)
    calls, identities = [], []
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    identity = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(identity))
    actual_node_identity = gates.gate.node_identity

    def selected_node(value):
        # An explicit absolute workflow executable maps to the owned stand-in.
        # Missing/relative selections still reach the real rejecting validator.
        chosen = str(node) if value and Path(value).is_absolute() else value
        result = actual_node_identity(chosen)
        identities.append((value, result))
        return result

    class Stages:
        def __init__(self, output, root):
            calls.append(("construct", output, root))

        def run(self, name, *args, **kwargs):
            calls.append((name, args, kwargs))
            raise StageReached("owned first-stage dispatch; no stage command ran")

    def forbidden_process(*args, **kwargs):
        raise AssertionError("workflow preflight must not start a process or installer")

    monkeypatch.setattr(gates.check, "node_identity", selected_node)
    monkeypatch.setattr(gates.check, "Stages", Stages)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden_process)

    def run(name, *, repair=False, missing=None, selection="argv"):
        executable, arguments, environment = invocation(name, runner)
        assert Path(executable).name == "python"
        if repair:
            environment["TMPDIR"] = str(runner)
            environment.pop("TELESCOPE_FIXTURE_NODE", None)
            if "--node" in arguments:
                index = arguments.index("--node")
                del arguments[index:index + 2]
            if selection == "argv":
                arguments += ["--node", str(node)]
            else:
                environment["TELESCOPE_FIXTURE_NODE"] = str(node)
        if missing == "node":
            if "--node" in arguments:
                index = arguments.index("--node")
                del arguments[index:index + 2]
            environment.pop("TELESCOPE_FIXTURE_NODE", None)
        elif missing == "scratch":
            environment.pop("TMPDIR", None)
        # Model only the workflow's named virtual interpreter and explicit env.
        # In particular no developer TMPDIR or Node selection can rescue it.
        monkeypatch.setattr(os, "environ", dict(environment))
        monkeypatch.setattr(sys, "argv", arguments)
        gates.check.main()

    return run, calls, identities, runner


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_workflow_supplies_required_gate_prerequisites(entry, workflow):
    run, calls, identities, runner = entry
    with pytest.raises(StageReached):
        run(workflow)
    assert [call[0] for call in calls] == ["construct", "lint"]
    assert len(identities) == 1
    assert Path(calls[-1][2]["env"]["TMPDIR"]).is_relative_to(runner)
    assert not Path(calls[-1][2]["env"]["TMPDIR"]).exists()


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize("missing", ["node", "scratch"])
def test_missing_prerequisite_prevents_every_stage(entry, workflow, missing, capsys):
    run, calls, identities, _ = entry
    if missing == "node":
        with pytest.raises(ValueError, match="absolute Node"):
            run(workflow, repair=True, missing=missing)
        assert identities == []
    else:
        with pytest.raises(SystemExit) as error:
            run(workflow, repair=True, missing=missing)
        assert error.value.code == 2
        assert "caller-owned TMPDIR" in capsys.readouterr().err
        assert len(identities) == 1
    assert calls == []


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize("selection", ["argv", "environment"])
def test_explicit_owned_prerequisites_reach_only_stage_double(entry, workflow, selection):
    run, calls, identities, runner = entry
    with pytest.raises(StageReached):
        run(workflow, repair=True, selection=selection)
    assert [call[0] for call in calls] == ["construct", "lint"]
    assert identities[0][1]["file"]["size"] == len(b"owned selected Node stand-in\n")
    scratch = Path(calls[-1][2]["env"]["TMPDIR"])
    assert scratch.is_relative_to(runner) and not scratch.exists()
