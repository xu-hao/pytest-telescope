"""Strict consumers of genuine, normally collected ownership and cache evidence."""

import copy
import json

import pytest

from pytest_telescope import controller
from pytest_telescope.model import InfrastructureError, case_unit, fingerprint, load, scoped_item
from pytest_telescope.result import owner_identity, ownership_matches, validate_collection


def genuine_collection(root):
    """Collect and execute one owned project for real; return its persisted evidence."""
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots = ["."]\n')
    (root / "test_cases.py").write_text(
        """
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.telescope(inputs=["sibling.txt"])
def test_sibling(): pass

@pytest.mark.parametrize("value", [
    pytest.param("same-value", marks=pytest.mark.telescope(inputs=["first.txt"]), id="same-id"),
    pytest.param("same-value", marks=pytest.mark.telescope(inputs=["second.txt"]), id="same-id"),
    pytest.param("same-value", id="same-id"),
])
def test_choice(value): pass
"""
    )
    for name in ("sibling", "first", "second"):
        (root / (name + ".txt")).write_text("present")
    report = controller.run(root, workers=1, timeout=20)
    assert report["status"] == "passed", report
    assert report["counts"]["executed"] == 4
    paths = list((root / ".pytest-telescope/cache/inventory").glob("*.json"))
    assert len(paths) == 1
    path = paths[0]
    record = json.loads(path.read_text())
    result = record["result"]
    manifest = load(root)
    assert validate_collection(result, successful_only=True, context_id=record["key"])
    assert controller.inventory_matches_manifest(result, manifest)
    first = next(
        row["id"] for row in manifest.registry if row["kwargs"].get("inputs") == ["first.txt"]
    )
    sibling = next(
        row["id"] for row in manifest.registry if row["kwargs"].get("inputs") == ["sibling.txt"]
    )
    module = next(row["id"] for row in manifest.registry if row["owner"] == [])
    position = next(index for index, row in enumerate(result["items"]) if first in row["origins"])
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    return root, manifest, path, record, position, module, sibling


@pytest.fixture
def collected(tmp_path):
    """A private genuine collection for a case that changes its project or cache."""
    return genuine_collection(tmp_path / "project")


@pytest.fixture(scope="module")
def unchanged_collection(tmp_path_factory):
    """One genuine collection shared by cases that only read it and fault private copies."""
    value = genuine_collection(tmp_path_factory.mktemp("ownership") / "project")
    root, _, path, record, _, _, _ = value
    evidence = path.read_bytes(), copy.deepcopy(record)
    yield value
    # Sharing is valid only while every consumer leaves the genuine evidence untouched.
    assert (path.read_bytes(), record) == evidence
    assert not list((root / ".pytest-telescope/scratch").iterdir())


FAULTS = (
    "missing_owners",
    "empty_owners",
    "scalar_owners",
    "invalid_owner_digest",
    "duplicate_owner",
    "unsorted_owners",
    "missing_module_owner",
    "missing_parameters",
    "scalar_parameters",
    "missing_dimension",
    "duplicate_dimension",
    "extra_parameter_field",
    "boolean_alternative",
    "negative_alternative",
    "unbounded_alternative",
    "unknown_dimension",
    "stale_alternative",
    "missing_required_origin",
    "unknown_origin",
    "extra_sibling_origin",
)


