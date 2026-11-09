"""Profile-selection controls use constructor doubles and never launch programs."""

import json
import sys
import types

import pytest
from test_functional_gate import gates as shared_gates
from test_functional_gate import write

gates = shared_gates


@pytest.fixture
def selection(tmp_path, monkeypatch):
    import frozen_tool_prepare_programs as preparation

    calls = []
    def worker(directory, patch):
        calls.append(("worker", directory.name))
        return types.SimpleNamespace(document={"owned": "worker"})
    def program(worker, *, browser, require_receipt):
        calls.append(("program", browser, require_receipt))
        return types.SimpleNamespace(identity="program", document={"owned": "frontend"})
    def installed(directory, patch, *, require_receipt):
        calls.append(("installed", require_receipt))
        return types.SimpleNamespace(identity="installed", admission_documents={"owned": "installed"})
    monkeypatch.setattr(preparation, "WorkerConfiguration", worker)
    monkeypatch.setattr(preparation, "ProgramConfiguration", program)
    monkeypatch.setattr(preparation, "InstalledConfiguration", installed)
    directory = tmp_path / "prepare"
    directory.mkdir()
    return types.SimpleNamespace(module=preparation, calls=calls, directory=directory,
                                 destination=tmp_path / "receipt.json")


def test_default_preparation_selects_only_node_and_installed(selection):
    value = selection
    value.module.prepare(value.directory, value.destination)
    assert value.calls == [("worker", "node"), ("program", False, False), ("installed", False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"node", "installed"}
    assert not (value.directory / "browser").exists()


def test_explicit_browser_metadata_uses_only_constructor_double(selection):
    value = selection
    value.module.prepare(value.directory, value.destination, profiles=("browser",))
    assert value.calls == [("worker", "browser"), ("program", True, False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"browser"}


@pytest.mark.parametrize("profiles", [(), ("node", "node"), ("unknown",)], ids=["empty", "duplicate", "unknown"])
def test_invalid_profile_selection_refuses_before_construction(selection, profiles):
    value = selection
    with pytest.raises(ValueError, match="distinct supported"):
        value.module.prepare(value.directory, value.destination, profiles=profiles)
    assert value.calls == [] and list(value.directory.iterdir()) == []
    assert not value.destination.exists()


def test_explicit_profile_cli_selects_metadata_only(selection, monkeypatch, tmp_path):
    value = selection
    directory = tmp_path / "cli-prepare"
    monkeypatch.setattr(sys, "argv", ["prepare", "--directory", str(directory),
                                     "--output", str(value.destination), "--profile", "browser"])
    value.module.main()
    assert value.calls == [("worker", "browser"), ("program", True, False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"browser"}


@pytest.mark.parametrize("source", ["cli", "environment", "cli_precedence"])
def test_gate_node_selection_is_explicit_before_any_build(gates, tmp_path, monkeypatch, source):
    node = write(tmp_path / "explicit-node", b"owned executable, not launched", 0o755)
    alternate = write(tmp_path / "alternate-node", b"other owned executable", 0o755)
    claimed = tmp_path / "caller"
    claimed.mkdir()
    monkeypatch.setenv("TMPDIR", str(claimed))
    monkeypatch.delenv("TELESCOPE_FIXTURE_NODE", raising=False)
    selected, stages = [], []
    real_identity = gates.check.node_identity
    def identify(value):
        selected.append(value)
        return real_identity(value)
    monkeypatch.setattr(gates.check, "node_identity", identify)
    monkeypatch.setattr(gates.check, "source_identity", lambda root: {"head": "owned", "tree": "owned"})
    class StopBeforeBuild:
        def __init__(self, output, root):
            pass
        def run(self, label, *args, **kwargs):
            stages.append(label)
            raise RuntimeError("owned stop before any gate command")
    monkeypatch.setattr(gates.check, "Stages", StopBeforeBuild)
    args = ["check", "--artifacts", str(tmp_path / "artifacts")]
    if source != "environment":
        args += ["--node", str(node)]
    if source != "cli":
        monkeypatch.setenv("TELESCOPE_FIXTURE_NODE", str(alternate if source == "cli_precedence" else node))
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(RuntimeError, match="owned stop before"):
        gates.check.main()
    assert selected == [str(node)] and stages == ["lint"]
    assert list(claimed.iterdir()) == []


@pytest.mark.parametrize("value", [None, "node", "missing", "nonexecutable"])
def test_missing_or_invalid_node_never_starts_a_gate(gates, tmp_path, monkeypatch, value):
    monkeypatch.delenv("TELESCOPE_FIXTURE_NODE", raising=False)
    calls = []
    monkeypatch.setattr(gates.check, "Stages", lambda *args: calls.append(args))
    args = ["check", "--artifacts", str(tmp_path / "artifacts")]
    if value is not None:
        selected = "node" if value == "node" else str(tmp_path / value)
        if value == "nonexecutable":
            write(tmp_path / value, b"owned", 0o644)
        args += ["--node", selected]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises((ValueError, FileNotFoundError)):
        gates.check.main()
    assert calls == [] and not (tmp_path / "artifacts").exists()
