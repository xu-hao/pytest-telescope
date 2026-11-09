"""Owned gate controls; installed origins are explicit unit doubles, not installs."""

import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
import warnings
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gates(monkeypatch):
    # Load the real scripts, preserving the outer runner's import configuration.
    monkeypatch.setattr(sys, "path", list(sys.path))
    gate = load_script("gate", SCRIPTS / "gate.py")
    monkeypatch.setitem(sys.modules, "gate", gate)
    functional = load_script("owned_functional_gate", SCRIPTS / "functional_gate.py")
    check = load_script("owned_check_gate", SCRIPTS / "check.py")
    return types.SimpleNamespace(gate=gate, functional=functional, check=check)


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def make_wheel(path, files, *, version="0.0.1", extra=()):
    with zipfile.ZipFile(path, "w") as wheel:
        for name, content in files.items():
            wheel.writestr("pytest_telescope/" + name, content)
        wheel.writestr(
            "pytest_telescope-0.0.1.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: pytest-telescope\nVersion: {version}\n\n",
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, content in extra:
                wheel.writestr(name, content)


@pytest.fixture
def candidate(tmp_path, monkeypatch, gates):
    """Real owned bytes/ZIP, with named origin/distribution/source-identity doubles."""
    gate = gates.gate
    root, prefix = tmp_path / "checkout", tmp_path / "environment"
    package = prefix / "lib/site-packages/pytest_telescope"
    source = root / "src/pytest_telescope"
    files = {"__init__.py": b"__version__ = '0.0.1'\n", "worker.py": b"VALUE = 7\n",
             "resources/value.json": b'{"answer": 7}\n'}
    for name, content in files.items():
        write(source / name, content)
        write(package / name, content)
    write(root / "pyproject.toml", b'[project]\nversion = "0.0.1"\n')
    node = write(tmp_path / "selected-node", b"owned executable bytes, never launched\n", 0o755)
    wheel = tmp_path / "candidate.whl"
    make_wheel(wheel, files)
    identity = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gate, "source_identity", lambda _: dict(identity))
    monkeypatch.setattr(gate, "interpreter_identity", lambda: {"unit_interpreter": "owned"})
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(gate.sysconfig, "get_path", lambda _: str(package.parent))
    distribution = types.SimpleNamespace(version="0.0.1", locate_file=lambda name: package.parent / name)
    monkeypatch.setattr(gate.importlib.metadata, "distribution", lambda _: distribution)
    for name in tuple(sys.modules):
        if name == "pytest_telescope" or name.startswith("pytest_telescope."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(package.parent))
    original_spec = gate.importlib.util.find_spec
    spec = importlib.machinery.ModuleSpec(
        "pytest_telescope", importlib.machinery.SourceFileLoader("pytest_telescope", str(package / "__init__.py")),
        origin=str(package / "__init__.py"), is_package=True,
    )
    spec.submodule_search_locations = [str(package)]
    monkeypatch.setattr(gate.importlib.util, "find_spec", lambda name: spec if name == "pytest_telescope" else original_spec(name))
    binding = gate.candidate_binding(root, wheel, str(node))
    value = types.SimpleNamespace(gate=gate, root=root, prefix=prefix, package=package,
                                  source=source, files=files, wheel=wheel, node=node,
                                  binding=binding, identity=identity, distribution=distribution, spec=spec)
    try:
        yield value
    finally:
        for name in tuple(sys.modules):
            if name == "pytest_telescope" or name.startswith("pytest_telescope."):
                sys.modules.pop(name)


def test_owned_exact_package_is_accepted(candidate):
    assert candidate.gate.verify_candidate(candidate.binding, first_import=True) == str(candidate.package / "__init__.py")
    assert not any(name == "pytest_telescope" for name in sys.modules)


@pytest.mark.parametrize("fault", ["missing", "extra", "changed", "version", "bytecode", "symlink", "fifo"])
def test_owned_installed_package_mismatch_refuses(candidate, fault):
    target = candidate.package / "worker.py"
    if fault == "missing":
        target.unlink()
    elif fault == "extra":
        write(candidate.package / "unexpected.py", b"extra\n")
    elif fault == "changed":
        target.write_bytes(b"VALUE = 8\n")
    elif fault == "version":
        candidate.distribution.version = "0.0.2"
    elif fault == "bytecode":
        write(candidate.package / "__pycache__/worker.cpython-312.pyc", b"untrusted bytecode")
    elif fault == "symlink":
        target.unlink()
        target.symlink_to(candidate.source / "worker.py")
    else:
        target.unlink()
        os.mkfifo(target)
    with pytest.raises(ValueError):
        candidate.gate.verify_candidate(candidate.binding, first_import=True)


