"""Explicit stand-ins for the sandbox boundary, for controls that must not launch bwrap."""

import contextlib
import hashlib
import shutil
import sys
import threading
from pathlib import Path

import pytest

from pytest_telescope import sandbox
from pytest_telescope.markers import discover
from pytest_telescope.model import fingerprint
from pytest_telescope.result import owner_identity

MODULE_SCOPE = "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"


def scoped_module(root, filename, functions):
    """Write a wholly scoped module and return its collected rows, as pytest reports them."""
    source = MODULE_SCOPE + "".join(f"def test_{name}(): pass\n" for name in functions)
    (root / filename).write_text(source)
    registry, _ = discover(source.encode(), filename)
    origins = sorted(row["id"] for row in registry)
    rows = []
    for name in functions:
        case = fingerprint(["default", f"{filename}::test_{name}"])
        rows.append(
            dict(
                case_id=case,
                file=filename,
                origins=origins,
                markers=["telescope"],
                owners=sorted(
                    [owner_identity(filename, []), owner_identity(filename, [f"test_{name}"])]
                ),
                parameters=[],
                selected=True,
                order=None,
                aliases=sorted([case, hashlib.sha256(filename.encode()).hexdigest()]),
            )
        )
    return rows


def number_rows(rows):
    for order, row in enumerate(rows):
        row["order"] = order
    return rows


@pytest.fixture
def sandbox_double(monkeypatch):
    """Replace preparation, owned scratch and worker launch; nothing reaches bwrap."""

    def install(execute, *, fail_outer=False):
        active = set()
        lock = threading.Lock()
        ordinal = 0

        @contextlib.contextmanager
        def invocation(root):
            nonlocal ordinal
            with lock:
                ordinal += 1
                number = ordinal
            directory = Path(root) / ".pytest-telescope" / "synthetic" / str(number)
            directory.mkdir(parents=True)
            active.add(directory)
            try:
                yield directory
            finally:
                active.discard(directory)
                shutil.rmtree(directory)
            if fail_outer and number == 1:
                raise sandbox.CleanupError("synthetic outer cleanup failure")

        monkeypatch.setattr(
            sandbox,
            "prepare",
            lambda root: sandbox.Sandbox(
                "/stand-in/bwrap", sys.executable, (), "/usr/bin", "f" * 64, {}
            ),
        )
        monkeypatch.setattr(sandbox, "recover", lambda root: None)
        monkeypatch.setattr(sandbox, "invocation", invocation)
        monkeypatch.setattr(sandbox, "execute", execute)
        return active

    return install


@pytest.fixture
def module_rows():
    """Write wholly scoped modules; return their numbered collected rows."""

    def write(root, modules):
        rows = []
        for filename, functions in modules:
            rows.extend(scoped_module(root, filename, functions))
        return number_rows(rows)

    return write


@pytest.fixture
def machines(tmp_path, sandbox_double, module_rows):
    """Identical checkouts with separate local caches, with a stand-in for the sandbox.

    `machine.executed` lists every case whose sandboxed session started.
    """
    executed, current = [], {}

    def execute(*args, request, **kwargs):
        if request["mode"] == "collection":
            return dict(
                protocol=11,
                declaration_error=False,
                context_id=request["context_id"],
                collection_skips=[],
                kind="collection",
                nonce="a" * 32,
                exit_code=0,
                complete=True,
                leaked_descendants=False,
                items=current["rows"],
                worker_seconds=0.1,
                failures=[],
            )
        kwargs["on_session_start"]()
        executed.append(request["case_id"])
        return dict(
            protocol=9,
            declaration_error=False,
            context_id=request["context_id"],
            collection_skips=[],
            nonce="a" * 32,
            exit_code=0,
            complete=True,
            leaked_descendants=False,
            collected=1,
            passed=1,
            failed=0,
            skipped=0,
            case_ids=[request["case_id"]],
            case_results=[
                dict(
                    case_id=request["case_id"],
                    outcome="passed",
                    complete=True,
                    reports=[
                        dict(phase=phase, outcome="passed", skip_reason=None)
                        for phase in ("setup", "call", "teardown")
                    ],
                )
            ],
            worker_seconds=0.1,
            failures=[],
        )

    def machine(name, *, count=3, fail_outer=False):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
        current["rows"] = module_rows(
            root, [(f"test_{number}.py", ["case"]) for number in range(count)]
        )
        sandbox_double(execute, fail_outer=fail_outer)
        return root

    machine.executed = executed
    return machine
