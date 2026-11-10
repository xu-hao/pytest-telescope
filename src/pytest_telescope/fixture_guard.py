"""Strict controller-side validation for guarded fixture inspections.

The sandbox worker owns fixture discovery. This module consumes only its bounded
wire records; it does not inspect pytest internals in the invoking process.
"""

import re
from pathlib import PurePosixPath

from .model import DeclarationError, InfrastructureError, UnsupportedCapability, fingerprint

SCOPES = frozenset({"function", "class", "module", "package", "session"})
ROW_FIELDS = frozenset(
    {"definition_id", "owner_id", "scope", "parameter_index", "effective_scope", "framework"}
)
INSPECTION_FIELDS = frozenset(
    {"protocol", "nonce", "context_id", "group_id", "case_ids", "registry_id", "graph_id", "items"}
)

# Exact installed source identities observed in the owned pytest qualification
# inputs. They are candidates for framework provenance, not fixture-name rules.
# A worker must additionally verify the actual registered function object and
# plugin before assigning any of these identities.
FRAMEWORK_SOURCE_IDENTITIES = {
    "9.1.1": (
        (
            "_pytest.doctest",
            "04261f5b3ddce0c36584f2be42e2938d5a0dbe914414502533a93e8c142dacf9",
            720,
        ),
        (
            "_pytest.fixtures",
            "f5d4d3f848d97411e456d50400d3aaecc19a9b0c90def9151e395ee8952042b7",
            1563,
        ),
        (
            "_pytest.junitxml",
            "557b8978c66b8010fafc50ada4bf56333f9927f31b2b5909fe4c1a9df1a3c0f0",
            335,
        ),
        ("_pytest.tmpdir", "5099f71d89413191c9e7515c9d4fe9fc7083bb73306f2cf1f73a9f89c862ed90", 275),
        (
            "_pytest.legacypath",
            "fe5eaff1a90d31f4dce53023bdb73a33efedd79ee9f5013afb5d7c58cd0346df",
            297,
        ),
    ),
    "9.2.0": (
        (
            "_pytest.doctest",
            "20d8d03d1efbf143b4094eab9cf4f2551c40a4ccce53e3e37c8eb626569d2e00",
            737,
        ),
        (
            "_pytest.fixtures",
            "3847b9bbb05ba2408c3e9e270276ee86a70c1f0d1d420ad14b37b06093a9a330",
            1644,
        ),
        (
            "_pytest.junitxml",
            "3f3b32bac64d387060d513844db6b70b1b43c5391bfe0cc819cf062d55bc4966",
            382,
        ),
        ("_pytest.tmpdir", "5834124a28502a4cbea5a98370c32eb023359163a42b491a7d7ef46c2dec459a", 313),
        (
            "_pytest.legacypath",
            "16ddbb331699b4f81ec7c0ef38ea5a53a691250dfcf5996a3398990a8a0c4966",
            297,
        ),
    ),
}


def framework_definition_ids(pytest_version):
    """Return independently fixed source identities for the exact pytest release."""
    if type(pytest_version) is not str or pytest_version not in FRAMEWORK_SOURCE_IDENTITIES:
        raise UnsupportedCapability(
            "execution:fixture-version", "unsupported fixture adapter version"
        )
    return frozenset(
        fingerprint({"pytest": pytest_version, "module": module, "source": source, "line": line})
        for module, source, line in FRAMEWORK_SOURCE_IDENTITIES[pytest_version]
    )


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _fixture_order(row):
    index = row["parameter_index"]
    return (
        row["definition_id"],
        row["owner_id"],
        row["effective_scope"],
        -1 if index is None else index,
        row["scope"],
        row["framework"],
    )


