"""Corrupt records and controller boundaries must fail closed."""

import json
import os
import subprocess
import sys

import pytest

from pytest_telescope.controller import SCHEMA, read_record
from pytest_telescope.model import DeclarationError, fingerprint, load


def success():
    return {
        "protocol": 1,
        "nonce": "b" * 32,
        "failures": [],
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "case_ids": ["a" * 64],
        "worker_seconds": 0.2,
        "cpu_seconds": 0.1,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"cpu_seconds": float("nan")},
        {"worker_seconds": float("inf")},
        {"cpu_seconds": -1},
        {"cpu_seconds": True},
        {"protocol": 0},
        {"exit_code": False},
        {"case_ids": "a" * 64, "collected": 64, "passed": 64},
        {"case_ids": ["a" * 64] * 2, "collected": 2, "passed": 2},
    ],
)
def test_semantically_corrupt_records_are_misses(tmp_path, changes):
    result = success() | changes
    path = tmp_path / "record.json"
    path.write_text(
        json.dumps(
            {"schema": SCHEMA, "key": "key", "result": result, "checksum": fingerprint(result)}
        )
    )
    assert read_record(path, "key") is None


def test_protocol_environment_cannot_be_declared(tmp_path):
    (tmp_path / "test_unit.py").write_text("def test_unit(): pass\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\nenv = ["TELESCOPE_REQUEST"]\n'
    )
    with pytest.raises(DeclarationError):
        load(tmp_path)


def test_controller_import_without_fcntl_is_clean():
    code = "import sys; sys.modules['fcntl'] = None; import pytest_telescope.controller"
    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_fifo_manifest_is_rejected_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pyproject.toml")
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "pytest_telescope", "plan"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert completed.returncode in (3, 4)
    assert "Traceback" not in completed.stdout + completed.stderr


def test_complete_success_record_is_accepted(tmp_path):
    result = success()
    path = tmp_path / "record.json"
    path.write_text(
        json.dumps(
            {"schema": SCHEMA, "key": "key", "result": result, "checksum": fingerprint(result)}
        )
    )
    assert read_record(path, "key") == result


def test_deep_json_record_is_a_cache_miss(tmp_path):
    path = tmp_path / "record.json"
    path.write_text("[" * 20000 + "0" + "]" * 20000)
    assert read_record(path, "key") is None


def test_nan_timeout_is_rejected_before_project_access(tmp_path):
    from pytest_telescope.controller import run

    with pytest.raises(DeclarationError, match="timeout"):
        run(tmp_path, timeout=float("nan"))


def test_cleanup_failure_cannot_publish_success_or_retry(tmp_path, monkeypatch):
    import contextlib

    from pytest_telescope import controller

    (tmp_path / "test_unit.py").write_text("def test_unit(): pass\n")
    (tmp_path / "test_second.py").write_text("def test_second(): pass\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    calls = []

    ordinal = 0

    @contextlib.contextmanager
    def invocation(root):
        nonlocal ordinal
        ordinal += 1
        current = ordinal
        directory = root / ".pytest-telescope" / "scratch" / str(current)
        directory.mkdir(parents=True)
        yield directory
        if current > 1:
            raise controller.runtime.CleanupError("owned cleanup failed")

    monkeypatch.setattr(controller.runtime, "recover", lambda root: None)
    monkeypatch.setattr(
        controller.runtime,
        "identity",
        lambda root: {"image": "fixture", "execution": {"fixture": True}},
    )
    monkeypatch.setattr(controller.runtime, "invocation", invocation)
    monkeypatch.setattr(
        controller.runtime, "execute", lambda *args, **kw: calls.append(1) or success()
    )
    report = controller.run(tmp_path)
    assert report["units"] == []
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert report["counts"]["executed"] == 0
    assert calls == [1]
    assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))


def test_unavailable_backend_reports_unknown_inventory_after_bounded_retry(tmp_path, monkeypatch):
    from pytest_telescope import controller

    for name in ("first", "second"):
        (tmp_path / f"test_{name}.py").write_text(f"def test_{name}(): pass\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    attempts = []

    def unavailable(root):
        attempts.append(1)
        raise controller.InfrastructureError("backend unavailable")

    monkeypatch.setattr(controller.runtime, "recover", lambda root: None)
    monkeypatch.setattr(controller.runtime, "identity", unavailable)
    report = controller.run(tmp_path)
    assert attempts == [1, 1]
    assert report["status"] == "infrastructure_error"
    assert report["units"] == []
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert report["collection"]["reason"] == "backend unavailable"


def test_outer_pool_cleanup_failure_publishes_no_new_inventory_or_case(tmp_path, monkeypatch):
    import contextlib

    from pytest_telescope import controller

    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    (tmp_path / "test_unit.py").write_text("def test_unit(): pass\n")
    ordinal = 0

    @contextlib.contextmanager
    def invocation(root):
        nonlocal ordinal
        ordinal += 1
        current = ordinal
        directory = root / ".pytest-telescope" / "scratch" / str(current)
        directory.mkdir(parents=True)
        yield directory
        if current == 1:
            raise controller.runtime.CleanupError("frozen pool cleanup failed")

    item = {
        "nodeid": "test_unit.py::test_unit",
        "file": "test_unit.py",
        "origins": [],
        "aliases": ["test_unit.py", "test_unit.py::test_unit"],
        "selected": True,
    }
    collected = {
        "protocol": 2,
        "kind": "collection",
        "nonce": "b" * 32,
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "items": [item],
        "worker_seconds": 0.1,
        "cpu_seconds": 0.1,
        "failures": [],
    }
    calls = []

    def execute(*args, request, **kwargs):
        calls.append(request["mode"])
        return collected if request["mode"] == "collection" else success()

    monkeypatch.setattr(controller.runtime, "recover", lambda root: None)
    monkeypatch.setattr(
        controller.runtime,
        "identity",
        lambda root: {"image": "fixture", "execution": {"fixture": True}},
    )
    monkeypatch.setattr(controller.runtime, "invocation", invocation)
    monkeypatch.setattr(controller.runtime, "execute", execute)
    with pytest.raises(controller.runtime.CleanupError, match="frozen pool"):
        controller.run(tmp_path)
    assert calls == ["collection", "execution"]
    cache = tmp_path / ".pytest-telescope" / "cache"
    assert not list(cache.glob("*.json"))
    assert not list((cache / "inventory").glob("*.json"))


def test_summary_output_budget_rejects_before_partial_json(monkeypatch, capsys):
    from pytest_telescope import controller, model

    monkeypatch.setattr(model, "MAX_OUTPUT_BYTES", 128)
    monkeypatch.setattr(controller, "run", lambda *args, **kwargs: {"units": []})
    assert controller.main(["run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"units": []}
    monkeypatch.setattr(controller, "run", lambda *args, **kwargs: {"units": ["a" * 100] * 3})
    assert controller.main(["run"]) == 3
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "infrastructure_error"
    assert "output limit" in output["error"]
