"""Bounded capture owned by a dedicated host subreaper, never a process group."""

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .host_supervisor import pidfd_open, pidfd_signal
from .model import InfrastructureError


def capture(argv, timeout, *, cwd=None, env=None, limit=64 * 1024 * 1024):
    process = None
    descriptor = None
    startup_read = startup_write = None
    output = bytearray()
    nonce = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="pytest-telescope-supervisor-") as directory:
        request_path, result_path = (
            Path(directory) / name for name in ("request.json", "result.json")
        )
        request_path.write_text(
            json.dumps(
                dict(
                    argv=argv,
                    timeout=timeout,
                    cwd=str(cwd) if cwd else None,
                    env=env,
                    limit=limit,
                    nonce=nonce,
                    parent=os.getpid(),
                    result=str(result_path),
                )
            )
        )
        deadline = time.monotonic() + timeout + 7
        try:
            # Refuse a missing ABI/kernel primitive before any subprocess exists.
            probe = pidfd_open(os.getpid())
            try:
                pidfd_signal(probe, 0)
            finally:
                os.close(probe)
            startup_read, startup_write = os.pipe2(os.O_CLOEXEC)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("host_supervisor.py")),
                    str(request_path),
                    str(startup_read),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=(startup_read,),
            )
            os.close(startup_read)
            startup_read = None
            descriptor = pidfd_open(process.pid)
            os.write(startup_write, b"1")
            os.close(startup_write)
            startup_write = None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InfrastructureError("ordinary supervisor deadline exceeded")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(output)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > limit:
                            raise InfrastructureError("ordinary supervisor output limit exceeded")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            with result_path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversize supervisor result")
            result = json.loads(raw)
            if (
                type(result) is not dict
                or set(result) != {"schema", "nonce", "code", "incident", "clean"}
                or type(result["schema"]) is not int
                or result["schema"] != 1
                or result["nonce"] != nonce
                or type(result["clean"]) is not bool
                or type(result["incident"]) not in (str, type(None))
                or (result["code"] is not None and type(result["code"]) is not int)
                or result["incident"]
                not in (
                    None,
                    "cancelled",
                    "deadline",
                    "live-descendant",
                    "output-limit",
                    "supervision",
                    "cleanup",
                )
            ):
                raise ValueError("invalid supervisor result")
            if result["incident"] is not None or not result["clean"]:
                raise InfrastructureError(
                    "ordinary session lifecycle failure: " + (result["incident"] or "cleanup")
                )
            if code != 0 or result["code"] is None:
                raise ValueError("invalid supervisor completion")
            return result["code"], bytes(output)
        except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError) as exc:
            raise InfrastructureError(
                "ordinary supervisor unavailable or invalid bounded result"
            ) from exc
        finally:
            for pending in (startup_read, startup_write):
                if pending is not None:
                    os.close(pending)
            if process is not None:
                if descriptor is not None:
                    # pidfd remains bound after wait; no reused PID or PGID can be targeted.
                    pidfd_signal(descriptor, signal.SIGTERM)
                try:
                    process.wait(timeout=7)
                except subprocess.TimeoutExpired as exc:
                    if descriptor is not None:
                        pidfd_signal(descriptor, signal.SIGKILL)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    raise InfrastructureError(
                        "ordinary supervisor cleanup deadline exceeded"
                    ) from exc
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    process.stdout.close()
