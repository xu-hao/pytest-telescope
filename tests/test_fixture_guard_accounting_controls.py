"""Guarded typed-refusal accounting, real drain and strict producer controls."""

import copy
import json
from dataclasses import FrozenInstanceError
from threading import Event

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.accounting import RefusalAccounting
from pytest_telescope.model import DeclarationError, InfrastructureError
from pytest_telescope.result import validate_guarded_batch
from test_fixture_guard_declaration_protocol import case_id, group, observe, project, put, unit


def changing_project(root, *, extra=False):
    project(root, 0)
    put(
        root,
        "tests/conftest.py",
        "import os\nimport pytest\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if not config.option.collectonly and os.getenv('GUARD_DECL_CHANGE'):\n"
        "        for item in items:\n"
        "            if item.nodeid.startswith('tests/test_b.py::'):\n"
        "                item.add_marker(pytest.mark.telescope(inputs=[]))\n",
    )
    put(
        root,
        "tests/test_b.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"], env=["GUARD_DECL_CHANGE"])\n'
        "def test_item(): pass\n",
    )
    if extra:
        put(
            root,
            "tests/test_c.py",
            'import os\nimport pytest\npytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])\n'
            'def test_item(): os.write(1, b"GUARD_DECL_C\\n")\n',
        )


def metadata(observations):
    result = controller.error_result(DeclarationError("synthetic declaration"), fixture_guard="available-v1")
    result["inventory_complete"] = observations["inventory_complete"]
    result["inventory_digest"] = observations["inventory_digest"]
    result["fixture_guard"] = copy.deepcopy(observations["fixture_guard"])
    return result


def test_prior_guarded_cache_survives_real_late_refusal(tmp_path, monkeypatch):
    changing_project(tmp_path)
    monkeypatch.delenv("GUARD_DECL_CHANGE", raising=False)
    opts = {"fixture_guard": "available-v1", "workers": 1, "arguments": ["-s"]}
    warm = controller.run(tmp_path, **opts)
    assert warm["status"] == "passed"
    a, b = group(warm, "a"), group(warm, "b")
    cache = tmp_path / ".pytest-telescope/cache"
    path_a = cache / (a["group_id"] + ".json")
    path_b = cache / (b["group_id"] + ".json")
    assert path_a.exists() and path_b.exists()
    path_b.unlink()
    prior = {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*.json")}
    monkeypatch.setenv("GUARD_DECL_CHANGE", "synthetic-present")
    records = observe(monkeypatch)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, **opts)
    assert [name for name, _, _ in records] == ["b"]
    assert records[0][2]["declaration_error"] is True and records[0][2]["exit_code"] == 4
    retained = captured.value.accounting.render()["observations"]
    assert group(retained, "a")["outcome"] == "reused" and group(retained, "a")["attempts"] == 0
    assert group(retained, "a")["cache_eligible"] and unit(retained, "a")["result_status"] == "passed"
    assert group(retained, "b")["attempts"] == 1
    assert {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*.json")} == prior
    assert path_a.read_bytes() == prior[path_a.relative_to(cache)] and not list(cache.rglob("*.pending"))


def test_real_active_sibling_is_drained_before_typed_refusal(tmp_path, monkeypatch):
    changing_project(tmp_path, extra=True)
    monkeypatch.setenv("GUARD_DECL_CHANGE", "synthetic-present")
    real_execute = sandbox.execute
    real_stage = controller.PendingRecords.stage
    a_staged, c_done, c_drained = Event(), Event(), Event()
    observed = []

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        real_stage(self, final, key, result, kind=kind, guarded=guarded)
        if guarded and result["case_ids"] == [case_id("a")]:
            a_staged.set()

    def execute(*args, request, **kwargs):
        result = real_execute(*args, request=request, **kwargs)
        if request["mode"] != "guard-batch":
            return result
        assert len(request["case_ids"]) == 1
        name = {case_id(n): n for n in ("a", "b", "c")}[request["case_ids"][0]]
        assert validate_guarded_batch(result, group_id=request["group_id"], case_ids=request["case_ids"], registry_id=request["registry_id"], graph_id=request["graph_id"])
        observed.append((name, result["declaration_error"], result["exit_code"]))
        if name == "b":
            assert result["declaration_error"] is True and result["exit_code"] == 4
            assert a_staged.wait(30) and c_done.wait(30)
        if name == "c":
            assert result["exit_code"] == 0 and result["case_results"][0]["outcome"] == "passed"
            c_done.set()
            assert kwargs["cancellation"].wait(30)
            c_drained.set()
        return result

    monkeypatch.setattr(controller.PendingRecords, "stage", stage)
    monkeypatch.setattr(sandbox, "execute", execute)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, fixture_guard="available-v1", workers=2, arguments=["-s"])
    assert a_staged.is_set() and c_done.is_set() and c_drained.is_set()
    assert sorted(name for name, _, _ in observed) == ["a", "b", "c"]
    retained = captured.value.accounting.render()["observations"]
    assert group(retained, "a")["attempts"] == group(retained, "b")["attempts"] == group(retained, "c")["attempts"] == 1
    assert unit(retained, "a")["result_status"] == unit(retained, "c")["result_status"] == "passed"
    assert not any(g["cache_eligible"] for g in retained["groups"])
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.pending"))
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


