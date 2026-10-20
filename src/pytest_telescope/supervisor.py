"""PID 1 lifecycle ownership; pytest retains ownership of its direct children."""

import json
import os
import re
import resource
import selectors
import signal
import sys
import time

VERSION = "pid1-reaper-v1"
MAX_CASE = 1_048_576
MAX_INVENTORY = 67_108_864


class LifecycleError(ValueError):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class Reaper:
    def __init__(self, worker):
        self.worker = worker
        self.status = None

    def drain(self):
        deadline = time.monotonic() + 0.05
        for _ in range(4096):
            if time.monotonic() >= deadline:
                break
            try:
                pid, status, _ = os.wait4(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if pid == self.worker:
                self.status = os.waitstatus_to_exitcode(status)


class Capture:
    def __init__(self, descriptor, limit):
        self.descriptor = descriptor
        self.limit = limit
        self.data = bytearray()
        self.eof = False

    def drain(self):
        # A continuously writing child cannot starve the reaper.
        for _ in range(16):
            try:
                chunk = os.read(self.descriptor, min(65536, self.limit + 1 - len(self.data)))
            except BlockingIOError:
                return
            if not chunk:
                self.eof = True
                return
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                raise LifecycleError("output")


def supervise(command, limit):
    started = time.monotonic()
    read, write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    previous_handler = signal.signal(signal.SIGCHLD, lambda *_: None)
    previous_wakeup = signal.set_wakeup_fd(wake_write)
    try:
        worker = os.fork()
        if worker == 0:
            try:
                signal.set_wakeup_fd(-1)
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                os.set_blocking(write, True)
                os.dup2(write, 1)
                os.dup2(write, 2)
                for descriptor in (read, write, wake_read, wake_write):
                    os.close(descriptor)
                os.execv(command[0], command)
            except BaseException:
                os._exit(127)
        os.close(write)
        write = None
        reaper = Reaper(worker)
        capture = Capture(read, limit)
        with selectors.DefaultSelector() as selector:
            selector.register(read, selectors.EVENT_READ)
            selector.register(wake_read, selectors.EVENT_READ)
            while reaper.status is None:
                reaper.drain()
                capture.drain()
                if capture.eof and read in selector.get_map():
                    selector.unregister(read)
                if reaper.status is not None:
                    break
                for key, _ in selector.select(0.05):
                    if key.fd == wake_read:
                        try:
                            os.read(wake_read, 65536)
                        except BlockingIOError:
                            pass
                    else:
                        capture.drain()
        # No wait for live descendants and no kill-to-pass. The controller owns
        # termination of an unsuccessful container, including this supervisor.
        reaper.drain()
        capture.drain()
        leaked = [pid for pid in os.listdir("/proc") if pid.isdecimal() and int(pid) != os.getpid()]
        if leaked or not capture.eof:
            raise LifecycleError("descendant")
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime
        return reaper.status, bytes(capture.data), time.monotonic() - started, cpu
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        signal.signal(signal.SIGCHLD, previous_handler)
        for descriptor in (read, write, wake_read, wake_write):
            if descriptor is not None:
                os.close(descriptor)


def final_record(status, output, seconds, cpu, request):
    frames = [
        line
        for line in output.splitlines()
        if line.startswith((b"TELESCOPE_RESULT ", b"TELESCOPE_UNSUPPORTED "))
    ]
    if len(frames) != 1:
        raise ValueError("worker did not return exactly one result")
    prefix, encoded = frames[0].split(b" ", 1)
    record = json.loads(encoded)
    if prefix == b"TELESCOPE_UNSUPPORTED":
        if status != 4 or record != {"capability": "project-metadata:runtime-conflict"}:
            raise ValueError("invalid worker capability refusal")
    else:
        if (
            type(record) is not dict
            or type(record.get("exit_code")) is not int
            or record["exit_code"] != status
            or status < 0
            or record.get("nonce") != request["nonce"]
            or type(record.get("complete")) is not bool
            or record.get("leaked_descendants") is not False
        ):
            raise ValueError("worker completion or exit identity mismatch")
        record.update(worker_seconds=seconds, cpu_seconds=cpu, leaked_descendants=False)
    return (
        prefix.decode()
        + " "
        + json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def main():
    nonce = None
    try:
        # Only the immutable in-image PID 1, never a host reaper.
        if os.getpid() != 1 or os.environ.get("TELESCOPE_REQUEST") != "/telescope-request.json":
            raise ValueError("invalid lifecycle entry point")
        with open("/telescope-request.json", "rb") as stream:
            encoded = stream.read(MAX_INVENTORY + 1)
        if len(encoded) > MAX_INVENTORY:
            raise ValueError("request limit exceeded")
        request = json.loads(encoded)
        if (
            type(request) is not dict
            or type(request.get("nonce")) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", request["nonce"])
        ):
            raise ValueError("invalid request identity")
        nonce = request["nonce"]
        if request.get("mode") not in ("collection", "execution", "batch"):
            raise ValueError("invalid worker mode")
        limit = MAX_CASE if request["mode"] == "execution" else MAX_INVENTORY
        status, output, seconds, cpu = supervise(
            [sys.executable, "-I", "/opt/telescope/worker.py"], limit
        )
        record = final_record(status, output, seconds, cpu, request)
        if len(record.encode()) + 1 > limit:
            raise LifecycleError("output")
        print(record, flush=True)
        return status
    except (OSError, ValueError, TypeError, KeyError, RecursionError, MemoryError) as exc:
        kind = exc.kind if isinstance(exc, LifecycleError) else "protocol"
        # No partial result, captured test output or exception data is forwarded.
        print("TELESCOPE_INFRASTRUCTURE " + json.dumps({"kind": kind, "nonce": nonce}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
