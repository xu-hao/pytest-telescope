"""Orphaned descendants are reaped continuously; surviving descendants refuse evidence.

Bubblewrap runs a minimal PID 1 init inside the sandbox namespace that adopts and reaps
exited orphans throughout a session, while the worker reaps any unwaited direct children
after teardown. These controls launch bwrap and are never skipped.
"""

import pytest

from pytest_telescope import controller, sandbox


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    try:
        yield tmp_path
    finally:
        sandbox.recover(tmp_path)
        assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


CHILD = r"""
import os, pathlib, time

def adopted_child(live):
    marker = pathlib.Path('/tmp/owned-adopted-child')
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        grandchild = os.fork()
        if grandchild == 0:
            if live:
                os.close(write_fd)
                time.sleep(30)
            os._exit(0)
        marker.write_text(str(grandchild))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    while os.read(read_fd, 1):
        pass
    os.close(read_fd)
    os.waitpid(child, 0)
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 2
    proc_entry = pathlib.Path('/proc/' + str(grandchild))
    if live:
        while time.monotonic() < deadline and not proc_entry.exists():
            time.sleep(0.002)
        assert proc_entry.exists()
        return
    while time.monotonic() < deadline and proc_entry.exists():
        time.sleep(0.002)
    assert not proc_entry.exists()
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
@pytest.mark.parametrize("phase", ["collection", "call"])
@pytest.mark.parametrize("live", [False, True])
def test_completed_adopted_child_vs_live_descendant(project, mode, phase, live):
    source = CHILD + "\nimport pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
    if phase == "collection":
        source += f"adopted_child({live!r})\ndef test_child(): pass\n"
    else:
        source += f"def test_child(): adopted_child({live!r})\n"
    (project / "tests/test_child.py").write_text(source)
    result = controller.run(project, execution_mode=mode)
    if live:
        assert result["status"] == "infrastructure_error"
        assert result["counts"]["reused"] == 0
        assert not any(group["cache_eligible"] for group in result["groups"])
        if phase == "call":
            assert result["groups"][0]["attempts"] == 2
    else:
        assert result["status"] == "passed", result
        assert result["counts"]["executed"] == 1
        warm = controller.run(project, execution_mode=mode)
        assert warm["status"] == "passed" and warm["counts"]["reused"] == 1


@pytest.mark.parametrize("bound", ["iterations", "deadline", "live", "none"])
def test_completed_child_reaping_is_nonblocking_and_bounded(monkeypatch, bound):
    from pytest_telescope import worker

    calls = []
    clock = [0]

    def monotonic():
        clock[0] += 0.02 if bound == "deadline" else 0
        return clock[0]

    def waitpid(pid, flags):
        assert pid == -1 and flags == worker.os.WNOHANG
        calls.append(1)
        if bound == "none":
            raise ChildProcessError
        return (0 if bound == "live" else 42), 0

    monkeypatch.setattr(worker.os, "waitpid", waitpid)
    monkeypatch.setattr(worker.time, "monotonic", monotonic)
    worker.reap_completed_children()
    assert len(calls) == {"iterations": 4096, "deadline": 2, "live": 1, "none": 1}[bound]


ORPHAN_BATCH = """
import json
import os
import pathlib
import time

import pytest

pytestmark = pytest.mark.telescope(inputs=[])
IS_BATCH = {is_batch!r}
LOG = pathlib.Path("/tmp/orphan-zombies.json")


def zombie_count():
    count = 0
    for name in os.listdir("/proc"):
        if not name.isdecimal():
            continue
        try:
            text = pathlib.Path(f"/proc/{{name}}/stat").read_text()
        except FileNotFoundError:
            continue
        if text.rpartition(")")[2].split()[0] == "Z":
            count += 1
    return count


def spawn_exited_orphan():
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        grandchild = os.fork()
        if grandchild == 0:
            os._exit(0)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    while os.read(read_fd, 1):
        pass
    os.close(read_fd)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize("turn", range(16))
