"""Required owned baseline-handle, mount, startup and publication controls."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox, tool_system
from pytest_telescope.model import InfrastructureError

from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put
from frozen_tool_system_fixtures import (
    assert_handles_closed, attach_system, budget, closed_handles, command,
    owned_baseline, run_child,
)


@pytest.mark.parametrize("controls", [False, True], ids=["absent-control-slots", "present-control-slots"])
def test_composed_owned_baseline_preserves_metadata_and_overrides_loader_slots(tmp_path, monkeypatch, controls):
    source = owned_baseline(tmp_path, monkeypatch, controls=controls)
    system = tool_system.BaselineEtc(budget())
    handles = closed_handles(system)
    try:
        arguments = command(tmp_path, system, """import json, os, stat
from pathlib import Path
assert os.getpid() == 1 and os.readlink('/proc/self') == '1'
assert Path('/etc/kept.txt').read_text() == 'admitted original'
assert stat.S_IMODE(Path('/etc/kept.txt').stat().st_mode) == 0o640
assert stat.S_IMODE(Path('/etc/nested').stat().st_mode) == 0o750
assert Path('/etc/nested/value.txt').read_text() == 'admitted directory'
assert Path('/etc/relative.txt').is_symlink()
assert os.readlink('/etc/relative.txt') == 'kept.txt'
for name in ('ld.so.cache', 'ld.so.preload'):
    item = Path('/etc') / name
    assert item.read_bytes() == b''
    assert stat.S_IMODE(item.stat().st_mode) == 0o444
try:
    Path('/etc/new-entry').write_text('refused')
except OSError:
    pass
else:
    raise AssertionError('composed parent remained writable')
print(json.dumps({'entries': sorted(path.name for path in Path('/etc').iterdir())}))
""")
        code, output = run_child(arguments)
        assert code == 0, output
        lines = output.decode().splitlines()
        assert lines[0] == "OWNED_WORKER_IMPORTED"
        assert json.loads(lines[1])["entries"] == ["kept.txt", "ld.so.cache", "ld.so.preload", "nested", "relative.txt"]
        assert (source / "ld.so.preload").exists() is controls
        if controls:
            assert (source / "ld.so.preload").read_text() == "unselected original preload"
    finally:
        system.close()
    assert_handles_closed(handles)


def test_real_worker_cannot_reopen_controller_held_handles_after_setup(tmp_path, monkeypatch):
    owned_baseline(tmp_path, monkeypatch)
    system = tool_system.BaselineEtc(budget())
    outside = put(tmp_path, "outside.txt", "outside sentinel")
    outside_fd = os.open(outside, os.O_RDONLY | os.O_NOFOLLOW)
    descriptors = [*closed_handles(system), outside_fd]
    tokens = [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors]
    host_paths = [f"/proc/{os.getpid()}/fd/{fd}" for fd in descriptors]
    assert Path(host_paths[-1]).read_text() == "outside sentinel"
    assert Path(f"/proc/{os.getpid()}/fd/{system.descriptors['kept.txt']}").read_text() == "admitted original"
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    project.add_tree("first", files={"value.txt": "first", "empty-cache": b"", "empty-preload": b""})
    attach_system(monkeypatch, admitted=system)
    project.module("test_handles.py", f"""import errno
import os
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_handles():
    assert os.getpid() == 1
    assert os.readlink('/proc/self') == '1'
    assert Path('/etc/kept.txt').read_text() == 'admitted original'
    for source in {host_paths!r}:
        with pytest.raises(OSError):
            descriptor = os.open(source, os.O_RDONLY)
            os.close(descriptor)
    for name in os.listdir('/proc/self/fd'):
        try:
            info = os.stat('/proc/self/fd/' + name)
        except OSError as error:
            assert error.errno in (errno.EBADF, errno.ENOENT)
        else:
            assert (info.st_dev, info.st_ino) not in {tokens!r}
    assert not Path({str(outside)!r}).exists()
