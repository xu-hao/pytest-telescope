"""Measure the synthetic workload with external wall/process accounting.

Run with an installed pytest-telescope interpreter; no third-party harness
packages are needed. Output directories must be new. This harness never modifies
the source example and fails if the expected per-case accounting differs.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

EXPECTED_CASES = 7
EXPECTED_COUNTS = {
    "cold": (7, 0),
    "warm": (0, 7),
    "narrow_code": (3, 4),
    "resource": (3, 4),
    "case_resource": (2, 5),
    "shared_input": (7, 0),
}
CASE_FILES = {
    "tests/test_scoring.py::test_empty": "tests/test_scoring.py",
    "tests/test_scoring.py::test_customer_scores": "tests/test_scoring.py",
    "tests/test_demand.py::test_empty": "tests/test_demand.py",
    "tests/test_demand.py::test_demand_projection": "tests/test_demand.py",
    "tests/test_pricing.py::test_empty": "tests/test_pricing.py",
    "tests/test_pricing.py::test_price_schedule": "tests/test_pricing.py",
    "tests/test_broad.py::test_project_file": "tests/test_broad.py",
}
EXPECTED_UNITS = {hashlib.sha256(node.encode()).hexdigest(): file for node, file in CASE_FILES.items()}


def expected_executed(phase):
    if phase in {"cold", "shared_input"}:
        nodes = CASE_FILES
    elif phase == "warm":
        nodes = []
    else:
        changed_file = "tests/test_demand.py" if phase == "resource" else "tests/test_scoring.py"
        nodes = [
            node for node, file in CASE_FILES.items()
            if file == "tests/test_broad.py" or (
                file == changed_file and (
                    phase != "case_resource" or node.endswith("::test_customer_scores")
                )
            )
        ]
    return {hashlib.sha256(node.encode()).hexdigest() for node in nodes}


def measure(command: list[str], project: Path, output: Path, phase: str) -> dict:
    """Measure only this invocation; Docker worker CPU is accounted separately."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    log = output / f"{phase}.log"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with log.open("wb") as stream:
        completed = subprocess.run(
            command,
            cwd=project,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if log.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError(f"{phase}: oversized output; inspect local log")
    if completed.returncode:
        raise RuntimeError(f"{phase}: exit {completed.returncode}; inspect {log}")
    return {
        "phase": phase,
        "elapsed_seconds": elapsed,
        "controller_cpu_seconds": (
            after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
        ),
        "log": log.name,
    }


def result_from_log(path: Path) -> dict:
    # The CLI result is one JSON object, not a heuristic parse of pytest prose.
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise RuntimeError("CLI result must be a JSON object")
    return result


def validate_result(result: dict, phase: str) -> None:
    executed_count, reused_count = EXPECTED_COUNTS[phase]
    counts = result["counts"]
    expected = {
        "executed": executed_count,
        "reused": reused_count,
        "failed": 0,
        "infrastructure_error": 0,
        "not-selected": 0,
    }
    if counts != expected:
        raise RuntimeError(f"{phase}: expected {expected}, received {counts}")
    cases = result["cases"]
    if cases != {"collected": EXPECTED_CASES, "passed": EXPECTED_CASES, "failed": 0, "skipped": 0}:
        raise RuntimeError(f"{phase}: unexpected case counts {cases}")
    expected_case_outcomes = {
        "fresh_passed": executed_count,
        "cached_passed": reused_count,
        "fresh_skipped": 0,
        "cached_skipped": 0,
    }
    if result["case_outcomes"] != expected_case_outcomes:
        raise RuntimeError(f"{phase}: executed/reused case counts disagree with unit outcomes")
    units = result["units"]
    if len(units) != EXPECTED_CASES or len({unit["name"] for unit in units}) != EXPECTED_CASES:
        raise RuntimeError(f"{phase}: duplicate or incomplete case inventory")
    if {unit["name"]: unit["file"] for unit in units} != EXPECTED_UNITS:
        raise RuntimeError(f"{phase}: unexpected case identity or source file")
    if any(
        {key: unit[key] for key in ("collected", "passed", "failed", "skipped")}
        != {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}
        for unit in units
    ):
        raise RuntimeError(f"{phase}: each unit must account for exactly one passing case")
    actual = {unit["name"] for unit in units if unit["outcome"] == "executed"}
    if actual != expected_executed(phase) or any(
        unit["outcome"] != ("executed" if unit["name"] in actual else "reused")
        for unit in units
    ):
        raise RuntimeError(f"{phase}: wrong case invalidated")
    collection = result["collection"]
    if (
        collection["outcome"] != ("reused" if phase == "warm" else "executed")
        or collection["collected"] != EXPECTED_CASES
        or collection["failures"]
    ):
        raise RuntimeError(f"{phase}: unexpected collection outcome or inventory count")
    for record in [collection, *units]:
        for key in ("worker_seconds", "cpu_seconds"):
            value = record[key]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise RuntimeError(f"{phase}: invalid worker accounting")
            if record["outcome"] == "reused" and value != 0:
                raise RuntimeError(f"{phase}: reused work must have zero current worker accounting")
    expected_cpu = collection["cpu_seconds"] + sum(unit["cpu_seconds"] for unit in units)
    if not math.isclose(result["worker_cpu_seconds"], expected_cpu, rel_tol=1e-9, abs_tol=1e-9):
        raise RuntimeError(f"{phase}: collection and case CPU must reconcile with worker CPU")


def mutate(project: Path, phase: str) -> None:
    if phase == "narrow_code":
        path = project / "demoapp/scoring.py"
        before = path.read_text()
        after = before.replace("return round(total, 8)", "return round(total, 7)")
        if before == after:
            raise RuntimeError("narrow-code mutation no longer matches workload")
        path.write_text(after)
    elif phase == "resource":
        path = project / "data/demand.json"
        data = json.loads(path.read_text())
        data[0]["units"] += 1
        path.write_text(json.dumps(data) + "\n")
    elif phase == "case_resource":
        path = project / "data/scoring-check.json"
        data = json.loads(path.read_text())
        data["minimum"] += 1
        path.write_text(json.dumps(data) + "\n")
    elif phase == "shared_input":
        path = project / "demoapp/common.py"
        before = path.read_text()
        after = before.replace("SCALE = 1.0", "SCALE = 1.001")
        if before == after:
            raise RuntimeError("shared-input mutation no longer matches workload")
        path.write_text(after)


def run_trial(python: str, source: Path, trial: Path) -> list[dict]:
    trial.mkdir()
    project = trial / "project"
    shutil.copytree(
        source,
        project,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".pytest-telescope"),
    )
    junit = trial / "ordinary.xml"
    ordinary = measure(
        [python, "-m", "pytest", "-p", "telescope", "-q", f"--junitxml={junit}"], project, trial, "ordinary"
    )
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    if len(cases) != EXPECTED_CASES or any(
        element.tag in {"failure", "error", "skipped"} for case in cases for element in case
    ):
        raise RuntimeError("ordinary pytest did not pass all seven workload cases")
    ordinary["cases"] = {"collected": len(cases), "passed": len(cases), "failed": 0, "skipped": 0}
    ordinary["counts"] = None  # Ordinary pytest has no isolated case-reuse accounting.
    rows = [ordinary]
    preparation = measure(
        [python, "-m", "pytest_telescope", "runtime"], project, trial, "runtime_setup"
    )
    preparation["runtime"] = result_from_log(trial / preparation["log"])
    rows.append(preparation)
    for phase in EXPECTED_COUNTS:
        mutate(project, phase)
        row = measure(
            [python, "-m", "pytest_telescope", "run", "--explain", "--", "-q"],
            project,
            trial,
            phase,
        )
        result = result_from_log(trial / row["log"])
        validate_result(result, phase)
        row.update(
            {
                key: result[key]
                for key in ("counts", "cases", "case_outcomes", "collection", "units", "setup_seconds")
            }
        )
        row["executed_cases"] = sum(
            unit["collected"] for unit in result["units"] if unit["outcome"] == "executed"
        )
        row["reused_cases"] = sum(
            unit["collected"] for unit in result["units"] if unit["outcome"] == "reused"
        )
        row["worker_cpu_seconds"] = result["worker_cpu_seconds"]
        row["collection_cpu_seconds"] = result["collection"]["cpu_seconds"]
        row["case_cpu_seconds"] = sum(unit["cpu_seconds"] for unit in result["units"])
        row["collection_worker_seconds"] = result["collection"]["worker_seconds"]
        row["non_session_elapsed_seconds"] = row["elapsed_seconds"] - sum(
            unit["worker_seconds"] for unit in result["units"]
        ) - result["collection"]["worker_seconds"]
        row["controller_reported_elapsed_seconds"] = result["elapsed_seconds"]
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("--repetitions must be between 1 and 20")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    python = str(Path(args.python).absolute())
    source = Path(__file__).resolve().parents[1] / "examples/analytics"
    versions = subprocess.run(
        [
            python,
            "-c",
            "import importlib.metadata,json,platform; "
            "from pytest_telescope.controller import runner_identity; "
            "print(json.dumps({'python':platform.python_version(),"
            "'runner':runner_identity(),"
            "'pytest':importlib.metadata.version('pytest'),"
            "'pytest_telescope':importlib.metadata.version('pytest-telescope')}))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "versions": json.loads(versions.stdout),
        "method": {
            "external_wall_clock": "time.perf_counter",
            "controller_cpu": "resource.getrusage(RUSAGE_CHILDREN), delta per command",
            "worker_cpu": "worker OS resource.getrusage self + reaped children",
            "ordinary_cpu": "ordinary pytest process and reaped child processes",
            "runtime_setup": "separate image preparation phase; Docker layer cache may be warm",
            "setup_seconds": "controller setup through inventory resolution, minus collection worker session duration",
            "non_session_elapsed_seconds": "external elapsed minus collection and per-case worker session durations",
            "cases_per_run": EXPECTED_CASES,
            "independent_cases_per_run": EXPECTED_CASES,
        },
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "harness_python": platform.python_version(),
        },
        "trials": [],
    }
    result_path = output / "results.json"
    for number in range(1, args.repetitions + 1):
        rows = run_trial(python, source, output / f"trial-{number}")
        report["trials"].append({"number": number, "phases": rows})
        result_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Completed trial {number}/{args.repetitions}", flush=True)
    report["medians"] = {}
    for phase in ("ordinary", "runtime_setup", *EXPECTED_COUNTS):
        rows = [
            row for trial in report["trials"] for row in trial["phases"] if row["phase"] == phase
        ]
        metrics = {"elapsed_seconds", "controller_cpu_seconds"}
        if phase in EXPECTED_COUNTS:
            metrics.update({"worker_cpu_seconds", "collection_cpu_seconds", "case_cpu_seconds", "collection_worker_seconds", "setup_seconds", "non_session_elapsed_seconds"})
        report["medians"][phase] = {
            metric: statistics.median(row[metric] for row in rows) for metric in sorted(metrics)
        }
    result_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Validated results: {result_path}")


if __name__ == "__main__":
    main()
