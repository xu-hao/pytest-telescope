"""An isolated Python CLI bootstrap preserving its owned driver's exit and output."""

import os
import selectors
import signal
import subprocess
import sys
import time

process = subprocess.Popen(
    sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
)
buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
deadline = time.monotonic() + 60
failure = None
with selectors.DefaultSelector() as selector:
    for stream in buffers:
        selector.register(stream, selectors.EVENT_READ)
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = 124
            break
        for key, _ in selector.select(min(remaining, 0.2)):
            chunk = os.read(key.fileobj.fileno(), 16384)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            if sum(len(value) for value in buffers.values()) + len(chunk) > 2 * 1024 * 1024:
                failure = 125
                break
            buffers[key.fileobj].extend(chunk)
        if failure is not None:
            break
if failure is None:
    try:
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        failure = 124
if failure is not None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=8)
    raise SystemExit(failure)
sys.stdout.buffer.write(buffers[process.stdout])
sys.stderr.buffer.write(buffers[process.stderr])
process.stdout.close()
process.stderr.close()
if process.returncode < 0:
    signum = -process.returncode
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
raise SystemExit(process.returncode)
