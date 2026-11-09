"""Guard the installed candidate before preparation, functional tests or harness checks."""

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# -I excludes both the checkout and script directory. Admit only the trusted gate
# helpers here; candidate imports must still resolve to the verified installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate import file_identity, read_json, verify_candidate, write_json  # noqa: E402


def admission(path, expected, directory):
    receipt = read_json(path, expected)
    if (type(receipt) is not dict or set(receipt) != {"schema", "profiles"}
            or type(receipt["schema"]) is not int or receipt["schema"] != 1
            or type(receipt["profiles"]) is not dict
            or set(receipt["profiles"]) != {"node", "installed"}):
        raise ValueError("functional readiness requires exactly node and installed profiles")
    expected_documents = {"node": {"worker", "frontend"}, "installed": {"worker", "unselected-baseline"}}
    for name, profile in receipt["profiles"].items():
        if type(profile) is not dict or set(profile) != {"identity", "documents"}:
            raise ValueError("invalid functional profile")
        documents = copy.deepcopy(profile["documents"])
        if type(documents) is not dict or set(documents) != expected_documents[name]:
            raise ValueError("functional profile has no declarations")
        # Reuse the guarded candidate's strict manifest parser. This only reads
        # the owned JSON copies; it does not capture source inputs or run a tool.
        from pytest_telescope.tool_context import parse

        with tempfile.TemporaryDirectory(prefix="receipt-validation-", dir=directory) as owned:
            project = Path(owned)
            for index, document in enumerate(documents.values()):
                filename = f"declaration-{index}.json"
                write_json(project / filename, document)
                parsed = parse(project, "gate", {"format": "frozen-v1", "manifest": filename})
                if not parsed.document["roots"]:
                    raise ValueError("functional profile has an empty declaration")
        for document in documents.values():
            for root in document["roots"]:
                if root["id"] == "driver":
                    root["source"] = "<owned-driver>"
        actual = hashlib.sha256(json.dumps(documents, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if actual != profile["identity"]:
            raise ValueError("functional prerequisite declaration identity disagrees")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "tests", "harness"))
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--admission", required=True, type=Path)
    parser.add_argument("--admission-sha256")
    args = parser.parse_args()
    claimed = os.environ.get("TMPDIR")
    if (not claimed or not Path(claimed).is_absolute() or not Path(claimed).is_dir()
            or not args.scratch.resolve().is_relative_to(Path(claimed).resolve())):
        raise ValueError("functional gate scratch must be below the actual caller TMPDIR")
    binding = read_json(args.binding, args.binding_sha256)
    origin = verify_candidate(binding, first_import=True)
    import pytest_telescope

    if Path(pytest_telescope.__file__) != Path(origin):
        raise ValueError("installed candidate origin changed during import")
    print(json.dumps({"installed_candidate_origin": origin, "source": binding["source"],
                      "wheel_sha256": binding["wheel"]["sha256"], "mode": args.mode}), flush=True)
    os.environ["TELESCOPE_FIXTURE_NODE"] = binding["node"]["path"]
    os.environ.pop("TELESCOPE_FIXTURE_CHROMIUM", None)
    sys.path.insert(0, str(Path(binding["root"]) / "tests"))
    status = 0
    if args.mode == "prepare":
        from frozen_tool_prepare_programs import prepare

        directory = args.scratch / "preparation"
        directory.mkdir()
        prepare(directory, args.receipt, profiles=("node", "installed"))
        receipt_hash = file_identity(args.receipt, 4 * 1024 * 1024)["sha256"]
        admission(args.receipt, receipt_hash, args.scratch)
        verify_candidate(binding)
        # Publish readiness only after preparation and all its postconditions pass.
        write_json(args.admission, {"schema": 1, "binding_sha256": args.binding_sha256,
                                   "receipt": str(args.receipt), "receipt_sha256": receipt_hash,
                                   "source": binding["source"], "interpreter": binding["interpreter"],
                                   "wheel_sha256": binding["wheel"]["sha256"]})
    else:
        if not args.receipt_sha256 or not args.admission_sha256:
            raise ValueError("functional execution requires exact new readiness hashes")
        ready = read_json(args.admission, args.admission_sha256)
        expected = {"schema": 1, "binding_sha256": args.binding_sha256,
                    "receipt": str(args.receipt), "receipt_sha256": args.receipt_sha256,
                    "source": binding["source"], "interpreter": binding["interpreter"],
                    "wheel_sha256": binding["wheel"]["sha256"]}
        if ready != expected:
            raise ValueError("functional admission belongs to a different candidate")
        admission(args.receipt, args.receipt_sha256, args.scratch)
        os.environ["TELESCOPE_FIXTURE_ADMISSION"] = str(args.receipt)
        os.environ["TELESCOPE_FIXTURE_ADMISSION_SHA256"] = args.receipt_sha256
        if args.mode == "tests":
            import pytest

            status = int(pytest.main(["-q", "--basetemp", str(args.scratch / "tests"), "tests"]))
        else:
            import unittest

            suite = unittest.defaultTestLoader.discover("benchmarks", pattern="test_*.py")
            status = int(not unittest.TextTestRunner().run(suite).wasSuccessful())
        verify_candidate(binding)
        admission(args.receipt, args.receipt_sha256, args.scratch)
        if read_json(args.admission, args.admission_sha256) != expected:
            raise ValueError("functional admission changed during execution")
    if read_json(args.binding, args.binding_sha256) != binding:
        raise ValueError("candidate binding changed during qualification")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