def test_exited_orphan_reaped(turn):
    spawn_exited_orphan()
    deadline = time.monotonic() + 2
    observed = zombie_count()
    while observed and time.monotonic() < deadline:
        time.sleep(0.002)
        observed = zombie_count()
    history = json.loads(LOG.read_text()) if LOG.exists() else []
    history.append(observed)
    LOG.write_text(json.dumps(history))
    expected_len = turn + 1 if IS_BATCH else 1
    assert len(history) == expected_len
    assert observed == 0
    assert max(history) == 0
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_exited_orphans_do_not_accumulate_during_session(project, mode):
    (project / "tests/test_orphans.py").write_text(ORPHAN_BATCH.format(is_batch=mode == "batch"))
    result = controller.run(project, execution_mode=mode)
    assert result["status"] == "passed", result
    assert result["counts"]["executed"] == 16
    warm = controller.run(project, execution_mode=mode)
    assert warm["status"] == "passed" and warm["counts"]["reused"] == 16


OWNED_STATUSES = r"""
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.telescope(inputs=[])


def orphan_once():
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        if os.fork() == 0:
            os._exit(0)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    while os.read(read_fd, 1):
        pass
    os.close(read_fd)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_popen_exit_and_signal_statuses():
    orphan_once()
    for code in (0, 7, 42):
        proc = subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({code})"])
        # Give any concurrent reaper opportunity to interfere if it ever touched direct children.
        time.sleep(0.01)
        assert proc.wait() == code
    signaled = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    signaled.send_signal(signal.SIGTERM)
    assert signaled.wait() == -signal.SIGTERM


def test_fork_waitpid_statuses():
    orphan_once()
    pid = os.fork()
    if pid == 0:
        os._exit(19)
    time.sleep(0.01)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 19


def test_unwaited_exited_direct_child_is_reaped_after_teardown():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    deadline = time.monotonic() + 10
    state = ""
    while time.monotonic() < deadline:
        try:
            state = pathlib.Path(f"/proc/{proc.pid}/stat").read_text().rpartition(")")[2].split()[0]
        except FileNotFoundError:
            state = ""
        if state == "Z":
            break
        time.sleep(0.005)
    assert state == "Z"
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_owned_child_exit_statuses_are_preserved(project, mode):
    (project / "tests/test_owned.py").write_text(OWNED_STATUSES)
    result = controller.run(project, execution_mode=mode)
    assert result["status"] == "passed", result
    assert result["counts"]["executed"] == 3


@pytest.mark.parametrize("scenario", ["clean", "live", "reaped-race", "zombie-timeout"])
def test_remaining_descendants_handles_pid_1_and_bounds(monkeypatch, scenario):
    from pytest_telescope import worker

    clock = [0.0]
    scans = [0]
    sleeps = []

    def monotonic():
        if scenario == "zombie-timeout" and scans[0] > 0:
            clock[0] += 0.03
        return clock[0]

    def listdir(path):
        assert path == "/proc"
        scans[0] += 1
        if scenario == "clean" or (scenario == "reaped-race" and scans[0] >= 2):
            return ["1", "2", "self"]
        return ["1", "2", "3", "self"]

    class FakePath:
        def __init__(self, target):
            self.target = target

        def read_text(self):
            assert self.target == "/proc/3/stat"
            if scenario == "live":
                return "3 (sleep) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0"
            return "3 (orphan) Z 1 1 1 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0"

    monkeypatch.setattr(worker, "reap_completed_children", lambda: None)
    monkeypatch.setattr(worker.os, "getpid", lambda: 2)
    monkeypatch.setattr(worker.os, "getppid", lambda: 1)
    monkeypatch.setattr(worker.os, "listdir", listdir)
    monkeypatch.setattr(worker, "Path", FakePath)
    monkeypatch.setattr(worker.time, "monotonic", monotonic)
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)

    remaining = worker.remaining_descendants()
    if scenario in ("clean", "reaped-race"):
        assert remaining == []
        assert len(sleeps) == (0 if scenario == "clean" else 1)
    elif scenario == "live":
        assert remaining == ["3"]
        assert sleeps == []
    else:
        assert remaining == ["3"]
        assert len(sleeps) == 1