@pytest.mark.parametrize("started", [False, True])
def test_simulated_typed_refusal_without_worker_result_keeps_start_truth(tmp_path, monkeypatch, started):
    project(tmp_path, 0)
    original = sandbox.execute
    events = []

    def execute(*args, request, **kwargs):
        if request["mode"] == "guard-batch" and request["case_ids"] == [case_id("b")]:
            events.append("simulated-refusal")
            if started:
                kwargs["on_session_start"]()
                events.append("simulated-start")
            raise DeclarationError("synthetic typed refusal without worker result")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert events == (["simulated-refusal", "simulated-start"] if started else ["simulated-refusal"])
    observed = captured.value.accounting.render()["observations"]
    b, member = group(observed, "b"), unit(observed, "b")
    assert unit(observed, "a")["result_status"] == "passed"
    assert b["attempts"] == int(started) and member["attempts"] == int(started)
    assert b["unresolved"] == int(started) and b["not_run"] == int(not started)
    assert member["result_status"] == (None if started else "not-run")
    assert not list((tmp_path / ".pytest-telescope/cache").rglob("*.json"))


def sample_accounting(root, monkeypatch):
    project(root, 0)
    original = sandbox.execute

    def execute(*args, request, **kwargs):
        if request["mode"] == "guard-batch" and request["case_ids"] == [case_id("b")]:
            kwargs["on_session_start"]()
            raise DeclarationError("synthetic typed refusal after simulated start")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    with pytest.raises(DeclarationError) as captured:
        controller.run(root, fixture_guard="available-v1", workers=1)
    return captured.value.accounting.render()["observations"]


def test_accounting_strict_guard_relations_and_immutable_copies(tmp_path, monkeypatch):
    observations = sample_accounting(tmp_path, monkeypatch)
    meta = metadata(observations)
    value = RefusalAccounting(observations, metadata=meta, maximum=1024 * 1024)
    assert value.render()["version"] == 2
    first = value.render()
    first["observations"]["fixture_guard"]["state"] = "unavailable"
    assert value.render()["observations"]["fixture_guard"]["state"] == "admitted"
    guard = value.fixture_guard
    guard["conditions"].append({})
    assert value.fixture_guard == observations["fixture_guard"]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.inventory_complete = False
    for fault in ("extra", "profile", "state", "inventory", "member", "group", "condition", "matched"):
        bad = copy.deepcopy(observations)
        badmeta = copy.deepcopy(meta)
        if fault == "extra":
            bad["unknown"] = 1
        elif fault == "profile":
            bad["execution"]["profile"] = "batch-v2"
        elif fault == "state":
            bad["fixture_guard"]["state"] = "other"
            badmeta["fixture_guard"] = bad["fixture_guard"]
        elif fault == "inventory":
            bad["inventory_digest"] = "a" * 64
        elif fault == "member":
            bad["groups"][0]["case_ids"] = ["f" * 64]
        elif fault == "group":
            bad["units"][0]["group_id"] = "f" * 64
        else:
            bad["fixture_guard"]["conditions"] = [{"case_id": case_id("a"), "group_id": group(bad, "a")["group_id"], "matched": fault != "matched"}]
            if fault == "condition":
                bad["fixture_guard"]["conditions"][0]["case_id"] = "f" * 64
            badmeta["fixture_guard"] = bad["fixture_guard"]
        with pytest.raises(ValueError):
            RefusalAccounting(bad, metadata=badmeta, maximum=1024 * 1024)


