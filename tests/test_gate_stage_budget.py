"""Consume actual gate deadlines with finite virtual stages, without waiting or dispatch."""

import json
import sys
from pathlib import Path

import pytest
from test_functional_gate import gates as gates


@pytest.mark.parametrize("functional_seconds", [1000, 1201])
def test_functional_budget_completes_or_stops_before_dependent_harness(
    gates, tmp_path, monkeypatch, functional_seconds
):
    scratch, output = tmp_path / "scratch", tmp_path / "artifacts"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "interpreter-double"))
    monkeypatch.setattr(sys, "argv", ["check.py", "--artifacts", str(output)])
    source = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(source))
    attempted, completed, clock = [], [], [0]

    class VirtualStages:
        def __init__(self, directory, root):
            assert directory == output

        def run(self, label, *arguments, timeout=300, **kwargs):
            attempted.append(label)
            duration = functional_seconds if label == "functional" else 1
            clock[0] += min(duration, timeout)
            if duration > timeout:
                raise TimeoutError("virtual functional deadline")
            if label == "build":
                directory = Path(arguments[arguments.index("--outdir") + 1])
                directory.mkdir()
                (directory / "candidate.whl").write_bytes(b"owned wheel double")
            completed.append(label)

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
        pytest.fail("virtual-stage units must not start a process")

    monkeypatch.setattr(gates.check, "Stages", VirtualStages)
    monkeypatch.setattr(gates.check, "candidate_binding", binding)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden)
    if functional_seconds <= 1200:
        gates.check.main()
        assert completed[-2:] == ["functional", "harness"]
        assert clock[0] == functional_seconds + 7
    else:
        with pytest.raises(TimeoutError, match="virtual functional deadline"):
            gates.check.main()
        assert attempted[-1] == "functional"
        assert completed[-1] == "install"
        assert "harness" not in attempted
        assert clock[0] == 1206
    result = json.loads((output / "gate-result.json").read_text())
    assert result["status"] == ("passed" if functional_seconds <= 1200 else "failed")
    assert (result["error"] is None) == (functional_seconds <= 1200)
    assert result["functional_profile"] == "all" and result["verdict_kind"] == "full"