@pytest.mark.parametrize("fault", ["source_bytes", "source_version", "wheel_bytes", "wheel_version", "head", "tree", "interpreter", "node"])
def test_candidate_binding_drift_refuses(candidate, monkeypatch, fault):
    if fault == "source_bytes":
        (candidate.source / "worker.py").write_bytes(b"changed\n")
    elif fault == "source_version":
        (candidate.root / "pyproject.toml").write_text('[project]\nversion = "0.0.2"\n')
    elif fault == "wheel_bytes":
        make_wheel(candidate.wheel, dict(candidate.files, **{"worker.py": b"changed\n"}))
    elif fault == "wheel_version":
        make_wheel(candidate.wheel, candidate.files, version="0.0.2")
    elif fault in {"head", "tree"}:
        candidate.identity[fault] = "c" * 40
    elif fault == "interpreter":
        monkeypatch.setattr(candidate.gate, "interpreter_identity", lambda: {"unit_interpreter": "changed"})
    else:
        candidate.node.write_bytes(b"other owned bytes")
    with pytest.raises(ValueError):
        candidate.gate.verify_candidate(candidate.binding, first_import=True)


@pytest.mark.parametrize("fault", ["wrong_origin", "multiple_locations", "already_imported", "loaded_substitute"])
def test_candidate_import_origin_refuses(candidate, monkeypatch, fault):
    if fault == "wrong_origin":
        candidate.spec.origin = str(candidate.source / "__init__.py")
    elif fault == "multiple_locations":
        candidate.spec.submodule_search_locations.append(str(candidate.source))
    else:
        module = types.ModuleType("pytest_telescope")
        module.__file__ = str(candidate.package / "__init__.py")
        monkeypatch.setitem(sys.modules, "pytest_telescope", module)
        if fault == "loaded_substitute":
            child = types.ModuleType("pytest_telescope.worker")
            child.__file__ = str(candidate.source / "worker.py")
            monkeypatch.setitem(sys.modules, child.__name__, child)
    with pytest.raises(ValueError):
        candidate.gate.verify_candidate(candidate.binding, first_import=fault != "loaded_substitute")


@pytest.mark.parametrize("fault", ["duplicate", "noncanonical", "absolute", "parent", "symlink", "bytecode"])
def test_wheel_unsupported_members_refuse(gates, tmp_path, fault):
    names = {"duplicate": "pytest_telescope/__init__.py", "noncanonical": "pytest_telescope//extra.py",
             "absolute": "/outside.py", "parent": "../outside.py", "symlink": "pytest_telescope/link.py",
             "bytecode": "pytest_telescope/worker.pyc"}
    name = names[fault]
    if fault == "symlink":
        name = zipfile.ZipInfo(name)
        name.external_attr = 0o120777 << 16
    wheel = tmp_path / "unsupported.whl"
    make_wheel(wheel, {"__init__.py": b""}, extra=[(name, b"owned")])
    with pytest.raises(ValueError):
        gates.gate.wheel_identity(wheel)