def test_guard_bounds_unavailable_and_default_unavailable_refusal(tmp_path, monkeypatch):
    observations = sample_accounting(tmp_path, monkeypatch)
    meta = metadata(observations)
    maximum = len(json.dumps(meta, indent=2).encode()) + 1024
    bounded = RefusalAccounting(observations, metadata=meta, maximum=maximum)
    assert bounded.render() == {"version": 2, "state": "unavailable", "reason": "bounds"}
    assert bounded.fixture_guard == {"profile": "available-v1", "state": "unavailable", "conditions": []}
    assert bounded.inventory_complete == observations["inventory_complete"]
    assert bounded.inventory_digest == observations["inventory_digest"]
    error = DeclarationError("synthetic declaration", accounting=bounded)
    envelope = controller.error_result(error, fixture_guard="available-v1")
    assert envelope["accounting"] == bounded.render() and envelope["fixture_guard"] == bounded.fixture_guard
    early = controller.error_result(DeclarationError("synthetic early"), fixture_guard="available-v1")
    assert "accounting" not in early and early["fixture_guard"] == bounded.fixture_guard
    default = controller.error_result(DeclarationError("synthetic early"))
    assert default["schema"] == 4 and "fixture_guard" not in default and "accounting" not in default
    with pytest.raises(InfrastructureError):
        controller.error_result(error)


def test_unguarded_late_typed_refusal_retains_version_one(tmp_path, monkeypatch):
    project(tmp_path, 0)
    original = sandbox.execute

    def execute(*args, request, **kwargs):
        if request["mode"] == "batch" and request["case_ids"] == [case_id("b")]:
            kwargs["on_session_start"]()
            raise DeclarationError("synthetic unguarded typed refusal")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, workers=1)
    retained = captured.value.accounting.render()
    assert retained["version"] == 1 and retained["state"] == "available"
    observations = retained["observations"]
    assert observations["execution"]["profile"] == "batch-v2"
    assert "fixture_guard" not in observations
    assert unit(observations, "a")["result_status"] == "passed"
    assert group(observations, "b")["attempts"] == 1 and group(observations, "b")["unresolved"] == 1
    envelope = controller.error_result(captured.value)
    assert envelope["schema"] == 4 and envelope["accounting"] == retained
    assert "fixture_guard" not in envelope


def test_distinct_matching_conditions_can_have_identical_public_rows(tmp_path, monkeypatch):
    project(tmp_path, 0)
    put(
        tmp_path,
        "tests/test_a.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"], env=["GUARD_MULTI"])\n'
        "def test_item(): pass\n",
    )
    monkeypatch.setenv("GUARD_MULTI", "synthetic-value")
    original = sandbox.execute

    def execute(*args, request, **kwargs):
        if request["mode"] == "guard-batch" and request["case_ids"] == [case_id("b")]:
            kwargs["on_session_start"]()
            raise DeclarationError("synthetic typed refusal")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    conditions = [
        {"case_id": case_id("a"), "name": "GUARD_MULTI", "op": "present"},
        {"case_id": case_id("a"), "name": "GUARD_MULTI", "op": "equals", "value": "synthetic-value"},
    ]
    with pytest.raises(DeclarationError) as captured:
        controller.run(tmp_path, fixture_guard="available-v1", workers=1, condition_checks=conditions)
    retained = captured.value.accounting.render()["observations"]
    rows = retained["fixture_guard"]["conditions"]
    assert len(rows) == 2 and rows[0] == rows[1]
    assert rows[0] == {"case_id": case_id("a"), "group_id": group(retained, "a")["group_id"], "matched": True}
    assert "synthetic-value" not in json.dumps(controller.error_result(captured.value, fixture_guard="available-v1"))


def test_outer_inventory_metadata_types_are_not_silently_repaired(tmp_path, monkeypatch):
    observations = sample_accounting(tmp_path, monkeypatch)
    reserved = metadata(observations)
    reserved["inventory_complete"] = False
    reserved["inventory_digest"] = None
    valid = RefusalAccounting(observations, metadata=reserved, maximum=1024 * 1024)
    assert valid.render()["state"] == "available"
    for field, invalid in (("inventory_complete", "false"), ("inventory_complete", 1), ("inventory_digest", 0), ("inventory_digest", "bad")):
        malformed = copy.deepcopy(reserved)
        malformed[field] = invalid
        with pytest.raises(ValueError):
            RefusalAccounting(observations, metadata=malformed, maximum=1024 * 1024)
