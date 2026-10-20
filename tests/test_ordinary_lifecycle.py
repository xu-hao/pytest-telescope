"""Ordinary sessions own descendants without changing their caller's child ownership."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import textwrap
import uuid

import pytest

from pytest_telescope import controller


def pidfd_open(pid):
    # Linux x86-64 syscall ABI, independent of the Python build's header version.
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    return fd


def pidfd_signal(fd, signum):
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(424, ctypes.c_int(fd), ctypes.c_int(signum), None, ctypes.c_uint(0))
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal")


def pidfd_reap(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    information = ctypes.create_string_buffer(128)
    if libc.waitid(3, fd, ctypes.byref(information), os.WEXITED) < 0:  # P_PIDFD.
        raise OSError(ctypes.get_errno(), "waitid pidfd")


# This separate harness adopts leaked fixture children so the test can reap them.
# It observes only its synthetic child's recorded PID/start identity, never /proc
# globally. The pidfd remains open through observation, signalling and exact wait.
HARNESS = r"""
import contextlib
import ctypes
import errno
import io
import json
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time

from pytest_telescope import controller
sys.path.insert(0, sys.argv[4])
from test_ordinary_lifecycle import pidfd_open, pidfd_signal, pidfd_reap

root, trigger, token = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
os.chdir(root)
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER, in this harness only.
owned = {}
errors = []
stop = threading.Event()


def start_identity(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def acquire():
    packet = json.loads((root / "child.json").read_text())
    assert packet["token"] == token
    pid = packet["pid"]
    assert type(pid) is int and pid > 1
    fd = pidfd_open(pid)
    try:
        assert start_identity(pid) == packet["start"]
    except BaseException:
        os.close(fd)
        raise
    owned.update(pid=pid, fd=fd, start=packet["start"])


def observe():
    deadline = time.monotonic() + 8
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            if (root / "child.json").exists():
                acquire()
                (root / "release").touch()
                if trigger == "cancel":
                    os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.005)
        if not stop.is_set():
            errors.append("child registration deadline")
    except BaseException as error:
        errors.append(type(error).__name__)


def deadline(signum, frame):
    raise TimeoutError("owned harness deadline")


signal.signal(signal.SIGALRM, deadline)
signal.alarm(15)
watcher = threading.Thread(target=observe)
watcher.start()
summary = {}
started = time.monotonic()
try:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = controller.main(["run", "--timeout", "2" if trigger == "timeout" else "8"])
    report = json.loads(output.getvalue())
    watcher.join(timeout=1)
    assert owned and not errors, errors
    ready = bool(select.select([owned["fd"]], [], [], 0)[0])
    try:
        same_process_exists = start_identity(owned["pid"]) == owned["start"]
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ESRCH):
            raise
        same_process_exists = False
    summary.update(
        status=report["status"], exit_code=exit_code, report=report,
        child_exited_at_return=ready,
        child_reaped_at_return=not same_process_exists,
        elapsed_seconds=time.monotonic() - started,
    )
finally:
    signal.alarm(0)
    stop.set()
    watcher.join(timeout=1)
    if not owned and (root / "child.json").exists():
        acquire()
    if owned:
        try:
            if not select.select([owned["fd"]], [], [], 0)[0]:
                pidfd_signal(owned["fd"], signal.SIGKILL)
            assert select.select([owned["fd"]], [], [], 5)[0], "owned child did not exit"
            try:
                pidfd_reap(owned["fd"])
            except ChildProcessError:
                # A correct producer already reaped it in its own supervisor.
                pass
            summary["fallback_cleanup_complete"] = True
        finally:
            os.close(owned["fd"])
    else:
        summary["fallback_cleanup_complete"] = False
print(json.dumps(summary), flush=True)
"""


SPAWN = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def spawn_detached():
    script = """
import json, os, time
from pathlib import Path
root = Path.cwd()
packet = {"pid": os.getpid(), "start": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
          "token": (root / "token").read_text()}
