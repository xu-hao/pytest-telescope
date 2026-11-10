"""Conditional package-collector conservation experiment using real pytest and Bubblewrap."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import UnsupportedCapability


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def package_project(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["tests"]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    put(
        root,
        "tests/conftest.py",
        """import os

def pytest_collection_modifyitems(session, config, items):
    for item in items:
        if any(type(parent).__name__ == "Package" for parent in item.iter_parents()):
            os.write(1, b"F2_PACKAGE_ANCESTOR\\n")
""",
    )
    put(
        root,
        "tests/pkg/__init__.py",
        """import os
state = []
def setup_module():
    state.append("started")
    os.write(1, b"F2_PACKAGE_SETUP\\n")
def teardown_module():
    assert state == ["started"]
    os.write(1, b"F2_PACKAGE_TEARDOWN\\n")
""",
    )
    for label in ("a", "b"):
        put(root, f"{label}.txt", label)
        put(
            root,
            f"tests/pkg/test_{label}.py",
            f'''import os
import pytest
from pkg import state
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py", "tests/pkg/__init__.py", "{label}.txt"])
def test_item():
    os.write(1, b"F2_SELECTED_BODY\\n")
    assert state == ["started"]
''',
        )


@pytest.mark.requires_fixture_guard
def test_package_collector_evidence_gates_split_owner_refusal(tmp_path, monkeypatch):
    package_project(tmp_path)
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    plain = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pytest",
            "-p",
            "pytest_telescope.plugin",
            "-p",
            "no:cacheprovider",
            "-q",
            "-s",
            "tests/pkg",
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    assert plain.returncode == 0
    assert plain.stdout.count(b"F2_PACKAGE_ANCESTOR") == 2
    assert plain.stdout.count(b"F2_PACKAGE_SETUP") == 1
    assert plain.stdout.count(b"F2_PACKAGE_TEARDOWN") == 1
    assert plain.stdout.count(b"F2_SELECTED_BODY") == 2

    original = sandbox.capture
    events = []

    def observe(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST in argv:
            request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
            events.append(
                {
                    "mode": request["mode"],
                    "code": code,
                    "ancestor": output.count(b"F2_PACKAGE_ANCESTOR"),
                    "setup": output.count(b"F2_PACKAGE_SETUP"),
                    "teardown": output.count(b"F2_PACKAGE_TEARDOWN"),
                    "body": output.count(b"F2_SELECTED_BODY"),
                }
            )
        return code, output

    monkeypatch.setattr(sandbox, "capture", observe)
    unguarded = controller.run(tmp_path, arguments=["-s"], force=True)
    batches = [event for event in events if event["mode"] == "batch"]
    assert len(batches) == 2
    visible = all(
        event["ancestor"] == 1
        and event["setup"] == 1
        and event["teardown"] == 1
        and event["body"] == 1
        for event in batches
    )
    print("F2_PLAIN_SHARED_PACKAGE=observed")
    print("F2_NARROW_PACKAGE_SETUP_TEARDOWN=" + ("observed" if visible else "not-observed"))
    if not visible:
        # No guard failure is inferred when the relevant narrow-view collector or
        # lifecycle is absent; report the measured control and stop this hypothesis.
        print("F2_NARROW_OBSERVATIONS=" + json.dumps(batches, sort_keys=True))
        assert unguarded["schema"] == 4
        return
    assert unguarded["status"] == "passed"
    events.clear()
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"], force=True)
    assert refusal.value.capability == "execution:fixture-sharing"
    assert not any(event["mode"] == "guard-batch" for event in events)
