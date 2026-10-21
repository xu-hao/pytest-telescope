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
