"""Actual workflow commands meet real preflight; all execution stages are doubles."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from itertools import product
from pathlib import Path

import pytest
import yaml
from test_functional_gate import gates as gates
from test_functional_gate import write

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def workflow_roles(name, runner):
    """Expand real workflow commands/matrix, without executing setup or action steps."""
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    found = []
    for job in document["jobs"].values():
        matrix = job.get("strategy", {}).get("matrix", {})
        axes = {key: values for key, values in matrix.items() if key != "include"}
        rows = [dict(zip(axes, values)) for values in product(*axes.values())]
        for included in matrix.get("include", []):
            matched = [
                row
                for row in rows
                if all(row[key] == value for key, value in included.items() if key in axes)
            ]
            assert matched, "workflow fixture does not silently manufacture matrix jobs"
            for row in matched:
                row.update(included)
        for row in rows:
            installs = []
            python_version = None
            for step in job.get("steps", []):
                environment = {"RUNNER_TEMP": str(runner)}
                for source in (document, job, step):
                    environment.update(source.get("env", {}))

                def expand(value):
                    value = re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", str(runner), value)
                    value = re.sub(
                        r"\$\{\{\s*matrix\.(\w+)\s*\}\}",
                        lambda match: row[match[1]],
                        value,
                    )
                    for key, content in environment.items():
                        value = value.replace("${" + key + "}", content)
                        value = value.replace("$" + key, content)
                    if "$" in value:
                        raise AssertionError("unsupported workflow expression in gate input")
                    return value

                if step.get("uses", "").startswith("actions/setup-python@"):
                    python_version = expand(step["with"]["python-version"])
                for line in step.get("run", "").splitlines():
                    tokens = shlex.split(line)
                    if len(tokens) > 3 and tokens[1:4] == ["-m", "pip", "install"]:
                        installs.append([expand(token) for token in tokens])
                    if len(tokens) <= 1 or tokens[1] != "scripts/check.py":
                        continue
                    found.append(
                        dict(
                            matrix=dict(row),
                            python=python_version,
                            installs=list(installs),
                            executable=tokens[0],
                            arguments=[expand(token) for token in tokens[1:]],
                            environment={key: expand(value) for key, value in environment.items()},
                        )
                    )
    return found


def invocation(name, runner):
    role = workflow_roles(name, runner)[0]
    return role["executable"], role["arguments"], role["environment"]


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


APT = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUBS/apt.log"
count=$(grep -c . "$STUBS/apt.log")
if [ "$count" -le "$APT_FAILURES" ]; then exit 100; fi
for word in "$@"; do
    if [ "$word" = install ] && [ "$APT_INSTALLS" = yes ]; then
        printf '#!/bin/sh\\n' > "$STUBS/bwrap" && chmod 755 "$STUBS/bwrap"
    fi
done
"""


def install_step(tmp_path, name, *, failures, installs):
    """Run a workflow's Bubblewrap installation step against doubles of its commands.

    `apt-get` fails its first `failures` calls and then succeeds; its `install` puts a
    `bwrap` on the path only when `installs` is true. `sleep` records its argument.
    """
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    steps = [
        step
        for job in document["jobs"].values()
        for step in job["steps"]
        if step.get("name") == "Install Bubblewrap"
    ]
    assert len(steps) == 1
    # Nothing lets the job continue after this step fails.
    assert set(steps[0]) == {"name", "run"}
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    doubles = {
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "apt-get": APT,
        "sleep": '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$STUBS/sleep.log"\n',
    }
    for command, text in doubles.items():
        (stubs / command).write_text(text)
        (stubs / command).chmod(0o755)
    for tool in ("grep", "chmod"):
        (stubs / tool).symlink_to(shutil.which(tool))
    # Hosted runners start a step's script with `bash -e`.
    completed = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", steps[0]["run"]],
        env={
            "PATH": str(stubs),
            "STUBS": str(stubs),
            "APT_FAILURES": str(failures),
            "APT_INSTALLS": "yes" if installs else "no",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    def lines(log):
        return (stubs / log).read_text().splitlines() if (stubs / log).exists() else []

    return completed, lines("apt.log"), lines("sleep.log")


UPDATE = "-o Acquire::Retries=3 update -q"
INSTALL = "-o Acquire::Retries=3 install -y -q bubblewrap"


@pytest.mark.parametrize("name", ["ci.yml", "publish.yml"])
def test_bubblewrap_installation_retries_a_bounded_number_of_times(tmp_path, name):
    completed, apt, sleeps = install_step(tmp_path, name, failures=100, installs=True)
    assert completed.returncode != 0
    assert apt == [UPDATE] * 3 and sleeps == ["30", "30"]
    assert "Bubblewrap could not be installed after 3 attempts" in completed.stdout


@pytest.mark.parametrize("name", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize(
    ("failures", "calls", "pauses"),
    [
        (0, [UPDATE, INSTALL], 0),
        (1, [UPDATE, UPDATE, INSTALL], 1),
        (2, [UPDATE] * 3 + [INSTALL], 2),
    ],
)
def test_bubblewrap_installation_succeeds_once_an_attempt_installs_it(
    tmp_path, name, failures, calls, pauses
):
    completed, apt, sleeps = install_step(tmp_path, name, failures=failures, installs=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert apt == calls and sleeps == ["30"] * pauses


@pytest.mark.parametrize("name", ["ci.yml", "publish.yml"])
def test_bubblewrap_installation_never_succeeds_without_bwrap(tmp_path, name):
    # Every package command succeeds, yet no bwrap is on the path afterwards.
    completed, apt, sleeps = install_step(tmp_path, name, failures=0, installs=False)
    assert completed.returncode != 0
    assert apt == [UPDATE, INSTALL] * 3 and sleeps == ["30", "30"]
    assert "Bubblewrap could not be installed after 3 attempts" in completed.stdout
