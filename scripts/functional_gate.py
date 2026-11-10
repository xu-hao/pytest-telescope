"""Guard the installed candidate before functional tests or portable harness checks."""

import argparse
import json
import os
import sys
from pathlib import Path

# -I excludes both the checkout and script directory. Admit only the trusted gate
# helpers here; candidate imports must still resolve to the verified installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate import read_json, verify_candidate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("tests", "harness"))
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    claimed = os.environ.get("TMPDIR")
    if (
        not claimed
        or not Path(claimed).is_absolute()
        or not Path(claimed).is_dir()
        or not args.scratch.resolve().is_relative_to(Path(claimed).resolve())
    ):
        raise ValueError("functional gate scratch must be below the actual caller TMPDIR")
    binding = read_json(args.binding, args.binding_sha256)
    origin = verify_candidate(binding, first_import=True)
    import pytest_telescope

    if Path(pytest_telescope.__file__) != Path(origin):
        raise ValueError("installed candidate origin changed during import")
    print(
        json.dumps(
            {
                "installed_candidate_origin": origin,
                "source": binding["source"],
                "wheel_sha256": binding["wheel"]["sha256"],
                "mode": args.mode,
            }
        ),
        flush=True,
    )
    for name in (
        "TELESCOPE_FIXTURE_NODE",
        "TELESCOPE_FIXTURE_CHROMIUM",
        "TELESCOPE_FIXTURE_ADMISSION",
        "TELESCOPE_FIXTURE_ADMISSION_SHA256",
    ):
        os.environ.pop(name, None)
    sys.path.insert(0, str(Path(binding["root"]) / "tests"))
    if args.mode == "tests":
        import pytest

        status = int(pytest.main(["-q", "--basetemp", str(args.scratch / "tests"), "tests"]))
    else:
        import unittest

        suite = unittest.defaultTestLoader.discover("benchmarks", pattern="test_*.py")
        status = int(not unittest.TextTestRunner().run(suite).wasSuccessful())
    verify_candidate(binding)
    if read_json(args.binding, args.binding_sha256) != binding:
        raise ValueError("candidate binding changed during qualification")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
