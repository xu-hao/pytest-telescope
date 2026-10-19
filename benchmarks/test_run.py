"""Behavioral checks for benchmark outcome validation and external accounting."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from run import measure, result_from_log, validate_result


def result():
    nodes = [
        "tests/test_scoring.py::test_empty",
        "tests/test_scoring.py::test_customer_scores",
        "tests/test_demand.py::test_empty",
        "tests/test_demand.py::test_demand_projection",
        "tests/test_pricing.py::test_empty",
        "tests/test_pricing.py::test_price_schedule",
        "tests/test_broad.py::test_project_file",
    ]
    units = []
    for node in nodes:
        executed = node.startswith(("tests/test_scoring.py", "tests/test_broad.py"))
        units.append(
            {
                "name": hashlib.sha256(node.encode()).hexdigest(),
                "file": node.split("::")[0],
                "outcome": "executed" if executed else "reused",
                "collected": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "worker_seconds": 0.2 if executed else 0,
                "cpu_seconds": 0.1 if executed else 0,
            }
        )
    return {
        "counts": {
            "executed": 3,
            "reused": 4,
            "failed": 0,
            "infrastructure_error": 0,
            "not-selected": 0,
        },
        "cases": {"collected": 7, "passed": 7, "failed": 0, "skipped": 0},
        "case_outcomes": {
            "fresh_passed": 3,
            "cached_passed": 4,
            "fresh_skipped": 0,
            "cached_skipped": 0,
        },
        "collection": {
            "outcome": "executed",
            "collected": 7,
            "failures": [],
            "worker_seconds": 0.2,
            "cpu_seconds": 0.1,
        },
        "worker_cpu_seconds": 0.4,
        "units": units,
    }


class BenchmarkTests(unittest.TestCase):
    def test_accepts_expected_selective_outcome(self):
        validate_result(result(), "narrow_code")

    def test_rejects_wrong_case_even_when_totals_match(self):
        data = result()
        data["units"][0]["outcome"] = "reused"
        data["units"][2]["outcome"] = "executed"
        with self.assertRaisesRegex(RuntimeError, "wrong case invalidated"):
            validate_result(data, "narrow_code")

    def test_rejects_missing_or_failed_cases(self):
        for collected, passed, failed in ((6, 6, 0), (7, 6, 1)):
            data = result()
            data["cases"].update(collected=collected, passed=passed, failed=failed)
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, "case counts"):
                validate_result(data, "narrow_code")

    def test_rejects_cached_cases_reported_as_fresh(self):
        data = result()
        data["case_outcomes"].update(fresh_passed=7, cached_passed=0)
        with self.assertRaisesRegex(RuntimeError, "executed/reused case counts"):
            validate_result(data, "narrow_code")

    def test_rejects_duplicate_inventory(self):
        data = result()
        data["units"].append(copy.deepcopy(data["units"][0]))
        with self.assertRaisesRegex(RuntimeError, "case inventory"):
            validate_result(data, "narrow_code")

    def test_rejects_infrastructure_failure(self):
        data = result()
        data["counts"].update(executed=0, infrastructure_error=1)
        with self.assertRaisesRegex(RuntimeError, "expected"):
            validate_result(data, "narrow_code")

    def test_rejects_same_file_wrong_case_resource_consumer(self):
        data = result()
        data["counts"].update(executed=2, reused=5)
        data["case_outcomes"].update(fresh_passed=2, cached_passed=5)
        data["units"][0].update(outcome="reused", cpu_seconds=0, worker_seconds=0)
        data["worker_cpu_seconds"] = 0.3
        validate_result(data, "case_resource")
        data["units"][0].update(outcome="executed", cpu_seconds=0.1, worker_seconds=0.2)
        data["units"][1].update(outcome="reused", cpu_seconds=0, worker_seconds=0)
        with self.assertRaisesRegex(RuntimeError, "wrong case invalidated"):
            validate_result(data, "case_resource")

    def test_rejects_incorrect_case_file(self):
        data = result()
        data["units"][0]["file"] = "tests/test_other.py"
        with self.assertRaisesRegex(RuntimeError, "identity or source file"):
            validate_result(data, "narrow_code")

    def test_rejects_aggregated_unit(self):
        data = result()
        data["units"][0].update(collected=2, passed=2)
        with self.assertRaisesRegex(RuntimeError, "exactly one passing case"):
            validate_result(data, "narrow_code")

    def test_rejects_incomplete_collection(self):
        data = result()
        data["collection"]["collected"] = 6
        with self.assertRaisesRegex(RuntimeError, "collection outcome or inventory"):
            validate_result(data, "narrow_code")

    def test_rejects_cached_worker_cpu(self):
        data = result()
        data["units"][2]["cpu_seconds"] = 1
        with self.assertRaisesRegex(RuntimeError, "zero current worker accounting"):
            validate_result(data, "narrow_code")

    def test_rejects_missing_collection_cpu(self):
        data = result()
        data["worker_cpu_seconds"] = 0.3
        with self.assertRaisesRegex(RuntimeError, "CPU must reconcile"):
            validate_result(data, "narrow_code")

    def test_requires_structured_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(result()))
            self.assertEqual(result_from_log(path), result())
            path.write_text("[]")
            with self.assertRaisesRegex(RuntimeError, "JSON object"):
                result_from_log(path)

    def test_external_measurement_records_real_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            row = measure(
                [sys.executable, "-c", "print(sum(i*i for i in range(100_000)))"],
                path,
                path,
                "computation",
            )
            self.assertGreater(row["elapsed_seconds"], 0)
            self.assertGreater(row["controller_cpu_seconds"], 0)
            self.assertEqual((path / row["log"]).read_text().strip(), "333328333350000")

    def test_failed_process_is_not_a_benchmark_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "exit 3"):
                measure([sys.executable, "-c", "raise SystemExit(3)"], path, path, "failure")


if __name__ == "__main__":
    unittest.main()
