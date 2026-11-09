"""Actual CPython/pytest startup and consumer results with an explicit frozen runtime."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from frozen_tool_fixtures import assert_counts, cache_bytes, put
from frozen_tool_worker_fixture import WorkerConfiguration

from pytest_telescope import sandbox, tool_capture
from pytest_telescope.sandbox import CleanupError


@pytest.fixture
def worker(tmp_path, monkeypatch):
    return WorkerConfiguration(tmp_path, monkeypatch)


def observe_native(monkeypatch):
    original = tool_capture.Capture.view
    observations = []

    def view(self, names):
        result = original(self, names)
        if "worker" in names:
            runtime = self.runtimes["worker"]
            observations.append((runtime.native, runtime.dynamic, result.native, result.closed))
        return result

    monkeypatch.setattr(tool_capture.Capture, "view", view)
    return observations


def observe_worker_capture(monkeypatch, directory):
    """Retain bounded private evidence; never alter the existing capture call/result."""
    original = sandbox.capture
    destination = directory / "worker-capture.jsonl"
    destination.open("xb").close()
    state = {"calls": 0, "bytes": 0, "overflow": False, "write_failed": False}
    maximum = 2 * 1024 * 1024

    def record(argv, result=None, error=None):
        state["calls"] += 1
        if state["overflow"]:
            return
        row = {"call": state["calls"]}
        encoded_argv = json.dumps(argv, ensure_ascii=True).encode()
        oversized = state["calls"] > 4 or len(encoded_argv) > 65536
        if len(encoded_argv) <= 65536:
            row["argv"] = argv
        else:
            row.update(argv_bytes=len(encoded_argv), argv_sha256=hashlib.sha256(encoded_argv).hexdigest())
        if error is not None:
            row["exception_type"] = type(error).__module__ + "." + type(error).__qualname__
        else:
            code, output = result
            row.update(returncode=code, output_bytes=len(output), output_sha256=hashlib.sha256(output).hexdigest())
            if len(output) <= 1024 * 1024:
                row["output_base64"] = base64.b64encode(output).decode("ascii")
            else:
                oversized = True
        encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if oversized or state["bytes"] + len(encoded) > maximum - 1024:
            state["overflow"] = True
            encoded = json.dumps({"diagnostic_bound_exceeded": True, "call": state["calls"],
                                  "complete_output_recorded": False}).encode() + b"\n"
        with destination.open("ab") as stream:
            stream.write(encoded)
        state["bytes"] += len(encoded)

    def capture(argv, *args, **kwargs):
        matched = bool(argv) and Path(argv[0]).name == "bwrap" and argv[-1] == sandbox.WORKER_PATH
        try:
            result = original(argv, *args, **kwargs)
        except BaseException as error:
            if matched:
                try:
                    record(argv, error=error)
                except Exception:
                    # Evidence failure must never replace the original exception.
                    state["write_failed"] = True
                    try:
                        print("PRIVATE_WORKER_DIAGNOSTIC_WRITE_FAILED", flush=True)
                    except Exception:
                        pass
            raise
        if matched:
            try:
                record(argv, result=result)
            except Exception:
                # The wrapper treats missing/incomplete evidence as a diagnostic fault.
                state["write_failed"] = True
                try:
                    print("PRIVATE_WORKER_DIAGNOSTIC_WRITE_FAILED", flush=True)
                except Exception:
                    pass
        return result

    def finish():
        try:
            encoded = json.dumps({"capture_summary": state}, separators=(",", ":")).encode() + b"\n"
            with destination.open("ab") as stream:
                stream.write(encoded)
        except Exception:
            # A missing final summary is an explicit wrapper diagnostic failure.
            pass

    monkeypatch.setattr(sandbox, "capture", capture)
    return finish


def test_actual_dynamic_worker_reads_only_declared_configuration_and_reuses(worker, monkeypatch):
    expected_cache = (worker.config / "ld.so.cache").read_bytes()
    digest = hashlib.sha256(expected_cache).hexdigest()
    worker.project.module("test_configuration.py", f"""import hashlib
import os
import stat
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_configuration():
    assert Path('/etc/selected.txt').read_bytes() == b'worker configuration\\n'
    assert Path('/tools/worker/config/selected.txt').read_bytes() == b'worker configuration\\n'
    assert set(path.name for path in Path('/etc').iterdir()) == {{'selected.txt', 'ld.so.cache', 'ld.so.preload'}}
    assert not Path('/etc/resolv.conf').exists()
    assert not Path('/etc/unselected.txt').exists()
    assert hashlib.sha256(Path('/etc/ld.so.cache').read_bytes()).hexdigest() == {digest!r}
    preload = Path('/etc/ld.so.preload')
    assert stat.S_ISREG(preload.lstat().st_mode)
    assert preload.read_bytes() == b''
    assert stat.S_IMODE(preload.stat().st_mode) == 0o444
    assert stat.S_IMODE(Path('/etc/selected.txt').stat().st_mode) == 0o440
    for path in (Path('/etc/selected.txt'), Path('/etc/new-file')):
        with pytest.raises(OSError): path.write_bytes(b'forbidden')
    assert os.getpid() == 1 and os.readlink('/proc/self') == '1'
