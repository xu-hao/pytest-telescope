"""Invocations sharing a cache directory: overlap, private staging and recovery.

These tests launch bwrap. The cache lock covers only staging allocation, recovery of
abandoned staging and publication, so sandboxes of different invocations run at once.
"""

import concurrent.futures
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

import pytest_telescope
from pytest_telescope import controller


class HostGate:
    """A host-side gate on an abstract socket, which scoped tests reach over the shared
    network namespace. A connected case waits until the host releases its name, opens
    the gate, or `expected` cases are connected at the same time."""

    def __init__(self, expected=None):
        self.address = "\0telescope-cache-" + uuid.uuid4().hex
        self.expected = expected
        self.condition = threading.Condition()
        self.arrivals, self.waiting, self.released = [], [], set()
        self.opened = False
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.address)
        self.server.listen()
        self.server.settimeout(0.2)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def client(self, timeout=90):
        """Module source defining `gate(name)`, used inside a sandboxed test."""
        return (
            "import socket\n"
            "def gate(name):\n"
            "    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            f"    client.settimeout({timeout})\n"
            f"    client.connect({self.address!r})\n"
            "    client.sendall(name.encode() + b'\\n')\n"
            "    assert client.makefile('rb').readline() == b'go\\n'\n"
        )

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            connection.settimeout(10)
            try:
                name = connection.makefile("rb").readline().decode().strip()
            except OSError:
                connection.close()
                continue
            with self.condition:
                self.arrivals.append(name)
                self.waiting.append((name, connection))
                self._release()
                self.condition.notify_all()

    def _release(self):
        everyone = self.opened or (self.expected is not None and len(self.waiting) >= self.expected)
        for entry in list(self.waiting):
            if everyone or entry[0] in self.released:
                self.waiting.remove(entry)
                with contextlib.suppress(OSError):
                    entry[1].sendall(b"go\n")
                entry[1].close()

    def arrived(self, name, count=1, timeout=90):
        with self.condition:
            return self.condition.wait_for(lambda: self.arrivals.count(name) >= count, timeout)

    def release(self, name=None):
        """Release one name, now and later; without a name, open the gate for every case."""
        with self.condition:
            if name is None:
                self.opened = True
            else:
                self.released.add(name)
            self._release()

    def close(self):
        self.stopping.set()
        self.thread.join(5)
        self.server.close()
        with self.condition:
            for _, connection in self.waiting:
                connection.close()


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


# A wholly scoped module: its collection is sandboxed and its inventory is recorded too.
SCOPED = "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"


def scoped_project(root, gate, name):
    """A passing scoped case, then one that waits at the gate under `name`."""
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(
        root,
        "tests/test_staged.py",
        SCOPED
        + gate.client()
        + "\n"
        + "def test_first():\n"
        + "    pass\n"
        + "\n"
        + "def test_held():\n"
        + f"    gate({name!r})\n",
    )
    return root


def staging(cache):
    directory = cache / "staging"
    return sorted(directory.iterdir()) if directory.exists() else []


def staged(directory):
    """The staged record bytes of one invocation, by file name."""
    return {
        path.name: path.read_bytes() for path in directory.iterdir() if path.name != "owner.lock"
    }


def kinds(directory):
    return sorted(name.split(".")[1] for name in staged(directory))


def records(cache):
    return sorted(
        path.relative_to(cache).as_posix()
        for path in [*cache.glob("*.json"), *cache.glob("inventory/*.json")]
    )


def start(root, cache):
    """A controller in its own process, which prints its report's outcome."""
    code = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pytest_telescope import controller
report = controller.run(Path(sys.argv[2]), cache_dir=Path(sys.argv[3]), workers=1, timeout=120)
print(json.dumps({key: report[key] for key in ("status", "counts", "collection")}))
"""
    return subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(Path(pytest_telescope.__file__).parent.parent),
            str(root),
            str(cache),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def reach(gate, name, process):
    """Wait until the process's held case is at the gate; report a controller that ended."""
    for _ in range(180):
        if gate.arrived(name, timeout=0.5):
            return
        if process.poll() is not None:
            pytest.fail(f"controller ended first: {process.communicate(timeout=30)[0]!r}")
    pytest.fail(f"{name} did not reach the gate")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=30)


def passed(report, **counts):
    assert report["status"] == "passed", report
    for name, value in counts.items():
        assert report["counts"][name] == value, report
    return report


def test_invocations_sharing_a_cache_overlap_in_their_sandboxes(tmp_path):
    """Two identical checkouts race on one cold key: both sandboxes run at the same time."""
    cache = tmp_path / "cache"
    gate = HostGate(expected=2)
    roots = []
    for name in ("left", "right"):
        root = tmp_path / name
        put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
        put(
            root,
            "tests/test_overlap.py",
            SCOPED
            + gate.client(timeout=60)
            + "\ndef test_overlap():\n"
            + "    # Released only while both invocations' sandboxes are connected.\n"
            + "    gate('overlap')\n",
        )
        roots.append(root)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(
                executor.map(lambda root: controller.run(root, cache_dir=cache, timeout=120), roots)
            )
    finally:
        gate.close()
    assert gate.arrivals == ["overlap", "overlap"], reports
    # Both executed the same cold key and published the same complete success.
    for report in reports:
        passed(report, executed=1, reused=0)
    assert len(records(cache)) == 2  # one execution record and one inventory
    assert staging(cache) == []
    for root in roots:
        passed(controller.run(root, cache_dir=cache), executed=0, reused=1)