""")
    try:
        assert_counts(project.run(), executed=1)
        assert_handles_closed(descriptors[:-1])
        assert os.pread(outside_fd, 64, 0) == b"outside sentinel"
    finally:
        system.close()
        os.close(outside_fd)


@pytest.mark.parametrize("swap", ["regular", "symlink"])
def test_source_swap_after_argv_capture_cannot_redirect_held_mount(tmp_path, monkeypatch, swap):
    source = owned_baseline(tmp_path, monkeypatch)
    system = tool_system.BaselineEtc(budget())
    try:
        arguments = command(tmp_path, system, """from pathlib import Path
assert Path('/etc/kept.txt').read_text() == 'admitted original'
print('HELD_ORIGINAL_USED')
""")
        system.verify(budget())
        (source / "kept.txt").rename(tmp_path / "retired-original")
        if swap == "regular":
            put(source, "kept.txt", "replacement must not win")
        else:
            target = put(tmp_path, "outside-replacement", "outside sentinel")
            (source / "kept.txt").symlink_to(target)
        # The argv holds /proc/controller/fd sources, so this deliberate post-verify
        # race must still mount the admitted object, never the replacement path.
        code, output = run_child(arguments)
        assert code == 0 and b"HELD_ORIGINAL_USED" in output, output
        with pytest.raises(InfrastructureError):
            system.verify(budget())
    finally:
        system.close()


def test_symlink_swap_during_admission_refuses_without_leaking_handles(tmp_path, monkeypatch):
    source = owned_baseline(tmp_path, monkeypatch)
    outside = put(tmp_path, "outside-admission-target", "outside sentinel")
    root_token = (source.stat().st_dev, source.stat().st_ino)
    original = os.open
    swapped = False
    admitted = []

    def open_entry(path, flags, *args, **kwargs):
        nonlocal swapped
        parent = kwargs.get("dir_fd")
        selected = False
        if parent is not None and flags & os.O_PATH:
            info = os.fstat(parent)
            selected = (info.st_dev, info.st_ino) == root_token
        if selected and path == "kept.txt" and not swapped:
            (source / "kept.txt").rename(tmp_path / "retired-admission-input")
            (source / "kept.txt").symlink_to(outside)
            swapped = True
        descriptor = original(path, flags, *args, **kwargs)
        if selected:
            admitted.append(descriptor)
            if path == "kept.txt":
                assert stat.S_ISLNK(os.fstat(descriptor).st_mode)
        return descriptor

    with monkeypatch.context() as fault:
        fault.setattr(os, "open", open_entry)
        with pytest.raises(InfrastructureError):
            tool_system.BaselineEtc(budget())
    assert swapped and admitted
    assert_handles_closed(admitted)
    assert outside.read_text() == "outside sentinel"


def test_preserved_baseline_link_does_not_grant_its_unmounted_target(tmp_path, monkeypatch):
    source = owned_baseline(tmp_path, monkeypatch)
    outside = put(tmp_path, "ungranted-link-target", "outside sentinel")
    (source / "outside-link").symlink_to(outside)
    system = tool_system.BaselineEtc(budget())
    try:
        arguments = command(tmp_path, system, f"""import os
from pathlib import Path
link = Path('/etc/outside-link')
assert link.is_symlink()
assert os.readlink(link) == {str(outside)!r}
try:
    link.read_text()
except FileNotFoundError:
    pass
else:
    raise AssertionError('preserved link granted an unmounted target')
