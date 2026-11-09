"""Observed and simulated session-start accounting controls."""

import json
import sys
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import InfrastructureError


def test_real_capture_signals_successful_process_start(tmp_path):
    starts = []
    code, output = sandbox.capture(
        [sys.executable, "-I", "-B", "-c", "import os; os.write(1, b'SESSION_STARTED\\n')"],
        10,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"},
        cwd=tmp_path,
        on_start=lambda: starts.append("started"),
    )
    assert code == 0 and output == b"SESSION_STARTED\n"
    assert starts == ["started"]


def test_real_capture_failed_popen_never_signals_start(tmp_path):
    starts = []
    with pytest.raises(InfrastructureError):
        sandbox.capture(
            [str(tmp_path / "owned-command-does-not-exist")],
            10,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"},
            cwd=tmp_path,
            on_start=lambda: starts.append("started"),
        )
    assert not starts


def test_simulated_prestart_worker_failure_reports_no_session(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_owned.py").write_text(
        'import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n',
        encoding="utf-8",
    )
    original = sandbox.capture
    calls = []

    def simulate_before_popen(args, timeout, **kwargs):
        if sandbox.REQUEST in args:
            request = json.loads(Path(args[args.index(sandbox.REQUEST) - 1]).read_text())
            if request["mode"] == "guard-batch":
                assert callable(kwargs["on_start"])
                calls.append("prestart")
                raise InfrastructureError("synthetic failure before worker process")
        return original(args, timeout, **kwargs)

    monkeypatch.setattr(sandbox, "capture", simulate_before_popen)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert calls == ["prestart", "prestart"]
    assert report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "admitted"
    (group,) = report["groups"]
    (unit,) = report["units"]
    assert group["attempts"] == 0 and group["not_run"] == 1 and group["unresolved"] == 0
    assert unit["attempts"] == 0 and unit["result_status"] == "not-run"
    assert not group["cache_eligible"]
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
