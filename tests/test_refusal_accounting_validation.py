"""Mutate actual ordinary observations at the strict accounting producer seam."""

import copy
import json

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts
from test_frozen_tool_refusal_accounting import FIELDS

from pytest_telescope import controller
from pytest_telescope.accounting import RefusalAccounting
from pytest_telescope.model import DeclarationError


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    directory = tmp_path_factory.mktemp("accounting-seed")
    project = ToolProject(directory / "project", directory / "external")
    project.module("test_real.py", "def test_real():\n    assert 6 * 7 == 42\n")
    report = project.run()
    assert_counts(report, executed=1)
    return {key: report[key] for key in FIELDS}


def construct(value, maximum=65536):
    metadata = controller.error_result(DeclarationError("owned strict accounting refusal"))
    return RefusalAccounting(value, metadata=metadata, maximum=maximum)


def set_field(value, path, replacement):
    target = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement


@pytest.mark.parametrize("path,replacement", [
    pytest.param(("version",), 999, id="foreign-version"),
    pytest.param(("raw_environment",), {"DUMMY_SECRET": "synthetic-only"}, id="raw-map"),
    pytest.param(("execution", "profile"), "case-future", id="unknown-profile"),
    pytest.param(("execution", "workers"), True, id="boolean-worker-count"),
    pytest.param(("units", 0, "file"), "../dummy-private-file", id="outside-path"),
    pytest.param(("units", 0, "case_id"), "NOT-A-DIGEST", id="malformed-digest"),
    pytest.param(("units", 0, "route"), "future", id="unknown-route"),
    pytest.param(("units", 0, "reports", 0, "phase"), "future", id="unknown-phase"),
    pytest.param(("units", 0, "complete"), False, id="phase-completeness"),
    pytest.param(("units", 0, "result_status"), "skipped", id="phase-status"),
    pytest.param(("units", 0, "attempts"), True, id="boolean-attempts"),
    pytest.param(("units", 0, "captured_output"), "synthetic-only", id="raw-unit-output"),
    pytest.param(("counts", "executed"), -1, id="negative-count"),
    pytest.param(("counts", "executed"), True, id="boolean-count"),
    pytest.param(("route_counts", "ordinary", "executed"), 0, id="route-conservation"),
    pytest.param(("cases", "passed"), 0, id="case-conservation"),
    pytest.param(("case_outcomes", "fresh_passed"), 0, id="outcome-conservation"),
    pytest.param(("elapsed_seconds",), float("nan"), id="nan-time"),
    pytest.param(("elapsed_seconds",), float("inf"), id="infinite-time"),
    pytest.param(("setup_seconds",), -1, id="negative-time"),
    pytest.param(("groups", 0, "case_ids"), ["f" * 64], id="foreign-group-member"),
    pytest.param(("groups", 0, "suite"), "unknown-suite", id="foreign-suite"),
    pytest.param(("groups", 0, "case_ids"), [], id="missing-group-member"),
    pytest.param(("collection", "suites", 0, "files"), ["/outside/dummy"], id="absolute-collection-path"),
    pytest.param(("inventory_digest",), "f" * 64, id="membership-digest-mismatch"),
])
def test_actual_seed_rejects_one_malformed_record(seed, path, replacement):
    value = copy.deepcopy(seed)
    set_field(value, path, replacement)
    with pytest.raises((ValueError, TypeError)):
        construct(value)


def test_actual_seed_rejects_duplicate_group(seed):
    value = copy.deepcopy(seed)
    value["groups"].append(copy.deepcopy(value["groups"][0]))
    with pytest.raises(ValueError):
        construct(value)


def test_executed_case_cannot_be_relabelled_as_unsubmitted(seed):
    value = copy.deepcopy(seed)
    # One semantic contradiction, with references/counts otherwise consistent:
    # retain actual passed reports but remove every record of its real attempt.
    value["units"][0].update(group_id=None, attempts=0)
    value["groups"] = []
    value["grouping_complete"] = False
    with pytest.raises(ValueError):
        construct(value)


def test_reserved_unsupported_count_is_not_a_unit_outcome(seed):
    value = copy.deepcopy(seed)
    value["units"][0]["outcome"] = "unsupported"
    for counts in (value["counts"], value["route_counts"]["ordinary"]):
        counts["executed"] = 0
        counts["unsupported"] = 1
    value["case_outcomes"]["fresh_passed"] = 0
    with pytest.raises(ValueError):
        construct(value)


def test_constructor_freezes_input_and_every_render_is_independent(seed):
    value = copy.deepcopy(seed)
    accounting = construct(value)
    expected = accounting.render()
    assert expected == {"version": 1, "state": "available", "observations": seed}
    value["units"].clear()
    first = accounting.render()
    first["observations"]["groups"].clear()
    assert accounting.render() == expected
    assert accounting.inventory_complete is True
    assert accounting.inventory_digest == seed["inventory_digest"]


def test_encoding_overflow_is_explicit_and_does_not_erase_inventory(seed):
    accounting = construct(copy.deepcopy(seed), maximum=512)
    assert accounting.render() == {"version": 1, "state": "unavailable", "reason": "bounds"}
    error = DeclarationError("owned strict accounting refusal", accounting=accounting)
    report = controller.error_result(error)
    assert report["inventory_complete"] is True
    assert report["inventory_digest"] == seed["inventory_digest"]
    assert len(json.dumps(report, sort_keys=True, indent=2).encode()) <= 512


def test_malformed_oversized_input_is_not_reported_as_bounds(seed):
    value = copy.deepcopy(seed)
    value["units"][0]["private_output"] = "synthetic-only" * 512
    with pytest.raises(ValueError) as caught:
        construct(value, maximum=512)
    assert "synthetic-only" not in str(caught.value)


def test_attachment_accepts_exact_immutable_type_once(seed):
    accounting = construct(copy.deepcopy(seed))
    error = DeclarationError("owned refusal")
    assert error.accounting is None
    with pytest.raises(TypeError):
        error.attach_accounting(accounting.render())
    error.attach_accounting(accounting)
    with pytest.raises(TypeError):
        error.attach_accounting(accounting)
    with pytest.raises(AttributeError):
        error.accounting = accounting

    class Subclass(RefusalAccounting):
        pass

    with pytest.raises(TypeError):
        DeclarationError("owned refusal", accounting=Subclass(
            copy.deepcopy(seed), metadata=controller.error_result(DeclarationError("owned refusal")),
            maximum=65536,
        ))
