"""Explicit benchmark receipt controls with owned data and no program dispatch."""

import copy
import hashlib
import json
import types

import frozen_tool_prepare_programs as preparation
import pytest
from frozen_tool_prepare_programs import validate_receipt
from frozen_tool_program_fixture import prerequisite_identity, read_prerequisite_receipt
from test_functional_gate import gates as gates
from test_functional_gate import write


def receipt_document(source):
    content = b"owned configuration\n"
    write(source / "value.txt", content)
    return {
        "schema": 1,
        "roots": [
            {
                "id": "owned",
                "source": str(source),
                "coverage": "tree",
                "preserve_source": False,
                "entries": [
                    {"path": ".", "kind": "directory", "mode": 0o755},
                    {
                        "path": "value.txt",
                        "kind": "file",
                        "mode": 0o644,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                ],
            }
        ],
        "executables": [],
        "python": [],
        "links": [],
        "locks": [],
        "commands": [],
        "loader": None,
    }


def owned_receipt(tmp_path):
    receipt = {"schema": 1, "profiles": {}}
    for name, keys in (
        ("node", ("worker", "frontend")),
        ("installed", ("worker", "unselected-baseline")),
    ):
        documents = {key: receipt_document(tmp_path / "inputs" / name / key) for key in keys}
        identity = hashlib.sha256(
            json.dumps(documents, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt["profiles"][name] = {"identity": identity, "documents": documents}
    return receipt


def test_owned_receipt_passes_real_manifest_parser_and_cleans_copies(gates, tmp_path):
    receipt = owned_receipt(tmp_path)
    path = tmp_path / "receipt.json"
    digest = gates.gate.write_json(path, receipt)
    scratch = tmp_path / "validation"
    scratch.mkdir()
    assert validate_receipt(path, digest, scratch, profiles=("node", "installed")) == receipt
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_profile",
        "missing_profile",
        "extra_profile",
        "schema_bool",
        "missing_document",
        "malformed_declaration",
        "identity",
        "stale_hash",
    ],
)
def test_receipt_refuses_invalid_profiles_declarations_and_identity(gates, tmp_path, fault):
    receipt = owned_receipt(tmp_path)
    profiles = receipt["profiles"]
    if fault == "wrong_profile":
        profiles["browser"] = profiles.pop("node")
    elif fault == "missing_profile":
        profiles.pop("installed")
    elif fault == "extra_profile":
        profiles["browser"] = copy.deepcopy(profiles["node"])
    elif fault == "schema_bool":
        receipt["schema"] = True
    elif fault == "missing_document":
        profiles["node"]["documents"].pop("worker")
    elif fault == "malformed_declaration":
        profiles["node"]["documents"]["worker"]["roots"][0]["entries"][1]["size"] = -1
    elif fault == "identity":
        profiles["node"]["identity"] = "f" * 64
    if fault != "identity":
        for profile in profiles.values():
            profile["identity"] = prerequisite_identity(profile["documents"])
    path = tmp_path / "receipt.json"
    digest = gates.gate.write_json(path, receipt)
    if fault == "stale_hash":
        path.write_bytes(path.read_bytes() + b" ")
    scratch = tmp_path / "validation"
    scratch.mkdir()
    from pytest_telescope.model import DeclarationError, UnsupportedCapability

    with pytest.raises((ValueError, DeclarationError, UnsupportedCapability)):
        validate_receipt(path, digest, scratch, profiles=("node", "installed"))
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("content", [b'{"schema":1,"schema":1}', b'{"schema":NaN}'])
def test_explicit_receipt_reader_rejects_ambiguous_json(tmp_path, content):
    path = tmp_path / "receipt.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        read_prerequisite_receipt(path, hashlib.sha256(content).hexdigest())


def test_explicit_receipt_reader_enforces_its_input_bound(tmp_path):
    receipt = {
        "schema": 1,
        "profiles": {
            "node": {
                "identity": "a" * 64,
                "documents": {"worker": {"padding": "x" * (4 * 1024 * 1024)}, "frontend": {}},
            }
        },
    }
    content = json.dumps(receipt).encode()
    path = tmp_path / "receipt.json"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="bound"):
        read_prerequisite_receipt(path, hashlib.sha256(content).hexdigest())


@pytest.fixture
def owned_preparation(monkeypatch):
    """Only tiny owned data manifests; no interpreter or installed-program inventory."""
    calls = []

    def worker(directory, patch):
        calls.append("worker")
        return types.SimpleNamespace(
            directory=directory, document=receipt_document(directory / "worker")
        )

    def program(value, *, browser, require_receipt):
        assert require_receipt is False
        name = "browser" if browser else "node"
        calls.append(name)
        document = receipt_document(value.directory / "frontend")
        return types.SimpleNamespace(
            document=document,
            identity=prerequisite_identity({"worker": value.document, "frontend": document}),
        )

    def installed(directory, patch, *, require_receipt):
        assert require_receipt is False
        calls.append("installed")
        documents = {
            name: receipt_document(directory / name) for name in ("worker", "unselected-baseline")
        }
        return types.SimpleNamespace(
            admission_documents=documents, identity=prerequisite_identity(documents)
        )

    monkeypatch.setattr(preparation, "WorkerConfiguration", worker)
    monkeypatch.setattr(preparation, "ProgramConfiguration", program)
    monkeypatch.setattr(preparation, "InstalledConfiguration", installed)
    return calls


@pytest.mark.parametrize(
    "profiles", [("node",), ("installed",), ("browser",), ("node", "installed")]
)
def test_explicit_named_preparation_validates_only_selected_data_profiles(
    tmp_path, owned_preparation, profiles
):
    directory = tmp_path / "preparation"
    directory.mkdir()
    destination = tmp_path / "receipt.json"
    preparation.prepare(directory, destination, profiles=profiles)
    receipt = read_prerequisite_receipt(
        destination, hashlib.sha256(destination.read_bytes()).hexdigest()
    )
    assert set(receipt["profiles"]) == set(profiles)
    assert [name for name in owned_preparation if name != "worker"] == list(profiles)
    assert list(directory.glob("receipt-validation-*")) == []


def test_failed_explicit_preparation_publishes_no_receipt(tmp_path, owned_preparation, monkeypatch):
    directory = tmp_path / "preparation"
    directory.mkdir()
    destination = tmp_path / "receipt.json"

    def refuse(*args, **kwargs):
        raise ValueError("owned preparation refusal")

    monkeypatch.setattr(preparation, "ProgramConfiguration", refuse)
    with pytest.raises(ValueError, match="owned preparation refusal"):
        preparation.prepare(directory, destination, profiles=("node",))
    assert not destination.exists()


def test_changed_explicit_receipt_cannot_report_readiness(
    tmp_path, owned_preparation, monkeypatch, capsys
):
    directory = tmp_path / "preparation"
    directory.mkdir()
    destination = tmp_path / "receipt.json"
    validate = preparation.validate_receipt

    def changed(path, expected, scratch, *, profiles):
        path.write_bytes(path.read_bytes() + b" ")
        return validate(path, expected, scratch, profiles=profiles)

    monkeypatch.setattr(preparation, "validate_receipt", changed)
    with pytest.raises(ValueError, match="changed"):
        preparation.prepare(directory, destination, profiles=("node",))
    assert capsys.readouterr().out == ""
