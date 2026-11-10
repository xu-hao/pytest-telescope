"""Profile selection uses constructor/validation doubles and never launches programs."""

import hashlib
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

    calls, validations = [], []

    def worker(directory, patch):
        calls.append(("worker", directory.name))
        return types.SimpleNamespace(document={"owned": "worker"})

    def program(worker, *, browser, require_receipt):
        calls.append(("program", browser, require_receipt))
        return types.SimpleNamespace(identity="program", document={"owned": "frontend"})

    def installed(directory, patch, *, require_receipt):
        calls.append(("installed", require_receipt))
        return types.SimpleNamespace(
            identity="installed", admission_documents={"owned": "installed"}
        )

    monkeypatch.setattr(preparation, "WorkerConfiguration", worker)
    monkeypatch.setattr(preparation, "ProgramConfiguration", program)
    monkeypatch.setattr(preparation, "InstalledConfiguration", installed)

    def validate(path, expected, directory, *, profiles):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        validations.append(tuple(profiles))
        return json.loads(path.read_text())

    monkeypatch.setattr(preparation, "validate_receipt", validate)
    directory = tmp_path / "prepare"
    directory.mkdir()
    return types.SimpleNamespace(
        module=preparation,
        calls=calls,
        validations=validations,
        directory=directory,
        destination=tmp_path / "receipt.json",
    )


def test_default_preparation_selects_only_node_and_installed(selection):
    value = selection
    value.module.prepare(value.directory, value.destination)
    assert value.calls == [("worker", "node"), ("program", False, False), ("installed", False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"node", "installed"}
    assert not (value.directory / "browser").exists()
    assert value.validations == [("node", "installed")]


def test_explicit_browser_metadata_uses_only_constructor_double(selection):
    value = selection
    value.module.prepare(value.directory, value.destination, profiles=("browser",))
    assert value.calls == [("worker", "browser"), ("program", True, False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"browser"}
    assert value.validations == [("browser",)]


@pytest.mark.parametrize(
    "profiles", [(), ("node", "node"), ("unknown",)], ids=["empty", "duplicate", "unknown"]
)
def test_invalid_profile_selection_refuses_before_construction(selection, profiles):
    value = selection
    with pytest.raises(ValueError, match="distinct supported"):
        value.module.prepare(value.directory, value.destination, profiles=profiles)
    assert value.calls == [] and list(value.directory.iterdir()) == []
    assert not value.destination.exists()
    assert value.validations == []


def test_explicit_profile_cli_selects_metadata_only(selection, monkeypatch, tmp_path):
    value = selection
    directory = tmp_path / "cli-prepare"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare",
            "--directory",
            str(directory),
            "--output",
            str(value.destination),
            "--profile",
            "browser",
        ],
    )
    value.module.main()
    assert value.calls == [("worker", "browser"), ("program", True, False)]
    assert set(json.loads(value.destination.read_text())["profiles"]) == {"browser"}
    assert value.validations == [("browser",)]


@pytest.mark.parametrize("source", ["cli", "environment", "cli_precedence"])
def test_legacy_node_option_refuses_while_environment_is_unselected(
    gates, tmp_path, monkeypatch, source
):
    node = write(tmp_path / "explicit-node", b"owned executable, not launched", 0o755)
    claimed = tmp_path / "caller"
    claimed.mkdir()
    monkeypatch.setenv("TMPDIR", str(claimed))
    monkeypatch.delenv("TELESCOPE_FIXTURE_NODE", raising=False)
    stages = []
    monkeypatch.setattr(
        gates.check, "source_identity", lambda root: {"head": "a" * 40, "tree": "b" * 40}
    )

    class StopBeforeBuild:
        def __init__(self, output, root):
            pass

        def run(self, label, *args, **kwargs):
            assert "TELESCOPE_FIXTURE_NODE" not in kwargs["env"]
            stages.append(label)
            raise RuntimeError("owned stop before any gate command")

    monkeypatch.setattr(gates.check, "Stages", StopBeforeBuild)
    arguments = ["check", "--artifacts", str(tmp_path / "artifacts")]
    if source != "environment":
        arguments += ["--node", str(node)]
    if source != "cli":
        monkeypatch.setenv("TELESCOPE_FIXTURE_NODE", str(node))
    monkeypatch.setattr(sys, "argv", arguments)
    if source == "environment":
        with pytest.raises(RuntimeError, match="owned stop before"):
            gates.check.main()
        assert stages == ["lint"]
    else:
        with pytest.raises(SystemExit) as error:
            gates.check.main()
        assert error.value.code == 2
        assert stages == []
    assert list(claimed.iterdir()) == []


@pytest.mark.parametrize("value", [None, "node", "missing", "nonexecutable"])
def test_default_gate_does_not_require_or_validate_program_environment(
    gates, tmp_path, monkeypatch, value
):
    claimed = tmp_path / "caller"
    claimed.mkdir()
    monkeypatch.setenv("TMPDIR", str(claimed))
    monkeypatch.delenv("TELESCOPE_FIXTURE_NODE", raising=False)
    if value is not None:
        selected = "node" if value == "node" else str(tmp_path / value)
        if value == "nonexecutable":
            write(tmp_path / value, b"owned", 0o644)
        monkeypatch.setenv("TELESCOPE_FIXTURE_NODE", selected)
    calls = []
    monkeypatch.setattr(
        gates.check, "source_identity", lambda root: {"head": "a" * 40, "tree": "b" * 40}
    )

    class StopBeforeBuild:
        def __init__(self, output, root):
            pass

        def run(self, label, *args, **kwargs):
            assert "TELESCOPE_FIXTURE_NODE" not in kwargs["env"]
            calls.append(label)
            raise RuntimeError("owned stop before any gate command")

    monkeypatch.setattr(gates.check, "Stages", StopBeforeBuild)
    monkeypatch.setattr(sys, "argv", ["check", "--artifacts", str(tmp_path / "artifacts")])
    with pytest.raises(RuntimeError, match="owned stop before"):
        gates.check.main()
    assert calls == ["lint"] and list(claimed.iterdir()) == []
