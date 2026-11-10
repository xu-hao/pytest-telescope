"""Owned Package collector lifecycle controls against real Bubblewrap."""

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


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["tests"]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


def package_init(kind):
    start = (
        """def setup_module():
    state.append("setup")
    os.write(1, b"PACKAGE_SETUP_OBSERVED\\n")
"""
        if kind in ("both", "setup")
        else ""
    )
    finish = (
        """def teardown_module():
    os.write(1, b"PACKAGE_TEARDOWN_OBSERVED\\n")
"""
        if kind in ("both", "teardown")
        else ""
    )
    return "import os\nstate = []\n" + start + finish


def observe_real_workers(monkeypatch):
    events = []
    original = sandbox.capture

    def observe(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST in argv:
            request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
            events.append(
                {
                    "mode": request["mode"],
                    "code": code,
                    "setup": output.count(b"PACKAGE_SETUP_OBSERVED"),
                    "teardown": output.count(b"PACKAGE_TEARDOWN_OBSERVED"),
                    "body": output.count(b"PACKAGE_BODY_OBSERVED"),
                }
            )
        return code, output

    monkeypatch.setattr(sandbox, "capture", observe)
    return events


@pytest.mark.requires_fixture_guard
def test_same_package_lifecycle_with_one_group_runs_and_tears_down(tmp_path, monkeypatch):
    configure(tmp_path)
    put(tmp_path, "tests/pkg/__init__.py", package_init("both"))
    put(
        tmp_path,
        "tests/pkg/test_single.py",
        """import os
import pytest
from pkg import state
pytestmark = pytest.mark.telescope(inputs=["tests/pkg/__init__.py"])
def test_first():
    os.write(1, b"PACKAGE_BODY_OBSERVED\\n")
    assert state == ["setup"]
    state.append("first")
def test_second():
    os.write(1, b"PACKAGE_BODY_OBSERVED\\n")
    assert state == ["setup", "first"]
""",
    )
    events = observe_real_workers(monkeypatch)
    report = controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"])
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert len(report["groups"]) == 1 and report["counts"]["executed"] == 2
    batches = [event for event in events if event["mode"] == "guard-batch"]
    assert len(batches) == 1
    assert (batches[0]["setup"], batches[0]["body"], batches[0]["teardown"]) == (1, 2, 1)
    assert report["groups"][0]["complete"] and report["groups"][0]["cache_eligible"]


@pytest.mark.requires_fixture_guard
@pytest.mark.parametrize("kind", ["setup", "teardown"])
def test_setup_only_or_teardown_only_same_package_refuses_before_body(tmp_path, monkeypatch, kind):
    configure(tmp_path)
    put(tmp_path, "tests/pkg/__init__.py", package_init(kind))
    for name in ("a", "b"):
        put(
            tmp_path,
            f"tests/pkg/test_{name}.py",
            """import os
import pytest
pytestmark = pytest.mark.telescope(inputs=["tests/pkg/__init__.py"])
def test_item(): os.write(1, b"PACKAGE_BODY_OBSERVED\\n")
""",
        )
    events = observe_real_workers(monkeypatch)
    unguarded = controller.run(tmp_path, arguments=["-s"], force=True)
    assert unguarded["status"] == "passed"
    batches = [event for event in events if event["mode"] == "batch"]
    assert len(batches) == 2 and all(event[kind] == 1 for event in batches)
    assert all(event["body"] == 1 for event in batches)
    events.clear()
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"], force=True)
    assert refusal.value.capability == "execution:fixture-sharing"
    assert not any(event["mode"] == "guard-batch" for event in events)


@pytest.mark.requires_fixture_guard
def test_distinct_package_lifetimes_are_admitted(tmp_path, monkeypatch):
    configure(tmp_path)
    for name in ("a", "b"):
        put(tmp_path, f"tests/pkg_{name}/__init__.py", package_init("both"))
        put(
            tmp_path,
            f"tests/pkg_{name}/test_item.py",
            f"""import os
import pytest
from pkg_{name} import state
pytestmark = pytest.mark.telescope(inputs=["tests/pkg_{name}/__init__.py"])
def test_item():
    os.write(1, b"PACKAGE_BODY_OBSERVED\\n")
    assert state == ["setup"]
""",
        )
    events = observe_real_workers(monkeypatch)
    report = controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"])
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert len(report["groups"]) == 2 and report["counts"]["executed"] == 2
    batches = [event for event in events if event["mode"] == "guard-batch"]
    assert len(batches) == 2
    assert all((event["setup"], event["body"], event["teardown"]) == (1, 1, 1) for event in batches)


@pytest.mark.requires_fixture_guard
def test_same_package_without_collector_lifecycle_is_admitted(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/pkg/__init__.py", "state = []\n")
    for name in ("a", "b"):
        put(
            tmp_path,
            f"tests/pkg/test_{name}.py",
            """import pytest
from pkg import state
pytestmark = pytest.mark.telescope(inputs=["tests/pkg/__init__.py"])
def test_item():
    assert state == []
    state.append("local")
""",
        )
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert len(report["groups"]) == 2 and report["counts"]["executed"] == 2


@pytest.mark.requires_fixture_guard
def test_nested_package_shared_ancestor_is_detected_when_pytest_collects_it(tmp_path, monkeypatch):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/conftest.py",
        """import os
def pytest_collection_modifyitems(session, config, items):
    for item in items:
        count = sum(type(parent).__name__ == "Package" for parent in item.iter_parents())
        os.write(1, ("NESTED_PACKAGE_ANCESTORS_" + str(count) + "\\n").encode())
""",
    )
    put(tmp_path, "tests/pkg/__init__.py", package_init("both"))
    put(tmp_path, "tests/pkg/sub/__init__.py", "")
    for filename in ("tests/pkg/test_outer.py", "tests/pkg/sub/test_inner.py"):
        put(
            tmp_path,
            filename,
            """import os
import pytest
from pkg import state
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py", "tests/pkg/__init__.py", "tests/pkg/sub/__init__.py"])
def test_item():
    os.write(1, b"PACKAGE_BODY_OBSERVED\\n")
    assert state == ["setup"]
""",
        )
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
    ancestry = (
        b"NESTED_PACKAGE_ANCESTORS_1" in plain.stdout
        and b"NESTED_PACKAGE_ANCESTORS_2" in plain.stdout
    )
    shared = (
        plain.stdout.count(b"PACKAGE_SETUP_OBSERVED") == 1
        and plain.stdout.count(b"PACKAGE_TEARDOWN_OBSERVED") == 1
    )
    print("F2_NESTED_SHARED_ANCESTOR=" + ("observed" if ancestry and shared else "not-observed"))
    if not ancestry or not shared:
        return
    events = observe_real_workers(monkeypatch)
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"])
    assert refusal.value.capability == "execution:fixture-sharing"
    assert not any(event["mode"] == "guard-batch" for event in events)
