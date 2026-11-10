"""Strict immutable observations attached to a late typed refusal."""

import hashlib
import json
import math
import re
from dataclasses import dataclass

from .result import _digest, _member, _path, _reason, _text

FIELDS = frozenset({
    "execution", "suites", "inventory_complete", "inventory_digest", "grouping_complete",
    "counts", "route_counts", "cases", "case_outcomes", "collection", "units", "groups",
    "elapsed_seconds", "setup_seconds",
})
OUTCOMES = ("executed", "reused", "failed", "infrastructure_error", "not-selected")
COUNTS = {*OUTCOMES, "unsupported", "selected"}


class AccountingBounds(ValueError):
    """Known observations cannot fit their declared representation budget."""


def require(valid):
    if not valid:
        raise ValueError("invalid refusal accounting observations")


def record(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def sequence(value, maximum=100_000):
    require(type(value) is list)
    if len(value) > maximum:
        raise AccountingBounds("refusal accounting cardinality exceeded")
    return value


def integer(value, maximum=100_000):
    return type(value) is int and 0 <= value <= maximum


def timing(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def text(value):
    return value == "" or _text(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def failures(rows):
    for row in sequence(rows, 64):
        record(row, {"file", "line", "phase", "exception_type", "case_id"})
        require(_path(row["file"]) and integer(row["line"], 10_000_000) and _digest(row["case_id"]))
        require(row["phase"] in ("collect", "setup", "call", "teardown"))
        require(type(row["exception_type"]) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", row["exception_type"]) is not None)


def skips(rows, *, suites=None):
    for row in sequence(rows):
        record(row, {"file", "phase", "reason"} | ({"suite", "route"} if suites is not None else set()))
        require(_path(row["file"]) and row["phase"] == "collect" and _reason(row["reason"], collection=True))
        if suites is not None:
            require(row["suite"] in suites and row["route"] in ("ordinary", "scoped"))


def counts(rows):
    result = {name: sum(row["outcome"] == name for row in rows) for name in OUTCOMES}
    result["unsupported"] = 0
    result["selected"] = sum(row["selected"] for row in rows)
    return result


def count_record(value, expected):
    record(value, expected)
    require(all(integer(item) for item in value.values()) and value == expected)


def validate_observations(value, *, guarded=False):
    """Reject malformed producers; cardinality overflow has a distinct bounded end."""
    record(value, FIELDS | ({"fixture_guard"} if guarded else set()))
    suites = sequence(value["suites"], 64)
    require(all(_text(name) for name in suites) and len(set(suites)) == len(suites))
    execution = value["execution"]
    record(execution, {"mode", "profile", "workers"})
    require(execution["mode"] in ("case", "batch"))
    require(execution["profile"] == ("batch-available-v1" if guarded else execution["mode"] + "-v2"))
    if guarded:
        require(execution["mode"] == "batch")
    require(integer(execution["workers"], 32) and execution["workers"] > 0)
    require(type(value["inventory_complete"]) is bool and type(value["grouping_complete"]) is bool)
    require(_digest(value["inventory_digest"]) if value["inventory_complete"] else value["inventory_digest"] is None)
    require(timing(value["elapsed_seconds"]) and timing(value["setup_seconds"]) and value["setup_seconds"] <= value["elapsed_seconds"])
    units, groups = sequence(value["units"]), sequence(value["groups"])
    by_case = {}
    for row in units:
        record(row, {"name", "case_id", "suite", "file", "route", "selected", "outcome", "reason", "attempts", "collected", "passed", "failed", "skipped", "worker_seconds", "failures", "group_id", "result_status", "complete", "reports"})
        require(_digest(row["case_id"]) and row["name"] == row["case_id"] and row["suite"] in suites and _path(row["file"]))
        require(row["case_id"] not in by_case)
        by_case[row["case_id"]] = row
        require(row["route"] in ("ordinary", "scoped") and row["outcome"] in OUTCOMES)
        require(type(row["selected"]) is bool and row["selected"] == (row["outcome"] != "not-selected"))
        require(type(row["complete"]) is bool and text(row["reason"]) and integer(row["attempts"], 2))
        require(all(integer(row[name], 1) for name in ("collected", "passed", "failed", "skipped")))
        require(row["group_id"] is None or _digest(row["group_id"]))
        status = row["result_status"]
        require(status in (None, "passed", "failed", "skipped", "not-run"))
        if status is None:
            require(not row["reports"] and row["reports"] == [] and not row["complete"])
        else:
            require(_member({"case_id": row["case_id"], "outcome": status, "complete": row["complete"], "reports": row["reports"]}, row["case_id"]))
        require(all(row[name] == int(status == name) for name in ("passed", "failed", "skipped")))
        require(sum(row[name] for name in ("passed", "failed", "skipped")) <= row["collected"])
        failures(row["failures"])
        if row["route"] == "ordinary" or execution["mode"] == "batch":
            require(row["worker_seconds"] is None)
        else:
            require(timing(row["worker_seconds"]))
        if not row["selected"]:
            require(row["group_id"] is None)
        if row["group_id"] is None:
            require(row["attempts"] == 0 and status is None and row["collected"] == 0)
            require(row["outcome"] == ("infrastructure_error" if row["selected"] else "not-selected"))
        if row["outcome"] == "reused":
            require(row["route"] == "scoped" and row["attempts"] == 0 and row["complete"] and status in ("passed", "skipped"))
    count_record(value["counts"], counts(units))
    record(value["route_counts"], {"ordinary", "scoped"})
    for route in ("ordinary", "scoped"):
        count_record(value["route_counts"][route], counts([row for row in units if row["route"] == route]))
    count_record(value["cases"], {name: sum(row[name] for row in units) for name in ("collected", "passed", "failed", "skipped")})
    count_record(value["case_outcomes"], {label: sum(row[field] for row in units if row["outcome"] == outcome) for label, field, outcome in (("fresh_passed", "passed", "executed"), ("fresh_skipped", "skipped", "executed"), ("cached_passed", "passed", "reused"), ("cached_skipped", "skipped", "reused"))})
    ids, grouped = set(), set()
    for group in groups:
        record(group, {"group_id", "suite", "files", "route", "profile", "case_ids", "outcome", "complete", "cache_eligible", "stop_reason", "attempts", "attempt_seconds", "timing_complete", "worker_seconds", "passed", "failed", "skipped", "not_run", "unresolved", "failures", "collection_skips"})
        require(_digest(group["group_id"]) and group["group_id"] not in ids)
        ids.add(group["group_id"])
        require(group["suite"] in suites and group["route"] in ("ordinary", "scoped"))
        require(group["profile"] == ("ordinary-v2" if group["route"] == "ordinary" else execution["profile"]))
        files = sequence(group["files"])
        require(all(_path(name) for name in files) and files == sorted(set(files)))
        members = sequence(group["case_ids"], 100_000 if group["route"] == "ordinary" else 4096)
        require(all(_digest(case) and case in by_case and case not in grouped for case in members) and len(set(members)) == len(members))
        grouped.update(members)
        require(group["outcome"] in ("executed", "reused", "failed", "incomplete", "infrastructure_error"))
        require(all(type(group[name]) is bool for name in ("complete", "cache_eligible", "timing_complete")))
        require(group["stop_reason"] in (None, "pytest-stop", "collection", "infrastructure", "cancelled"))
        require((group["stop_reason"] is None) == group["complete"])
        require(integer(group["attempts"], 2) and timing(group["attempt_seconds"]) and timing(group["worker_seconds"]))
        if group["route"] == "ordinary":
            require(group["attempts"] == 1 and not group["cache_eligible"])
        if group["cache_eligible"]:
            require(group["outcome"] == "reused" and group["complete"] and group["attempts"] == 0)
        if group["route"] == "scoped":
            require(len(files) == 1 and bool(members) and (execution["mode"] != "case" or len(members) == 1))
        rows = [by_case[case] for case in members]
        for row in rows:
            require(row["selected"] and row["group_id"] == group["group_id"] and row["suite"] == group["suite"] and row["file"] in files and row["route"] == group["route"] and row["attempts"] == group["attempts"])
        require(all(integer(group[name]) for name in ("passed", "failed", "skipped", "not_run", "unresolved")))
        require(all(group[name] == sum(row["result_status"] == name for row in rows) for name in ("passed", "failed", "skipped")))
        require(group["not_run"] == sum(row["result_status"] == "not-run" for row in rows))
        require(group["unresolved"] == sum(row["result_status"] is None for row in rows))
        require(not group["complete"] or all(row["complete"] for row in rows))
        failures(group["failures"])
        skips(group["collection_skips"])
    for row in units:
        require((row["group_id"] is not None) == (row["case_id"] in grouped))
        if value["grouping_complete"] and row["selected"]:
            require(row["case_id"] in grouped)
    if guarded:
        guard = value["fixture_guard"]
        record(guard, {"profile", "state", "conditions"})
        require(guard["profile"] == "available-v1" and guard["state"] in ("admitted", "unavailable"))
        conditions = sequence(guard["conditions"], 4096)
        if guard["state"] == "unavailable":
            require(not conditions)
        for condition in conditions:
            record(condition, {"case_id", "group_id", "matched"})
            case = condition["case_id"]
            require(_digest(case) and case in by_case and by_case[case]["selected"])
            require(_digest(condition["group_id"]) and by_case[case]["group_id"] == condition["group_id"])
            require(type(condition["matched"]) is bool and condition["matched"])
    collection = value["collection"]
    require(type(collection) is dict)
    record(collection, {"outcome", "collected", "worker_seconds", "failures", "suites", "skips"} | ({"reason"} if "reason" in collection else set()))
    require(collection["outcome"] in ("executed", "reused", "failed", "infrastructure_error"))
    require(integer(collection["collected"]) and collection["collected"] == len(units) and timing(collection["worker_seconds"]))
    if "reason" in collection:
        require(text(collection["reason"]))
    failures(collection["failures"])
    skips(collection["skips"], suites=suites)
    seen = set()
    observations = sequence(collection["suites"], 128)
    for row in observations:
        record(row, {"name", "route", "files", "outcome", "collected", "inventory_digest", "worker_seconds", "skips"})
        require(row["name"] in suites and row["route"] in ("ordinary", "scoped") and (row["name"], row["route"]) not in seen)
        seen.add((row["name"], row["route"]))
        files = sequence(row["files"])
        require(all(_path(name) for name in files) and files == sorted(set(files)))
        require(row["outcome"] in ("not-started", "executed", "reused", "failed", "infrastructure_error"))
        require(row["collected"] is None or integer(row["collected"]))
        require(row["inventory_digest"] is None or _digest(row["inventory_digest"]))
        if row["outcome"] in ("executed", "reused"):
            require(row["collected"] is not None and _digest(row["inventory_digest"]))
        require(row["worker_seconds"] is None if row["route"] == "ordinary" else timing(row["worker_seconds"]))
        skips(row["skips"])
    require(collection["worker_seconds"] == sum(row["worker_seconds"] for row in observations if row["route"] == "scoped"))
    require(collection["skips"] == [{"suite": row["name"], "route": row["route"], **skip} for row in observations for skip in row["skips"]])
    require(value["inventory_complete"] == all(row["outcome"] in ("executed", "reused") for row in observations))
    if value["inventory_complete"]:
        require(value["inventory_digest"] == digest(sorted([row["suite"], row["case_id"], row["file"], row["route"]] for row in units)))


def encoded(value, maximum):
    output = bytearray()
    for chunk in json.JSONEncoder(sort_keys=True, indent=2, allow_nan=False).iterencode(value):
        data = chunk.encode("utf-8")
        if len(output) + len(data) > maximum:
            raise AccountingBounds("refusal accounting encoding exceeded")
        output.extend(data)
    return bytes(output)


@dataclass(frozen=True, init=False)
class RefusalAccounting:
    """An immutable validated payload; render returns a fresh independent JSON value."""

    _encoded: bytes
    inventory_complete: bool
    inventory_digest: str | None
    _guard_encoded: bytes | None

    def __init__(self, observations, *, metadata, maximum):
        require(type(maximum) is int and 0 < maximum <= 64 * 1024 * 1024)
        guarded = type(metadata) is dict and metadata.get("schema") == 5
        record(observations, FIELDS | ({"fixture_guard"} if guarded else set()))
        require(type(observations["inventory_complete"]) is bool)
        complete, identity = observations["inventory_complete"], observations["inventory_digest"]
        require(_digest(identity) if complete else identity is None)
        require(type(metadata) is dict and "status" in metadata)
        record(metadata, {"schema", "kind", "inventory_complete", "inventory_digest", "status", "error"} | ({"capability"} if metadata["status"] == "unsupported" else set()) | ({"fixture_guard"} if guarded else set()))
        require(type(metadata["schema"]) is int and metadata["schema"] == (5 if guarded else 4) and metadata["kind"] == "refusal")
        require(metadata["status"] in ("unsupported", "declaration_error") and _text(metadata["error"]))
        if metadata["status"] == "unsupported":
            require(_text(metadata["capability"]))
        if guarded:
            require(metadata["fixture_guard"] == observations["fixture_guard"])
        envelope = {**metadata, "inventory_complete": complete, "inventory_digest": identity}
        version = 2 if guarded else 1
        try:
            validate_observations(observations, guarded=guarded)
            payload = {"version": version, "state": "available", "observations": observations}
            encoded({**envelope, "accounting": payload}, maximum)
        except AccountingBounds:
            payload = {"version": version, "state": "unavailable", "reason": "bounds"}
            if guarded:
                envelope["fixture_guard"] = {"profile": "available-v1", "state": "unavailable", "conditions": []}
            encoded({**envelope, "accounting": payload}, maximum)
        object.__setattr__(self, "_encoded", encoded(payload, maximum))
        object.__setattr__(self, "inventory_complete", complete)
        object.__setattr__(self, "inventory_digest", identity)
        object.__setattr__(self, "_guard_encoded", encoded(envelope["fixture_guard"], maximum) if guarded else None)

    def render(self):
        return json.loads(self._encoded)

    @property
    def fixture_guard(self):
        return json.loads(self._guard_encoded) if self._guard_encoded is not None else None
