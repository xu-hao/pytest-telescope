"""Explicit synthetic launch events distinguish calls, starts and retries."""

import copy

import pytest
from test_cache_contract import success
from test_worker_contract import collection_record

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, InfrastructureError


@pytest.mark.parametrize("scenario,expected_calls,expected_starts", [
    ("before-refusal", 1, 0),
    ("after-refusal", 1, 1),
    ("before-cancel", 1, 0),
    ("normal", 1, 1),
    ("started-then-prestart-failure", 2, 1),
    ("started-then-started-retry", 2, 2),
])
def test_controller_counts_explicit_launches_not_execute_entries(
    tmp_path, sandbox_double, module_rows, scenario, expected_calls, expected_starts
):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    rows = module_rows(tmp_path, [("test_one.py", ["one"])])
    calls, starts = [], []

    def execute(*args, request, **kwargs):
        if request["mode"] == "collection":
            result = copy.deepcopy(collection_record())
            result.update(context_id=request["context_id"], items=rows)
            return result
        calls.append(request["case_id"])
        if scenario == "before-refusal":
            raise DeclarationError("owned pre-start refusal")
        if scenario == "before-cancel":
            raise sandbox.InvocationInterrupted("owned pre-start cancellation")
        if scenario == "started-then-prestart-failure" and len(calls) == 2:
            raise InfrastructureError("owned retry failed before start")
        # This double explicitly models successful Popen, not function entry.
        kwargs["on_session_start"]()
        starts.append(request["case_id"])
        if scenario == "after-refusal":
            raise DeclarationError("owned post-start unavailable result")
        if scenario.startswith("started-then-") and len(calls) == 1:
            raise InfrastructureError("owned first worker result unavailable")
        result = success()
        result.update(context_id=request["context_id"], case_ids=[request["case_id"]])
        result["case_results"][0]["case_id"] = request["case_id"]
        return result

    active = sandbox_double(execute)
    try:
        report = controller.run(tmp_path, timeout=10)
    except DeclarationError as error:
        report = controller.error_result(error)
    assert len(calls) == expected_calls and len(starts) == expected_starts
    assert not active
    observed = report["accounting"]["observations"] if scenario.endswith("refusal") else report
    row, group = observed["units"][0], observed["groups"][0]
    assert row["attempts"] == group["attempts"] == expected_starts
    if scenario in {"before-refusal", "before-cancel"}:
        assert row["result_status"] == "not-run" and group["not_run"] == 1
    elif scenario in {"after-refusal", "started-then-prestart-failure"}:
        assert row["result_status"] is None and group["unresolved"] == 1
    else:
        assert row["result_status"] == "passed" and row["complete"]
