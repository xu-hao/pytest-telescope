"""Corrupt records and controller boundaries must fail closed."""

import json
import os
import subprocess
import sys

import pytest

from pytest_telescope import controller, sandbox
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
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_seconds": float("nan")},
        {"worker_seconds": float("inf")},
        {"worker_seconds": -1},
        {"worker_seconds": True},
        {"cpu_seconds": 0.1},
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


def test_cleanup_failure_cannot_publish_success_or_retry(tmp_path, sandbox_double, module_rows):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = module_rows(tmp_path, [("test_unit.py", ["unit"]), ("test_second.py", ["second"])])
    calls = []

    def execute(*args, request, **kwargs):
        calls.append(request["mode"])
        if request["mode"] == "collection":
            return dict(
                protocol=4,
                kind="collection",
                nonce="b" * 32,
                exit_code=0,
                complete=True,
                leaked_descendants=False,
                items=rows,
                worker_seconds=0.1,
                failures=[],
            )
        # The worker's own scratch fails to clean after this attempt.
        raise sandbox.CleanupError("owned cleanup failed")

    sandbox_double(execute)
    report = controller.run(tmp_path)
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert report["counts"]["executed"] == 0
    assert report["counts"]["infrastructure_error"] == 2
    assert calls == ["collection", "execution"]
    assert not list((tmp_path / ".pytest-telescope/cache").glob("*.json"))


def test_unavailable_sandbox_reports_unknown_inventory_after_bounded_retry(
    tmp_path, monkeypatch, module_rows
):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    module_rows(tmp_path, [("test_first.py", ["first"]), ("test_second.py", ["second"])])
    stand_in = tmp_path / "bin"
    stand_in.mkdir()
    (stand_in / "bwrap").write_text("#!/bin/sh\nexit 0\n")
    (stand_in / "bwrap").chmod(0o755)
    monkeypatch.setenv("PATH", str(stand_in))
    attempts = []

    def unavailable(args, timeout, **kwargs):
        attempts.append(args[0])
        raise controller.InfrastructureError("session could not start or exceeded its deadline")

    monkeypatch.setattr(sandbox, "capture", unavailable)
    report = controller.run(tmp_path)
    assert len(attempts) == 2
    assert report["status"] == "infrastructure_error"
    assert report["units"] == []
    assert report["sandbox"]["required"] is True and report["sandbox"]["identity"] is None
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert "deadline" in report["collection"]["reason"]
    assert report["inventory_complete"] is False
    assert report["inventory_digest"] is None
    assert report["collection"]["suites"][0]["files"] == ["test_first.py", "test_second.py"]
    assert not (tmp_path / ".pytest-telescope").exists()


def test_outer_pool_cleanup_failure_publishes_no_new_inventory_or_case(
    tmp_path, sandbox_double, module_rows
):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = module_rows(tmp_path, [("test_unit.py", ["unit"])])
    collected = {
        "protocol": 4,
        "kind": "collection",
        "nonce": "b" * 32,
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "items": rows,
        "worker_seconds": 0.1,
        "failures": [],
    }
    calls = []

    def execute(*args, request, **kwargs):
        calls.append(request["mode"])
        return (
            collected
            if request["mode"] == "collection"
            else success() | {"case_ids": [rows[0]["case_id"]]}
        )

    sandbox_double(execute, fail_outer=True)
    report = controller.run(tmp_path)
    assert report["status"] == "infrastructure_error"
    assert report["inventory_complete"] is True
    assert len(report["units"]) == 1
    assert "outer cleanup" in report["collection"]["reason"]
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
