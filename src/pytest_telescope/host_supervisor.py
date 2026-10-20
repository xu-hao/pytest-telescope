"""Dedicated host subreaper for one ordinary session, with pidfd-only signalling.

Only this process adopts orphaned descendants. The pytest process keeps ownership
and exit statuses of its own children. No host-wide process scan or process-group
signal is used, including after the original worker has exited.
"""

import ctypes
import errno
import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_CHILDREN = 4096
CLEANUP_SECONDS = 5


def _syscall(number, *arguments):
    # Linux x86-64 UAPI numbers; glibc 2.35 does not export pidfd convenience symbols.
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise OSError(errno.ENOTSUP, "unsupported pidfd ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.syscall
    function.restype = ctypes.c_long
    value = function(ctypes.c_long(number), *arguments)
    if value < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return value


def pidfd_open(pid):
    return _syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_signal(descriptor, number):
    try:
        _syscall(
            424, ctypes.c_int(descriptor), ctypes.c_int(number), ctypes.c_void_p(), ctypes.c_uint(0)
        )
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def child_chunk():
    # An oversized list refuses execution. Cleanup drains bounded owned prefixes.
    with open(f"/proc/self/task/{os.getpid()}/children", "rb") as stream:
        raw = stream.read(65537)
    overflow = len(raw) > 65536
    if overflow:
        raw = raw[:65536]
        raw = raw[: raw.rfind(b" ") + 1]
    fields = raw.split()
    if any(not value.isdigit() for value in fields):
        raise RuntimeError("owned child list invalid")
    return [int(value) for value in fields[:MAX_CHILDREN]], overflow or len(fields) > MAX_CHILDREN


def children():
    owned, overflow = child_chunk()
    if overflow:
        raise RuntimeError("owned child list exceeded bound")
    return owned


def child_pidfd(pid):
    descriptor = pidfd_open(pid)
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            raw = stream.read(4097)
        fields = raw[raw.rfind(b")") + 2 :].split()
        if len(raw) > 4096 or len(fields) < 2 or int(fields[1]) != os.getpid():
            raise RuntimeError("process is no longer an owned child")
        # A pidfd always addresses its original process even if its number is reused.
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def main(request_path, startup_fd=None):
    with open(request_path, "rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return 3
    request = json.loads(raw)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        return 3
    cancelled = False

    def cancel(_number, _frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    # A disappearing controller requests owned cleanup; this never changes pytest handlers.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        return 3
    if os.getppid() != request["parent"]:
        cancelled = True
    if startup_fd is not None:
        try:
            with selectors.DefaultSelector() as startup:
                startup.register(startup_fd, selectors.EVENT_READ)
                ready = startup.select(2)
                if not ready or os.read(startup_fd, 2) != b"1" or cancelled:
                    return 3
        finally:
            os.close(startup_fd)
    process = None
    code = None
    output = bytearray()
    incident = None
    deadline = time.monotonic() + request["timeout"]

    def reap():
        nonlocal code
        until = time.monotonic() + 0.05
        for _ in range(MAX_CHILDREN):
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if process is not None and pid == process.pid:
                code = os.waitstatus_to_exitcode(status)
                process.returncode = code
            if time.monotonic() >= until:
                return

    def cleanup():
        until = time.monotonic() + CLEANUP_SECONDS
        while time.monotonic() < until:
            reap()
            owned, overflow = child_chunk()
            if not owned and not overflow:
                return True
            for pid in owned:
                try:
                    descriptor = child_pidfd(pid)
                except ProcessLookupError:
                    continue
                try:
                    pidfd_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
            # Killing a direct parent adopts its still-live descendants here.
            time.sleep(0.005)
        reap()
        owned, overflow = child_chunk()
        return not owned and not overflow

    try:
        if cancelled:
            incident = "cancelled"
        else:
            process = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                env=request["env"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    reap()
                    if cancelled:
                        incident = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        incident = "deadline"
                        break
                    if code is not None:
                        reap()
                        if children():
                            incident = "live-descendant"
                            break
                        if not selector.get_map():
                            break
                    for key, _ in selector.select(0.02):
                        data = os.read(
                            key.fileobj.fileno(), min(65536, request["limit"] + 1 - len(output))
                        )
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(data)
                    if len(output) > request["limit"]:
                        incident = "output-limit"
                        break
    except BaseException:
        incident = "supervision"
    finally:
        try:
            clean = cleanup()
        except BaseException:
            clean = False
        if process is not None and process.stdout is not None:
            process.stdout.close()
    if not clean:
        incident = "cleanup"
    if code is None and incident is None:
        incident = "supervision"
    result = dict(schema=1, nonce=request["nonce"], code=code, incident=incident, clean=clean)
    target = Path(request["result"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True))
    os.replace(temporary, target)
    if incident is None:
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
    return 0 if incident is None else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], int(sys.argv[2])))
