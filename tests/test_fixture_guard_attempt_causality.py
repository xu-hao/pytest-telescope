"""Session-attempt accounting at deterministic guarded cancellation boundaries."""

import concurrent.futures
import hashlib
import json
from pathlib import Path
from threading import Event

from pytest_telescope import controller, sandbox
from pytest_telescope.model import InfrastructureError
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
            'import os\nimport pytest\npytestmark = pytest.mark.telescope(inputs=[])\n'
            '@pytest.fixture\ndef local():\n    yield object()\n'
            f'def test_item(local): os.write(1, b"H3_BODY_{name.upper()}\\n")\n',
            encoding="utf-8",
        )


def group(report, name):
    return next(row for row in report["groups"] if row["case_ids"] == [case_id(name)])


def unit(report, name):
    return next(row for row in report["units"] if row["case_id"] == case_id(name))


def run_case(root, monkeypatch, mode):
    names = ("a", "b") if mode == "positive" else ("a", "b", "c")
    project(root, names)
    base_executor = concurrent.futures.ThreadPoolExecutor
    real_execute = sandbox.execute
    real_capture = sandbox.capture
    submitted = Event()
    cancel_known = Event()
    b_failed = Event()
    finished = Event()
    cancellation = []
    events = []
    ids = {case_id(name): name for name in names}

    class ObservedExecutor(base_executor):
        def submit(self, fn, *args, **kwargs):
            job = args[0]
            assert len(job["case_ids"]) == 1 and job["case_ids"][0] in ids
            name = ids[job["case_ids"][0]]
            events.append(("submitted", name, job["key"]))

            def invoke():
                if name == "b" and mode == "pre-first":
                    assert cancel_known.wait(30), "A cancellation event unavailable"
                    assert cancellation[0].wait(30), "A did not cancel"
                    events.append(("released", "b", job["key"]))
                return fn(*args, **kwargs)

            future = super().submit(invoke)
            if name == "b":
                submitted.set()
            return future

    def execute(*args, request, **kwargs):
        if request["mode"] != "guard-batch":
            return real_execute(*args, request=request, **kwargs)
        assert len(request["case_ids"]) == 1 and request["case_ids"][0] in ids
        name = ids[request["case_ids"][0]]
        events.append(("execute", name, request["group_id"]))
        if name == "a" and not cancellation:
            cancellation.append(kwargs["cancellation"])
            cancel_known.set()
        try:
            return real_execute(*args, request=request, **kwargs)
        except InfrastructureError:
            if name == "b" and mode == "between":
                events.append(("b-first-failed", "b", request["group_id"]))
                b_failed.set()
                assert cancel_known.wait(30), "A cancellation event unavailable"
                assert cancellation[0].wait(30), "A did not cancel"
                events.append(("released", "b", request["group_id"]))
            raise

    def capture(argv, timeout, **kwargs):
        code, output = real_capture(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
        if request["mode"] != "guard-batch":
            return code, output
        assert len(request["case_ids"]) == 1 and request["case_ids"][0] in ids
        name = ids[request["case_ids"][0]]
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines(keepends=True)
        indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert code == 0 and len(indices) == 1
        response = json.loads(lines[indices[0]][len(prefix) :])
        assert validate_guarded_batch(
            response,
            nonce=request["nonce"],
            successful_only=True,
            group_id=request["group_id"],
            case_ids=request["case_ids"],
            registry_id=request["registry_id"],
            graph_id=request["graph_id"],
        )
        assert output.count(f"H3_BODY_{name.upper()}".encode()) == 1
        events.append(("response", name, request["group_id"]))
        if mode != "positive" and name == "a":
            assert submitted.wait(30), "B was not submitted"
            if mode == "between":
                assert b_failed.wait(30), "B first real response not observed"
        if mode != "positive" and (name == "a" or (mode == "between" and name == "b")):
            lines[indices[0]] = prefix + b"{\n"
            events.append(("corrupted", name, request["group_id"]))
            output = b"".join(lines)
        return code, output

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(sandbox, "execute", execute)
    monkeypatch.setattr(sandbox, "capture", capture)
    try:
        report = controller.run(root, fixture_guard="available-v1", workers=2, arguments=["-s"])
    finally:
        finished.set()
    assert finished.is_set()
    return report, events, cancellation


def names(events, kind):
    return [name for event, name, _ in events if event == kind]


def test_h3_positive_uncancelled_real_workers_and_reuse(tmp_path, monkeypatch):
    report, events, cancellation = run_case(tmp_path, monkeypatch, "positive")
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert sorted(names(events, "submitted")) == ["a", "b"]
    assert sorted(names(events, "execute")) == ["a", "b"]
    assert sorted(names(events, "response")) == ["a", "b"]
    assert cancellation and not cancellation[0].is_set()
    assert all(group(report, n)["attempts"] == 1 and group(report, n)["cache_eligible"] for n in ("a", "b"))
    assert all(unit(report, n)["result_status"] == "passed" and unit(report, n)["complete"] for n in ("a", "b"))
    before = list(events)
    warm = controller.run(tmp_path, fixture_guard="available-v1", workers=2, arguments=["-s"])
    assert warm["status"] == "passed" and warm["counts"]["reused"] == 2
    assert events == before


def assert_common(report, events, cancellation, root):
    a, c = group(report, "a"), group(report, "c")
    assert report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "admitted"
    assert cancellation and cancellation[0].is_set()
    assert names(events, "response").count("a") == 2
    assert names(events, "corrupted").count("a") == 2
    assert names(events, "execute").count("a") == 2
    assert a["attempts"] == 2 and a["unresolved"] == 1
    assert "c" not in names(events, "submitted") and c["attempts"] == 0
    assert not any(row["cache_eligible"] for row in report["groups"])
    cache = root / ".pytest-telescope/cache"
    assert not list(cache.rglob("*.json")) and not list(cache.rglob("*.pending"))
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    assert names(events, "submitted").count("b") == 1
    assert names(events, "released").count("b") == 1


def test_h3_submitted_b_cancelled_before_first_session_is_known_unrun(tmp_path, monkeypatch):
    report, events, cancellation = run_case(tmp_path, monkeypatch, "pre-first")
    assert_common(report, events, cancellation, tmp_path)
    assert "b" not in names(events, "execute") and "b" not in names(events, "response")
    b, member = group(report, "b"), unit(report, "b")
    print("H3_PRE_FIRST_OBSERVED", events, b["attempts"], b["not_run"], b["unresolved"], member["result_status"])
    assert b["attempts"] == 0 and b["not_run"] == 1 and b["unresolved"] == 0
    assert b["stop_reason"] == "cancelled"
    assert member["attempts"] == 0 and member["result_status"] == "not-run"
    assert not member["complete"]


def test_h3_cancelled_between_attempts_keeps_one_unresolved_session(tmp_path, monkeypatch):
    report, events, cancellation = run_case(tmp_path, monkeypatch, "between")
    assert_common(report, events, cancellation, tmp_path)
    assert names(events, "execute").count("b") == 1
    assert names(events, "response").count("b") == 1
    assert names(events, "corrupted").count("b") == 1
    assert names(events, "b-first-failed").count("b") == 1
    b, member = group(report, "b"), unit(report, "b")
    print("H3_BETWEEN_OBSERVED", events, b["attempts"], b["not_run"], b["unresolved"], member["result_status"])
    assert b["attempts"] == 1 and b["not_run"] == 0 and b["unresolved"] == 1
    assert b["stop_reason"] == "cancelled"
    assert member["attempts"] == 1 and member["result_status"] is None
    assert not member["complete"]
