"""Real controller sequencing, collection-union and owned interruption controls."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_telescope

from pytest_telescope import controller, sandbox, tool_capture
from pytest_telescope.model import UnsupportedCapability

from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, entries, put


def two_tools(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    first = project.add_tree("first")
    second = project.add_tree("second")
    for name in ("first", "second"):
        project.module(f"test_{name}.py", f"import pytest\npytestmark=pytest.mark.telescope(tools=['{name}'])\ndef test_{name}(): pass\n")
    return project, first, second


@pytest.mark.parametrize("selector", ["keyword", "node"])
def test_stale_structural_tool_refuses_selected_unrelated_case_without_erasing_cache(tmp_path, selector):
    project, _, second = two_tools(tmp_path)
    assert_counts(project.run(), executed=2)
    previous = cache_bytes(project.root)
    put(second, "value.txt", "changed without matching pin")
    arguments = ["-k", "first"] if selector == "keyword" else ["tests/test_first.py::test_first"]
    report = project.run(arguments=arguments)
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    assert cache_bytes(project.root) == previous
    assert_counts(project.run(structural_files={"default": ["tests/test_first.py"]}), reused=1)


def test_valid_unrelated_repin_reuses_independent_case(tmp_path):
    project, _, second = two_tools(tmp_path)
    assert_counts(project.run(), executed=2)
    put(second, "value.txt", "new valid bytes")
    project.refresh("second")
    assert_counts(project.run(), executed=1, reused=1)


def mixed_project(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    project.add_tree("first")
    counter = tmp_path / "ordinary-counter"
    project.module("test_mixed.py", f'''from pathlib import Path
import pytest
def test_ordinary():
    p = Path({str(counter)!r})
    p.write_text(p.read_text() + 'x' if p.exists() else 'x')
@pytest.mark.telescope(tools=['first'])
def test_scoped(): pass
''')
    return project, counter


def test_deferred_typed_view_refusal_retains_available_ordinary_accounting(tmp_path, monkeypatch):
    project, counter = mixed_project(tmp_path)
    original = tool_capture.Capture.view

    def deferred(self, names):
        if counter.exists():
            raise UnsupportedCapability("tool:layout", "owned deferred view refusal")
        return original(self, names)

    monkeypatch.setattr(tool_capture.Capture, "view", deferred)
    report = project.run()
    assert counter.read_text() == "x"
    assert report["kind"] == "run", report
    assert report["route_counts"]["ordinary"]["executed"] == 1, report
    assert report["status"] != "passed", report
    assert not cache_bytes(project.root)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())


def test_declared_loader_env_conflict_refuses_before_ordinary_side_effect(tmp_path):
    project, counter = mixed_project(tmp_path)
    module = project.root / "tests/test_mixed.py"
    module.write_text(module.read_text().replace("tools=['first']", "tools=['first'], env=['LD_PRELOAD']"))
    report = project.run()
    assert report["status"] == "declaration_error", report
    assert not counter.exists(), "knowable conflict ran ordinary project code before refusal"
    assert not cache_bytes(project.root)


def test_hard_killed_owned_controller_recovers_capture_and_keeps_old_cache(tmp_path):
    project, first, second = two_tools(tmp_path)
    assert_counts(project.run(), executed=2)
    previous = cache_bytes(project.root)
    before = entries(first), entries(second)
    ready = tmp_path / "controller-ready.json"
    code = '''import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pytest_telescope import controller, tool_capture
original = tool_capture.Capture.__enter__
def held(self):
    result = original(self)
    pending = Path(sys.argv[3] + '.pending')
    pending.write_text(json.dumps({'pid': os.getpid(), 'directory': str(self.directory.parent)}))
    pending.replace(sys.argv[3])
    while True: time.sleep(0.1)
tool_capture.Capture.__enter__ = held
controller.run(Path(sys.argv[2]), timeout=10)
'''
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", code,
         str(Path(pytest_telescope.__file__).parent.parent), str(project.root), str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    directory = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "owned controller did not reach its capture handshake"
        record = json.loads(ready.read_text())
        assert record["pid"] == process.pid
        directory = Path(record["directory"])
        assert directory.parent == project.root / ".pytest-telescope/scratch"
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate(timeout=10)
        assert len(output) < 8192 and process.returncode == -signal.SIGKILL
        with controller.lock(project.root / ".pytest-telescope/cache/.lock", 20):
            sandbox.recover(project.root)
        assert not directory.exists()
        assert cache_bytes(project.root) == previous
        assert (entries(first), entries(second)) == before
        assert_counts(project.run(), reused=2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
        if directory is not None and (directory / "tools").exists():
            tool_capture._remove(directory / "tools")
        sandbox.recover(project.root)
