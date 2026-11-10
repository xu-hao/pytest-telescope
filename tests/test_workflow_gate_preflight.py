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

                    found.append(
                        (
                            tokens[0],
                            [expand(token) for token in tokens[1:]],
                            {key: expand(value) for key, value in environment.items()},
                        )
                    )
    assert len(found) == 1, "one gate entry is required per workflow"
    return found[0]


class StageReached(Exception):
    pass


@pytest.fixture
def entry(tmp_path, monkeypatch, gates):
    runner = tmp_path / "runner-temp"
    runner.mkdir()
    prefix = tmp_path / "owned-venv"
    interpreter = write(prefix / "bin/python", b"owned interpreter stand-in\n", 0o755)
    calls = []
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    identity = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(identity))

    class Stages:
        def __init__(self, output, root):
            calls.append(("construct", output, root))

        def run(self, name, *args, **kwargs):
            calls.append((name, args, kwargs))
            raise StageReached("owned first-stage dispatch; no stage command ran")

    def forbidden_process(*args, **kwargs):
        raise AssertionError("workflow preflight must not start a process or installer")

    monkeypatch.setattr(gates.check, "Stages", Stages)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden_process)

    def run(name, *, missing_scratch=False, old_profiles=False, old_node_option=False):
        executable, arguments, environment = invocation(name, runner)
        assert Path(executable).name == "python"
        if missing_scratch:
            environment.pop("TMPDIR", None)
        if old_profiles:
            environment.update(
                TELESCOPE_FIXTURE_NODE="/unselected/owned-node",
                TELESCOPE_FIXTURE_CHROMIUM="/unselected/owned-browser",
                TELESCOPE_FIXTURE_ADMISSION="/unselected/owned-receipt",
                TELESCOPE_FIXTURE_ADMISSION_SHA256="not-current-readiness",
            )
        if old_node_option:
            arguments += ["--node", "/unselected/owned-node"]
        # Only actual workflow settings may supply the runner prerequisite.
        monkeypatch.setattr(os, "environ", dict(environment))
        monkeypatch.setattr(sys, "argv", arguments)
        gates.check.main()

    return run, calls, runner


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_workflow_supplies_required_gate_prerequisites(entry, workflow):
    run, calls, runner = entry
    with pytest.raises(StageReached):
        run(workflow)
    assert [call[0] for call in calls] == ["construct", "lint"]
    scratch = Path(calls[-1][2]["env"]["TMPDIR"])
    assert scratch.is_relative_to(runner) and not scratch.exists()


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_missing_scratch_prevents_every_stage(entry, workflow, capsys):
    run, calls, _ = entry
    with pytest.raises(SystemExit) as error:
        run(workflow, missing_scratch=True)
    assert error.value.code == 2
    assert "caller-owned TMPDIR" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_stale_program_profile_environment_is_not_a_default_prerequisite(entry, workflow):
    run, calls, _ = entry
    with pytest.raises(StageReached):
        run(workflow, old_profiles=True)
    assert [call[0] for call in calls] == ["construct", "lint"]
    assert all(
        name not in calls[-1][2]["env"]
        for name in (
            "TELESCOPE_FIXTURE_NODE",
            "TELESCOPE_FIXTURE_CHROMIUM",
            "TELESCOPE_FIXTURE_ADMISSION",
            "TELESCOPE_FIXTURE_ADMISSION_SHA256",
        )
    )


@pytest.mark.parametrize("workflow", ["ci.yml", "publish.yml"])
def test_removed_node_option_refuses_before_every_stage(entry, workflow):
    run, calls, _ = entry
    with pytest.raises(SystemExit) as error:
        run(workflow, old_node_option=True)
    assert error.value.code == 2
    assert calls == []
