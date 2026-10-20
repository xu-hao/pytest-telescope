"""Required Docker behavior for the in-image PID 1 lifecycle supervisor."""

import json
import threading
import time

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.execution import DEFAULT_WORKER


@pytest.fixture(params=["python-v1", "git-local-v1"])
def project(tmp_path, request):
    profile = request.param
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots=["tests"]\n'
        f'[tool.pytest_telescope.runtime]\nprofile="{profile}"\n'
    )
    image = runtime.build_runtime(tmp_path)["image"]
    try:
        yield tmp_path, profile
    finally:
        runtime.recover(tmp_path)
        assert_no_owned_workers(tmp_path)
        runtime._image_info(image, runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", image], timeout=30)


def assert_no_owned_workers(root):
    assert not runtime.command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=" + runtime.LABEL + "project=" + runtime._project(root),
        ]
    ).strip()
    scratch = root / ".pytest-telescope/scratch"
    assert not scratch.exists() or not list(scratch.iterdir())


# The intermediate child is explicitly waited for. Only its completed descendant
# is adopted by PID 1. Pipe EOF synchronizes completion without requiring a zombie
# to remain visible long enough for the test to observe it.
ORPHAN = r"""
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=[])


def completed_orphan():
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            grandchild = os.fork()
        except OSError:
            os.write(write_fd, b"failed")
            os._exit(1)
        if grandchild == 0:
            os.write(write_fd, b"complete")
            os._exit(0)
        os._exit(0)
    os.close(write_fd)
    assert os.waitpid(child, 0) == (child, 0)
    with os.fdopen(read_fd, "rb") as stream:
        assert stream.read() == b"complete"


def exercise(count):
    for _ in range(count):
        completed_orphan()
        if WITH_GIT:
            subprocess.run(["git", "-C", "/tmp/repository", "status", "--porcelain"],
                           check=True, capture_output=True, timeout=10)


if WITH_GIT:
    subprocess.run(["git", "init", "-b", "main", "/tmp/repository"],
                   check=True, capture_output=True, timeout=10)
"""

