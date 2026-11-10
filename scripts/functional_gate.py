"""Guard the installed candidate before functional tests or portable harness checks."""

import argparse
import json
import os
import sys
from pathlib import Path

# -I excludes both the checkout and script directory. Admit only the trusted gate
# helpers here; candidate imports must still resolve to the verified installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate import read_json, verify_candidate, write_json  # noqa: E402


def selection_plugin(pytest, profile, membership, version):
    """Record the actual collection and permit only the explicit profile exclusion."""

    class Selection:
        def __init__(self):
            self.own_deselection = False
            self.external_deselection = False
            self.collection_failed = False
            self.selected = None
            self.deselected = []
            self.digest = None
            self.record = None

        def pytest_deselected(self, items):
            if items and not self.own_deselection:
                self.external_deselection = True

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_failed = True

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_collection_modifyitems(self, config, items):
            original = list(items)
            yield
            identities = [item.nodeid for item in original]
            current = [item.nodeid for item in items]
            if (
                self.external_deselection
                or len(set(identities)) != len(identities)
                or len(current) != len(identities)
                or set(current) != set(identities)
            ):
                raise pytest.UsageError("functional profile refuses external collection changes")
            if profile == "core":
                removed = [
                    item for item in items if item.get_closest_marker("requires_fixture_guard")
                ]
                self.deselected = [item.nodeid for item in removed]
                excluded = set(self.deselected)
                items[:] = [item for item in items if item.nodeid not in excluded]
                self.own_deselection = True
                try:
                    config.hook.pytest_deselected(items=removed)
                finally:
                    self.own_deselection = False
            self.selected = [item.nodeid for item in items]

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtestloop(self, session):
            # Every collection callback has returned before this execution boundary.
            if self.collection_failed:
                yield
                return
            if self.selected is None:
                raise pytest.UsageError("functional profile did not observe collection")
            selected = [item.nodeid for item in session.items]
            if (
                self.external_deselection
                or len(selected) != len(self.selected)
                or set(selected) != set(self.selected)
            ):
                raise pytest.UsageError("functional profile membership changed after selection")
            self.record = {
                "schema": 1,
                "profile": profile,
                "pytest_version": version,
                "selected": selected,
                "deselected": self.deselected,
            }
            self.digest = write_json(membership, self.record)
            yield

    return Selection()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("tests", "harness"))
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--functional-profile", required=True, choices=("core", "all"))
    parser.add_argument("--membership", type=Path)
    args = parser.parse_args()
    if (args.mode == "tests") != (args.membership is not None):
        parser.error("tests mode requires --membership; harness mode does not accept it")
    if (
        args.membership is not None
        and args.membership.parent.resolve() != args.binding.parent.resolve()
    ):
        parser.error("membership must be in the owned candidate artifact directory")
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
                "functional_profile": args.functional_profile,
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

        if os.environ.get("PYTEST_ADDOPTS", "").strip():
            raise ValueError("ambient pytest arguments are not a functional profile")
        if args.functional_profile == "all":
            from pytest_telescope.fixture_guard import framework_definition_ids

            framework_definition_ids(pytest.__version__)
        selection = selection_plugin(
            pytest, args.functional_profile, args.membership, pytest.__version__
        )
        status = int(
            pytest.main(
                ["-q", "--basetemp", str(args.scratch / "tests"), "tests"], plugins=[selection]
            )
        )
        if selection.digest is not None:
            if read_json(args.membership, selection.digest) != selection.record:
                raise ValueError("functional membership changed during qualification")
        elif status == 0:
            raise ValueError("successful functional collection did not record membership")
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
