"""Bounded owned subprocess capture; no replay or interpretation of application output."""

import os
import selectors
import signal
import subprocess
import time

from .model import InfrastructureError


def capture(argv, timeout, *, cwd=None, env=None, limit=64 * 1024 * 1024):
    process = None
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("owned subprocess deadline exceeded")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InfrastructureError("owned subprocess output limit exceeded")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError("owned subprocess unavailable or deadline exceeded") from exc
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise InfrastructureError("owned subprocess cleanup deadline exceeded") from exc
            process.stdout.close()