""")
    finish_diagnostics = observe_worker_capture(monkeypatch, worker.project.external)
    observed = observe_native(monkeypatch)
    try:
        assert_counts(worker.project.run(timeout=60), executed=1)
        assert observed and all(native and dynamic and projected and "/etc" in closed for native, dynamic, projected, closed in observed)
        assert_counts(worker.project.run(timeout=60), reused=1)
        assert (worker.config / "selected.txt").read_bytes() == b"worker configuration\n"
    finally:
        finish_diagnostics()


def test_actual_isolated_python_child_keeps_configuration_and_has_no_source_descriptors(worker, monkeypatch):
    original = sandbox.capture
    commands = []

    def capture(argv, *args, **kwargs):
        if argv and Path(argv[0]).name == "bwrap":
            commands.append(tuple(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(sandbox, "capture", capture)
    tokens = [(path.stat().st_dev, path.stat().st_ino) for path in (worker.config / "selected.txt", worker.config / "ld.so.cache")]
    child = f"""import errno, os
from pathlib import Path
assert Path('/etc/selected.txt').read_bytes() == b'worker configuration\\n'
assert not Path('/etc/resolv.conf').exists()
for fd in os.listdir('/proc/self/fd'):
    try:
        info = os.stat('/proc/self/fd/' + fd)
    except OSError as error:
        assert error.errno in (errno.EBADF, errno.ENOENT)
    else:
        assert (info.st_dev, info.st_ino) not in {tokens!r}
print('DECLARED_CHILD_OK')
"""
    worker.project.module("test_child.py", f"""import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_child():
    result = subprocess.run([sys.executable, '-I', '-c', {child!r}], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, b'DECLARED_CHILD_OK\\n', b'')
""")
    assert_counts(worker.project.run(timeout=60), executed=1)
    assert commands
    for command in commands:
        assert not any(argument.startswith("/proc/") and "/fd/" in argument for argument in command)
        assert not any(argument in {"--ro-bind-data", "--bind-data", "--file"} for argument in command)
    # No descriptor is deliberately kept or passed to the child for this control.


def test_config_change_invalidates_only_consumer_and_unselected_sibling_does_not(worker):
    worker.project.add_tree("unrelated", files={"value.txt": "unrelated"})
    worker.project.module("test_consumers.py", """from pathlib import Path
import pytest
@pytest.mark.telescope(tools=['worker'])
def test_native():
    assert Path('/etc/selected.txt').read_text() in ('worker configuration\\n', 'updated configuration\\n')
@pytest.mark.telescope(tools=['unrelated'])
def test_unrelated():
    assert Path('/tools/unrelated/install/value.txt').read_text() == 'unrelated'
    assert not Path('/tools/worker').exists()
""")
    assert_counts(worker.project.run(timeout=60), executed=2)
    assert_counts(worker.project.run(timeout=60), reused=2)
    put(worker.config, "unselected.txt", "changed outside selection\n")
    assert_counts(worker.project.run(timeout=60), reused=2)
    put(worker.config, "selected.txt", "updated configuration\n", 0o640)
    worker.save()
    assert_counts(worker.project.run(timeout=60), executed=1, reused=1)


def test_stale_configuration_pin_does_not_reuse_or_publish(worker):
    worker.project.module("test_stale.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_value(): assert Path('/etc/selected.txt').read_bytes() == b'worker configuration\\n'
""")
    assert_counts(worker.project.run(timeout=60), executed=1)
    previous = cache_bytes(worker.project.root)
    put(worker.config, "selected.txt", "stale pin\n", 0o640)
    report = worker.project.run(timeout=60)
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    assert cache_bytes(worker.project.root) == previous
    public = json.dumps(report)
    assert str(worker.config) not in public and "stale pin" not in public


def test_native_cleanup_failure_suppresses_new_publication(worker, monkeypatch):
    worker.project.module("test_initial.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_initial(): pass
""")
    assert_counts(worker.project.run(timeout=60), executed=1)
    previous = cache_bytes(worker.project.root)
    worker.project.module("test_new.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_new(): assert Path('/etc/selected.txt').read_bytes() == b'worker configuration\\n'
""")
    original = tool_capture._remove
    execute = sandbox.execute
    attempted = []
    completed = []

    def observe(*args, **kwargs):
        result = execute(*args, **kwargs)
        completed.append(kwargs["request"]["mode"])
        return result

    def fail(directory):
        attempted.append(directory)
        raise CleanupError("owned native projection cleanup fault")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(tool_capture, "_remove", fail)
            fault.setattr(sandbox, "execute", observe)
            report = worker.project.run(timeout=60)
        assert report["status"] == "infrastructure_error", report
        assert "execution" in completed
        assert len(attempted) == 1
        assert cache_bytes(worker.project.root) == previous
    finally:
        for directory in attempted:
            if directory.exists():
                original(directory)