def receipt_document(source):
    content = b"owned configuration\n"
    write(source / "value.txt", content)
    return {"schema": 1, "roots": [{"id": "owned", "source": str(source), "coverage": "tree",
            "preserve_source": False, "entries": [
                {"path": ".", "kind": "directory", "mode": 0o755},
                {"path": "value.txt", "kind": "file", "mode": 0o644,
                 "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}],
            "executables": [], "python": [], "links": [], "locks": [], "commands": [], "loader": None}


def owned_receipt(tmp_path):
    receipt = {"schema": 1, "profiles": {}}
    for name, keys in (("node", ("worker", "frontend")), ("installed", ("worker", "unselected-baseline"))):
        documents = {key: receipt_document(tmp_path / "inputs" / name / key) for key in keys}
        identity = hashlib.sha256(json.dumps(documents, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt["profiles"][name] = {"identity": identity, "documents": documents}
    return receipt


def test_owned_receipt_passes_real_manifest_parser_and_cleans_copies(gates, tmp_path):
    receipt = owned_receipt(tmp_path)
    path = tmp_path / "receipt.json"
    digest = gates.gate.write_json(path, receipt)
    scratch = tmp_path / "validation"
    scratch.mkdir()
    assert gates.functional.admission(path, digest, scratch) == receipt
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("fault", ["wrong_profile", "missing_profile", "extra_profile", "schema_bool", "missing_document", "malformed_declaration", "identity", "stale_hash"])
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
    path = tmp_path / "receipt.json"
    digest = gates.gate.write_json(path, receipt)
    if fault == "stale_hash":
        path.write_bytes(path.read_bytes() + b" ")
    scratch = tmp_path / "validation"
    scratch.mkdir()
    from pytest_telescope.model import DeclarationError, UnsupportedCapability

    with pytest.raises((ValueError, DeclarationError, UnsupportedCapability)):
        gates.functional.admission(path, digest, scratch)
    assert list(scratch.iterdir()) == []


def stage_result(path, name):
    return json.loads((path / (name + "-result.json")).read_text())


def test_stage_success_nonzero_and_fresh_attempt(gates, tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    stages = gates.gate.Stages(first, tmp_path)
    with pytest.raises(RuntimeError, match="stage failed"):
        stages.run("child", "-I", "-B", "-c", "print('owned failure', flush=True); raise SystemExit(7)")
    failed = stage_result(first, "child")
    assert failed["exit_status"] == 7 and failed["output_complete"] is True
    previous = {path.name: path.read_bytes() for path in first.iterdir()}
    with pytest.raises(FileExistsError):
        stages.run("child", "-I", "-B", "-c", "raise SystemExit('must not run')")
    assert {path.name: path.read_bytes() for path in first.iterdir()} == previous
    second = tmp_path / "second"
    second.mkdir()
    gates.gate.Stages(second, tmp_path).run("child", "-I", "-B", "-c", "print('owned success')")
    assert stage_result(second, "child")["error"] is None
    assert (second / "child.log").read_bytes() == b"owned success\n"


def test_stage_output_overflow_refuses_complete_verdict(gates, tmp_path, monkeypatch):
    monkeypatch.setattr(gates.gate, "MAX_OUTPUT", 64)
    with pytest.raises(ValueError, match="output exceeded"):
        gates.gate.Stages(tmp_path, tmp_path).run("overflow", "-I", "-B", "-c", "import os; os.write(1, b'x' * 256)")
    result = stage_result(tmp_path, "overflow")
    assert result["output_bytes"] > 64 and result["output_complete"] is False
    assert result["error"] is not None


def test_stage_deadline_reaps_its_owned_child(gates, tmp_path):
    script = """import json, os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(30)'])
def stop(signum, frame):
    child.terminate()
    child.wait(timeout=5)
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({'parent': os.getpid(), 'child': child.pid}), flush=True)
time.sleep(30)
"""
    with pytest.raises(TimeoutError, match="deadline"):
        gates.gate.Stages(tmp_path, tmp_path).run("deadline", "-I", "-B", "-c", script, timeout=1)
    owned = json.loads((tmp_path / "deadline.log").read_text())
    for pid in owned.values():
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    result = stage_result(tmp_path, "deadline")
    assert result["error"] and result["seconds"] < 25
    assert result["output_complete"] is False


def test_evidence_is_exclusive_and_hash_bound(gates, tmp_path):
    path = tmp_path / "evidence.json"
    digest = gates.gate.write_json(path, {"owned": True})
    with pytest.raises(FileExistsError):
        gates.gate.write_json(path, {"owned": False})
    assert gates.gate.read_json(path, digest) == {"owned": True}
    path.write_text('{"owned": true, "owned": false}')
    with pytest.raises(ValueError, match="duplicate"):
        gates.gate.read_json(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.fixture
def invocation(candidate, gates, tmp_path, monkeypatch):
    """Real guard and evidence I/O; preparation/parser/pytest dispatch are unit doubles."""
    functional, gate = gates.functional, gates.gate
    scratch = tmp_path / "caller-scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    binding_file, receipt, ready = (tmp_path / name for name in ("binding.json", "receipt.json", "ready.json"))
    binding_hash = gate.write_json(binding_file, candidate.binding)
    calls = []
    receipt_value = {"schema": 1, "profiles": {"node": {}, "installed": {}}}
    # The real parser is separately exercised above. This orchestration double
    # keeps real bounded file/hash verification without importing another producer.
    monkeypatch.setattr(functional, "admission", lambda path, expected, directory: gate.read_json(path, expected))
    preparation = types.ModuleType("frozen_tool_prepare_programs")
    def prepare(directory, destination, *, profiles):
        calls.append(("prepare", profiles))
        gate.write_json(destination, receipt_value)
    preparation.prepare = prepare
    monkeypatch.setitem(sys.modules, "frozen_tool_prepare_programs", preparation)
    def dispatch(args):
        calls.append(("tests", args))
        return 0
    monkeypatch.setattr(pytest, "main", dispatch)
    def arguments(mode, *, receipt_hash=None, ready_hash=None):
        result = ["functional_gate.py", mode, "--binding", str(binding_file), "--binding-sha256", binding_hash,
                  "--scratch", str(scratch), "--receipt", str(receipt), "--admission", str(ready)]
        if receipt_hash is not None:
            result += ["--receipt-sha256", receipt_hash, "--admission-sha256", ready_hash]
        return result
    def ready_record():
        receipt_hash = gate.write_json(receipt, receipt_value)
        value = {"schema": 1, "binding_sha256": binding_hash, "receipt": str(receipt),
                 "receipt_sha256": receipt_hash, "source": candidate.binding["source"],
                 "interpreter": candidate.binding["interpreter"], "wheel_sha256": candidate.binding["wheel"]["sha256"]}
        return receipt_hash, value
    return types.SimpleNamespace(candidate=candidate, gate=gate, functional=functional, scratch=scratch,
                                 binding_file=binding_file, receipt=receipt, ready=ready, calls=calls,
                                 preparation=preparation, arguments=arguments, ready_record=ready_record)


def test_failed_guard_never_prepares_or_dispatches(invocation, monkeypatch):
    value = invocation
    (value.candidate.package / "worker.py").write_bytes(b"changed before guard")
    monkeypatch.setattr(sys, "argv", value.arguments("prepare"))
    with pytest.raises(ValueError):
        value.functional.main()
    assert value.calls == [] and not value.receipt.exists() and not value.ready.exists()


def test_failed_preparation_never_publishes_readiness(invocation, monkeypatch):
    value = invocation
    def refuse(*args, **kwargs):
        raise ValueError("owned preparation failed")
    monkeypatch.setattr(value.preparation, "prepare", refuse)
    monkeypatch.setattr(sys, "argv", value.arguments("prepare"))
    with pytest.raises(ValueError, match="owned preparation failed"):
        value.functional.main()
    assert not value.ready.exists() and not value.receipt.exists()
    assert value.calls == []


@pytest.mark.parametrize("field", ["binding_sha256", "source", "wheel_sha256", "interpreter", "receipt_sha256", "receipt"])
def test_stale_readiness_never_dispatches(invocation, monkeypatch, field):
    value = invocation
    receipt_hash, ready = value.ready_record()
    ready[field] = {"changed": True} if isinstance(ready[field], dict) else "changed"
    ready_hash = value.gate.write_json(value.ready, ready)
    monkeypatch.setattr(sys, "argv", value.arguments("tests", receipt_hash=receipt_hash, ready_hash=ready_hash))
    with pytest.raises(ValueError, match="different candidate"):
        value.functional.main()
    assert value.calls == []


@pytest.mark.parametrize("fault", ["producer", "receipt", "ready", "binding"])
def test_successful_dispatch_cannot_hide_postcondition_mutation(invocation, monkeypatch, fault):
    value = invocation
    receipt_hash, ready = value.ready_record()
    ready_hash = value.gate.write_json(value.ready, ready)
    targets = {"producer": value.candidate.package / "worker.py", "receipt": value.receipt,
               "ready": value.ready, "binding": value.binding_file}
    def mutate(args):
        value.calls.append(("tests", args))
        target = targets[fault]
        target.write_bytes(target.read_bytes() + b" ")
        return 0
    monkeypatch.setattr(pytest, "main", mutate)
    monkeypatch.setattr(sys, "argv", value.arguments("tests", receipt_hash=receipt_hash, ready_hash=ready_hash))
    with pytest.raises(ValueError):
        value.functional.main()
    assert len(value.calls) == 1 and value.calls[0][0] == "tests"


def test_valid_preparation_publishes_exact_two_profile_binding(invocation, monkeypatch):
    value = invocation
    monkeypatch.setattr(sys, "argv", value.arguments("prepare"))
    assert value.functional.main() == 0
    assert value.calls == [("prepare", ("node", "installed"))]
    ready = json.loads(value.ready.read_text())
    assert ready["source"] == value.candidate.binding["source"]
    assert ready["receipt_sha256"] == hashlib.sha256(value.receipt.read_bytes()).hexdigest()


def test_changed_binding_after_preparation_never_publishes_readiness(invocation, monkeypatch):
    value = invocation
    original = value.preparation.prepare
    def changed_binding(*args, **kwargs):
        original(*args, **kwargs)
        value.binding_file.write_bytes(value.binding_file.read_bytes() + b" ")
    monkeypatch.setattr(value.preparation, "prepare", changed_binding)
    monkeypatch.setattr(sys, "argv", value.arguments("prepare"))
    with pytest.raises(ValueError):
        value.functional.main()
    assert not value.ready.exists(), "failed preparation binding must not publish readiness"
    assert value.calls == [("prepare", ("node", "installed"))]
