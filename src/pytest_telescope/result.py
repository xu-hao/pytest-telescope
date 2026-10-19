"""Strict, distinct collection and single-case execution result protocols."""

import json
import math
import re
from pathlib import PurePosixPath

COMMON = {
    "protocol",
    "nonce",
    "exit_code",
    "complete",
    "leaked_descendants",
    "worker_seconds",
    "cpu_seconds",
    "failures",
}
FIELDS = COMMON | {"collected", "passed", "failed", "skipped", "case_ids"}
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
        and len(PurePosixPath(value).parts) <= 64
    )


def _common(result, nonce, codes):
    if type(result["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", result["nonce"]):
        return False
    if nonce is not None and result["nonce"] != nonce:
        return False
    if type(result["exit_code"]) is not int or result["exit_code"] not in codes:
        return False
    if type(result["complete"]) is not bool or type(result["leaked_descendants"]) is not bool:
        return False
    if result["leaked_descendants"]:
        return False
    for field in ("worker_seconds", "cpu_seconds"):
        value = result[field]
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


def validate_result(result, nonce=None, successful_only=False, case_id=None):
    """Return False for an incompatible or malformed execution record."""
    if type(result) is not dict or set(result) != FIELDS:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 1:
        return False
    if not _common(result, nonce, (0, 1, 2, 4, 5)):
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


def validate_collection(result, nonce=None, successful_only=False):
    """Validate real item inventory without interpreting it as executed test success."""
    if type(result) is not dict or set(result) != COLLECTION_FIELDS:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 2:
        return False
    if result["kind"] != "collection" or not _common(result, nonce, (0, 2, 4, 5)):
        return False
    items = result["items"]
    if type(items) is not list or len(items) > 100_000:
        return False
    identities = set()
    encoded_budget = len(json.dumps({**result, "items": []}, separators=(",", ":")).encode())
    for index, item in enumerate(items):
        if type(item) is not dict or set(item) != {
            "nodeid",
            "file",
            "origins",
            "aliases",
            "selected",
        }:
            return False
        if (
            not _text(item["nodeid"])
            or not _path(item["file"])
            or item["nodeid"].split("::", 1)[0] != item["file"]
            or type(item["selected"]) is not bool
        ):
            return False
        if item["nodeid"] in identities:
            return False
        identities.add(item["nodeid"])
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
            or any(not _text(alias) for alias in aliases)
        ):
            return False
        if (
            aliases != sorted(set(aliases))
            or item["nodeid"] not in aliases
            or item["file"] not in aliases
        ):
            return False
        encoded_budget += len(json.dumps(item, separators=(",", ":")).encode()) + bool(index)
        if encoded_budget > MAX_INVENTORY_BYTES:
            return False
    if result["exit_code"] == 0 and (not result["complete"] or result["failures"]):
        return False
    if result["exit_code"] == 5 and (
        not result["complete"] or result["failures"] or any(item["selected"] for item in items)
    ):
        return False
    return not successful_only or result["exit_code"] == 0
