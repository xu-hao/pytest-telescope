"""One strict protocol for fresh and reusable worker results."""

import math
import re
from pathlib import PurePosixPath

FIELDS = {
    "protocol",
    "nonce",
    "exit_code",
    "complete",
    "leaked_descendants",
    "collected",
    "passed",
    "failed",
    "skipped",
    "case_ids",
    "worker_seconds",
    "cpu_seconds",
    "failures",
}


def validate_result(result, nonce=None, successful_only=False):
    """Return False for an incompatible, incomplete-success, or malformed record."""
    if type(result) is not dict or set(result) != FIELDS:
        return False
    if type(result["protocol"]) is not int or result["protocol"] != 1:
        return False
    if type(result["nonce"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", result["nonce"]):
        return False
    if nonce is not None and result["nonce"] != nonce:
        return False
    code = result["exit_code"]
    if type(code) is not int or code not in (0, 1, 2, 5):
        return False
    if type(result["complete"]) is not bool or type(result["leaked_descendants"]) is not bool:
        return False
    if result["leaked_descendants"]:
        return False
    for field in ("collected", "passed", "failed", "skipped"):
        if type(result[field]) is not int or not 0 <= result[field] <= 100_000:
            return False
    ids = result["case_ids"]
    if type(ids) is not list or len(ids) != result["collected"]:
        return False
    if any(type(item) is not str or not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
        return False
    if len(set(ids)) != len(ids):
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
    total = result["passed"] + result["failed"] + result["skipped"]
    if total > result["collected"] or (result["complete"] and total != result["collected"]):
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
        path = failure["file"]
        if type(path) is not str or not path or len(path) > 512:
            return False
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            return False
        if "\\" in path or any(ord(char) < 32 for char in path):
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
    if code == 0 and (
        not result["complete"]
        or result["failed"] != 0
        or result["passed"] + result["skipped"] != result["collected"]
        or failures
    ):
        return False
    if code == 5 and (not result["complete"] or result["collected"] != 0 or failures):
        return False
    return not successful_only or code == 0
