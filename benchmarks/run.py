"""Measure plain pytest against pytest-telescope ordinary and Bubblewrap-scoped execution.

The same synthetic workload runs fresh in every measurement: a new copy of the project, no
result reuse. One measurement is one invocation, measured from outside as a whole:

    python benchmarks/run.py environment --output DIR [--label TEXT] [--head SHA]
    python benchmarks/run.py plan --output DIR [--repetitions 5]
    python benchmarks/run.py measure --output DIR --series SERIES --workers N --repetition R
    python benchmarks/run.py summarize --output DIR

`plan` prints one `measure` command per line in an interleaved order. Run each line under
the same outer limits (for example one CI job or one resource-limited container each).
CPU is the user plus system time of the measured invocation's whole process tree: every
process in it, including Bubblewrap and the sandboxed workers, is a descendant that its
parent waits for, so RUSAGE_CHILDREN of this waiting process includes it. When this
process runs in its own cgroup v2, the cgroup's cpu.stat delta is recorded as a cross-check.
"""

import argparse
import json
import os
import platform
import re
import resource
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

WORKLOAD = Path(__file__).resolve().with_name("workload")
CASES = 16
WORKERS = (1, 2, 4)
SERIES = ("plain", "ordinary", "scoped-case", "scoped-batch")
SCOPE = re.compile(r"^# telescope-scope: (.+)$", re.MULTILINE)
INVOCATION_DEADLINE = 1800


def configurations():
    """Plain pytest is one process; every pytest-telescope series runs at 1, 2 and 4 workers."""
    return [("plain", 1)] + [(series, workers) for series in SERIES[1:] for workers in WORKERS]


def write_variant(destination, scoped):
    """Copy the workload; scoped copies turn each scope comment into a module declaration."""
    shutil.copytree(WORKLOAD, destination)
    for path in sorted((destination / "tests").glob("test_*.py")):
        text = path.read_text()
        if scoped:
            text = SCOPE.sub(r"pytestmark = pytest.mark.telescope(\1)", text)
        path.write_text(text)
    return destination


def command(series, workers, project):
    if series == "plain":
        return [sys.executable, "-m", "pytest", "-q", "--junitxml", str(project / "junit.xml")]
    arguments = [sys.executable, "-m", "pytest_telescope", "run", "--no-cache"]
    arguments += ["--workers", str(workers)]
    if series == "scoped-batch":
        arguments += ["--execution-mode", "batch"]
    return arguments


def own_cgroup_usage():
    """CPU seconds charged so far to this process's cgroup v2, or None when unreadable."""
    try:
        line = Path("/proc/self/cgroup").read_text().strip().splitlines()[0]
        if not line.startswith("0::"):
            return None
        stat = Path("/sys/fs/cgroup", line[3:].lstrip("/"), "cpu.stat").read_text()
        return int(re.search(r"^usage_usec (\d+)$", stat, re.MULTILINE).group(1)) / 1e6
    except (OSError, AttributeError, IndexError, ValueError):
        return None


def measure_tree(arguments, cwd, stdout_path):
    """Wall time and the CPU of the whole waited-for process tree of one invocation."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    cgroup_before = own_cgroup_usage()
    started = time.perf_counter()
    with open(stdout_path, "wb") as stdout:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            timeout=INVOCATION_DEADLINE,
        )
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cgroup_after = own_cgroup_usage()
    return {
        "exit_code": completed.returncode,
        "wall_seconds": wall,
        "cpu_seconds": (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime),
        "cgroup_cpu_seconds": None
        if cgroup_before is None or cgroup_after is None
        else cgroup_after - cgroup_before,
    }


def plain_outcome(project, exit_code):
    try:
        root = ElementTree.parse(project / "junit.xml").getroot()
    except (OSError, ElementTree.ParseError):
        return {"valid": False, "reason": "no JUnit report"}
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    counts = {key: int(suite.get(key, 0)) for key in ("tests", "failures", "errors", "skipped")}
    valid = exit_code == 0 and counts == {"tests": CASES, "failures": 0, "errors": 0, "skipped": 0}
    return {"valid": valid, "counts": counts}


def telescope_outcome(series, report, exit_code):
    """A measurement counts only when every case executed fresh on the intended route."""
    route = "ordinary" if series == "ordinary" else "scoped"
    other = "scoped" if route == "ordinary" else "ordinary"
    try:
        counts = report["route_counts"]
        valid = (
            exit_code == 0
            and report["status"] == "passed"
            and report["inventory_complete"] is True
            and counts[route]["executed"] == CASES
            and counts[route]["reused"] == 0
            and counts[other]["selected"] == 0
            and report["case_outcomes"]["fresh_passed"] == CASES
            and report["execution"]["mode"] == ("batch" if series == "scoped-batch" else "case")
        )
    except (KeyError, TypeError):
        return {"valid": False, "reason": "malformed report"}
    return {"valid": valid, "counts": counts[route], "status": report["status"]}


def phases(report):
    """The product's own timings; the whole-invocation wall time is measured outside."""
    groups = report.get("groups", [])
    scoped = [group for group in groups if group["route"] == "scoped"]
    ordinary = [group for group in groups if group["route"] == "ordinary"]
    return {
        "controller_elapsed_seconds": report.get("elapsed_seconds"),
        "setup_seconds": report.get("setup_seconds"),
        "sandboxed_collection_seconds": report["collection"].get("worker_seconds"),
        "ordinary_session_seconds": sum(group["worker_seconds"] or 0 for group in ordinary),
        "scoped_session_seconds": sum(group["worker_seconds"] or 0 for group in scoped),
        "scoped_sessions": sum(1 for group in scoped if group["attempts"]),
    }