OWNERSHIP = r"""

@pytest.fixture(scope="session", autouse=True)
def session_lifetime():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(); sys.exit(37)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline() == "ready\n"
    assert child.poll() is None
    yield
    child.stdin.close()
    assert child.wait(timeout=5) == 37
    assert child.poll() == 37
    child.stdout.close()
    assert len(seen) == EXPECTED_CASES


def owned_statuses():
    import signal
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline() == "ready\n"
    assert child.poll() is None
    child.terminate()
    assert child.wait(timeout=5) == -signal.SIGTERM
    child.stdout.close()
    pid = os.fork()
    if pid == 0:
        os._exit(23)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid and os.waitstatus_to_exitcode(status) == 23


seen = []
@pytest.mark.parametrize("ordinal", range(EXPECTED_CASES))
def test_session(ordinal):
    if ordinal == 0:
        owned_statuses()
    exercise(ITERATIONS)
    seen.append(ordinal)
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_long_collection_and_execution_preserve_children_fixtures_and_atomic_reuse(
    project, monkeypatch, mode
):
    root, profile = project
    count = 1 if mode == "case" else 160
    source = f"WITH_GIT = {profile == 'git-local-v1'!r}\n"
    source += f"EXPECTED_CASES = {count}\nITERATIONS = {160 if mode == 'case' else 1}\n"
    source += ORPHAN + "\nexercise(160)\n" + OWNERSHIP
    (root / "tests/test_session.py").write_text(source)
    cold = controller.run(root, execution_mode=mode)
    assert cold["status"] == "passed", json.dumps(cold, indent=2)
    assert cold["collection"]["outcome"] == "executed"
    assert cold["counts"]["executed"] == cold["cases"]["passed"] == count
    assert len(cold["groups"]) == 1
    assert cold["groups"][0]["attempts"] == 1
    assert cold["groups"][0]["cache_eligible"] is True
    assert_no_owned_workers(root)

    def no_worker(*args, **kwargs):
        pytest.fail("a fully warm lifecycle result launched or prepared a worker")

    with monkeypatch.context() as guard:
        guard.setattr(runtime, "execute", no_worker)
        guard.setattr(runtime, "build_runtime", no_worker)
        warm = controller.run(root, execution_mode=mode)
    assert warm["status"] == "passed"
    assert warm["collection"]["outcome"] == "reused"
    assert warm["counts"]["reused"] == count and warm["counts"]["executed"] == 0
    assert warm["groups"][0]["attempts"] == 0
    assert warm["groups"][0]["cache_eligible"] is True
    assert_no_owned_workers(root)


LIVE = r"""
def live_orphan():
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        grandchild = os.fork()
        if grandchild == 0:
            os.write(write_fd, b"ready")
            os.close(write_fd)
            time.sleep(30)
            os._exit(0)
        os._exit(0)
    os.close(write_fd)
    assert os.waitpid(child, 0) == (child, 0)
    with os.fdopen(read_fd, "rb") as stream:
        assert stream.read() == b"ready"
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
@pytest.mark.parametrize("phase", ["collection", "teardown"])
def test_live_adopted_child_refuses_execution_cache_evidence(project, mode, phase):
    root, _ = project
    source = "WITH_GIT = False\n" + ORPHAN + LIVE
    if phase == "collection":
        source += "\nlive_orphan()\ndef test_live(): pass\n"
    else:
        source += """
@pytest.fixture(scope="session", autouse=True)
def leave_live_child():
    yield
    live_orphan()

def test_first(): pass
def test_second(): pass
"""
    (root / "tests/test_live.py").write_text(source)
    result = controller.run(root, execution_mode=mode)
    assert result["status"] == "infrastructure_error", result
    assert result["counts"]["reused"] == 0
    assert not any(group["cache_eligible"] for group in result["groups"])
    cache = root / ".pytest-telescope/cache"
    assert not list(cache.glob("*.json"))
    if phase == "teardown":
        assert all(group["attempts"] == 2 for group in result["groups"])
        # A separate, clean collection remains valid inventory evidence.
        assert result["collection"]["outcome"] == "executed"
        assert len(list((cache / "inventory").glob("*.json"))) == 1
    else:
        assert not list(cache.rglob("*.json"))
    assert_no_owned_workers(root)


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_cancellation_drains_actual_supervisor_worker_and_child(project, monkeypatch, mode):
    root, _ = project
    (root / "tests/test_a.py").write_text("def test_fast(): pass\n")
    (root / "tests/test_b.py").write_text(
        "import subprocess, sys, time\n"
        "def test_wait():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    time.sleep(30)\n"
    )
    original = runtime.execute
    entered = []
    interrupted = []
    observed = threading.Event()

    def execute(*args, **kwargs):
        if kwargs["request"]["mode"] == "collection":
            return original(*args, **kwargs)
        filename = args[2][-1]
        entered.append(filename)
        if filename.endswith("test_a.py"):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                owned = (
                    runtime.command(
                        [
                            "docker",
                            "ps",
                            "-q",
                            "--filter",
                            "label=" + runtime.LABEL + "project=" + runtime._project(root),
                        ]
                    )
                    .decode()
                    .split()
                )
                if owned:
                    assert len(owned) == 1
                    processes = runtime.command(
                        ["docker", "top", owned[0], "-eo", "pid,ppid,args"]
                    ).splitlines()
                    if len(processes) >= 4:  # Header, supervisor, worker, test child.
                        observed.set()
                        break
                time.sleep(0.05)
            assert observed.is_set(), "the test child never started before cancellation"
            result = original(*args, **kwargs)
            assert result["exit_code"] == 0
            raise runtime.CleanupError("synthetic terminal fault after the sibling child started")
        try:
            return original(*args, **kwargs)
        except runtime.InvocationInterrupted:
            interrupted.append(filename)
            raise

    monkeypatch.setattr(runtime, "execute", execute)
    result = controller.run(
        root,
        execution_mode=mode,
        budget={key: value * 2 for key, value in DEFAULT_WORKER.items()},
    )
    assert observed.is_set()
    assert len(entered) == 2 and len(interrupted) == 1
    assert result["status"] == "infrastructure_error"
    assert result["counts"]["selected"] == result["counts"]["infrastructure_error"] == 2
    assert sum(group["attempts"] for group in result["groups"]) == 2
    assert not any(group["cache_eligible"] for group in result["groups"])
    assert not list((root / ".pytest-telescope/cache").rglob("*.json"))
    assert_no_owned_workers(root)
