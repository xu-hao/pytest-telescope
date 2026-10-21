"""Harness checks that need no Bubblewrap: workload variants, accounting and summaries."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import run


class WorkloadVariants(unittest.TestCase):
    def test_variants_differ_only_in_the_module_scope_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            ordinary = run.write_variant(Path(directory, "ordinary"), scoped=False)
            scoped = run.write_variant(Path(directory, "scoped"), scoped=True)
            files = sorted(path.relative_to(ordinary) for path in ordinary.rglob("*.py"))
            self.assertEqual(
                files, sorted(path.relative_to(scoped) for path in scoped.rglob("*.py"))
            )
            changed = 0
            for name in files:
                before = (ordinary / name).read_text().splitlines()
                after = (scoped / name).read_text().splitlines()
                self.assertEqual(len(before), len(after))
                differences = [pair for pair in zip(before, after) if pair[0] != pair[1]]
                changed += len(differences)
                for old, new in differences:
                    self.assertTrue(old.startswith("# telescope-scope: "))
                    self.assertTrue(new.startswith("pytestmark = pytest.mark.telescope(bundles="))
            self.assertEqual(changed, 3)
            for path in ordinary.rglob("test_*.py"):
                self.assertNotIn("pytest.mark.telescope", path.read_text())


class Accounting(unittest.TestCase):
    def test_cpu_of_grandchildren_is_counted(self):
        burn = "import time\nstarted = time.process_time()\nwhile time.process_time() - started < 0.3: pass\n"
        parent = f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {burn!r}], check=True)\n"
        with tempfile.TemporaryDirectory() as directory:
            measured = run.measure_tree(
                [sys.executable, "-c", parent], directory, Path(directory, "out")
            )
        self.assertEqual(measured["exit_code"], 0)
        self.assertGreaterEqual(measured["cpu_seconds"], 0.25)
        self.assertGreaterEqual(measured["wall_seconds"], 0.25)

    def test_telescope_outcomes_require_fresh_cases_on_the_intended_route(self):
        counts = {key: 0 for key in ("executed", "reused", "failed", "selected")}
        report = {
            "status": "passed",
            "inventory_complete": True,
            "route_counts": {
                "scoped": counts | {"executed": run.CASES, "selected": run.CASES},
                "ordinary": dict(counts),
            },
            "case_outcomes": {"fresh_passed": run.CASES},
            "execution": {"mode": "case"},
        }
        self.assertTrue(run.telescope_outcome("scoped-case", report, 0)["valid"])
        self.assertFalse(run.telescope_outcome("scoped-batch", report, 0)["valid"])
        self.assertFalse(run.telescope_outcome("ordinary", report, 0)["valid"])
        reused = json.loads(json.dumps(report))
        reused["route_counts"]["scoped"].update(executed=run.CASES - 1, reused=1)
        self.assertFalse(run.telescope_outcome("scoped-case", reused, 0)["valid"])
        self.assertFalse(run.telescope_outcome("scoped-case", report, 1)["valid"])
        self.assertFalse(run.telescope_outcome("scoped-case", {"status": "passed"}, 0)["valid"])


class Measurements(unittest.TestCase):
    """The plain and ordinary series never launch Bubblewrap."""

    def setUp(self):
        self.output = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.output)

    def test_plain_and_ordinary_series_measure_the_whole_workload(self):
        plain = run.measure(self.output, "plain", 1, 1)
        self.assertTrue(plain["outcome"]["valid"], plain)
        self.assertEqual(plain["outcome"]["counts"]["tests"], run.CASES)
        self.assertGreater(plain["cpu_seconds"], 0.5)
        ordinary = run.measure(self.output, "ordinary", 2, 1)
        self.assertTrue(ordinary["outcome"]["valid"], ordinary)
        self.assertEqual(ordinary["phases"]["scoped_sessions"], 0)
        self.assertGreater(ordinary["phases"]["ordinary_session_seconds"], 0)
        self.assertFalse(
            (self.output / "projects").exists() and any((self.output / "projects").iterdir())
        )
        environment = run.environment(self.output, "harness check", "0" * 40)
        self.assertEqual(environment["workload_cases"], run.CASES)
        summary = run.summarize(self.output)
        self.assertEqual(summary["invalid_rows"], [])
        self.assertEqual([entry["series"] for entry in summary["results"]], ["plain", "ordinary"])
        self.assertEqual(summary["results"][0]["wall_ratio_to_plain"], 1.0)
        self.assertIn("| ordinary | 2 | 1 |", (self.output / "results.md").read_text())

    def test_summary_reports_median_and_spread_and_flags_invalid_rows(self):
        rows = self.output / "rows"
        rows.mkdir()
        for repetition, (wall, cpu) in enumerate([(1.0, 2.0), (3.0, 4.0), (2.0, 3.0)], 1):
            row = {
                "series": "plain",
                "workers": 1,
                "repetition": repetition,
                "wall_seconds": wall,
                "cpu_seconds": cpu,
                "cgroup_cpu_seconds": None,
                "outcome": {"valid": True},
                "phases": None,
            }
            (rows / f"plain-w1-r{repetition}.json").write_text(json.dumps(row))
        broken = dict(row, repetition=4, outcome={"valid": False})
        (rows / "plain-w1-r4.json").write_text(json.dumps(broken))
        summary = run.summarize(self.output)
        (entry,) = summary["results"]
        self.assertEqual(entry["wall_seconds"]["median"], 2.0)
        self.assertEqual((entry["wall_seconds"]["min"], entry["wall_seconds"]["max"]), (1.0, 3.0))
        self.assertEqual(entry["cpu_seconds"]["n"], 3)
        self.assertEqual(summary["invalid_rows"], ["plain-w1-r4"])


class Plan(unittest.TestCase):
    def test_plan_interleaves_every_configuration_each_repetition(self):
        lines = run.plan(Path("/results"), 3)
        configurations = run.configurations()
        self.assertEqual(len(lines), 3 * len(configurations))
        self.assertEqual(len(configurations), 10)
        firsts = [lines[index * len(configurations)] for index in range(3)]
        self.assertEqual(len(set(firsts)), 3)
        for series, workers in configurations:
            matching = [
                line
                for line in lines
                if re.search(rf"--series {series} --workers {workers} ", line + " ")
            ]
            self.assertEqual(len(matching), 3)

    def test_plan_lines_run_the_measure_command(self):
        line = run.plan(Path("/results"), 1)[0]
        self.assertTrue(line.startswith("python benchmarks/run.py measure --output /results "))
        completed = subprocess.run(
            [sys.executable, str(Path(run.__file__)), "measure", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