print('NO_NEW_TARGET_GRANT')
""")
        code, output = run_child(arguments)
        assert code == 0 and b"NO_NEW_TARGET_GRANT" in output, output
    finally:
        system.close()


@pytest.mark.parametrize("change", ["membership", "mode", "link", "type"])
def test_composition_metadata_changes_refuse_held_view_and_change_identity(tmp_path, monkeypatch, change):
    source = owned_baseline(tmp_path, monkeypatch)
    system = tool_system.BaselineEtc(budget())
    old_identity = system.identity
    try:
        if change == "membership":
            put(source, "added.txt", "new baseline entry")
        elif change == "mode":
            (source / "kept.txt").chmod(0o600)
        elif change == "link":
            (source / "relative.txt").unlink()
            (source / "relative.txt").symlink_to("nested/value.txt")
        else:
            (source / "kept.txt").unlink()
            (source / "kept.txt").mkdir(mode=0o755)
        with pytest.raises(InfrastructureError):
            system.verify(budget())
    finally:
        system.close()
    updated = tool_system.BaselineEtc(budget())
    try:
        assert updated.identity != old_identity
    finally:
        updated.close()


def test_guard_rejects_non_pid1_before_worker_start():
    code, output = run_child([sys.executable, "-I", "-S", str(sandbox.TOOL_BOOTSTRAP)])
    assert code == 4
    assert output.splitlines() == [b"TELESCOPE_TOOL_VIEW_REFUSAL"]


def test_guard_rejects_wrong_proc_self_before_worker_import(tmp_path, monkeypatch):
    owned_baseline(tmp_path, monkeypatch)
    system = tool_system.BaselineEtc(budget())
    try:
        arguments = command(tmp_path, system, "raise AssertionError('worker must not import')\n", bad_proc=True)
        code, output = run_child(arguments)
        assert code == 4
        assert output.splitlines() == [b"TELESCOPE_TOOL_VIEW_REFUSAL"]
    finally:
        system.close()


def test_system_cleanup_failure_blocks_new_publication_and_hides_private_inventory(tmp_path, monkeypatch):
    source = owned_baseline(tmp_path, monkeypatch)
    private_name = "owned-private-system-entry"
    put(source, private_name, "owned metadata privacy control")
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    project.add_tree("first", files={"value.txt": "first", "empty-cache": b"", "empty-preload": b""})
    project.module("test_first.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_first(): pass
""")
    systems = attach_system(monkeypatch)
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    assert private_name.encode() not in b"".join(previous.values())
    project.module("test_new.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_new(): pass
""")
    original = tool_system.BaselineEtc.close
    closed = []

    def fail_close(self):
        handles = closed_handles(self)
        original(self)
        assert_handles_closed(handles)
        closed.append(self)
        raise sandbox.CleanupError("synthetic owned system cleanup failure")

    with monkeypatch.context() as fault:
        fault.setattr(tool_system.BaselineEtc, "close", fail_close)
        report = project.run()
    assert report["status"] == "infrastructure_error", report
    assert len(closed) == 1
    assert all(system.closed for system in systems)
    assert cache_bytes(project.root) == previous
    assert private_name not in json.dumps(report)


def test_actual_owned_bootstrap_byte_change_invalidates_consumer_cache(tmp_path, monkeypatch):
    owned_baseline(tmp_path, monkeypatch)
    implementation = tmp_path / "owned-implementation"
    runner = put(implementation, "controller.py", b"owned runner identity fixture\n")
    guard = put(implementation, "tool_bootstrap.py", sandbox.TOOL_BOOTSTRAP.read_bytes())
    monkeypatch.setattr(controller, "__file__", str(runner))
    monkeypatch.setattr(sandbox, "TOOL_BOOTSTRAP", guard)
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    project.add_tree("first", files={"value.txt": "first", "empty-cache": b"", "empty-preload": b""})
    attach_system(monkeypatch)
    project.module("test_guard_identity.py", """import os
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_guard_identity():
    assert os.getpid() == 1 and os.readlink('/proc/self') == '1'
""")
    assert_counts(project.run(), executed=1)
    assert_counts(project.run(), reused=1)
    guard.write_bytes(guard.read_bytes() + b"\n# owned byte-identity change\n")
    assert_counts(project.run(), executed=1)
