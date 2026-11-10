"""Independent retry, drain, cache preservation and selection controls."""

import hashlib
import json
import time
from pathlib import Path
from threading import Event

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError
from pytest_telescope.result import validate_guarded_batch


def case_id(name):
    node = f"tests/test_{name}.py::test_item"
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def project(root, names):
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 2\n',
        encoding="utf-8",
    )
    for name in names:
        (root / "tests" / f"test_{name}.py").write_text(
            "import os\nimport pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"
            f'def test_item(): os.write(1, b"CONTINUATION_{name.upper()}\\n")\n',
            encoding="utf-8",
        )


def group(report, name):
    return next(g for g in report["groups"] if g["case_ids"] == [case_id(name)])


def corrupt_b(monkeypatch, *, guarded, ready=None):
    original = sandbox.capture
    modes = []
    ids = {case_id(name): name for name in ("a", "b", "c")}

    def capture(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
        mode = "guard-batch" if guarded else "batch"
        if request["mode"] != mode:
            return code, output
        assert len(request["case_ids"]) == 1 and request["case_ids"][0] in ids
        name = ids[request["case_ids"][0]]
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines(keepends=True)
        indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert code == 0 and len(indices) == 1
        result = json.loads(lines[indices[0]][len(prefix) :])
        assert output.count(f"CONTINUATION_{name.upper()}".encode()) == 1
        if guarded:
            assert validate_guarded_batch(
                result,
                nonce=request["nonce"],
                successful_only=True,
                group_id=request["group_id"],
                case_ids=request["case_ids"],
                registry_id=request["registry_id"],
                graph_id=request["graph_id"],
            )
        modes.append((name, request["group_id"]))
        if name == "b":
            if ready is not None:
                assert ready.wait(30), "concurrent A never reached held completion"
            lines[indices[0]] = prefix + b"{\n"
            output = b"".join(lines)
        return code, output

    monkeypatch.setattr(sandbox, "capture", capture)
    return modes


def test_concurrent_active_worker_is_drained_after_other_exhausts(tmp_path, monkeypatch):
    project(tmp_path, ("a", "b", "c"))
    ready = Event()
    released = Event()
    real_execute = sandbox.execute
    modes = corrupt_b(monkeypatch, guarded=True, ready=ready)

    def execute(*args, request, **kwargs):
        result = real_execute(*args, request=request, **kwargs)
        if request["mode"] == "guard-batch" and request["case_ids"] == [case_id("a")]:
            assert result["exit_code"] == 0
            cancellation = kwargs["cancellation"]
            ready.set()
            deadline = time.monotonic() + 30
            while not cancellation.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert cancellation.is_set(), "B exhaustion did not cancel active A"
            released.set()
        return result

    monkeypatch.setattr(sandbox, "execute", execute)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=2, arguments=["-s"])
    a, b, c = (group(report, name) for name in ("a", "b", "c"))
    assert ready.is_set() and released.is_set()
    assert [name for name, _ in modes].count("a") == 1
    assert [name for name, _ in modes].count("b") == 2
    assert not any(name == "c" for name, _ in modes)
    assert report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "admitted"
    assert a["attempts"] == 1 and a["complete"] and a["passed"] == 1
    assert b["attempts"] == 2 and b["unresolved"] == 1
    assert c["attempts"] == 0
    assert not any(g["cache_eligible"] for g in report["groups"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.pending"))
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


def test_existing_guarded_cache_survives_other_group_exhaustion(tmp_path, monkeypatch):
    project(tmp_path, ("a", "b"))
    options = {"fixture_guard": "available-v1", "workers": 1, "arguments": ["-s"]}
    initial = controller.run(tmp_path, **options)
    assert initial["status"] == "passed"
    a, b = (group(initial, name) for name in ("a", "b"))
    cache = tmp_path / ".pytest-telescope/cache"
    a_path = cache / (a["group_id"] + ".json")
    b_path = cache / (b["group_id"] + ".json")
    assert a_path.exists() and b_path.exists()
    b_path.unlink()
    before = {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*.json")}
    modes = corrupt_b(monkeypatch, guarded=True)
    report = controller.run(tmp_path, **options)
    assert report["status"] == "infrastructure_error"
    assert group(report, "a")["outcome"] == "reused" and group(report, "a")["cache_eligible"]
    assert group(report, "a")["attempts"] == 0
    assert group(report, "b")["attempts"] == 2 and group(report, "b")["unresolved"] == 1
    assert [name for name, _ in modes] == ["b", "b"]
    after = {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*.json")}
    assert after == before and a_path.read_bytes() == before[a_path.relative_to(cache)]
    assert not b_path.exists() and not list(cache.rglob("*.pending"))


def test_k_deselection_condition_rejects_zero_selected(tmp_path, monkeypatch):
    project(tmp_path, ("a",))
    (tmp_path / "tests/test_a.py").write_text(
        'import pytest\npytestmark = pytest.mark.telescope(env=["CONTROL_NAME"])\n'
        "def test_item(): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTROL_NAME", "present")
    events = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        events.append(request["mode"])
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    options = {"fixture_guard": "available-v1", "arguments": ["-k", "no_such_test_493"]}
    plain = controller.run(tmp_path, **options)
    assert plain["status"] == "no_tests" and plain["counts"]["selected"] == 0
    assert "collection" in events and "guard-batch" not in events
    events.clear()
    with pytest.raises(DeclarationError):
        controller.run(
            tmp_path,
            **options,
            condition_checks=[{"case_id": case_id("a"), "name": "CONTROL_NAME", "op": "present"}],
        )
    assert "guard-batch" not in events


def test_unguarded_exhaustion_continues_to_later_group(tmp_path, monkeypatch):
    project(tmp_path, ("a", "b", "c"))
    modes = corrupt_b(monkeypatch, guarded=False)
    report = controller.run(tmp_path, workers=1, arguments=["-s"])
    assert report["schema"] == 4 and "fixture_guard" not in report
    assert report["status"] == "infrastructure_error"
    a, b, c = (group(report, name) for name in ("a", "b", "c"))
    assert [name for name, _ in modes] == ["a", "b", "b", "c"]
    assert a["attempts"] == c["attempts"] == 1 and a["passed"] == c["passed"] == 1
    assert b["attempts"] == 2 and b["unresolved"] == 1
