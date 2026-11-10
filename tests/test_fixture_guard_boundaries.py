"""Real worker transport and ordinary-route boundaries for the optional guard."""

import json
from pathlib import Path

import pytest

from pytest_telescope import controller, ordinary, sandbox
from pytest_telescope.model import UnsupportedCapability


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


@pytest.mark.parametrize(
    "mode,field",
    [
        ("guard-capture", "nonce"),
        ("guard-capture", "base_context_id"),
        ("guard-capture", "registry_id"),
        ("guard-capture", "content_id"),
        ("guard-batch", "nonce"),
        ("guard-batch", "graph_id"),
    ],
)
def test_actual_worker_response_mismatch_is_bounded_and_not_cached(
    tmp_path, monkeypatch, mode, field
):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_item.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    original = sandbox.capture
    corrupted = []

    def alter(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        path = Path(argv[argv.index(sandbox.REQUEST) - 1])
        request = json.loads(path.read_text())
        if request["mode"] != mode:
            return code, output
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert len(indices) == 1
        index = indices[0]
        data = json.loads(lines[index][len(prefix) :])
        data[field] = ("1" if data[field][0] == "0" else "0") + data[field][1:]
        lines[index] = prefix + json.dumps(data).encode()
        corrupted.append(field)
        return code, b"\n".join(lines) + b"\n"

    monkeypatch.setattr(sandbox, "capture", alter)
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert len(corrupted) == 2 and report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == (
        "unavailable" if mode == "guard-capture" else "admitted"
    )
    assert not any(group["cache_eligible"] for group in report["groups"])
    for path in (tmp_path / ".pytest-telescope/cache").glob("*.json"):
        assert json.loads(path.read_text())["schema"] != 8


def test_guard_refuses_mixed_ordinary_boundary_before_ordinary_session(tmp_path, monkeypatch):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_scoped.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    put(tmp_path, "tests/test_ordinary.py", "def test_item(): pass\n")
    calls = []
    original = ordinary.session

    def observe(*args, **kwargs):
        calls.append("ordinary")
        return original(*args, **kwargs)

    monkeypatch.setattr(ordinary, "session", observe)
    with pytest.raises(UnsupportedCapability) as exc:
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["tests/test_scoped.py"])
    assert exc.value.capability == "execution:fixture-profile" and not calls
    unguarded = controller.run(tmp_path)
    assert unguarded["status"] == "passed" and calls == ["ordinary"]
    assert unguarded["schema"] == 4
