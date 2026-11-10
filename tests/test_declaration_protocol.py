"""Explicit declaration flags from real scoped workers and strict cache readers."""

import copy
import json

import pytest

from pytest_telescope import controller, ordinary, result, sandbox
from pytest_telescope.model import DeclarationError, fingerprint


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def project(root, *, code=0, collection=False, changed=False):
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    hook = f"""def pytest_sessionfinish(session):
    if bool(session.config.option.collectonly) == {collection!r}:
        session.exitstatus = {code}
"""
    if changed:
        hook += """import pytest
def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.telescope(inputs=['unexpected.txt']))
"""
    put(root, "conftest.py", hook)
    put(
        root,
        "tests/test_case.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=['conftest.py'])\ndef test_case(): assert 6 * 7 == 42\n",
    )


def observed_run(root, monkeypatch, *, mode="case"):
    original = sandbox.execute
    records = []

    def execute(*args, request, **kwargs):
        actual = original(*args, request=request, **kwargs)
        records.append((copy.deepcopy(request), copy.deepcopy(actual)))
        return actual

    monkeypatch.setattr(sandbox, "execute", execute)
    try:
        report = controller.run(root, execution_mode=mode, timeout=20)
    except DeclarationError as error:
        report = controller.error_result(error)
    return report, records


@pytest.fixture(scope="module", params=["case", "batch"])
def actual_records(tmp_path_factory, request):
    root = tmp_path_factory.mktemp("declaration-protocol")
    project(root)
    with pytest.MonkeyPatch.context() as patch:
        report, records = observed_run(root, patch, mode=request.param)
    assert report["kind"] == "run" and report["status"] == "passed", report
    return request.param, records


def validate(request, record):
    if request["mode"] == "collection":
        return result.validate_collection(record, context_id=request["context_id"])
    if request["mode"] == "batch":
        return result.validate_batch(
            record, group_id=request["group_id"], case_ids=request["case_ids"]
        )
    return result.validate_result(
        record, case_id=request["case_id"], context_id=request["context_id"]
    )


def test_actual_worker_records_have_strict_declaration_boolean_and_versions(actual_records):
    mode, records = actual_records
    assert [record["protocol"] for _, record in records] == [11, 9 if mode == "case" else 10]
    for request, record in records:
        assert record["declaration_error"] is False
        assert validate(request, record)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "integer", "string", "true-zero", "true-three", "true-complete", "unknown"],
)
def test_invalid_declaration_flags_reject_actual_record_derivatives(actual_records, mutation):
    _, records = actual_records
    for request, original in records:
        record = copy.deepcopy(original)
        if mutation == "missing":
            record.pop("declaration_error")
        elif mutation == "integer":
            record["declaration_error"] = 0
        elif mutation == "string":
            record["declaration_error"] = "false"
        elif mutation == "unknown":
            record["unknown_declaration"] = False
        else:
            record.update(
                declaration_error=True,
                exit_code={"true-zero": 0, "true-three": 3, "true-complete": 4}[mutation],
            )
            if mutation != "true-complete":
                record["complete"] = False
        assert not validate(request, record)


def test_old_scoped_protocols_and_old_cache_envelopes_are_ineligible(actual_records, tmp_path):
    mode, records = actual_records
    for request, original in records:
        for protocol in range(original["protocol"]):
            old = copy.deepcopy(original)
            old["protocol"] = protocol
            assert not validate(request, old)
        key = request["context_id"]
        path = tmp_path / (key + ".json")
        arguments = (
            {"kind": "collection"}
            if request["mode"] == "collection"
            else (
                {"kind": "batch", "group_id": request["group_id"], "case_ids": request["case_ids"]}
                if mode == "batch"
                else {"case_id": request["case_id"]}
            )
        )
        for schema, accepted in [(8, True), *((old, False) for old in range(8))]:
            path.write_text(
                json.dumps(
                    {
                        "schema": schema,
                        "key": key,
                        "result": original,
                        "checksum": fingerprint(original),
                    }
                )
            )
            assert (controller.read_record(path, key, **arguments) is not None) is accepted


@pytest.mark.parametrize("code", [3, 4])
@pytest.mark.parametrize("mode", ["case", "batch"])
def test_valid_nonzero_session_preserves_actual_passes_without_declaration_refusal(
    tmp_path, monkeypatch, code, mode
):
    project(tmp_path, code=code)
    report, records = observed_run(tmp_path, monkeypatch, mode=mode)
    request, actual = records[-1]
    assert actual["exit_code"] == code and actual["passed"] == 1 and actual["complete"]
    assert actual["declaration_error"] is False and validate(request, actual)
    assert report["kind"] == "run" and report["status"] == "failed", report
    assert report["cases"]["passed"] == 1
    assert report["units"][0]["reports"] == actual["case_results"][0]["reports"]
    assert report["groups"][0]["outcome"] == "failed" and not report["groups"][0]["cache_eligible"]


def test_valid_collection_exit_four_is_failed_collection(tmp_path, monkeypatch):
    project(tmp_path, code=4, collection=True)
    report, records = observed_run(tmp_path, monkeypatch)
    assert len(records) == 1
    request, actual = records[0]
    assert actual["declaration_error"] is False and actual["exit_code"] == 4
    assert not actual["complete"] and validate(request, actual)
    assert report["kind"] == "run" and report["status"] == "failed", report
    assert report["collection"]["outcome"] == "failed"


def test_actual_changed_declaration_sets_true_and_keeps_typed_refusal(tmp_path, monkeypatch):
    project(tmp_path, changed=True)
    report, records = observed_run(tmp_path, monkeypatch)
    request, actual = records[-1]
    assert actual["declaration_error"] is True and actual["exit_code"] == 4
    assert not actual["complete"] and validate(request, actual)
    assert report["kind"] == "refusal" and report["status"] == "declaration_error", report


def test_ordinary_protocol_keeps_its_field_and_strict_coherence(tmp_path, monkeypatch):
    put(tmp_path, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    put(tmp_path, "tests/test_case.py", "def test_case(): pass\n")
    original, records = ordinary.session, []

    def session(*args, **kwargs):
        actual = original(*args, **kwargs)
        records.append(copy.deepcopy(actual))
        return actual

    monkeypatch.setattr(ordinary, "session", session)
    assert controller.run(tmp_path, timeout=20)["status"] == "passed"
    (actual,) = records
    assert actual["protocol"] == 4 and actual["declaration_error"] is False
    assert result.validate_ordinary(actual)
    for protocol in (0, 1, 2, 3, 5, True):
        assert not result.validate_ordinary({**actual, "protocol": protocol})
    for flag in (0, "false", None):
        invalid = {**actual, "declaration_error": flag}
        assert not result.validate_ordinary(invalid)
    missing = copy.deepcopy(actual)
    del missing["declaration_error"]
    assert not result.validate_ordinary(missing)
    assert not result.validate_ordinary({**actual, "declaration_error": True, "exit_code": 4})
    assert not result.validate_ordinary(
        {**actual, "declaration_error": True, "exit_code": 4, "complete": False}
    )