def measure(output, series, workers, repetition):
    if (series, workers) not in configurations():
        raise SystemExit(f"unknown configuration {series} with {workers} workers")
    name = f"{series}-w{workers}-r{repetition}"
    project = output / "projects" / name
    project.parent.mkdir(parents=True, exist_ok=True)
    write_variant(project, scoped=series.startswith("scoped"))
    stdout_path = output / "logs" / (name + ".out")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    arguments = command(series, workers, project)
    measured = measure_tree(arguments, project, stdout_path)
    row = {
        "series": series,
        "workers": workers,
        "repetition": repetition,
        "command": [Path(arguments[0]).name, *arguments[1:]],
        **measured,
    }
    if series == "plain":
        row["outcome"] = plain_outcome(project, measured["exit_code"])
        row["phases"] = None
    else:
        try:
            report = json.loads(stdout_path.read_text())
        except (OSError, ValueError):
            report = None
        row["outcome"] = (
            telescope_outcome(series, report, measured["exit_code"])
            if isinstance(report, dict)
            else {"valid": False, "reason": "no JSON report"}
        )
        row["phases"] = phases(report) if row["outcome"]["valid"] else None
    rows = output / "rows"
    rows.mkdir(exist_ok=True)
    (rows / (name + ".json")).write_text(json.dumps(row, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(project)
    return row


def plan(output, repetitions):
    """Interleave configurations, rotating their order in each repetition to spread drift."""
    configs = configurations()
    lines = []
    for repetition in range(1, repetitions + 1):
        shift = (repetition - 1) % len(configs)
        for series, workers in configs[shift:] + configs[:shift]:
            lines.append(
                shlex.join(
                    [
                        "python",
                        "benchmarks/run.py",
                        "measure",
                        "--output",
                        str(output),
                        "--series",
                        series,
                        "--workers",
                        str(workers),
                        "--repetition",
                        str(repetition),
                    ]
                )
            )
    return lines


def spread(values):
    values = sorted(values)
    return {
        "n": len(values),
        "median": statistics.median(values),
        "min": values[0],
        "max": values[-1],
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def summarize(output):
    rows = [json.loads(path.read_text()) for path in sorted((output / "rows").glob("*.json"))]
    invalid = [row for row in rows if not row["outcome"]["valid"]]
    groups = {}
    for row in rows:
        if row["outcome"]["valid"]:
            groups.setdefault((row["series"], row["workers"]), []).append(row)
    results = []
    for series, workers in configurations():
        members = groups.get((series, workers), [])
        if not members:
            continue
        entry = {
            "series": series,
            "workers": workers,
            "wall_seconds": spread([row["wall_seconds"] for row in members]),
            "cpu_seconds": spread([row["cpu_seconds"] for row in members]),
        }
        cgroup = [row["cgroup_cpu_seconds"] for row in members]
        entry["cgroup_cpu_seconds"] = None if None in cgroup else spread(cgroup)
        if members[0]["phases"] is not None:
            entry["phases"] = {
                key: statistics.median(row["phases"][key] for row in members)
                for key in members[0]["phases"]
                if all(row["phases"][key] is not None for row in members)
            }
        results.append(entry)
    baseline = next((entry for entry in results if entry["series"] == "plain"), None)
    for entry in results:
        for measure_name in ("wall_seconds", "cpu_seconds"):
            entry[measure_name.replace("_seconds", "_ratio_to_plain")] = (
                None
                if baseline is None
                else entry[measure_name]["median"] / baseline[measure_name]["median"]
            )
    environment_path = output / "environment.json"
    summary = {
        "environment": json.loads(environment_path.read_text())
        if environment_path.exists()
        else None,
        "invalid_rows": [
            f"{row['series']}-w{row['workers']}-r{row['repetition']}" for row in invalid
        ],
        "results": results,
    }
    (output / "results.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    lines = [
        "| Series | Workers | n | Wall median s (min-max) | CPU median s (min-max) | Wall/plain | CPU/plain |",
        "| --- | ---: | ---: | --- | --- | ---: | ---: |",
    ]
    for entry in results:
        wall, cpu = entry["wall_seconds"], entry["cpu_seconds"]
        lines.append(
            f"| {entry['series']} | {entry['workers']} | {wall['n']} | "
            f"{wall['median']:.3f} ({wall['min']:.3f}-{wall['max']:.3f}) | "
            f"{cpu['median']:.3f} ({cpu['min']:.3f}-{cpu['max']:.3f}) | "
            + (
                "n/a | n/a |"
                if entry["wall_ratio_to_plain"] is None
                else f"{entry['wall_ratio_to_plain']:.2f} | {entry['cpu_ratio_to_plain']:.2f} |"
            )
        )
    (output / "results.md").write_text("\n".join(lines) + "\n")
    return summary


def environment(output, label, head):
    """Host facts without host names, user names or local paths."""

    def read(path):
        try:
            return Path(path).read_text()
        except OSError:
            return ""

    release = dict(
        line.split("=", 1) for line in read("/etc/os-release").splitlines() if "=" in line
    )
    model = re.search(r"^model name\s*:\s*(.+)$", read("/proc/cpuinfo"), re.MULTILINE)
    memory = re.search(r"^MemTotal:\s*(\d+) kB$", read("/proc/meminfo"), re.MULTILINE)
    bwrap = shutil.which("bwrap")
    limits = {}
    cgroup_line = read("/proc/self/cgroup").strip().splitlines()[:1]
    if cgroup_line and cgroup_line[0].startswith("0::"):
        base = Path("/sys/fs/cgroup", cgroup_line[0][3:].lstrip("/"))
        for name in ("cpu.max", "memory.max", "pids.max"):
            value = read(base / name).strip()
            limits[name] = value or None

    def git(*arguments):
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=WORKLOAD.parent.parent,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    if head is None:
        head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    modified = None if status is None else bool(status)
    from importlib.metadata import PackageNotFoundError, version

    packages = {}
    for name in ("pytest", "pytest-telescope", "pluggy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    data = {
        "label": label,
        "source_head": head,
        "source_modified": modified,
        "os": release.get("PRETTY_NAME", "").strip('"') or platform.system(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_model": model.group(1).strip() if model else None,
        "logical_cpus": os.cpu_count(),
        "memory_bytes": int(memory.group(1)) * 1024 if memory else None,
        "outer_cgroup_limits": limits or None,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "packages": packages,
        "bubblewrap": subprocess.run(
            [bwrap, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if bwrap
        else None,
        "workload_cases": CASES,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "environment.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("environment", "plan", "measure", "summarize"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", type=Path, required=True)
        if name == "environment":
            sub.add_argument("--label")
            sub.add_argument("--head")
        if name == "plan":
            sub.add_argument("--repetitions", type=int, default=5)
        if name == "measure":
            sub.add_argument("--series", choices=SERIES, required=True)
            sub.add_argument("--workers", type=int, choices=WORKERS, required=True)
            sub.add_argument("--repetition", type=int, required=True)
    options = parser.parse_args(argv)
    output = options.output.resolve()
    if options.command == "environment":
        print(json.dumps(environment(output, options.label, options.head), indent=1))
    elif options.command == "plan":
        print("\n".join(plan(output, options.repetitions)))
    elif options.command == "measure":
        row = measure(output, options.series, options.workers, options.repetition)
        print(json.dumps(row, sort_keys=True))
        return 0 if row["outcome"]["valid"] else 1
    else:
        summary = summarize(output)
        print((output / "results.md").read_text(), end="")
        return 1 if summary["invalid_rows"] else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
