"""Runs only inside the prepared restricted runtime, never on the host."""

import hashlib
import json
import os
import re
import resource
import sys
import time
from pathlib import Path

import pytest


def main():
    started = time.monotonic()
    request = json.loads(os.environ.pop("TELESCOPE_REQUEST"))
    cases = {}
    finished = set()
    collected = []
    failures = []

    def evidence(nodeid, phase, filename, line, kind):
        if len(failures) >= 64:
            return
        try:
            path = Path(filename).resolve().relative_to(Path.cwd()).as_posix()
            if len(path) > 512 or any(ord(char) < 32 for char in path):
                path = "<runtime>"
        except (ValueError, OSError):
            path = "<runtime>"
        failures.append(
            {
                "file": path,
                "line": line if type(line) is int and 0 <= line <= 10_000_000 else 0,
                "phase": phase,
                "exception_type": kind
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", kind)
                else "TestError",
                "case_id": hashlib.sha256(nodeid.encode()).hexdigest(),
            }
        )

    class Observer:
        def pytest_collection_finish(self, session):
            collected.extend(
                hashlib.sha256(item.nodeid.encode()).hexdigest() for item in session.items
            )

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            if report.failed:
                filename, line = str(item.path), item.location[1] + 1
                kind = "UnexpectedPass"
                if call.excinfo is not None:
                    kind = call.excinfo.type.__name__
                    traceback = call.excinfo.traceback
                    if traceback:
                        filename, line = str(traceback[-1].path), traceback[-1].lineno + 1
                evidence(report.nodeid, report.when, filename, line, kind)

        def pytest_collectreport(self, report):
            if report.failed:
                evidence(
                    report.nodeid, "collect", report.nodeid.split("::")[0], 0, "CollectionError"
                )

        def pytest_runtest_logreport(self, report):
            identity = hashlib.sha256(report.nodeid.encode()).hexdigest()
            previous = cases.get(identity)
            if report.failed:
                cases[identity] = "failed"
            elif previous != "failed":
                if report.skipped:
                    cases[identity] = "skipped"
                elif report.when == "call":
                    # Expected failures count as skipped; non-strict XPASS is a pass.
                    cases[identity] = "passed"
            if report.when == "teardown":
                finished.add(identity)

    saved_out, saved_err = sys.stdout, sys.stderr
    with open(os.devnull, "w") as sink:
        sys.stdout = sys.stderr = sink
        try:
            exit_code = int(pytest.main(request["args"], plugins=[Observer()]))
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err
    # pytest.main has now returned through sessionfinish and unconfigure hooks.
    leaked = [name for name in os.listdir("/proc") if name.isdecimal() and int(name) != os.getpid()]
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {
        "protocol": 1,
        "nonce": request["nonce"],
        "exit_code": exit_code,
        "collected": len(collected),
        "case_ids": collected,
        "passed": sum(value == "passed" for value in cases.values()),
        "failed": sum(value == "failed" for value in cases.values()),
        "skipped": sum(value == "skipped" for value in cases.values()),
        "complete": set(collected) == finished and len(cases) == len(collected) and not leaked,
        "leaked_descendants": bool(leaked),
        "worker_seconds": time.monotonic() - started,
        "cpu_seconds": usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime,
        "failures": failures,
    }
    print("TELESCOPE_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
