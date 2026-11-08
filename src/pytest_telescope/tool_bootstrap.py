"""Native-view startup guard, loaded by path with -I -S before pytest imports."""

import errno
import os
import sys


def main():
    try:
        # Matching PID 1 and proc-self identities are essential: close_fds alone
        # would not prevent reopening a controller handle through a host proc mount.
        if os.getpid() != 1 or os.readlink("/proc/self") != "1":
            raise ValueError("private process view required")
        descriptors = os.listdir("/proc/self/fd")
        if len(descriptors) > 8192 or any(not name.isdecimal() for name in descriptors):
            raise ValueError("descriptor inventory bound")
        for name in descriptors:
            descriptor = int(name)
            if descriptor > 2:
                try:
                    os.close(descriptor)
                except OSError as error:
                    # The directory iterator's descriptor has already been closed.
                    # No foreign threads or project imports exist in this guard.
                    if error.errno != errno.EBADF:
                        raise
    except (OSError, ValueError):
        print("TELESCOPE_TOOL_VIEW_REFUSAL", flush=True)
        return 4
    os.execv(sys.executable, [sys.executable, "-I", "/telescope/worker.py"])


if __name__ == "__main__":
    raise SystemExit(main())
