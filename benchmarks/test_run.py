"""Behavioral checks for benchmark outcome validation and external accounting."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from run import measure, result_from_log, validate_result


def result():
    return {
        "counts": {
            "executed": 1,
            "reused": 2,
            "failed": 0,
            "infrastructure_error": 0,
            "not-selected": 0,
        },
        "cases": {"collected": 6, "passed": 6, "failed": 0, "skipped": 0},
        "case_outcomes": {
            "fresh_passed": 2,
            "cached_passed": 4,
            "fresh_skipped": 0,
            "cached_skipped": 0,
        },
        "units": [
            {"name": name, "outcome": "executed" if name == "scoring" else "reused", "collected": 2}
            for name in ("scoring", "demand", "pricing")
        ],
    }


class BenchmarkTests(unittest.TestCase):
    def test_accepts_expected_selective_outcome(self):
        validate_result(result(), "narrow_code")

    def test_rejects_wrong_target_even_when_totals_match(self):
        data = result()
        data["units"][0]["outcome"] = "reused"
        data["units"][1]["outcome"] = "executed"
        with self.assertRaisesRegex(RuntimeError, "wrong target invalidated"):
            validate_result(data, "narrow_code")

    def test_rejects_missing_or_failed_cases(self):
        for collected, passed, failed in ((5, 5, 0), (6, 5, 1)):
            data = result()
            data["cases"].update(collected=collected, passed=passed, failed=failed)
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, "case counts"):
                validate_result(data, "narrow_code")

    def test_rejects_cached_cases_reported_as_fresh(self):
        data = result()
        data["case_outcomes"].update(fresh_passed=6, cached_passed=0)
        with self.assertRaisesRegex(RuntimeError, "executed/reused case counts"):
            validate_result(data, "narrow_code")

    def test_rejects_duplicate_inventory(self):
        data = result()
        data["units"].append(copy.deepcopy(data["units"][0]))
        with self.assertRaisesRegex(RuntimeError, "exactly two cases"):
            validate_result(data, "narrow_code")

    def test_rejects_infrastructure_failure(self):
        data = result()
        data["counts"].update(executed=0, infrastructure_error=1)
        with self.assertRaisesRegex(RuntimeError, "expected"):
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