(root / "child.pending").write_text(json.dumps(packet))
os.replace(root / "child.pending", root / "child.json")
time.sleep(20)
"""
    subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 8
    while not Path("release").exists():
        assert time.monotonic() < deadline, "owned observer did not acquire the child"
        time.sleep(0.005)
'''


def run_owned_lifecycle(root, trigger):
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    token = uuid.uuid4().hex
    (root / "token").write_text(token)
    if trigger == "declaration":
        (root / "tests/conftest.py").write_text(
            SPAWN
            + """
def pytest_collection_finish(session):
    spawn_detached()
    session.items[0].own_markers[0].kwargs["cache"] = True
"""
        )
        source = """
import pytest
@pytest.mark.telescope(cache=False)
def test_never_dispatched():
    raise AssertionError("declaration-invalid body must not execute")
"""
    else:
        source = SPAWN + "\ndef test_detached():\n    spawn_detached()\n"
        if trigger in ("timeout", "cancel"):
            source += "    time.sleep(20)\n"
    (root / "tests/test_child.py").write_text(source)
    helper = root / "owned_harness.py"
    helper.write_text(textwrap.dedent(HARNESS))
    assert os.uname().machine == "x86_64"
    probe = pidfd_open(os.getpid())
    os.close(probe)
    sentinel = process = None
    descriptors = {}
    try:
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(40)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        descriptors["sentinel"] = pidfd_open(sentinel.pid)
        process = subprocess.Popen(
            [sys.executable, str(helper), str(root), trigger, token, os.path.dirname(__file__)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        descriptors["process"] = pidfd_open(process.pid)
        try:
            output, error = process.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            pidfd_signal(descriptors["process"], signal.SIGINT)
            output, error = process.communicate(timeout=10)
            pytest.fail("owned lifecycle harness exceeded its deadline: " + error)
        assert process.returncode == 0, output + error
        sample = json.loads(output)
        assert sample["fallback_cleanup_complete"], sample
        assert sentinel.poll() is None, "ordinary cleanup touched an unrelated owned sentinel"
        return sample
    finally:
        # Direct children cannot reuse their PID before this parent reaps them.
        # Retained pidfds are used once available, including exceptional cleanup.
        for name, child in (("process", process), ("sentinel", sentinel)):
            if child is None:
                continue
            fd = descriptors.get(name)
            try:
                if child.poll() is None:
                    if fd is None:
                        child.kill()
                    else:
                        pidfd_signal(fd, signal.SIGKILL)
                child.wait(timeout=5)
            finally:
                if fd is not None:
                    os.close(fd)


@pytest.mark.parametrize("trigger", ["return", "timeout", "cancel"])
def test_ordinary_detached_descendant_is_refused_and_cleaned(tmp_path, trigger):
    sample = run_owned_lifecycle(tmp_path, trigger)
    assert sample["status"] == "infrastructure_error", json.dumps(sample, indent=2)
    assert sample["exit_code"] == 3
    assert sample["child_exited_at_return"] is True, json.dumps(sample, indent=2)
    assert sample["child_reaped_at_return"] is True, json.dumps(sample, indent=2)
    assert sample["elapsed_seconds"] < 12


def test_ordinary_preserves_direct_child_statuses_and_caller_ownership(tmp_path):
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    before = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(before), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER.
    (tmp_path / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_status.py").write_text("""
import os
import signal
import subprocess
import sys

def test_statuses():
    child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(); sys.exit(37)"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline() == "ready\\n"
    assert child.poll() is None
    child.stdin.close()
    assert child.wait(timeout=5) == 37 and child.poll() == 37
    child.stdout.close()
    child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(20)"],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline() == "ready\\n"
    child.terminate()
    assert child.wait(timeout=5) == -signal.SIGTERM
    child.stdout.close()
    pid = os.fork()
    if pid == 0:
        os._exit(23)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid and os.waitstatus_to_exitcode(status) == 23
""")
    report = controller.run(tmp_path)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    after = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(after), 0, 0, 0) == 0
    assert after.value == before.value
