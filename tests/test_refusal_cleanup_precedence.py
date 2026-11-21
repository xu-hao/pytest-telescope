"""Terminal outer cleanup supersedes a typed boundary after genuine staged work."""

import errno
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put

from pytest_telescope import controller, sandbox, tool_capture
from pytest_telescope.model import DeclarationError, UnsupportedCapability


def scoped(project, name, inputs=()):
    project.module(
        name,
        f"""import pytest
pytestmark = pytest.mark.telescope(inputs={list(inputs)!r})
def test_body():
    assert 1 + 1 == 2
""",
    )


def test_outer_cleanup_failure_supersedes_typed_refusal_after_staged_success(tmp_path, monkeypatch):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    # Use the actual interpreter closure, including a hosted venv's base runtime.
    # Prepare before launch observation so the five recorded processes remain
    # collection, ordinary dispatch and the three scoped workers under test.
    box = sandbox.prepare(project.root)

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
    project.module(
        "test_ordinary.py",
        f"""from pathlib import Path
def test_ordinary():
    p = Path({str(ordinary_count)!r})
    p.write_text(p.read_text() + 'x' if p.exists() else 'x')
""",
    )

    original_execute = sandbox.execute
    original_enter = tool_capture.Capture.__enter__
    original_remove = sandbox._remove
    original_stage, original_publish = (
        controller.PendingRecords.stage,
        controller.PendingRecords.publish,
    )
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

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        original_stage(self, final, key, result, kind=kind, guarded=guarded)
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

        original_unlink = Path.unlink

        def unlink(target, *args, **kwargs):
            if target == directory / "owner.lock":
                raise OSError(errno.EIO, "owned outer removal I/O failure")
            return original_unlink(target, *args, **kwargs)

        # The actual _remove operation translates the underlying owned I/O fault
        # into terminal CleanupError while the real DeclarationError unwinds.
        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", unlink)
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
        expected = {
            "tests/test_00_prior.py",
            "tests/test_10_success.py",
            "tests/test_20_refusal.py",
        }
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


