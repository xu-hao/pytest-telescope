"""The benchmark's scoped workload runs fresh in real Bubblewrap sandboxes.

This control launches bwrap and is never skipped, so the benchmark's scoped series are
known to work before they are measured.
"""

import importlib.util
from pathlib import Path

import pytest

from pytest_telescope import controller

HARNESS = Path(__file__).resolve().parents[1] / "benchmarks" / "run.py"


@pytest.fixture(scope="module")
def harness():
    specification = importlib.util.spec_from_file_location("benchmark_harness", HARNESS)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_scoped_benchmark_workload_passes_fresh_in_sandboxes(tmp_path, harness, mode):
    project = harness.write_variant(tmp_path / "workload", scoped=True)
    report = controller.run(project, no_cache=True, workers=2, execution_mode=mode)
    assert harness.telescope_outcome(f"scoped-{mode}", report, 0)["valid"], report
    sandboxes = [group for group in report["groups"] if group["route"] == "scoped"]
    assert len(sandboxes) == (harness.CASES if mode == "case" else 3)
