"""Guarded declaration flag controls using actual scoped Bubblewrap sessions."""

import copy
import hashlib
import json
from threading import Event

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, fingerprint
from pytest_telescope.result import validate_guarded_batch


def case_id(name):
    node = f"tests/test_{name}.py::test_item"
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def put(root, path, contents):
    path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def project(root, code):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    put(
        root,
        "tests/conftest.py",
        "def pytest_sessionfinish(session):\n"
        "    if not session.config.option.collectonly and any(\n"
        "        item.nodeid.startswith('tests/test_b.py::') for item in session.items\n"
        "    ):\n"
        f"        session.exitstatus = {code}\n",
    )
    for name in ("a", "b"):
        put(
            root,
            f"tests/test_{name}.py",
            "import os\nimport pytest\n"
            'pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])\n'
            "@pytest.fixture\ndef local():\n    yield object()\n"
            f'def test_item(local): os.write(1, b"GUARD_DECL_{name.upper()}\\n")\n',
        )


def group(report, name):
    return next(g for g in report["groups"] if g["case_ids"] == [case_id(name)])


def unit(report, name):
    return next(u for u in report["units"] if u["case_id"] == case_id(name))


def observe(monkeypatch, *, synchronize=False, started=None):
    actual_execute = sandbox.execute
    done_a = Event()
    records = []
    ids = {case_id(name): name for name in ("a", "b")}

    def execute(*args, request, **kwargs):
        if request["mode"] == "guard-batch" and started is not None:
            callback = kwargs["on_session_start"]

            def on_start():
                started.append(request["case_ids"][0])
                callback()

            kwargs["on_session_start"] = on_start
        value = actual_execute(*args, request=request, **kwargs)
        if request["mode"] == "guard-batch":
            assert len(request["case_ids"]) == 1 and request["case_ids"][0] in ids
            name = ids[request["case_ids"][0]]
            assert value["protocol"] == 9 and len(value["nonce"]) == 64
            assert validate_guarded_batch(
                value,
                group_id=request["group_id"],
                case_ids=request["case_ids"],
                registry_id=request["registry_id"],
                graph_id=request["graph_id"],
            )
            records.append((name, copy.deepcopy(request), copy.deepcopy(value)))
            print("GUARD_RECORD", name, request["group_id"], value["exit_code"], value["declaration_error"])
            if name == "a":
                done_a.set()
            elif synchronize:
                assert done_a.wait(30), "A's real session did not complete"
        return value

    monkeypatch.setattr(sandbox, "execute", execute)
    return records


def check_failed_session(root, monkeypatch, code, workers):
    project(root, code)
    records = observe(monkeypatch, synchronize=workers == 2)
    report = controller.run(root, fixture_guard="available-v1", workers=workers, arguments=["-s"])
    assert {name for name, _, _ in records} == {"a", "b"}
    a_request, a = next((request, value) for name, request, value in records if name == "a")
    b_request, b = next((request, value) for name, request, value in records if name == "b")
    assert a_request["group_id"] != b_request["group_id"]
    assert a["declaration_error"] is False and a["exit_code"] == 0 and a["complete"]
    assert b["declaration_error"] is False and b["exit_code"] == code and b["complete"]
    assert b["passed"] == 1 and b["case_results"][0]["outcome"] == "passed"
    assert [row["phase"] for row in b["case_results"][0]["reports"]] == ["setup", "call", "teardown"]
    print("GUARD_FALSE_SESSION", code, workers, [(name, req["group_id"], value["exit_code"], value["declaration_error"]) for name, req, value in records])
    assert report["schema"] == 5 and report["kind"] == "run" and report["status"] == "failed"
    assert report["fixture_guard"]["state"] == "admitted"
    assert group(report, "a")["attempts"] == group(report, "b")["attempts"] == 1
    assert group(report, "a")["cache_eligible"] and not group(report, "b")["cache_eligible"]
    assert group(report, "b")["outcome"] == "failed"
    assert unit(report, "b")["result_status"] == "passed"
    assert unit(report, "b")["reports"] == b["case_results"][0]["reports"]
    before = len(records)
    warm = controller.run(root, fixture_guard="available-v1", workers=workers, arguments=["-s"])
    assert warm["status"] == "failed" and group(warm, "a")["outcome"] == "reused"
    assert group(warm, "a")["attempts"] == 0 and group(warm, "b")["attempts"] == 1
    assert [name for name, _, _ in records[before:]] == ["b"]


def test_guard_false_exit_four_keeps_pass_and_independent_sibling_serial(tmp_path, monkeypatch):
    check_failed_session(tmp_path, monkeypatch, code=4, workers=1)


def test_guard_false_exit_four_keeps_pass_and_independent_sibling_parallel(tmp_path, monkeypatch):
    check_failed_session(tmp_path, monkeypatch, code=4, workers=2)


def test_guard_false_exit_three_keeps_pass_and_independent_sibling(tmp_path, monkeypatch):
    check_failed_session(tmp_path, monkeypatch, code=3, workers=1)