@pytest.mark.parametrize("order", ["refusal-first", "cleanup-first"])
def test_worker_cleanup_failure_supersedes_sibling_typed_refusal_in_execution_pool(
    tmp_path, monkeypatch, order
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    box = sandbox.prepare(project.root)

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

    original_execute = sandbox.execute
    original_remove = sandbox._remove
    original_stage, original_publish = (
        controller.PendingRecords.stage,
        controller.PendingRecords.publish,
    )
    original_popen = sandbox.subprocess.Popen
    staged = threading.Event()
    prior_ready = threading.Event()
    refusal_entered = threading.Event()
    cleanup_failed = threading.Event()
    guard = threading.Lock()
    calls, started, returned, processes = [], [], {}, []
    prior_directories, publications, cleanup_failures = [], [], []
    refusal = UnsupportedCapability("tool:layout", "owned typed refusal in sibling unit")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        with guard:
            processes.append(process)
        return process

    def execute(*args, **kwargs):
        request = kwargs["request"]
        if request["mode"] != "execution":
            return original_execute(*args, **kwargs)
        filename = request["registry"][0]["file"]
        if filename == "tests/test_20_refusal.py":
            assert staged.wait(10), "real successful result was not staged"
            assert prior_ready.wait(10), "sibling worker did not reach execution"
        callback = kwargs["on_session_start"]

        def on_start():
            with guard:
                started.append(filename)
            callback()

        kwargs["on_session_start"] = on_start
        with guard:
            calls.append(filename)
            if filename == "tests/test_00_prior.py":
                prior_directories.append(kwargs["invocation_directory"])
        result = original_execute(*args, **kwargs)
        assert result["passed"] == 1 and result["complete"] and result["exit_code"] == 0
        with guard:
            returned[filename] = result
        if filename == "tests/test_00_prior.py":
            prior_ready.set()
            assert refusal_entered.wait(10), "refusal worker did not enter execution slot"
            if order == "refusal-first":
                assert kwargs["cancellation"].wait(10), "controller did not stop after refusal"
        if filename == "tests/test_20_refusal.py":
            refusal_entered.set()
            if order == "cleanup-first":
                assert cleanup_failed.wait(10), "sibling worker cleanup fault did not fire first"
            raise refusal
        return result

    def stage(self, final, key, result, *, kind="execution", guarded=False):
        original_stage(self, final, key, result, kind=kind, guarded=guarded)
        if kind == "execution" and result is returned.get("tests/test_10_success.py"):
            staged.set()

    def publish(self):
        publications.append(True)
        return original_publish(self)

    def remove(directory):
        if directory not in prior_directories:
            return original_remove(directory)

        original_unlink = Path.unlink

        def unlink(target, *args, **kwargs):
            if target == directory / "owner.lock":
                raise OSError(errno.EIO, "owned worker removal I/O failure")
            return original_unlink(target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", unlink)
            try:
                return original_remove(directory)
            except sandbox.CleanupError as error:
                cleanup_failures.append(error)
                cleanup_failed.set()
                raise

    try:
        with monkeypatch.context() as fault:
            fault.setattr(sandbox.subprocess, "Popen", popen)
            fault.setattr(sandbox, "execute", execute)
            fault.setattr(sandbox, "_remove", remove)
            fault.setattr(controller.PendingRecords, "stage", stage)
            fault.setattr(controller.PendingRecords, "publish", publish)
            report = project.run(workers=2, timeout=10)

        assert len(cleanup_failures) == 1 and isinstance(cleanup_failures[0].__cause__, OSError)
        assert report["kind"] == "run" and report["status"] == "infrastructure_error", report
        assert report["collection"]["outcome"] == "infrastructure_error"
        assert (
            report["collection"]["reason"]
            == "run stopped after owned cleanup failure or cancellation"
        )
        assert report["inventory_complete"] and report["inventory_digest"] is not None
        assert not publications and cache_bytes(project.root) == previous
        assert not list((project.root / ".pytest-telescope/cache").rglob("*.pending"))
        rows = {row["file"]: row for row in report["units"]}
        passed_row = rows["tests/test_10_success.py"]
        assert passed_row["attempts"] == 1 and passed_row["complete"]
        assert passed_row["result_status"] == "passed" and passed_row["outcome"] == "executed"
        prior_row = rows["tests/test_00_prior.py"]
        assert prior_row["attempts"] == 1 and not prior_row["complete"]
        assert prior_row["result_status"] is None and prior_row["outcome"] == "infrastructure_error"
        assert prior_row["reason"] == "owned frozen copy cleanup failed"
        refused_row = rows["tests/test_20_refusal.py"]
        assert refused_row["attempts"] == 1 and not refused_row["complete"]
        assert (
            refused_row["result_status"] is None
            and refused_row["outcome"] == "infrastructure_error"
        )
        assert refused_row["reason"] == str(refusal)
        never_row = rows["tests/test_30_never.py"]
        assert never_row["attempts"] == 0 and never_row["result_status"] == "not-run"
        assert all(not group["cache_eligible"] for group in report["groups"])
        for process in processes:
            assert process.poll() is not None
    finally:
        for directory in prior_directories:
            if directory.exists():
                original_remove(directory)
    assert prior_directories and all(not directory.exists() for directory in prior_directories)
    assert not list((project.root / ".pytest-telescope/scratch").iterdir())


def _socket_held(name):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_NONBLOCK)
    try:
        probe.connect("\0" + name)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_second_termination_signal_during_pool_drain_waits_for_sandbox_cleanup(
    tmp_path, monkeypatch, signum
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    box = sandbox.prepare(project.root)
    monkeypatch.setattr(sandbox, "prepare", lambda root: box)
    token = uuid.uuid4().hex[:12]
    names = [f"telescope-122-{token}-{index}" for index in range(2)]
    for index, name in enumerate(names):
        project.module(
            f"test_{index}_active.py",
            f"""import socket, time, pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_hold():
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.bind('\\0{name}')
    held.listen(1)
    time.sleep(30)
""",
        )
    scoped(project, "test_2_unscheduled.py")

    original_popen = sandbox.subprocess.Popen
    original_terminate = sandbox._terminate
    guard = threading.Lock()
    processes = []
    draining = threading.Event()
    second_sent = threading.Event()

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        with guard:
            processes.append(process)
        return process

    def terminate(process):
        # Collection completes before the two active execution sandboxes start.
        with guard:
            active_workers = len(processes) >= 3
        if active_workers and not draining.is_set():
            draining.set()
            assert second_sent.wait(10), "second termination signal was not sent during drain"
            time.sleep(0.1)
        return original_terminate(process)

    def send_signals():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all(_socket_held(name) for name in names):
            time.sleep(0.02)
        assert all(_socket_held(name) for name in names)
        os.kill(os.getpid(), signum)
        assert draining.wait(10), "first signal did not start draining active sandboxes"
        os.kill(os.getpid(), signum)
        second_sent.set()

    before_sigint = signal.getsignal(signal.SIGINT)
    before_sigterm = signal.getsignal(signal.SIGTERM)
    driver = threading.Thread(target=send_signals, daemon=True)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(sandbox.subprocess, "Popen", popen)
            fault.setattr(sandbox, "_terminate", terminate)
            driver.start()
            report = controller.run(project.root, workers=2, timeout=30)
            driver.join(timeout=10)

        assert draining.is_set() and second_sent.is_set()
        assert not list((project.root / ".pytest-telescope/scratch").iterdir())
        assert len(processes) == 3  # collection + two active scoped workers
        for process in processes:
            assert process.poll() is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_socket_held(name) for name in names):
            time.sleep(0.02)
        assert time.monotonic() < deadline
        assert not any(_socket_held(name) for name in names)
        assert report["kind"] == "run" and report["status"] == "infrastructure_error"
        assert (
            report["collection"]["reason"]
            == "run stopped after owned cleanup failure or cancellation"
        )
        assert all(group["stop_reason"] == "cancelled" for group in report["groups"])
        assert signal.getsignal(signal.SIGINT) == before_sigint
        assert signal.getsignal(signal.SIGTERM) == before_sigterm
    finally:
        second_sent.set()
        driver.join(timeout=5)
        for process in processes:
            if process.poll() is None:
                original_terminate(process)
        sandbox.recover(project.root)