def test_same_checkout_runs_overlap_beyond_shared_scratch_limit(tmp_path, monkeypatch):
    """Real controllers share scratch; a small abandoned-copy bound excludes live owners."""
    monkeypatch.setattr(controller.sandbox, "MAX_INVOCATIONS", 2)
    root, cache = tmp_path / "project", tmp_path / "cache"
    gate = HostGate()
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(
        root,
        "tests/test_overlap.py",
        SCOPED + gate.client() + "\ndef test_overlap():\n    gate('overlap')\n",
    )
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            try:
                first = executor.submit(controller.run, root, cache_dir=cache, timeout=120)
                assert gate.arrived("overlap"), first.result() if first.done() else None
                second = executor.submit(controller.run, root, cache_dir=cache, timeout=120)
                assert gate.arrived("overlap", count=2), second.result() if second.done() else None
                # Both runs hold an outer copy and a worker copy in the same checkout.
                scratch = root / ".pytest-telescope/scratch"
                assert len(list(scratch.iterdir())) == 4
                controller.sandbox.recover(root)
                assert len(list(scratch.iterdir())) == 4
            finally:
                gate.release()
            for future in (first, second):
                passed(future.result(timeout=120), executed=1, reused=0)
    finally:
        gate.release()
        gate.close()
    assert list(scratch.iterdir()) == []
    assert staging(cache) == []


def test_live_staging_survives_while_abandoned_staging_is_recovered(tmp_path):
    cache = tmp_path / "cache"
    gate = HostGate()
    live = scoped_project(tmp_path / "live", gate, "live")
    crashed = scoped_project(tmp_path / "crashed", gate, "crashed")
    other = tmp_path / "other"
    put(other, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(
        other,
        "tests/test_other.py",
        SCOPED + "def test_other():\n    pass\n",
    )
    process = start(crashed, cache)
    try:
        reach(gate, "crashed", process)
        (abandoned,) = staging(cache)
        # The passing case and the inventory are staged, never in the cache itself.
        assert kinds(abandoned) == ["collection", "execution"] and records(cache) == []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            try:
                held = executor.submit(
                    controller.run, live, cache_dir=cache, workers=1, timeout=120
                )
                assert gate.arrived("live"), held.exception() if held.done() else None
                (owned,) = set(staging(cache)) - {abandoned}
                assert kinds(owned) == ["collection", "execution"]
                before = staged(owned)
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=30)
                assert abandoned.exists()
                # Another invocation runs, recovers and publishes while the live one is held.
                passed(controller.run(other, cache_dir=cache, timeout=60), executed=1)
                assert not held.done()
                assert not abandoned.exists()
                assert staged(owned) == before
                gate.release("live")
                passed(held.result(timeout=120), executed=2)
            except BaseException:
                # Do not leave the live invocation waiting for its client timeout.
                gate.release()
                raise
        assert staging(cache) == []
        passed(controller.run(live, cache_dir=cache), reused=2, executed=0)
        passed(controller.run(other, cache_dir=cache), reused=1, executed=0)
        # The killed invocation published nothing, so its passing case runs again.
        gate.release()
        rerun = passed(controller.run(crashed, cache_dir=cache, timeout=60), executed=2)
        assert rerun["counts"]["reused"] == 0
        assert rerun["collection"]["outcome"] == "executed"
    finally:
        stop(process)
        gate.close()
        controller.sandbox.recover(crashed)


def test_interrupted_invocation_publishes_nothing_and_removes_its_staging(tmp_path):
    cache = tmp_path / "cache"
    gate = HostGate()
    root = scoped_project(tmp_path / "project", gate, "interrupted")
    process = start(root, cache)
    try:
        reach(gate, "interrupted", process)
        (owned,) = staging(cache)
        assert kinds(owned) == ["collection", "execution"] and records(cache) == []
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=60)
        assert process.returncode == 0, output
        report = json.loads(output.splitlines()[-1])
        assert report["status"] == "infrastructure_error", report
        # An interrupted active session is a terminal cleanup condition of the run.
        assert "cancel" in report["collection"]["reason"], report
        assert not owned.exists() and staging(cache) == []
        assert records(cache) == []
        gate.release()
        passed(controller.run(root, cache_dir=cache, timeout=60), executed=2, reused=0)
    finally:
        stop(process)
        gate.close()


def test_staging_recovery_spares_live_owners(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    result = {"protocol": 9, "value": 1}

    def owner():
        return controller.PendingRecords(cache, inventory_count=8, timeout=10).__enter__()

    live = owner()
    live.stage(cache / ("a" * 64 + ".json"), "a" * 64, result)
    dead = owner()
    dead.stage(cache / ("b" * 64 + ".json"), "b" * 64, result)
    # The dead owner's process ended: its lock is released and its staging stays.
    os.close(dead.descriptor)
    retired = owner()
    os.close(retired.descriptor)
    # An owner that already removed its records and its owner lock.
    (retired.directory / "owner.lock").unlink()
    before = staged(live.directory)
    with controller.PendingRecords(cache, inventory_count=8, timeout=10) as later:
        assert staging(cache) == sorted([live.directory, later.directory])
        assert staged(live.directory) == before
    assert live.publish() == [cache / ("a" * 64 + ".json")]
    live.__exit__(None, None, None)
    assert staging(cache) == [] and records(cache) == ["a" * 64 + ".json"]
    (cache / "staging" / "unexpected").mkdir()
    with pytest.raises(controller.InfrastructureError, match="unrecognized cache staging"):
        owner()
