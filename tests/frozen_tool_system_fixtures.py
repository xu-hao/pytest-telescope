"""Owned synthetic baseline inputs exercising real handle/mount/bootstrap code."""

import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from pytest_telescope import sandbox, tool_capture
from pytest_telescope.tool_context import digest
from pytest_telescope.tool_system import BaselineEtc

from frozen_tool_fixtures import put


def budget():
    return tool_capture.Budget(time.monotonic() + 20, None)


def owned_baseline(root, monkeypatch, *, controls=False):
    directory = root / "baseline"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    put(directory, "kept.txt", "admitted original", mode=0o640)
    put(directory, "nested/value.txt", "admitted directory")
    (directory / "nested").chmod(0o750)
    (directory / "relative.txt").symlink_to("kept.txt")
    if controls:
        put(directory, "ld.so.cache", "unselected original cache")
        put(directory, "ld.so.preload", "unselected original preload")
    original = tool_capture._parent

    @contextmanager
    def parent(filename):
        # Only this exact production admission point is routed to the owned double.
        # No real /etc directory or its children are inventoried by these controls.
        target = directory if Path(filename) == Path("/etc") else filename
        with original(target) as result:
            yield result

    monkeypatch.setattr(tool_capture, "_parent", parent)
    return directory


def command(root, system, body, *, bad_proc=False):
    """Real production setup/guard, with a small owned post-guard worker payload."""
    executable = shutil.which("bwrap")
    if executable is None:
        raise RuntimeError("required Bubblewrap executable is unavailable")
    box = replace(sandbox.describe(root), bwrap=executable)
    payload = put(root, "owned-worker.py", "print('OWNED_WORKER_IMPORTED', flush=True)\n" + body)
    cache = put(root, "frozen-cache", b"", mode=0o444)
    preload = put(root, "empty-preload", b"", mode=0o444)
    arguments = sandbox._base_arguments(box, tool_system=system)
    if bad_proc:
        wrong = root / "wrong-proc"
        wrong.mkdir()
        (wrong / "self").symlink_to("2")
        arguments.extend(["--ro-bind", str(wrong), "/proc"])
    arguments.extend([
        "--ro-bind", str(cache), "/etc/ld.so.cache",
        "--ro-bind", str(preload), "/etc/ld.so.preload",
        "--ro-bind", str(payload), sandbox.WORKER_PATH,
        "--ro-bind", str(sandbox.TOOL_BOOTSTRAP), sandbox.TOOL_BOOTSTRAP_PATH,
        "--remount-ro", "/", "--chdir", "/tmp", *sandbox._environment(box),
        "--", box.executable, "-I", "-S", sandbox.TOOL_BOOTSTRAP_PATH,
    ])
    return arguments


def run_child(arguments):
    return sandbox.capture(arguments, 20, env=sandbox._host_environment(), limit=1024 * 1024)


def attach_system(monkeypatch, *, admitted=None):
    """Attach the actual system mechanism to a data-only owned tool view.

    No ELF/cache discovery or host-runtime qualification is implied. Both fixed loader
    slot sources are ordinary declared owned empty files. Real controller, capture,
    sandbox.execute, startup guard, worker, teardown and publication paths still run.
    """
    original_capture = tool_capture.Capture._capture
    original_view = tool_capture.Capture.view
    original_verify = tool_capture.Capture.verify
    systems = []

    def capture(self, deadline):
        original_capture(self, deadline)
        self.system = BaselineEtc(self._budget(deadline)) if admitted is None else admitted
        systems.append(self.system)

    def verify(self, names, *, deadline=None):
        names = tuple(names)
        if self.system is not None and names:
            self.system.verify(self._budget(deadline))
        return original_verify(self, names, deadline=deadline)

    def view(self, names):
        result = original_view(self, names)
        if not result.names:
            return result
        source = self.directory / "first/install"
        return replace(
            result,
            system=self.system,
            bindings=(*result.bindings,
                      (source / "empty-cache", "/etc/ld.so.cache"),
                      (source / "empty-preload", "/etc/ld.so.preload")),
            identity=digest([result.identity, self.system.identity, "owned-empty-controls"]),
        )

    monkeypatch.setattr(tool_capture.Capture, "_capture", capture)
    monkeypatch.setattr(tool_capture.Capture, "verify", verify)
    monkeypatch.setattr(tool_capture.Capture, "view", view)
    return systems


def closed_handles(system):
    return [system.directory, *system.descriptors.values()]


def assert_handles_closed(handles):
    import errno

    for descriptor in handles:
        try:
            os.fstat(descriptor)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError("owned controller descriptor remained open")
