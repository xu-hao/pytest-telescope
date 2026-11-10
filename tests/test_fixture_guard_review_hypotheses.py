"""Causal controls for staged cache publication and empty selections."""

import hashlib
import json
from pathlib import Path

import pytest

from pytest_telescope import controller, ordinary, sandbox
from pytest_telescope.model import DeclarationError
from pytest_telescope.result import validate_guarded_batch


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def configure(root, names):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    for name in names:
        put(
            root,
            f"tests/test_{name}.py",
            "import os\nimport pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"
            f'def test_item(): os.write(1, b"REVIEW_BODY_{name.upper()}\\n")\n',
        )


def case_id(name):
    node = f"tests/test_{name}.py::test_item"
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def observe_transport(monkeypatch, names, corrupt_b):
    real_capture = sandbox.capture
    real_stage = controller.PendingRecords.stage
    ids = {case_id(name): name for name in names}
    events = []
    mutations = []
    staged = []

    def capture(argv, timeout, **kwargs):
        code, output = real_capture(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
        if request["mode"] != "guard-batch":
            return code, output
        assert len(request["case_ids"]) == 1 and request["case_ids"][0] in ids
        name = ids[request["case_ids"][0]]
        lines = output.splitlines(keepends=True)
        prefix = b"TELESCOPE_RESULT "
        matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        assert len(matches) == 1
        index = matches[0]
        result = json.loads(lines[index][len(prefix) :])
        assert code == 0
        assert validate_guarded_batch(
            result,
            nonce=request["nonce"],
            successful_only=True,
            group_id=request["group_id"],
            case_ids=request["case_ids"],
            registry_id=request["registry_id"],
            graph_id=request["graph_id"],
        )
        assert output.count(f"REVIEW_BODY_{name.upper()}".encode()) == 1
        events.append(("worker", name, request["group_id"]))
        if name == "b" and len(mutations) < corrupt_b:
            lines[index] = prefix + b"{\n"
            mutations.append(request["group_id"])
            output = b"".join(lines)
        return code, output

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        real_stage(self, final, key, result, kind=kind, guarded=guarded)
        pending = final.with_name(key + "." + self.token + ".pending")
        assert pending.exists()
        staged.append((kind, guarded, key, final, pending))
        events.append(("stage", kind, key))

    monkeypatch.setattr(sandbox, "capture", capture)
    monkeypatch.setattr(controller.PendingRecords, "stage", stage)
    return events, mutations, staged


def group_for(report, name):
    case = case_id(name)
    return next(group for group in report["groups"] if group["case_ids"] == [case])


def valid_record(root, group, events):
    key = group["group_id"]
    request = next(event for event in events if event[0] == "worker" and event[2] == key)
    assert request[1] in ("a", "b", "c")
    path = root / ".pytest-telescope/cache" / (key + ".json")
    record = json.loads(path.read_text())
    assert set(record) == {"schema", "key", "result", "checksum"}
    assert record["schema"] == 8 and record["key"] == key
    result = record["result"]
    assert (
        controller.read_record(
            path,
            key,
            kind="guarded-batch",
            group_id=key,
            case_ids=group["case_ids"],
            registry_id=result["registry_id"],
            graph_id=result["graph_id"],
        )
        == result
    )
    return path


@pytest.mark.requires_fixture_guard
def test_review_h1_recovery_publishes_and_warm_reuses(tmp_path, monkeypatch):
    configure(tmp_path, ("a", "b"))
    assert not (tmp_path / ".pytest-telescope/cache").exists()
    events, mutations, staged = observe_transport(monkeypatch, ("a", "b"), 1)
    report = controller.run(
        tmp_path, execution_mode="batch", fixture_guard="available-v1", arguments=["-s"]
    )
    assert report["schema"] == 5 and report["kind"] == "run" and report["status"] == "passed"
    assert report["fixture_guard"]["state"] == "admitted"
    a, b = (group_for(report, name) for name in ("a", "b"))
    assert mutations == [b["group_id"]]
    assert [event[1] for event in events if event[0] == "worker"] == ["a", "b", "b"]
    assert a["attempts"] == 1 and b["attempts"] == 2
    assert all(group["complete"] and group["cache_eligible"] for group in (a, b))
    assert all(
        next(unit for unit in report["units"] if unit["case_id"] == case_id(name))["result_status"]
        == "passed"
        for name in ("a", "b")
    )
    assert {entry[2] for entry in staged if entry[1]} == {a["group_id"], b["group_id"]}
    for group in (a, b):
        valid_record(tmp_path, group, events)
    worker_count = sum(event[0] == "worker" for event in events)
    warm = controller.run(
        tmp_path, execution_mode="batch", fixture_guard="available-v1", arguments=["-s"]
    )
    assert warm["status"] == "passed" and warm["counts"]["reused"] == 2
    assert all(group_for(warm, name)["outcome"] == "reused" for name in ("a", "b"))
    assert sum(event[0] == "worker" for event in events) == worker_count


@pytest.mark.requires_fixture_guard
def test_review_h1_exhaustion_discards_all_new_staged_successes(tmp_path, monkeypatch):
    configure(tmp_path, ("a", "b", "c"))
    assert not (tmp_path / ".pytest-telescope/cache").exists()
    events, mutations, staged = observe_transport(monkeypatch, ("a", "b", "c"), 2)
    report = controller.run(
        tmp_path, execution_mode="batch", fixture_guard="available-v1", arguments=["-s"]
    )
    assert report["schema"] == 5 and report["kind"] == "run"
    assert (
        report["fixture_guard"]["state"] == "admitted"
        and report["status"] == "infrastructure_error"
    )
    a, b = (group_for(report, name) for name in ("a", "b"))
    assert mutations == [b["group_id"], b["group_id"]]
    assert [event[1] for event in events if event[0] == "worker"][:3] == ["a", "b", "b"]
    assert events.index(("stage", "execution", a["group_id"])) < events.index(
        ("worker", "b", b["group_id"])
    )
    assert a["attempts"] == 1 and a["complete"]
    assert (
        next(unit for unit in report["units"] if unit["case_id"] == case_id("a"))["result_status"]
        == "passed"
    )
    assert b["attempts"] == 2 and b["unresolved"] == 1 and not b["complete"]
    assert any(entry[1] and entry[2] == a["group_id"] for entry in staged)
    print("H1_DISPATCHES=" + ",".join(event[1] for event in events if event[0] == "worker"))
    print("H1_STAGED=" + repr([(kind, guarded, key) for kind, guarded, key, _, _ in staged]))
    print(
        "H1_GROUPS="
        + repr([(g["group_id"], g["attempts"], g["cache_eligible"]) for g in report["groups"]])
    )
    print(
        "H1_CACHE_FILES="
        + repr(
            sorted(
                str(p.relative_to(tmp_path))
                for p in (tmp_path / ".pytest-telescope/cache").rglob("*.json")
            )
        )
    )
    assert not any(group["cache_eligible"] for group in report["groups"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
    assert all(not final.exists() and not pending.exists() for _, _, _, final, pending in staged)


def condition_project(root):
    configure(root, ("selected",))
    put(
        root,
        "tests/test_selected.py",
        'import pytest\npytestmark = pytest.mark.telescope(env=["REVIEW_CONDITION"])\ndef test_item(): pass\n',
    )


def condition(case=None, name="REVIEW_CONDITION"):
    return {"case_id": case or case_id("selected"), "name": name, "op": "present"}


def empty_boundary(root):
    manifest = controller.load(root, structural_files={"default": []})
    assert not manifest.scope_possible and not manifest.scoped_files["default"]


@pytest.mark.requires_fixture_guard
def test_review_h2_empty_without_conditions_is_no_tests(tmp_path, monkeypatch):
    condition_project(tmp_path)
    empty_boundary(tmp_path)
    calls = []
    monkeypatch.setattr(sandbox, "execute", lambda *args, **kwargs: calls.append("sandbox"))
    monkeypatch.setattr(ordinary, "session", lambda *args, **kwargs: calls.append("ordinary"))
    report = controller.run(
        tmp_path,
        execution_mode="batch",
        fixture_guard="available-v1",
        structural_files={"default": []},
        condition_checks=[],
    )
    assert report["schema"] == 5 and report["kind"] == "run" and report["status"] == "no_tests"
    assert report["counts"]["selected"] == 0 and not calls
    assert not (tmp_path / ".pytest-telescope/cache").exists()


@pytest.mark.requires_fixture_guard
def test_review_h2_empty_condition_rejects_unselected_case(tmp_path, monkeypatch):
    condition_project(tmp_path)
    empty_boundary(tmp_path)
    calls = []
    monkeypatch.setattr(sandbox, "execute", lambda *args, **kwargs: calls.append("sandbox"))
    monkeypatch.setattr(ordinary, "session", lambda *args, **kwargs: calls.append("ordinary"))
    with pytest.raises(DeclarationError) as refused:
        controller.run(
            tmp_path,
            execution_mode="batch",
            fixture_guard="available-v1",
            structural_files={"default": []},
            condition_checks=[condition()],
        )
    assert type(refused.value) is DeclarationError
    assert not calls and not (tmp_path / ".pytest-telescope/cache").exists()


@pytest.mark.requires_fixture_guard
def test_review_h2_selected_and_invalid_conditions(tmp_path, monkeypatch):
    condition_project(tmp_path)
    monkeypatch.setenv("REVIEW_CONDITION", "synthetic-present")
    report = controller.run(
        tmp_path,
        execution_mode="batch",
        fixture_guard="available-v1",
        condition_checks=[condition()],
    )
    assert report["status"] == "passed" and report["counts"]["selected"] == 1
    assert report["fixture_guard"]["conditions"][0]["matched"] is True
    for invalid in (condition("a" * 64), condition(name="REVIEW_UNDECLARED")):
        with pytest.raises(DeclarationError):
            controller.run(
                tmp_path,
                execution_mode="batch",
                fixture_guard="available-v1",
                condition_checks=[invalid],
            )


def test_review_h2_default_unguarded_schema_and_cache(tmp_path):
    configure(tmp_path, ("a",))
    first = controller.run(tmp_path, execution_mode="batch")
    warm = controller.run(tmp_path, execution_mode="batch")
    assert first["schema"] == 4 and first["status"] == "passed"
    assert "fixture_guard" not in first and warm["counts"]["reused"] == 1
    records = [
        json.loads(path.read_text())
        for path in (tmp_path / ".pytest-telescope/cache").glob("*.json")
    ]
    assert records and all(record["schema"] == 8 for record in records)