def test_actual_changed_guard_declaration_remains_typed_refusal(tmp_path, monkeypatch):
    project(tmp_path, 0)
    monkeypatch.setenv("GUARD_ACCOUNTING_PRESENT", "synthetic-value")
    put(
        tmp_path,
        "tests/test_a.py",
        "import os\nimport pytest\n"
        'pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"], env=["GUARD_ACCOUNTING_PRESENT"])\n'
        'def test_item(): os.write(1, b"GUARD_DECL_A\\n")\n',
    )
    put(
        tmp_path,
        "tests/conftest.py",
        "import pytest\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if not config.option.collectonly:\n"
        "        for item in items:\n"
        "            if item.nodeid.startswith('tests/test_b.py::'):\n"
        "                item.add_marker(pytest.mark.telescope(inputs=[]))\n",
    )
    starts = []
    records = observe(monkeypatch, started=starts)
    staged = []
    real_stage = controller.PendingRecords.stage

    def stage(self, final, key, value, *, kind="execution", guarded=False):
        real_stage(self, final, key, value, kind=kind, guarded=guarded)
        staged.append((kind, guarded, key, final))

    monkeypatch.setattr(controller.PendingRecords, "stage", stage)
    refusal = None
    returned = None
    try:
        returned = controller.run(
            tmp_path,
            fixture_guard="available-v1",
            workers=1,
            arguments=["-s"],
            condition_checks=[{"case_id": case_id("a"), "name": "GUARD_ACCOUNTING_PRESENT", "op": "present"}],
        )
    except DeclarationError as error:
        refusal = error
    assert [name for name, _, _ in records] == ["a", "b"]
    a_request, a = records[0][1:]
    b_request, b = records[1][1:]
    assert starts == [case_id("a"), case_id("b")]
    assert a["exit_code"] == 0 and a["case_results"][0]["outcome"] == "passed"
    assert [row["phase"] for row in a["case_results"][0]["reports"]] == ["setup", "call", "teardown"]
    assert b["declaration_error"] is True and b["exit_code"] == 4 and not b["complete"]
    assert a_request["group_id"] != b_request["group_id"]
    assert any(kind == "execution" and guarded and key == a_request["group_id"] for kind, guarded, key, _ in staged)
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.pending"))
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())
    print("GUARD_TRUE_PRECONDITIONS", [(name, req["group_id"], value["declaration_error"]) for name, req, value in records], "starts", starts, "returned_status", returned["status"] if returned else None)
    assert type(refusal) is DeclarationError, "known declaration refusal was not raised"
    retained = refusal.accounting.render()
    assert retained["version"] == 2 and retained["state"] == "available"
    observations = retained["observations"]
    assert observations["execution"]["profile"] == "batch-available-v1"
    assert observations["fixture_guard"] == {"profile": "available-v1", "state": "admitted", "conditions": [{"case_id": case_id("a"), "group_id": a_request["group_id"], "matched": True}]}
    assert group(observations, "a")["attempts"] == 1 and unit(observations, "a")["result_status"] == "passed"
    assert unit(observations, "a")["reports"] == a["case_results"][0]["reports"]
    assert group(observations, "b")["attempts"] == 1
    assert unit(observations, "b")["reports"] == b["case_results"][0]["reports"]
    envelope = controller.error_result(refusal, fixture_guard="available-v1")
    assert envelope["schema"] == 5 and envelope["status"] == "declaration_error"
    assert envelope["accounting"] == retained
    assert envelope["fixture_guard"] == observations["fixture_guard"]
    assert envelope["inventory_complete"] == observations["inventory_complete"]
    assert envelope["inventory_digest"] == observations["inventory_digest"]
    assert "synthetic-value" not in json.dumps(envelope)


def test_guard_success_record_validates_strict_flag_and_cache_protocol(tmp_path, monkeypatch):
    project(tmp_path, 0)
    records = observe(monkeypatch)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1, arguments=["-s"])
    assert report["status"] == "passed" and {name for name, _, _ in records} == {"a", "b"}
    _, request, value = records[0]

    def valid(candidate):
        return validate_guarded_batch(
            candidate,
            group_id=request["group_id"],
            case_ids=request["case_ids"],
            registry_id=request["registry_id"],
            graph_id=request["graph_id"],
        )

    assert value["declaration_error"] is False and value["protocol"] == 9 and valid(value)
    for mutation in ("missing", "integer", "string", "true-zero", "true-three", "true-complete", "old-protocol"):
        altered = copy.deepcopy(value)
        if mutation == "missing":
            del altered["declaration_error"]
        elif mutation == "integer":
            altered["declaration_error"] = 0
        elif mutation == "string":
            altered["declaration_error"] = "false"
        elif mutation == "old-protocol":
            altered["protocol"] = 6
        else:
            altered.update(declaration_error=True, exit_code={"true-zero": 0, "true-three": 3, "true-complete": 4}[mutation])
            if mutation != "true-complete":
                altered["complete"] = False
        assert not valid(altered)
    key = request["group_id"]
    path = tmp_path / ("protocol-control-" + key + ".json")
    arguments = dict(kind="guarded-batch", group_id=key, case_ids=request["case_ids"], registry_id=request["registry_id"], graph_id=request["graph_id"])
    for schema, allowed in ((7, True), (6, False)):
        path.write_text(json.dumps({"schema": schema, "key": key, "result": value, "checksum": fingerprint(value)}))
        assert (controller.read_record(path, key, **arguments) is not None) is allowed