@pytest.mark.parametrize("fault", FAULTS)
def test_genuine_collection_rejects_malformed_missing_stale_or_extra_ownership(
    unchanged_collection, fault
):
    _, manifest, _, record, position, module, sibling = unchanged_collection
    changed = copy.deepcopy(record["result"])
    item = changed["items"][position]
    original = record["result"]["items"][position]
    if fault == "missing_owners":
        del item["owners"]
    elif fault == "empty_owners":
        item["owners"] = []
    elif fault == "scalar_owners":
        item["owners"] = item["owners"][0]
    elif fault == "invalid_owner_digest":
        item["owners"] = ["not-an-owner-digest"]
    elif fault == "duplicate_owner":
        item["owners"] = sorted([*item["owners"], item["owners"][0]])
    elif fault == "unsorted_owners":
        item["owners"].reverse()
    elif fault == "missing_module_owner":
        item["owners"].remove(owner_identity("test_cases.py", []))
    elif fault == "missing_parameters":
        del item["parameters"]
    elif fault == "scalar_parameters":
        item["parameters"] = {}
    elif fault == "missing_dimension":
        item["parameters"] = []
    elif fault == "duplicate_dimension":
        item["parameters"].append(copy.deepcopy(item["parameters"][0]))
    elif fault == "extra_parameter_field":
        item["parameters"][0]["extra"] = True
    elif fault == "boolean_alternative":
        item["parameters"][0]["index"] = True
    elif fault == "negative_alternative":
        item["parameters"][0]["index"] = -1
    elif fault == "unbounded_alternative":
        item["parameters"][0]["index"] = 100_000
    elif fault == "unknown_dimension":
        item["parameters"][0]["dimension"] = "0" * 64
    elif fault == "stale_alternative":
        item["parameters"][0]["index"] = 2
    elif fault == "missing_required_origin":
        item["origins"].remove(module)
    elif fault == "unknown_origin":
        item["origins"] = sorted([*item["origins"], "0" * 64])
    elif fault == "extra_sibling_origin":
        item["origins"] = sorted([*item["origins"], sibling])
    else:
        raise AssertionError("unhandled fixture fault")

    # All baseline data was emitted by a real normal collection. Only a copy's
    # ownership fields changed; its actual nonce, context and case identities remain.
    assert ownership_matches(original, manifest.ownership_index)
    assert scoped_item(manifest, original)
    assert not ownership_matches(item, manifest.ownership_index)
    assert not (
        validate_collection(changed, successful_only=True)
        and controller.inventory_matches_manifest(changed, manifest)
    )
    with pytest.raises(InfrastructureError, match="ownership"):
        scoped_item(manifest, item)
    with pytest.raises(InfrastructureError, match="ownership"):
        case_unit(manifest, item, manifest.suites[0])


@pytest.mark.parametrize(
    "fault", ("old_envelope", "old_protocol", "missing_owners", "stale_alternative")
)
def test_stale_cached_ownership_refreshes_collection_before_reusing_case_results(collected, fault):
    root, _, path, record, position, _, _ = collected
    changed = copy.deepcopy(record)
    if fault == "old_envelope":
        changed["schema"] = 6
    elif fault == "old_protocol":
        changed["result"]["protocol"] = 7
    elif fault == "missing_owners":
        del changed["result"]["items"][position]["owners"]
    elif fault == "stale_alternative":
        changed["result"]["items"][position]["parameters"][0]["index"] = 2
    else:
        raise AssertionError("unhandled fixture fault")
    changed["checksum"] = fingerprint(changed["result"])
    path.write_text(json.dumps(changed))

    report = controller.run(root, workers=1, timeout=20)

    assert report["status"] == "passed", report
    assert report["collection"]["outcome"] == "executed"
    assert report["counts"]["executed"] == 0
    assert report["counts"]["reused"] == 4
    replacement = json.loads(path.read_text())
    assert replacement["result"]["items"] == record["result"]["items"]
    assert not list((root / ".pytest-telescope/scratch").iterdir())


def test_ownership_context_and_persistent_evidence_bind_genuine_collection_choices(collected):
    root, _, _, record, position, _, _ = collected
    items = record["result"]["items"]
    original = controller._file_digest(items, "test_cases.py")
    for field in ("origins", "owners", "parameters"):
        changed = copy.deepcopy(items)
        changed[position][field] = []
        assert controller._file_digest(changed, "test_cases.py") != original
    for path in (root / ".pytest-telescope/cache").rglob("*.json"):
        encoded = path.read_bytes()
        assert b"same-value" not in encoded
        assert b"same-id" not in encoded
