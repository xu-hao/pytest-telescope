"""Guard the installed candidate before functional tests or portable harness checks.

Parallel functional workers load this module as a plugin before pytest configures, so it
opts out of assertion rewriting (PYTEST_DONT_REWRITE); it holds no test assertions.
"""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

# -I excludes both the checkout and script directory. Admit only the trusted gate
# helpers here; candidate imports must still resolve to the verified installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate import read_json, verify_candidate, write_json  # noqa: E402

# Parallel workers receive the recorded membership under this name and return their own.
RECEIPT = "telescope_functional_membership"
# Parallel workers receive the verified installed candidate origin under this name.
ORIGIN = "telescope_functional_candidate_origin"
# Each parallel worker loads this exact guard file before it configures pytest.
WORKER_GUARD = """
import sys
from pathlib import Path
sys.path.insert(0, {directory!r})
import functional_gate
if Path(functional_gate.__file__).resolve() != Path({filename!r}):
    raise ImportError("parallel worker loaded a different functional guard")
"""


def selection_plugin(pytest, profile, membership, version, expected=None):
    """Record the actual collection and permit only the explicit profile exclusion.

    With `expected`, a parallel worker instead proves that its own collection equals the
    recorded membership exactly; it writes no file and runs no body on a mismatch.
    """

    class Selection:
        def __init__(self):
            self.pending_deselection = None
            self.external_deselection = False
            self.collection_failed = False
            self.selected = None
            self.deselected = []
            self.digest = None
            self.record = None

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_deselected(self, items):
            expected = self.pending_deselection
            self.pending_deselection = None
            owned = (
                expected is not None
                and items is expected[0]
                and len(items) == len(expected[1])
                and all(item is original for item, original in zip(items, expected[1]))
            )
            if items and not owned:
                self.external_deselection = True
            yield

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
            self.selected = current
            if profile == "core":
                removed = [
                    item for item in items if item.get_closest_marker("requires_fixture_guard")
                ]
                self.deselected = [item.nodeid for item in removed]
                excluded = set(self.deselected)
                items[:] = [item for item in items if item.nodeid not in excluded]
                self.selected = [item.nodeid for item in items]
                self.pending_deselection = (removed, tuple(removed))
                try:
                    config.hook.pytest_deselected(items=removed)
                finally:
                    self.pending_deselection = None

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
            if expected is None:
                self.digest = write_json(membership, self.record)
            elif self.record != expected:
                raise pytest.UsageError("parallel worker membership differs from its receipt")
            else:
                session.config.workeroutput[RECEIPT] = self.record
            yield

    return Selection()


def pytest_configure(config):
    """Guard each parallel functional worker; inert in every other pytest process."""
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        return
    import pytest

    expected = workerinput.get(RECEIPT)
    if type(expected) is not dict or set(expected) != {
        "schema",
        "profile",
        "pytest_version",
        "selected",
        "deselected",
    }:
        raise pytest.UsageError("parallel functional worker has no membership receipt")
    origin = workerinput.get(ORIGIN)
    if type(origin) is not str:
        raise pytest.UsageError("parallel functional worker has no verified candidate origin")
    import pytest_telescope

    # The serial gate process makes this same comparison after its first import.
    filename = getattr(pytest_telescope, "__file__", None)
    if type(filename) is not str or Path(filename) != Path(origin):
        raise pytest.UsageError("parallel functional worker imported another candidate origin")
    config.pluginmanager.register(
        selection_plugin(pytest, expected["profile"], None, pytest.__version__, expected=expected),
        "telescope-functional-selection",
    )


def parallel_plugin(pytest, record, origin):
    """Bind every parallel worker to the recorded membership and verified candidate origin.

    Both reach a worker before it configures, so it refuses before any test is scheduled.
    """

    class Parallel:
        def __init__(self):
            self.collected = set()
            self.confirmed = set()

        def pytest_xdist_newgateway(self, gateway):
            guard = Path(__file__).resolve()
            source = WORKER_GUARD.format(directory=str(guard.parent), filename=str(guard))
            gateway.remote_exec(source).waitclose(60)

        def pytest_configure_node(self, node):
            node.workerinput[RECEIPT] = record
            node.workerinput[ORIGIN] = origin

        @pytest.hookimpl(tryfirst=True)
        def pytest_xdist_node_collection_finished(self, node, ids):
            # Scheduling follows this hook; a refusal here stops before any test body.
            if list(ids) != record["selected"]:
                raise pytest.UsageError("parallel worker collection differs from the membership")
            self.collected.add(node.gateway.id)

        def pytest_testnodedown(self, node, error):
            if getattr(node, "workeroutput", {}).get(RECEIPT) == record:
                self.confirmed.add(node.gateway.id)

    return Parallel()


def run_parallel(pytest, workers, scratch, selection, origin):
    """Record membership from one serial collection, then run files on `workers` processes.

    Each worker must import the candidate from `origin`, the verified installed package.
    """
    try:
        import xdist  # noqa: F401
    except ImportError:
        raise ValueError("parallel functional workers require pytest-xdist") from None
    status = int(
        pytest.main(
            ["-qq", "--collect-only", "--basetemp", str(scratch / "collection"), "tests"],
            plugins=[selection],
        )
    )
    if status or selection.record is None:
        return status
    controller = parallel_plugin(pytest, selection.record, origin)
    # Each worker starts like this gate process: isolated, without bytecode writes.
    python = shlex.join([sys.executable, "-I"])
    status = int(
        pytest.main(
            [
                "-q",
                "--basetemp",
                str(scratch / "tests"),
                "-p",
                "xdist",
                "-p",
                "functional_gate",
                "--dist",
                "loadfile",
                "--max-worker-restart",
                "0",
                "--tx",
                f"{workers}*popen//python={python}//dont_write_bytecode",
                "tests",
            ],
            plugins=[controller],
        )
    )
    if status == 0 and (
        len(controller.collected) != workers or controller.confirmed != controller.collected
    ):
        raise ValueError("parallel workers did not confirm the functional membership")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("tests", "harness"))
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--functional-profile", required=True, choices=("core", "all"))
    parser.add_argument("--membership", type=Path)
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()
    if (args.mode == "tests") != (args.membership is not None):
        parser.error("tests mode requires --membership; harness mode does not accept it")
    if args.workers is not None and (args.mode != "tests" or args.workers < 1):
        parser.error("--workers takes a positive count of tests-mode worker processes")
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
        if (args.workers or 1) == 1:
            status = int(
                pytest.main(
                    ["-q", "--basetemp", str(args.scratch / "tests"), "tests"], plugins=[selection]
                )
            )
        else:
            status = run_parallel(pytest, args.workers, args.scratch, selection, origin)
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