def validate_inspection(result, *, nonce, group_id, case_ids, registry_id, framework_ids):
    """Validate complete exact group membership and recompute its graph identity."""
    if (
        type(result) is not dict
        or set(result) != INSPECTION_FIELDS
        or result["protocol"] != "fixture-inspection-v1"
        or result["nonce"] != nonce
        or result["context_id"] != group_id
        or result["group_id"] != group_id
        or result["registry_id"] != registry_id
        or not all(_digest(value) for value in (nonce, group_id, registry_id, result["graph_id"]))
        or type(case_ids) is not list
        or not 0 < len(case_ids) <= 4096
        or any(not _digest(case) for case in case_ids)
        or len(set(case_ids)) != len(case_ids)
        or result["case_ids"] != case_ids
        or type(result["items"]) is not list
        or len(result["items"]) != len(case_ids)
        or type(framework_ids) is not frozenset
    ):
        raise InfrastructureError("invalid fixture inspection identity")
    definitions, associations = set(), 0
    for item, case in zip(result["items"], case_ids):
        if (
            type(item) is not dict
            or set(item) != {"case_id", "fixtures", "packages"}
            or item["case_id"] != case
            or type(item["fixtures"]) is not list
            or type(item["packages"]) is not list
            or len(item["packages"]) > 64
        ):
            raise InfrastructureError("invalid fixture inspection membership")
        owners = []
        for package in item["packages"]:
            if (
                type(package) is not dict
                or set(package) != {"owner_id", "lifecycle"}
                or not _digest(package["owner_id"])
                or type(package["lifecycle"]) is not list
                or len(package["lifecycle"]) != 2
                or any(value is not None and not _digest(value) for value in package["lifecycle"])
            ):
                raise InfrastructureError("invalid package collector evidence")
            owners.append(package["owner_id"])
        if owners != sorted(set(owners)):
            raise InfrastructureError("invalid package collector ordering")
        rows = item["fixtures"]
        associations += len(rows)
        if associations > 100_000:
            raise UnsupportedCapability(
                "execution:fixture-limit", "fixture association limit exceeded"
            )
        for row in rows:
            if (
                type(row) is not dict
                or set(row) != ROW_FIELDS
                or not _digest(row["definition_id"])
                or not _digest(row["owner_id"])
                or type(row["scope"]) is not str
                or row["scope"] not in SCOPES
                or type(row["effective_scope"]) is not str
                or row["effective_scope"] not in SCOPES
                or (
                    row["parameter_index"] is not None
                    and (type(row["parameter_index"]) is not int or row["parameter_index"] < 0)
                )
                or type(row["framework"]) is not bool
                or row["framework"] != (row["definition_id"] in framework_ids)
            ):
                raise InfrastructureError("invalid fixture inspection definition")
            definitions.add(row["definition_id"])
        if len(definitions) > 4096:
            raise UnsupportedCapability(
                "execution:fixture-limit", "fixture definition limit exceeded"
            )
        ordering = [_fixture_order(row) for row in rows]
        if ordering != sorted(ordering) or len(set(ordering)) != len(ordering):
            raise InfrastructureError("invalid fixture inspection ordering")
    committed = {
        "protocol": result["protocol"],
        "context_id": group_id,
        "group_id": group_id,
        "case_ids": case_ids,
        "registry_id": registry_id,
        "items": result["items"],
    }
    if fingerprint(committed) != result["graph_id"]:
        raise InfrastructureError("fixture inspection graph mismatch")
    return result


def require_conservation(inspections, candidates):
    """Refuse possible shared lifetimes split across their actual owners.

    A fixture absent from another narrow view is not proof that the other view
    could not use it. Compare each possible owner with all selected groups in
    that owner's structural boundary, not just groups reporting the fixture.
    """
    if len(inspections) != len(candidates):
        raise InfrastructureError("fixture group membership is ambiguous")
    modules, suites, packages, seen = {}, {}, {}, set()
    for inspection, candidate in zip(inspections, candidates):
        group = inspection["group_id"]
        if group != candidate["key"] or group in seen:
            raise InfrastructureError("fixture group membership is ambiguous")
        seen.add(group)
        suite = candidate["suite"].name
        filename = candidate["item"]["file"]
        modules.setdefault((suite, filename), set()).add(group)
        suites.setdefault(suite, set()).add(group)
        for parent in PurePosixPath(filename).parents:
            owner = fingerprint({"suite": suite, "scope": "package", "path": str(parent)})
            packages.setdefault(owner, set()).add(group)
    associations = 0
    for inspection, candidate in zip(inspections, candidates):
        group = inspection["group_id"]
        suite = candidate["suite"].name
        filename = candidate["item"]["file"]
        for item in inspection["items"]:
            for package in item["packages"]:
                owner = package["owner_id"]
                if group not in packages.get(owner, set()):
                    raise InfrastructureError("package owner disagrees with selected group")
                if (
                    any(value is not None for value in package["lifecycle"])
                    and len(packages[owner]) > 1
                ):
                    raise UnsupportedCapability(
                        "execution:fixture-sharing", "package lifecycle spans execution groups"
                    )
            for row in item["fixtures"]:
                associations += 1
                if associations > 100_000:
                    raise UnsupportedCapability(
                        "execution:fixture-limit", "fixture association limit exceeded"
                    )
                if row["framework"] or row["effective_scope"] == "function":
                    continue
                scope = row["effective_scope"]
                if scope == "module":
                    owner = fingerprint({"suite": suite, "scope": "module", "file": filename})
                    if row["owner_id"] != owner:
                        raise InfrastructureError(
                            "fixture module owner disagrees with selected group"
                        )
                    possible_groups = modules[(suite, filename)]
                elif scope == "class":
                    # The inventory does not expose every selected item's class
                    # owner. Another group in the module could share this class.
                    possible_groups = modules[(suite, filename)]
                elif scope == "session":
                    owner = fingerprint({"suite": suite, "scope": "session"})
                    if row["owner_id"] != owner:
                        raise InfrastructureError(
                            "fixture session owner disagrees with selected group"
                        )
                    possible_groups = suites[suite]
                else:
                    # Package ownership is not represented by the current worker.
                    raise UnsupportedCapability(
                        "execution:fixture-metadata", "fixture owner cannot be validated"
                    )
                if len(possible_groups) > 1:
                    raise UnsupportedCapability(
                        "execution:fixture-sharing", "fixture lifetime spans execution groups"
                    )


