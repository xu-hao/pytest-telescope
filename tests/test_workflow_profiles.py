"""Expand actual workflow roles and exercise gate dispatch with explicit stage doubles."""

import os
import sys
from pathlib import Path

import pytest
from test_functional_gate import gates as gates
from test_workflow_gate_preflight import workflow_roles


@pytest.mark.parametrize(
    ("workflow", "python_version", "pytest_spec", "profile"),
    [
        ("ci.yml", "3.11", "pytest==8.3.5", "core"),
        ("ci.yml", "3.13", "pytest==8.3.5", "core"),
        ("ci.yml", "3.11", "pytest==9.2.0", "all"),
        ("ci.yml", "3.13", "pytest==9.2.0", "all"),
        ("publish.yml", "3.12", "pytest==9.2.0", "all"),
    ],
)
def test_workflow_roles_use_explicit_profile_and_pinned_adapter(
    gates, tmp_path, monkeypatch, workflow, python_version, pytest_spec, profile
):
    runner = tmp_path / "runner"
    runner.mkdir()
    roles = workflow_roles(workflow, runner)
    assert len(roles) == (4 if workflow == "ci.yml" else 1)
    (role,) = [
        role for role in roles
        if role["python"] == python_version
        and any(pytest_spec in command for command in role["installs"])
    ]
    # Setup is a named installer double: selected command arguments are consumed,
    # and no interpreter installation, pip or other process may run.
    installed = []
    for command in role["installs"]:
        installed.extend(argument for argument in command if argument.startswith("pytest=="))
    assert installed == [pytest_spec]
    monkeypatch.setattr(os, "environ", role["environment"])
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "environment-double"))
    monkeypatch.setattr(sys, "argv", role["arguments"])
    source = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(source))
    stages = []

    class StageDouble:
        def __init__(self, output, root):
            self.output = output

        def run(self, label, *arguments, **kwargs):
            stages.append((label, arguments))
            if label == "build":
                directory = Path(arguments[arguments.index("--outdir") + 1])
                directory.mkdir()
                (directory / "owned.whl").write_bytes(b"explicit wheel double")

    def binding(root, wheel):
        return {
            "schema": 2, "root": str(root), "source": dict(source),
            "wheel_path": str(wheel), "wheel": {"sha256": "c" * 64},
            "interpreter": {"unit_double": "not executed"},
        }

    def forbidden(*args, **kwargs):
        pytest.fail("workflow profile units must not launch a process")

    monkeypatch.setattr(gates.check, "Stages", StageDouble)
    monkeypatch.setattr(gates.check, "candidate_binding", binding)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden)
    gates.check.main()
    for label, arguments in stages:
        if label in {"functional", "harness"}:
            assert arguments[arguments.index("--functional-profile") + 1] == profile
    assert [name for name, _ in stages][-2:] == ["functional", "harness"]
