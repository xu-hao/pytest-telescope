"""Measure fresh-case parallelism on a newly generated two-suite project.

Every phase validates exact case identities and counts. No project source is installed
in the runtime image. Output is local evidence; publish only the sanitized results.json.
"""

import argparse
import hashlib
import json
import platform
import sys
import time
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path

from run import measure, result_from_log

from pytest_telescope import runtime
from pytest_telescope.model import load
from pytest_telescope.project_metadata import materialize_metadata


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def generate(root, count):
    """Two independent suites, eight actual parameter cases per test module."""
    configuration = []
    cases = []
    for package in ("core", "addon"):
        configuration.append(
            f"[tool.pytest_telescope.distributions.{package}]\n"
            'format = "pep621-static-v1"\n'
            f'metadata = "packages/{package}/pyproject.toml"\n'
            f"[tool.pytest_telescope.suites.{package}]\n"
            f'root = "tests/{package}"\nconfig = "tests/{package}/pytest.ini"\n'
            f'test_roots = ["tests/{package}"]\n'
            f"[tool.pytest_telescope.bundles.{package}]\n"
            f'distributions = ["{package}"]\ninputs = ["packages/{package}/{package}.py"]\n'
        )
        write(
            root,
            f"packages/{package}/pyproject.toml",
            f'[project]\nname="example-{package}"\nversion="1.0.0"\n[project.entry-points."example.plugins"]\n{package}="{package}:compute"\n',
        )
        write(
            root,
            f"packages/{package}/{package}.py",
            "def compute(value):\n    return sum((value + i) % 97 for i in range(100000))\n",
        )
        write(
            root,
            f"tests/{package}/pytest.ini",
            f"[pytest]\npythonpath = ../../packages/{package}\n",
        )
        for module in range(count // 16):
            parameters = []
            for offset in range(8):
                number = module * 8 + offset
                resource = f"resources/{package}/{number:04}.json"
                write(root, resource, json.dumps({"value": number}) + "\n")
                parameters.append(
                    f'pytest.param("{number:04}", marks=pytest.mark.telescope(inputs=["{resource}"]), id="case{number:04}")'
                )
                node = f"tests/{package}/test_{module:03}.py::test_value[case{number:04}]"
                cases.append(
                    (
                        package,
                        hashlib.sha256(
                            json.dumps([package, node], separators=(",", ":")).encode()
                        ).hexdigest(),
                        node.split("::")[0],
                        resource,
                    )
                )
            write(
                root,
                f"tests/{package}/test_{module:03}.py",
                "import json\nfrom pathlib import Path\nfrom importlib.metadata import version, entry_points\nimport pytest\n"
                + f'pytestmark = pytest.mark.telescope(bundles=["{package}"])\n'
                + '@pytest.mark.parametrize("key", ['
                + ",\n".join(parameters)
                + "])\n"
                + "def test_value(key):\n"
                + f'    assert version("example-{package}") == "1.0.0"\n    compute = entry_points(group="example.plugins", name="{package}")[0].load()\n'
                + f'    value = json.loads((Path(__file__).resolve().parents[2] / "resources/{package}" / (key + ".json")).read_text())["value"]\n'
                + "    assert compute(value) >= 0\n",
            )
    write(root, "pyproject.toml", "\n".join(configuration))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", type=int, default=128)
    parser.add_argument("--parallel", type=int, default=4)
    options = parser.parse_args()
    if options.cases < 16 or options.cases > 1024 or options.cases % 16:
        parser.error("cases must be a multiple of 16 between 16 and 1024")
    if not 2 <= options.parallel <= 32:
        parser.error("parallel must be between 2 and 32")
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / "project"
    project.mkdir()
    cases = generate(project, options.cases)
    expected = {case_id: (suite, filename) for suite, case_id, filename, _ in cases}
    metadata = output / "ordinary-metadata"
    manifest = load(project)
    materialize_metadata(manifest.distributions, sorted(manifest.distributions), metadata)
    ordinary = []
    for suite in ("core", "addon"):
        junit = output / f"ordinary-{suite}.xml"
        command = [
            sys.executable,
            "-c",
            "import sys;sys.path.insert(0,sys.argv.pop(1));import pytest;raise SystemExit(pytest.main(sys.argv[1:]))",
            str(metadata),
            "-p",
            "telescope",
            "-q",
            "-c",
            "pytest.ini",
            "--rootdir=.",
            f"--junitxml={junit}",
        ]
        row = measure(command, project / "tests" / suite, output, "ordinary-" + suite)
        records = list(ET.parse(junit).getroot().iter("testcase"))
        if len(records) != options.cases // 2 or any(
            child.tag in {"failure", "error", "skipped"} for record in records for child in record
        ):
            raise RuntimeError("ordinary suite did not pass the expected actual cases")
        ordinary.append(row)
    started = time.perf_counter()
    prepared = runtime.build_runtime(project)
    setup = time.perf_counter() - started
    phases = []
    try:
        for concurrency in (1, options.parallel):
            resource = project / cases[0][3]
            resource.write_text('{"value": 0}\n')
            budget = dict(
                cpus=concurrency,
                memory_bytes=536870912 * concurrency,
                processes=128 * concurrency,
                scratch_bytes=67108864 * concurrency,
            )
            flags = [
                item
                for key, value in budget.items()
                for item in ("--budget-" + key.replace("_", "-"), str(value))
            ]
            for phase in ("cold", "warm", "selective"):
                if phase == "selective":
                    resource.write_text('{"value": 1}\n')
                label = f"workers-{concurrency}-{phase}"
                command = [
                    sys.executable,
                    "-I",
                    "-m",
                    "pytest_telescope",
                    "run",
                    *flags,
                    *(["--force"] if phase == "cold" else []),
                    "--",
                    "-q",
                ]
                row = measure(command, project, output, label)
                result = result_from_log(output / row["log"])
                fresh = options.cases if phase == "cold" else 1 if phase == "selective" else 0
                expected_counts = dict(
                    executed=fresh,
                    reused=options.cases - fresh,
                    failed=0,
                    unsupported=0,
                    infrastructure_error=0,
                    selected=options.cases,
                )
                expected_counts["not-selected"] = 0
                if (
                    result["schema"] != 1
                    or not result["inventory_complete"]
                    or result["status"] != "passed"
                    or result["counts"] != expected_counts
                ):
                    raise RuntimeError(f"{label}: incomplete or unexpected case accounting")
                if {
                    unit["case_id"]: (unit["suite"], unit["file"]) for unit in result["units"]
                } != expected:
                    raise RuntimeError(f"{label}: case membership changed")
                if phase == "selective" and [
                    unit["case_id"] for unit in result["units"] if unit["outcome"] == "executed"
                ] != [cases[0][1]]:
                    raise RuntimeError(f"{label}: wrong resource consumer reran")
                if result["resources"]["concurrency"] != concurrency:
                    raise RuntimeError(f"{label}: admitted worker count mismatch")
                row.update(
                    phase=phase,
                    concurrency=concurrency,
                    budget=budget,
                    counts=result["counts"],
                    inventory_digest=result["inventory_digest"],
                    collection=result["collection"]["outcome"],
                    worker_cpu_seconds=result["worker_cpu_seconds"],
                    setup_seconds=result["setup_seconds"],
                )
                phases.append(row)
                print(
                    f"Validated {label}: {fresh} fresh, {options.cases - fresh} reused", flush=True
                )
    finally:
        runtime.command(["docker", "image", "rm", prepared["image"]])
    report = dict(
        schema=1,
        python=platform.python_version(),
        pytest=version("pytest"),
        pytest_telescope=version("pytest-telescope"),
        cases=options.cases,
        suites=2,
        modules=options.cases // 8,
        ordinary_elapsed_seconds=sum(row["elapsed_seconds"] for row in ordinary),
        ordinary_controller_cpu_seconds=sum(row["controller_cpu_seconds"] for row in ordinary),
        runtime_preparation_seconds=setup,
        phases=phases,
        method="One newly generated fixture; two ordinary independent suites with temporary metadata, then forced-cold/warm/one-resource mutation at explicit worker budgets. No universal speedup claim. Runtime image removed; logs and synthetic source retained locally.",
    )
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
