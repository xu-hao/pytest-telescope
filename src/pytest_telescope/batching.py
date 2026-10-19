"""Exact-scope grouping and public projection of atomic session evidence."""

from .model import UnsupportedCapability, fingerprint

MAX_GROUP_CASES = 4096


def group_jobs(jobs, mode, state):
    """Partition immutable effective scopes without granting a union of inputs."""
    grouped = {}
    for job in sorted(jobs, key=lambda job: (job["suite"].name, job["item"]["order"])):
        partition = (
            job["suite"].name,
            job["item"]["file"],
            job["scope"],
            job["item"]["case_id"] if mode == "case" else None,
        )
        if partition not in grouped:
            grouped[partition] = {**job, "members": []}
        grouped[partition]["members"].append((job["index"], job["item"]))
    for job in grouped.values():
        ids = [item["case_id"] for _, item in job["members"]]
        if mode == "batch" and len(ids) > MAX_GROUP_CASES:
            raise UnsupportedCapability(
                "execution:batch-size",
                "batch exceeds 4096 selected members; choose explicit structural or selection boundaries",
            )
        job["case_ids"] = ids
        job["key"] = fingerprint({**job["identity"], "case_ids": ids})
        job["path"] = state / (job["key"] + ".json")
    return list(grouped.values())


def group_row(job):
    return dict(
        group_id=job["key"],
        suite=job["suite"].name,
        file=job["item"]["file"],
        case_ids=job["case_ids"],
        outcome="infrastructure_error",
        complete=False,
        cache_eligible=False,
        stop_reason="infrastructure",
        attempts=0,
        attempt_seconds=0,
        timing_complete=True,
        worker_seconds=0,
        cpu_seconds=0,
        passed=0,
        failed=0,
        skipped=0,
        not_run=len(job["case_ids"]),
    )


def project_result(job, result, mode, *, attempts=0, reused=False, reason="", attempt_seconds=0):
    """Keep observed member outcomes separate from the whole session's eligibility."""
    group = group_row(job)
    group.update(attempts=attempts, attempt_seconds=attempt_seconds, timing_complete=attempts <= 1)
    if result is None:
        cases = [dict(case_id=case, outcome=None, complete=False) for case in job["case_ids"]]
    elif mode == "batch":
        cases = result["case_results"]
    else:
        status = next((name for name in ("failed", "skipped", "passed") if result[name]), "not-run")
        cases = [dict(case_id=job["case_ids"][0], outcome=status, complete=result["complete"])]
    if result is not None:
        complete = result["complete"]
        group.update(
            outcome="reused"
            if reused
            else "incomplete"
            if not complete
            else "executed"
            if result["exit_code"] == 0
            else "failed",
            complete=complete,
            cache_eligible=reused,
            stop_reason=None
            if complete
            else "collection"
            if any(failure["phase"] == "collect" for failure in result["failures"])
            else "pytest-stop"
            if result["exit_code"] in (1, 2)
            else "infrastructure",
            worker_seconds=0 if reused else result["worker_seconds"],
            cpu_seconds=0 if reused else result["cpu_seconds"],
            passed=result["passed"],
            failed=result["failed"],
            skipped=result["skipped"],
            not_run=sum(row["outcome"] == "not-run" for row in cases),
        )
    rows = []
    for (index, item), case in zip(job["members"], cases):
        status = case["outcome"]
        outcome = (
            "infrastructure_error"
            if status in (None, "not-run")
            else "reused"
            if reused
            else "failed"
            if status == "failed"
            else "executed"
        )
        rows.append(
            (
                index,
                dict(
                    name=item["case_id"],
                    case_id=item["case_id"],
                    suite=job["suite"].name,
                    file=item["file"],
                    selected=True,
                    group_id=job["key"],
                    result_status=status,
                    complete=case["complete"],
                    outcome=outcome,
                    reason=reason,
                    attempts=attempts,
                    collected=int(result is not None and (mode == "batch" or result["collected"])),
                    passed=int(status == "passed"),
                    failed=int(status == "failed"),
                    skipped=int(status == "skipped"),
                    worker_seconds=None if mode == "batch" else group["worker_seconds"],
                    cpu_seconds=None if mode == "batch" else group["cpu_seconds"],
                    failures=[]
                    if result is None
                    else [
                        failure
                        for failure in result["failures"]
                        if failure["case_id"] == item["case_id"]
                    ],
                ),
            )
        )
    return rows, group
