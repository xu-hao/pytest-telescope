"""pidfd ownership is available on supported kernels with older glibc headers."""

import ctypes
import errno
import os
import sys

import pytest

from pytest_telescope import host_supervisor, process
from pytest_telescope.model import InfrastructureError


def test_pidfds_work_without_glibc_236_convenience_symbols(monkeypatch):
    actual = ctypes.CDLL(None, use_errno=True)

    class OlderLibc:
        def __getattr__(self, name):
            if name in {"pidfd_open", "pidfd_send_signal"}:
                raise AttributeError("symbol is absent on glibc 2.35")
            return getattr(actual, name)

    monkeypatch.setattr(host_supervisor.ctypes, "CDLL", lambda *args, **kwargs: OlderLibc())
    descriptor = host_supervisor.pidfd_open(os.getpid())
    try:
        os.fstat(descriptor)
        host_supervisor.pidfd_signal(descriptor, 0)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("fail_call", [1, 2])
def test_pidfd_failure_before_admission_never_starts_payload(tmp_path, monkeypatch, fail_call):
    marker = tmp_path / "payload-started"
    calls = 0
    actual = process.pidfd_open
    spawned = []
    popen = process.subprocess.Popen

    def opening(pid):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise OSError(errno.EMFILE, "synthetic descriptor exhaustion")
        return actual(pid)

    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(process, "pidfd_open", opening)
    monkeypatch.setattr(process.subprocess, "Popen", launch)
    with pytest.raises(InfrastructureError):
        process.capture(
            [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"], 3
        )
    assert not marker.exists()
    assert all(child.returncode is not None for child in spawned)
    assert not spawned if fail_call == 1 else len(spawned) == 1


def test_child_observation_bound_still_cleans_all_owned_descendants(tmp_path):
    import subprocess

    helper = tmp_path / "owned-bound.py"
    helper.write_text(BOUND_HELPER)
    result = subprocess.run(
        [sys.executable, str(helper), str(tmp_path)], capture_output=True, text=True, timeout=12
    )
    assert result.returncode == 0, result.stdout + result.stderr


BOUND_HELPER = r"""
import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from pytest_telescope import host_supervisor

root = Path(sys.argv[1])
readiness, release = root / 'ready.json', root / 'release'
request, result = root / 'request.json', root / 'result.json'
payload = '''
import json, os, time
from pathlib import Path
children=[]
for _ in range(5):
    pid=os.fork()
    if pid == 0:
        for fd in (0,1,2):
            try: os.close(fd)
            except OSError: pass
        time.sleep(8)
        os._exit(0)
    children.append(pid)
Path(READY).write_text(json.dumps(children))
while not Path(RELEASE).exists(): time.sleep(.005)
'''.replace('READY', repr(str(readiness))).replace('RELEASE', repr(str(release)))
request.write_text(json.dumps(dict(argv=[sys.executable, '-c', payload], cwd=None, env=None,
                                  parent=os.getpid(), timeout=5, limit=65536,
                                  nonce='test-owned-bound', result=str(result))))
entry = ('import sys;sys.path.insert(0,sys.argv[1]);from pytest_telescope import host_supervisor as s;'
         's.MAX_CHILDREN=2;raise SystemExit(s.main(sys.argv[2]))')
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, 1, 0, 0, 0) == 0
child = None
descriptors = []
try:
    child = subprocess.Popen([sys.executable, '-I', '-c', entry,
                              str(Path(host_supervisor.__file__).parent.parent), str(request)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    until = time.monotonic() + 3
    while not readiness.exists() and time.monotonic() < until:
        time.sleep(.01)
    assert readiness.exists()
    for pid in json.loads(readiness.read_text()):
        descriptors.append(host_supervisor.pidfd_open(pid))
    release.touch()
    child.communicate(timeout=8)
    report = json.loads(result.read_text())
    assert report['incident'] is not None
    assert report['clean'] is True
    with selectors.DefaultSelector() as selector:
        for fd in descriptors:
            selector.register(fd, selectors.EVENT_READ)
        assert len(selector.select(.2)) == len(descriptors)
finally:
    for fd in descriptors:
        host_supervisor.pidfd_signal(fd, signal.SIGKILL)
        os.close(fd)
    if child is not None:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=2)
    if readiness.exists():
        for pid in json.loads(readiness.read_text()):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
"""