def evaluate_conditions(conditions, candidates):
    """Compare caller predicates against the same captured per-group environment."""
    selected = {}
    for candidate in candidates:
        for case in candidate["case_ids"]:
            if case in selected:
                raise InfrastructureError("fixture group membership is ambiguous")
            selected[case] = candidate
    evidence = []
    for condition in conditions:
        candidate = selected.get(condition["case_id"])
        if candidate is None or condition["name"] not in candidate["environment"]:
            raise DeclarationError("fixture condition does not identify a selected declared input")
        actual = candidate["environment"][condition["name"]]
        op = condition["op"]
        matched = (
            actual is None
            if op == "absent"
            else actual is not None
            if op == "present"
            else actual is not None and actual == condition["value"]
        )
        if not matched:
            raise UnsupportedCapability(
                "execution:condition-mismatch", "fixture condition did not match"
            )
        evidence.append(
            {"case_id": condition["case_id"], "group_id": candidate["key"], "matched": True}
        )
    return evidence


def validate_capture(result, *, nonce, base_context_id, case_ids, framework_ids):
    """Validate a body-free capture before deriving the final guarded key."""
    fields = {
        "protocol",
        "nonce",
        "base_context_id",
        "case_ids",
        "registry_id",
        "content_id",
        "items",
    }
    if (
        type(result) is not dict
        or set(result) != fields
        or result["protocol"] != "fixture-capture-v1"
        or result["nonce"] != nonce
        or result["base_context_id"] != base_context_id
        or result["case_ids"] != case_ids
        or not all(
            _digest(value)
            for value in (nonce, base_context_id, result["registry_id"], result["content_id"])
        )
    ):
        raise InfrastructureError("invalid fixture capture identity")
    # Reuse the exact row and membership validator, binding the synthetic
    # inspection to the provisional context, without treating it as an
    # independently completed inspection.
    inspection = {
        "protocol": "fixture-inspection-v1",
        "nonce": nonce,
        "context_id": base_context_id,
        "group_id": base_context_id,
        "case_ids": case_ids,
        "registry_id": result["registry_id"],
        "items": result["items"],
    }
    inspection["graph_id"] = fingerprint(
        {key: value for key, value in inspection.items() if key != "nonce"}
    )
    validate_inspection(
        inspection,
        nonce=nonce,
        group_id=base_context_id,
        case_ids=case_ids,
        registry_id=result["registry_id"],
        framework_ids=framework_ids,
    )
    content = fingerprint(
        {
            "protocol": "fixture-content-v1",
            "registry_id": result["registry_id"],
            "case_ids": case_ids,
            "items": result["items"],
        }
    )
    if content != result["content_id"]:
        raise InfrastructureError("fixture capture content mismatch")
    return result


def guarded_group_id(base_context_id, registry_id, content_id):
    """Bind a provisional group to context-independent captured fixture content."""
    if not all(_digest(value) for value in (base_context_id, registry_id, content_id)):
        raise InfrastructureError("invalid guarded group identity")
    return fingerprint(
        {
            "profile": "batch-available-v1",
            "base_context_id": base_context_id,
            "registry_id": registry_id,
            "content_id": content_id,
        }
    )


def require_capture_match(capture, inspection):
    """Ensure final-context inspection reproduces the captured actual graph."""
    if (
        capture["registry_id"] != inspection["registry_id"]
        or capture["case_ids"] != inspection["case_ids"]
        or capture["items"] != inspection["items"]
    ):
        raise InfrastructureError("fixture graph changed after capture")
