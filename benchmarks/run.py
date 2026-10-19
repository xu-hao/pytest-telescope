"""Measure the synthetic workload with external wall/process accounting.

Run with an installed pytest-telescope interpreter; no third-party harness
packages are needed. Output directories must be new. This harness never modifies
the source example and fails if the expected target/case accounting differs.
"""

import argparse
import json
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

EXPECTED_CASES = 6
EXPECTED_COUNTS = {
    "cold": (3, 0),
    "warm": (0, 3),
    "narrow_code": (1, 2),
    "resource": (1, 2),
    "shared_input": (3, 0),
}


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
    expected_executed, expected_reused = EXPECTED_COUNTS[phase]
    counts = result["counts"]
    expected = {
        "executed": expected_executed,
        "reused": expected_reused,
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
        "fresh_passed": expected_executed * 2,
        "cached_passed": expected_reused * 2,
        "fresh_skipped": 0,
        "cached_skipped": 0,
    }
    if result["case_outcomes"] != expected_case_outcomes:
        raise RuntimeError(f"{phase}: executed/reused case counts disagree with target outcomes")
    units = result["units"]
    if {unit["name"] for unit in units} != {"scoring", "demand", "pricing"}:
        raise RuntimeError(f"{phase}: incomplete or unexpected target inventory")
    if len(units) != 3 or any(unit["collected"] != 2 for unit in units):
        raise RuntimeError(f"{phase}: each target must account for exactly two cases")
    expected_changed = {"narrow_code": "scoring", "resource": "demand"}.get(phase)
    if expected_changed is not None:
        actual = {unit["name"] for unit in units if unit["outcome"] == "executed"}
        if actual != {expected_changed}:
            raise RuntimeError(f"{phase}: wrong target invalidated: {sorted(actual)}")


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
        [python, "-m", "pytest", "-q", f"--junitxml={junit}"], project, trial, "ordinary"
    )
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    if len(cases) != EXPECTED_CASES or any(
        element.tag in {"failure", "error", "skipped"} for case in cases for element in case
    ):
        raise RuntimeError("ordinary pytest did not pass all six workload cases")
    ordinary["cases"] = {"collected": len(cases), "passed": len(cases), "failed": 0, "skipped": 0}
    ordinary["counts"] = None  # Ordinary pytest has no manifest target accounting.
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
                for key in ("counts", "cases", "case_outcomes", "units", "setup_seconds")
            }
        )
        row["executed_cases"] = sum(
            unit["collected"] for unit in result["units"] if unit["outcome"] == "executed"
        )
        row["reused_cases"] = sum(
            unit["collected"] for unit in result["units"] if unit["outcome"] == "reused"
        )
        row["worker_cpu_seconds"] = result["worker_cpu_seconds"]
        row["non_session_elapsed_seconds"] = row["elapsed_seconds"] - sum(
            unit["worker_seconds"] for unit in result["units"]
        )
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
            "setup_seconds": "controller manifest/runtime preflight and snapshot construction",
            "non_session_elapsed_seconds": "external elapsed minus summed worker session durations",
            "cases_per_run": EXPECTED_CASES,
            "targets_per_run": 3,
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
            metrics.update({"worker_cpu_seconds", "setup_seconds", "non_session_elapsed_seconds"})
        report["medians"][phase] = {
            metric: statistics.median(row[metric] for row in rows) for metric in sorted(metrics)
        }
    result_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Validated results: {result_path}")


if __name__ == "__main__":
    main()
