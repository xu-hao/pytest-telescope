"""Default gate orchestration with explicit stages and provenance unit doubles."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

import pytest
from test_functional_gate import gates as gates


def test_default_gate_needs_no_program_selection_or_profile_preparation(
    gates, tmp_path, monkeypatch
):
    scratch = tmp_path / "caller-scratch"
    scratch.mkdir()
    output = tmp_path / "candidate-artifacts"
    monkeypatch.setenv("TMPDIR", str(scratch))
    for name in (
        "TELESCOPE_FIXTURE_NODE",
        "TELESCOPE_FIXTURE_CHROMIUM",
        "TELESCOPE_FIXTURE_ADMISSION",
        "TELESCOPE_FIXTURE_ADMISSION_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "owned-venv-double"))
    monkeypatch.setattr(sys, "argv", ["check.py", "--artifacts", str(output)])
    identity = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(identity))
    stages, bindings = [], []

    class StageDouble:
        def __init__(self, directory, root):
            assert directory == output

        def run(self, name, *arguments, **kwargs):
            stages.append((name, arguments, kwargs))
            assert name != "prepare", "default gate dispatched a program-profile preparation"
            if name == "build":
                directory = Path(arguments[arguments.index("--outdir") + 1])
                directory.mkdir()
                (directory / "owned-candidate.whl").write_bytes(b"owned wheel double\n")

    def candidate_binding(root, wheel):
        """Explicit provenance double; no wheel build/install/qualification is claimed."""
        bindings.append((root, wheel))
        return {
            "schema": 2,
            "root": str(root),
            "source": dict(identity),
            "wheel_path": str(wheel),
            "wheel": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
            "interpreter": {"unit_double": "not-executed"},
        }

    def forbidden_process(*args, **kwargs):
        pytest.fail("the gate orchestration unit double must not start a process")

    monkeypatch.setattr(gates.check, "Stages", StageDouble)
    monkeypatch.setattr(gates.check, "candidate_binding", candidate_binding)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden_process)
    gates.check.main()
    assert [name for name, _, _ in stages] == [
        "lint",
        "format",
        "build",
        "metadata",
        "docs",
        "install",
        "functional",
        "harness",
    ]
    assert len(bindings) == 1
    for name, arguments, options in stages:
        assert Path(options["env"]["TMPDIR"]).is_relative_to(scratch)
        if name in {"functional", "harness"}:
            assert ("tests" if name == "functional" else "harness") in arguments
            assert "--binding" in arguments and "--binding-sha256" in arguments
            assert "--scratch" in arguments
            assert "--receipt" not in arguments and "--admission" not in arguments
    assert json.loads((output / "gate-result.json").read_text())["status"] == "passed"


def test_default_harness_discovery_excludes_explicit_compatibility_cases():
    suite = unittest.defaultTestLoader.discover(
        str(Path(__file__).resolve().parents[1] / "benchmarks"), pattern="test_*.py"
    )

    def cases(current):
        for item in current:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item

    modules = [type(case).__module__ for case in cases(suite)]
    assert "test_run" in modules
    assert all("frozen_runtime" not in name for name in modules)
    assert "test_programs" not in modules and "test_installed" not in modules
