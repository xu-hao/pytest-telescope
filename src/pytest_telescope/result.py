"""Strict, distinct collection, execution and ordinary-session result protocols."""

import hashlib
import json
import math
import re
from pathlib import PurePosixPath

COMMON = {
    "protocol",
    "nonce",
    "exit_code",
    "declaration_error",
    "complete",
    "leaked_descendants",
    "worker_seconds",
    "failures",
    "context_id",
    "collection_skips",
}
FIELDS = COMMON | {"collected", "passed", "failed", "skipped", "case_ids", "case_results"}
COLLECTION_FIELDS = COMMON | {"kind", "items"}
MAX_INVENTORY_BYTES = 67_108_864


def _text(value, maximum=4096):
    if type(value) is not str or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _path(value):
    return (
        _text(value)
        and not PurePosixPath(value).is_absolute()
        and ".." not in PurePosixPath(value).parts
        and "\\" not in value
        and PurePosixPath(value).as_posix() == value
        and len(PurePosixPath(value).parts) <= 64
    )


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def owner_identity(filename, names):
    return hashlib.sha256("::".join([filename, *names]).encode()).hexdigest()


def dimension_identity(filename, position):
    encoded = json.dumps([filename, position], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _ownership(item):
    if (
        type(item) is not dict
        or not {"file", "owners", "parameters"}.issubset(item)
        or not _path(item["file"])
    ):
        return False
    owners, parameters = item["owners"], item["parameters"]
    if (
        type(owners) is not list
        or not 1 <= len(owners) <= 128
        or any(not _digest(owner) for owner in owners)
        or owners != sorted(set(owners))
        or owner_identity(item["file"], []) not in owners
        or type(parameters) is not list
        or len(parameters) > 128
    ):
        return False
    dimensions = []
    for parameter in parameters:
        if (
            type(parameter) is not dict
            or set(parameter) != {"dimension", "index"}
            or not _digest(parameter["dimension"])
            or type(parameter["index"]) is not int
            or not 0 <= parameter["index"] < 100_000
        ):
            return False
        dimensions.append(parameter["dimension"])
    return dimensions == sorted(set(dimensions))


def ownership_index(registry):
    """Index authenticated static owners once, rather than scanning every case pair."""
    index = {}
    for row in registry:
        owner = owner_identity(row["file"], row["owner"])
        spec = index.setdefault(owner, {"origins": set(), "dimensions": {}})
        if row["kind"] != "parameter":
            spec["origins"].add(row["id"])
            continue
        binding = row["parameter"]
        dimension = dimension_identity(row["file"], binding["position"])
        parameter = spec["dimensions"].setdefault(
            dimension, {"count": binding["count"], "alternatives": {}}
        )
        parameter["alternatives"].setdefault(binding["index"], set()).add(row["id"])
    return index


def ownership_matches(item, index):
    """Require every grant, and only grants of the actual lineage/alternatives.

    The worker produces the bounded lineage and generation-time choices; consumers
    repeat this converse check before routing, materialization or inventory reuse.
    """
    if not _ownership(item) or not {"origins", "aliases"}.issubset(item):
        return False
    for field, maximum in (("origins", 100_000), ("aliases", 128)):
        values = item[field]
        if (
            type(values) is not list
            or len(values) > maximum
            or any(not _digest(value) for value in values)
            or values != sorted(set(values))
        ):
            return False
    owners = set(item["owners"])
    choices = {row["dimension"]: row["index"] for row in item["parameters"]}
    dimensions, expected = set(), set()
    # Collected lexical ancestry cannot be omitted from ownership evidence.
    if any(alias in index and alias not in owners for alias in item["aliases"]):
        return False
    for owner in owners:
        if owner not in index:
            continue
        spec = index[owner]
        expected.update(spec["origins"])
        for dimension, parameter in spec["dimensions"].items():
            dimensions.add(dimension)
            if dimension not in choices or not 0 <= choices[dimension] < parameter["count"]:
                return False
            expected.update(parameter["alternatives"].get(choices[dimension], ()))
    return set(choices) == dimensions and set(item["origins"]) == expected


def _reason(value, *, collection=False):
    return (
        type(value) is dict
        and set(value) == {"profile", "kind", "sha256"}
        and value["profile"] == "pytest-reason-sha256-v1"
        and value["kind"] in (("skip",) if collection else ("skip", "xfail"))
        and _digest(value["sha256"])
    )


def _member(row, case):
    if type(row) is not dict or set(row) != {"case_id", "outcome", "complete", "reports"}:
        return False
    if row["case_id"] != case or type(row["complete"]) is not bool:
        return False
    reports = row["reports"]
    if type(reports) is not list or len(reports) > 3:
        return False
    phases = []
    for report in reports:
        if type(report) is not dict or set(report) != {"phase", "outcome", "skip_reason"}:
            return False
        phase, outcome, reason = report["phase"], report["outcome"], report["skip_reason"]
        if phase not in ("setup", "call", "teardown") or outcome not in (
            "passed",
            "failed",
            "skipped",
        ):
            return False
        if (outcome == "skipped" and not _reason(reason)) or (
            outcome != "skipped" and reason is not None
        ):
            return False
        phases.append(phase)
    if reports:
        if phases[0] != "setup" or phases not in (
            ["setup"],
            ["setup", "call"],
            ["setup", "teardown"],
            ["setup", "call", "teardown"],
        ):
            return False
        if ("call" in phases) != (len(phases) > 1 and reports[0]["outcome"] == "passed"):
            return False
    complete = bool(reports) and phases[-1] == "teardown"
    status = (
        "failed"
        if any(report["outcome"] == "failed" for report in reports)
        else "skipped"
        if any(report["outcome"] == "skipped" for report in reports)
        else "passed"
        if any(report["phase"] == "call" and report["outcome"] == "passed" for report in reports)
        else "not-run"
    )
    return row["complete"] == complete and row["outcome"] == status


def _failure_phases(result, rows):
    failed = {
        (row["case_id"], report["phase"])
        for row in rows
        for report in row["reports"]
        if report["outcome"] == "failed"
    }
    return all(
        failure["phase"] == "collect" or (failure["case_id"], failure["phase"]) in failed
        for failure in result["failures"]
    )


def _common(result, nonce, codes, context_id=None, *, nonce_length=32):
    if not _digest(result["context_id"]) or (
        context_id is not None and result["context_id"] != context_id
    ):
        return False
    skips = result["collection_skips"]
    if type(skips) is not list or len(skips) > 100_000:
        return False
    for skip in skips:
        if (
            type(skip) is not dict
            or set(skip) != {"file", "reason"}
            or not _path(skip["file"])
            or not _reason(skip["reason"], collection=True)
        ):
            return False
    if type(result["nonce"]) is not str or not re.fullmatch(
        rf"[0-9a-f]{{{nonce_length}}}", result["nonce"]
    ):
        return False
    if nonce is not None and result["nonce"] != nonce:
        return False
    if type(result["exit_code"]) is not int or result["exit_code"] not in codes:
        return False
    if type(result["complete"]) is not bool or type(result["leaked_descendants"]) is not bool:
        return False
    if type(result["declaration_error"]) is not bool or (
        result["declaration_error"] and (result["exit_code"] != 4 or result["complete"])
    ):
        return False
    if result["leaked_descendants"]:
        return False
    value = result["worker_seconds"]
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        if not math.isfinite(value):
            return False
    except OverflowError:
        return False
    failures = result["failures"]
    if type(failures) is not list or len(failures) > 64:
        return False
    for failure in failures:
        if type(failure) is not dict or set(failure) != {
            "file",
            "line",
            "phase",
            "exception_type",
            "case_id",
        }:
            return False
        if not _path(failure["file"]):
            return False
        if type(failure["line"]) is not int or not 0 <= failure["line"] <= 10_000_000:
            return False
        if failure["phase"] not in ("collect", "setup", "call", "teardown"):
            return False
        kind = failure["exception_type"]
        if type(kind) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", kind):
            return False
        case = failure["case_id"]
        if type(case) is not str or not re.fullmatch(r"[0-9a-f]{64}", case):
            return False
    return True


def validate_result(result, nonce=None, successful_only=False, case_id=None, context_id=None):
    """Return False for an incompatible or malformed execution record."""
    if type(result) is not dict or set(result) != FIELDS:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 9:
        return False
    if not _common(result, nonce, (0, 1, 2, 3, 4, 5), context_id):
        return False
    for field in ("collected", "passed", "failed", "skipped"):
        if type(result[field]) is not int or not 0 <= result[field] <= 1:
            return False
    ids = result["case_ids"]
    if type(ids) is not list or len(ids) != result["collected"]:
        return False
    if any(type(item) is not str or not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
        return False
    if len(set(ids)) != len(ids):
        return False
    if case_id is not None:
        if type(case_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", case_id):
            return False
        if ids and ids != [case_id]:
            return False
    rows = result["case_results"]
    if type(rows) is not list or len(rows) != len(ids):
        return False
    if any(not _member(row, case) for case, row in zip(ids, rows)):
        return False
    if any(
        result[status] != sum(row["outcome"] == status for row in rows)
        for status in ("passed", "failed", "skipped")
    ):
        return False
    if result["complete"] and not all(row["complete"] for row in rows):
        return False
    if any(
        failure["phase"] != "collect" and failure["case_id"] not in ids
        for failure in result["failures"]
    ):
        return False
    if not _failure_phases(result, rows):
        return False
    total = result["passed"] + result["failed"] + result["skipped"]
    if total > result["collected"] or (result["complete"] and total != result["collected"]):
        return False
    code = result["exit_code"]
    if code == 0 and (
        result["collected"] != 1
        or not result["complete"]
        or result["failed"] != 0
        or result["passed"] + result["skipped"] != result["collected"]
        or result["failures"]
    ):
        return False
    if code == 5 and (not result["complete"] or result["collected"] != 0 or result["failures"]):
        return False
    return not successful_only or code == 0


def validate_collection(result, nonce=None, successful_only=False, context_id=None):
    """Validate real item inventory without interpreting it as executed test success."""
    if type(result) is not dict or set(result) != COLLECTION_FIELDS:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 11:
        return False
    if result["kind"] != "collection" or not _common(result, nonce, (0, 1, 2, 3, 4, 5), context_id):
        return False
    items = result["items"]
    if not _inventory(items):
        return False
    if result["exit_code"] == 0 and (not result["complete"] or result["failures"]):
        return False
    if result["exit_code"] == 5 and (
        not result["complete"] or result["failures"] or any(item["selected"] for item in items)
    ):
        return False
    return not successful_only or result["exit_code"] == 0


def _inventory(items):
    if type(items) is not list or len(items) > 100_000:
        return False
    identities = set()
    encoded_budget = 4096
    for index, item in enumerate(items):
        if type(item) is not dict or set(item) != {
            "case_id",
            "file",
            "origins",
            "aliases",
            "selected",
            "order",
            "owners",
            "parameters",
        }:
            return False
        if not _ownership(item):
            return False
        if (
            (type(item["case_id"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", item["case_id"]))
            or not _path(item["file"])
            or type(item["selected"]) is not bool
        ):
            return False
        if item["selected"]:
            if type(item["order"]) is not int or not 0 <= item["order"] < len(items):
                return False
        elif item["order"] is not None:
            return False
        if item["case_id"] in identities:
            return False
        identities.add(item["case_id"])
        origins, aliases = item["origins"], item["aliases"]
        if type(origins) is not list or len(origins) > 100_000:
            return False
        if any(
            type(origin) is not str or not re.fullmatch(r"[0-9a-f]{64}", origin)
            for origin in origins
        ):
            return False
        if origins != sorted(set(origins)):
            return False
        if (
            type(aliases) is not list
            or not 1 <= len(aliases) <= 128
            or any(
                type(alias) is not str or not re.fullmatch(r"[0-9a-f]{64}", alias)
                for alias in aliases
            )
        ):
            return False
        if (
            aliases != sorted(set(aliases))
            or item["case_id"] not in aliases
            or hashlib.sha256(item["file"].encode()).hexdigest() not in aliases
        ):
            return False
        encoded_budget += len(json.dumps(item, separators=(",", ":")).encode()) + bool(index)
        if encoded_budget > MAX_INVENTORY_BYTES:
            return False
    orders = [item["order"] for item in items if item["selected"]]
    return sorted(orders) == list(range(len(orders)))


def validate_ordinary(result, nonce=None, context_id=None):
    """One ordinary session: its full collected inventory and its own ordinary outcomes."""
    fields = FIELDS | {
        "kind",
        "inventory_complete",
        "items",
        "case_results",
        "dispatch_started",
    }
    if type(result) is not dict or set(result) != fields:
        return False
    if (
        type(result["protocol"]) is not int
        or result["protocol"] != 4
        or result["kind"] != "ordinary"
    ):
        return False
    if not _common(result, nonce, (0, 1, 2, 3, 4, 5), context_id):
        return False
    if (
        type(result["inventory_complete"]) is not bool
        or type(result["dispatch_started"]) is not bool
        or (result["declaration_error"] and result["inventory_complete"])
        or not _inventory(result["items"])
    ):
        return False
    ids, rows = result["case_ids"], result["case_results"]
    if type(ids) is not list or type(rows) is not list or len(ids) != len(rows):
        return False
    if any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        return False
    if len(set(ids)) != len(ids) or not set(ids).issubset(
        {item["case_id"] for item in result["items"] if item["selected"]}
    ):
        return False
    counts = dict(passed=0, failed=0, skipped=0, **{"not-run": 0})
    for case, row in zip(ids, rows):
        if not _member(row, case):
            return False
        if not result["dispatch_started"] and row["outcome"] != "not-run":
            return False
        counts[row["outcome"]] += 1
    for key in ("collected", "passed", "failed", "skipped"):
        if type(result[key]) is not int or result[key] != (
            len(ids) if key == "collected" else counts[key]
        ):
            return False
    if not _failure_phases(result, rows):
        return False
    if result["complete"] and (
        not result["inventory_complete"] or not all(row["complete"] for row in rows)
    ):
        return False
    if result["exit_code"] in (0, 5) and (
        not result["complete"] or counts["failed"] or counts["not-run"] or result["failures"]
    ):
        return False
    return True


def validate_batch(
    result,
    nonce=None,
    successful_only=False,
    group_id=None,
    case_ids=None,
    context_id=None,
    *,
    _nonce_length=32,
):
    """Validate one atomic ordered session, including honest partial pytest stops."""
    if type(result) is not dict or set(result) != FIELDS | {"group_id"}:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 10:
        return False
    if not _common(result, nonce, (0, 1, 2, 3, 4, 5), context_id, nonce_length=_nonce_length):
        return False
    identity = result["group_id"]
    if type(identity) is not str or not re.fullmatch(r"[0-9a-f]{64}", identity):
        return False
    if group_id is not None and identity != group_id:
        return False
    if result["context_id"] != identity:
        return False
    ids = result["case_ids"]
    if type(ids) is not list or not 1 <= len(ids) <= 4096:
        return False
    if any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        return False
    if len(set(ids)) != len(ids) or (case_ids is not None and ids != case_ids):
        return False
    for field in ("collected", "passed", "failed", "skipped"):
        if type(result[field]) is not int or not 0 <= result[field] <= len(ids):
            return False
    if result["collected"] != len(ids):
        return False
    rows = result["case_results"]
    if type(rows) is not list or len(rows) != len(ids):
        return False
    counts = dict(passed=0, failed=0, skipped=0, **{"not-run": 0})
    for case, row in zip(ids, rows):
        if not _member(row, case):
            return False
        outcome = row["outcome"]
        if type(outcome) is not str or outcome not in counts:
            return False
        if outcome == "not-run" and row["complete"]:
            return False
        counts[outcome] += 1
    if any(result[name] != counts[name] for name in ("passed", "failed", "skipped")):
        return False
    if not _failure_phases(result, rows):
        return False
    if result["complete"] and not all(row["complete"] for row in rows):
        return False
    if any(
        failure["phase"] != "collect" and failure["case_id"] not in ids
        for failure in result["failures"]
    ):
        return False
    if result["exit_code"] == 0 and (
        not result["complete"] or counts["failed"] or counts["not-run"] or result["failures"]
    ):
        return False
    return not successful_only or result["exit_code"] == 0


def validate_guarded_batch(
    result, *, nonce=None, successful_only=False, group_id, case_ids, registry_id, graph_id
):
    """Validate the guarded batch protocol without relaxing legacy nonce rules."""
    extra = {"guard_protocol", "registry_id", "graph_id"}
    if (
        type(result) is not dict
        or set(result) != FIELDS | {"group_id"} | extra
        or result["guard_protocol"] != "batch-guard-v1"
        or not _digest(registry_id)
        or not _digest(graph_id)
        or result["registry_id"] != registry_id
        or result["graph_id"] != graph_id
        or (nonce is not None and not _digest(nonce))
    ):
        return False
    ordinary_fields = {key: value for key, value in result.items() if key not in extra}
    return validate_batch(
        ordinary_fields,
        nonce=nonce,
        successful_only=successful_only,
        group_id=group_id,
        case_ids=case_ids,
        context_id=group_id,
        _nonce_length=64,
    )
