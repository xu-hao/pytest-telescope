"""Terminal outer cleanup supersedes a typed boundary after genuine staged work."""

import errno
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put

from pytest_telescope import controller, sandbox, tool_capture
from pytest_telescope.model import DeclarationError, fingerprint


def scoped(project, name, inputs=()):
    project.module(name, f'''import pytest
pytestmark = pytest.mark.telescope(inputs={list(inputs)!r})
def test_body():
    assert 1 + 1 == 2
''')


def test_outer_cleanup_failure_supersedes_typed_refusal_after_staged_success(tmp_path, monkeypatch):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    # Preparation is an explicit default-view fixture boundary. The real worker,
    # result protocol, invocation ownership and cleanup remain production paths.
    # This does not qualify runtime discovery, preflight or the native tool profile.
    prefix = Path(sys.prefix)
    box = sandbox.Sandbox(
        bwrap="/usr/bin/bwrap", executable=sys.executable,
        binds=((str(prefix), str(prefix)),), path=str(prefix / "bin") + ":/usr/bin:/bin",
        identity=fingerprint(["owned-default-worker", sys.executable, str(prefix)]), description={},
    )

    def prepare(root):
        assert Path(root) == project.root
        return box

    monkeypatch.setattr(sandbox, "prepare", prepare)
    put(project.root, "prior.txt", "first identity\n")
    scoped(project, "test_00_prior.py", ["prior.txt"])
    assert_counts(project.run(workers=1, timeout=10), executed=1)
    previous = cache_bytes(project.root)
    assert previous
    put(project.root, "prior.txt", "second identity\n")
    for name in ("test_10_success.py", "test_20_refusal.py", "test_30_never.py"):
        scoped(project, name)
    ordinary_count = tmp_path / "ordinary-once"
    project.module("test_ordinary.py", f'''from pathlib import Path
def test_ordinary():
    p = Path({str(ordinary_count)!r})
    p.write_text(p.read_text() + 'x' if p.exists() else 'x')
''')

    original_execute = sandbox.execute
    original_enter = tool_capture.Capture.__enter__
    original_remove = sandbox._remove
    original_stage, original_publish = controller.PendingRecords.stage, controller.PendingRecords.publish
    original_popen = sandbox.subprocess.Popen
    staged = threading.Event()
    guard = threading.Lock()
    calls, started, returned, processes, child_directories = [], [], {}, [], []
    pools, staged_paths, publications, cleanup_failures, chronology = [], [], [], [], []
    refusal = DeclarationError("owned typed boundary after completed real worker")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        with guard:
            processes.append(process)
        return process

    def enter(self):
        result = original_enter(self)
        pools.append(self.directory.parent)
        return result

    def execute(*args, **kwargs):
        request = kwargs["request"]
        if request["mode"] != "execution":
            return original_execute(*args, **kwargs)
        filename = request["registry"][0]["file"]
        if filename == "tests/test_20_refusal.py":
            assert staged.wait(10), "real successful result was not staged"
        callback = kwargs["on_session_start"]

        def on_start():
            with guard:
                started.append(filename)
            callback()

        kwargs["on_session_start"] = on_start
        with guard:
            calls.append(filename)
            child_directories.append(kwargs["invocation_directory"])
        result = original_execute(*args, **kwargs)
        assert result["passed"] == 1 and result["complete"] and result["exit_code"] == 0
        with guard:
            returned[filename] = result
        if filename == "tests/test_00_prior.py":
            assert kwargs["cancellation"].wait(10), "controller did not stop after typed refusal"
            chronology.append("prior-drained")
        if filename == "tests/test_20_refusal.py":
            chronology.append("typed-refusal")
            raise refusal
        return result

    def stage(self, final, key, result, *, kind="execution"):
        original_stage(self, final, key, result, kind=kind)
        if kind == "execution" and result is returned.get("tests/test_10_success.py"):
            paths = [path for path, (target, _, _) in self.paths.items() if target == final]
            assert len(paths) == 1 and paths[0].is_file()
            staged_paths.extend(paths)
            chronology.append("success-staged")
            staged.set()

    def publish(self):
        publications.append(True)
        return original_publish(self)

    def remove(directory):
        if directory not in pools:
            return original_remove(directory)
        assert len(pools) == 1 and directory.exists()
        assert sys.exc_info()[1] is refusal
        assert staged_paths and all(path.exists() for path in staged_paths)
        assert all(not path.exists() for path in child_directories)
        assert "prior-drained" in chronology
        chronology.append("outer-cleanup")

        def rmtree(target):
            assert target == directory
            raise OSError(errno.EIO, "owned outer removal I/O failure")

        # The actual _remove operation translates the underlying owned I/O fault
        # into terminal CleanupError while the real DeclarationError unwinds.
        with monkeypatch.context() as fault:
            fault.setattr(sandbox, "shutil", SimpleNamespace(rmtree=rmtree))
            try:
                return original_remove(directory)
            except sandbox.CleanupError as error:
                cleanup_failures.append(error)
                raise

    try:
        with monkeypatch.context() as fault:
            fault.setattr(sandbox.subprocess, "Popen", popen)
            fault.setattr(tool_capture.Capture, "__enter__", enter)
            fault.setattr(sandbox, "execute", execute)
            fault.setattr(sandbox, "_remove", remove)
            fault.setattr(controller.PendingRecords, "stage", stage)
            fault.setattr(controller.PendingRecords, "publish", publish)
            report = project.run(workers=2, timeout=10)

        assert chronology.index("success-staged") < chronology.index("typed-refusal")
        assert chronology.index("typed-refusal") < chronology.index("prior-drained")
        assert chronology.index("prior-drained") < chronology.index("outer-cleanup")
        assert len(cleanup_failures) == 1 and isinstance(cleanup_failures[0].__cause__, OSError)
        assert report["kind"] == "run" and report["status"] == "infrastructure_error", report
        assert report["inventory_complete"] and report["inventory_digest"] is not None
        expected = {"tests/test_00_prior.py", "tests/test_10_success.py", "tests/test_20_refusal.py"}
        assert set(calls) == set(started) == set(returned) == expected
        assert len(calls) == len(started) == 3
        assert ordinary_count.read_text() == "x"
        assert not publications and cache_bytes(project.root) == previous
        assert not list((project.root / ".pytest-telescope/cache").rglob("*.pending"))
        rows = {row["file"]: row for row in report["units"]}
        for filename in ("tests/test_00_prior.py", "tests/test_10_success.py"):
            row = rows[filename]
            assert row["attempts"] == 1 and row["complete"] and row["result_status"] == "passed"
            assert row["reports"] == returned[filename]["case_results"][0]["reports"]
        refused = rows["tests/test_20_refusal.py"]
        assert refused["attempts"] == 1 and refused["result_status"] is None
        assert not refused["complete"] and refused["reports"] == []
        never = rows["tests/test_30_never.py"]
        assert never["attempts"] == 0 and never["result_status"] == "not-run"
        assert all(not group["cache_eligible"] for group in report["groups"])
        assert len(processes) == 5  # collection + ordinary + three actual scoped workers
        for process in processes:
            assert process.poll() is not None
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("recorded owned worker process group survived")
    finally:
        # Only the one exact recorded outer invocation is left by the injected
        # removal fault. Restore the real operation after all doubles unwind.
        for directory in pools:
            if directory.exists():
                original_remove(directory)
    assert pools and all(not directory.exists() for directory in pools)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())
