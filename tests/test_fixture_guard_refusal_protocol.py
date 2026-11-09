"""Real worker declaration-refusal envelopes and bounded failure behavior."""

import json
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def narrow_project(root, *, leaked=False):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    put(root, "flag.txt", "synthetic")
    leak_source = (
        """import subprocess
if not Path("flag.txt").exists(): subprocess.Popen(["sleep", "20"])
"""
        if leaked
        else ""
    )
    put(
        root,
        "tests/test_inventory.py",
        """from pathlib import Path
import pytest
"""
        + leak_source
        + """pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(inputs=["flag.txt"])
def test_flagged(): pass
def test_unflagged(): pass
if Path("flag.txt").exists():
    def test_generated(): pass
""",
    )


def different(value):
    return ("1" if value[0] == "0" else "0") + value[1:]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "truncated",
        "protocol",
        "nonce",
        "context",
        "mode",
        "reason",
        "extra",
        "exit",
        "leaked-field",
    ],
)
def test_real_fixture_refusal_corruption_fails_closed(tmp_path, monkeypatch, fault):
    narrow_project(tmp_path)
    original = sandbox.capture
    events = []
    altered = []

    def corrupt(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
        events.append(request["mode"])
        if request["mode"] not in ("guard-capture", "guard-inspection"):
            return code, output
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        if len(indices) != 1:
            return code, output
        index = indices[0]
        data = json.loads(lines[index][len(prefix) :])
        if data.get("protocol") != "fixture-refusal-v1":
            return code, output
        assert code == 4 and data == {
            "protocol": "fixture-refusal-v1",
            "nonce": request["nonce"],
            "context_id": request["context_id"],
            "mode": request["mode"],
            "reason": "declaration",
        }
        altered.append(fault)
        if fault == "missing":
            del lines[index]
        elif fault == "duplicate":
            lines.append(lines[index])
        elif fault == "truncated":
            lines[index] = lines[index][:-4]
        elif fault == "exit":
            code = 0
        else:
            if fault == "protocol":
                data["protocol"] = "fixture-refusal-v0"
            elif fault == "nonce":
                data["nonce"] = different(data["nonce"])
            elif fault == "context":
                data["context_id"] = different(data["context_id"])
            elif fault == "mode":
                data["mode"] = (
                    "guard-inspection" if data["mode"] == "guard-capture" else "guard-capture"
                )
            elif fault == "reason":
                data["reason"] = "metadata"
            elif fault == "extra":
                data["extra"] = True
            else:
                data["leaked_descendants"] = True
            lines[index] = prefix + json.dumps(data).encode()
        return code, b"\n".join(lines) + b"\n"

    monkeypatch.setattr(sandbox, "capture", corrupt)
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert altered == [fault, fault]
    assert report["schema"] == 5 and report["status"] == "infrastructure_error"
    assert report["fixture_guard"] == {
        "profile": "available-v1",
        "state": "unavailable",
        "conditions": [],
    }
    assert "guard-batch" not in events
    assert not any(group["cache_eligible"] for group in report["groups"])


def test_real_descendant_during_refusal_is_infrastructure_failure(tmp_path, monkeypatch):
    narrow_project(tmp_path, leaked=True)
    original = sandbox.execute
    events = []

    def observe(*args, request, **kwargs):
        events.append(request["mode"])
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    report = controller.run(tmp_path, fixture_guard="available-v1", timeout=4)
    assert report["schema"] == 5 and report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "unavailable"
    assert "guard-capture" in events and "guard-batch" not in events
    assert not any(group["cache_eligible"] for group in report["groups"])
    scratch = tmp_path / ".pytest-telescope/scratch"
    assert not list(scratch.iterdir())
