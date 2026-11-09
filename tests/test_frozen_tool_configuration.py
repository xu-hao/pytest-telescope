"""Static native view controls; these do not claim CPython worker compatibility."""

import os
import stat
import threading
from dataclasses import replace

import pytest

from pytest_telescope import tool_capture
from pytest_telescope.model import InfrastructureError, UnsupportedCapability
from pytest_telescope.sandbox import InvocationInterrupted

from frozen_tool_configuration_fixtures import StaticConfiguration, direct_static_read
from frozen_tool_fixtures import put


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return StaticConfiguration(tmp_path, monkeypatch)


def test_static_elf_alone_selects_closed_configuration_and_real_program_reads_it(configured):
    with configured.capture() as capture:
        runtime = capture.runtimes["native"]
        assert runtime.native and not runtime.dynamic and runtime.control is None
        view = capture.view(["native"])
        assert view.native and "/etc" in view.closed
        code, output = direct_static_read(configured.box, view, "/etc/selected.txt")
        assert (code, output) == (0, b"declared configuration\n")
        for filename in ("unselected.txt", "resolv.conf", "ld.so.cache"):
            code, output = direct_static_read(configured.box, view, "/etc/" + filename)
            assert (code, output) == (7, b"")
        code, output = direct_static_read(configured.box, view, "/etc/ld.so.preload")
        assert (code, output) == (0, b"")


def test_explicit_loader_without_elf_also_selects_native_view(configured):
    configured.executable.write_bytes(b"owned path-model executable, not launched\n")
    configured.document["executables"] = []
    configured.document["loader"] = {"profile": "linux-x86_64-glibc-v1", "directories": [], "cache": None}
    configured.save()
    with configured.capture() as capture:
        runtime = capture.runtimes["native"]
        assert not runtime.elfs and runtime.native and not runtime.dynamic
        assert capture.view(["native"]).native


def test_data_only_selection_does_not_close_configuration(configured):
    configured.executable.write_bytes(b"owned non-native data\n")
    configured.document["executables"] = []
    configured.save()
    with configured.capture() as capture:
        view = capture.view(["native"])
        assert not capture.runtimes["native"].native and not view.native
        assert "/etc" not in view.closed and str(configured.prefix) not in view.closed


@pytest.mark.parametrize("source_mode, expected", [(0o755, 0o555), (0o750, 0o550)])
def test_config_projection_preserves_exact_read_modes_and_fixed_preload(configured, source_mode, expected):
    put(configured.config, "nested/value", "nested\n", 0o640)
    (configured.config / "nested").chmod(source_mode)
    configured.selected.add("nested/value")
    configured.save()
    with configured.capture() as capture:
        view = capture.view(["native"])
        mapped = {destination: source for source, destination in view.bindings}
        etc = mapped["/etc"]
        assert stat.S_IMODE((etc / "nested").stat().st_mode) == expected
        assert stat.S_IMODE((etc / "nested/value").stat().st_mode) == 0o440
        preload = (etc / "ld.so.preload").lstat()
        assert stat.S_ISREG(preload.st_mode) and preload.st_size == 0
        assert stat.S_IMODE(preload.st_mode) == 0o444
        assert set(path.name for path in etc.iterdir()) == {"selected.txt", "nested", "ld.so.preload"}
        assert (configured.config / "nested").stat().st_mode & 0o777 == source_mode


@pytest.mark.parametrize("kind", ["nonempty", "symlink", "directory", "wrong-mode"])
def test_conflicting_preload_refuses_before_launch(configured, kind):
    path = configured.config / "ld.so.preload"
    if kind == "symlink":
        path.symlink_to("selected.txt")
    elif kind == "directory":
        path.mkdir()
    else:
        put(configured.config, path.name, "nonempty" if kind == "nonempty" else b"",
            0o600 if kind == "wrong-mode" else 0o644)
    configured.selected.add(path.name)
    configured.save()
    with pytest.raises(UnsupportedCapability) as caught:
        with configured.capture() as capture:
            capture.view(["native"])
    assert caught.value.capability == "tool:layout"


@pytest.mark.parametrize("absolute", [False, True])
def test_declared_configuration_link_preserves_literal_text_and_target(configured, absolute):
    target = "/etc/selected.txt" if absolute else "selected.txt"
    (configured.config / "alias").symlink_to(target)
    configured.selected.add("alias")
    configured.save()
    with configured.capture() as capture:
        view = capture.view(["native"])
        mapped = {destination: source for source, destination in view.bindings}
        assert os.readlink(mapped["/etc"] / "alias") == target
        for path in ("/etc/alias", "/tools/native/config/alias"):
            assert direct_static_read(configured.box, view, path) == (0, b"declared configuration\n")


@pytest.mark.parametrize("target", ["missing", "/unselected-outside-file", "alias"])
def test_unpinned_configuration_link_refuses(configured, target):
    (configured.config / "alias").symlink_to(target)
    configured.selected.add("alias")
    configured.save()
    with pytest.raises(UnsupportedCapability) as caught:
        with configured.capture():
            pytest.fail("an unclosed link was admitted")
    assert caught.value.capability == "tool:layout"


def test_unselected_owned_config_mutation_does_not_change_view_identity(configured):
    with configured.capture() as capture:
        before = capture.view(["native"]).identity
        put(configured.config, "unselected.txt", "changed but still absent\n")
        capture.verify(["native"])
        assert capture.view(["native"]).identity == before
    with configured.capture() as capture:
        assert capture.view(["native"]).identity == before


@pytest.mark.parametrize("swap", ["regular", "symlink"])
def test_selected_source_swap_refuses_without_redirecting_frozen_copy(configured, swap):
    with configured.capture() as capture:
        view = capture.view(["native"])
        selected = configured.config / "selected.txt"
        selected.rename(configured.config / "retired")
        if swap == "regular":
            put(configured.config, "selected.txt", "replacement\n", 0o640)
        else:
            selected.symlink_to("unselected.txt")
        with pytest.raises(InfrastructureError):
            capture.verify(["native"])
        # Inspect only the immutable owned artifact; do not launch after revalidation failed.
        mapped = {destination: source for source, destination in view.bindings}
        assert (mapped["/etc"] / "selected.txt").read_bytes() == b"declared configuration\n"


def test_owned_configuration_copy_corruption_is_refused(configured):
    with configured.capture() as capture:
        view = capture.view(["native"])
        copied = capture.directory / "native/config/selected.txt"
        copied.chmod(0o640)
        copied.write_bytes(b"corrupt frozen bytes\n")
        copied.chmod(0o440)
        with pytest.raises(InfrastructureError):
            capture.verify(["native"])
        assert view.native


@pytest.mark.parametrize("hook", [
    "lib/python3.12/site-packages/escape.pth", "lib/python3.12/site-packages/escape.egg-link",
    "bin/python3.12._pth", "lib/python3.12/sitecustomize.py",
    "lib/python3.12/usercustomize/__init__.py", "lib/python312.zip",
    "bin/pyvenv.cfg", "pyvenv.cfg", "pybuilddir.txt", "Modules/Setup.local",
])
def test_selected_unsupported_startup_candidate_refuses(configured, hook):
    put(configured.prefix, hook, "owned unsupported candidate\n")
    configured.save()
    with pytest.raises(UnsupportedCapability) as caught:
        with configured.capture() as capture:
            capture.view(["native"])
    assert caught.value.capability == "tool:layout"


@pytest.mark.parametrize("field", ["path", "sites"])
def test_observed_startup_search_bound_is_enforced(configured, field):
    def change(description):
        target = description if field == "path" else description["startup"]
        target[field] = [str(configured.prefix / ("search-" + str(i))) for i in range(65)]
    configured.description(change)
    with pytest.raises(UnsupportedCapability) as caught:
        with configured.capture() as capture:
            capture.view(["native"])
    assert caught.value.capability == "tool:layout"


def test_environment_overlay_outside_closed_prefix_refuses(configured):
    configured.box = replace(configured.box, binds=(*configured.box.binds, ("/owned-uncovered", "/owned-uncovered")))
    with pytest.raises(UnsupportedCapability) as caught:
        with configured.capture() as capture:
            capture.view(["native"])
    assert caught.value.capability == "tool:layout"


@pytest.mark.parametrize("case", ["valid", "missing", "oversize", "wrong-home", "duplicate", "unknown-key"])
def test_owned_venv_path_model_requires_exact_bounded_configuration(configured, case):
    # This is a Startup path-model fixture, not an executed virtual environment.
    venv = configured.prefix / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(str(configured.executable))
    content = f"home = {configured.prefix}/bin\ninclude-system-site-packages = false\n"
    if case == "oversize":
        content += "prompt = " + "x" * 65536
    elif case == "wrong-home":
        content = content.replace(str(configured.prefix / "bin"), "/outside/bin")
    elif case == "duplicate":
        content += "home = duplicate\n"
    elif case == "unknown-key":
        content += "unknown = value\n"
    if case != "missing":
        put(venv, "pyvenv.cfg", content)

    def change(description):
        fields = description["startup"]
        fields.update(prefix=str(venv), exec_prefix=str(venv),
                      platstdlib=str(venv / "lib/python3.12"),
                      purelib=str(venv / "lib/python3.12/site-packages"),
                      platlib=str(venv / "lib/python3.12/site-packages"))
    configured.description(change)
    configured.box = replace(configured.box, executable=str(venv / "bin/python"))
    configured.save()
    if case == "valid":
        with configured.capture() as capture:
            assert capture.view(["native"]).native
    else:
        with pytest.raises(UnsupportedCapability) as caught:
            with configured.capture() as capture:
                capture.view(["native"])
        assert caught.value.capability == "tool:layout"


def test_owned_projection_cleanup_finishes_and_never_changes_sources(configured):
    with configured.capture() as capture:
        directory = capture.directory
        capture.view(["native"])
    assert not directory.exists()
    assert (configured.config / "selected.txt").read_bytes() == b"declared configuration\n"
    assert stat.S_IMODE((configured.config / "selected.txt").stat().st_mode) == 0o640


@pytest.mark.parametrize("persistent", [False, True])
def test_native_configuration_copy_failure_has_two_bounded_attempts(configured, monkeypatch, persistent):
    original = tool_capture._measure
    attempts = []

    def measure(filename, expected, budget, destination=None, *, frozen=False):
        if str(filename) == "/etc/selected.txt" and destination is not None:
            attempts.append(destination)
            if persistent or len(attempts) == 1:
                raise InfrastructureError("owned configuration copy fault")
        return original(filename, expected, budget, destination, frozen=frozen)

    monkeypatch.setattr(tool_capture, "_measure", measure)
    capture = configured.capture()
    if persistent:
        with pytest.raises(InfrastructureError):
            with capture:
                pytest.fail("persistent configuration fault was accepted")
    else:
        with capture:
            assert capture.view(["native"]).native
    assert len(attempts) == 2
    assert not capture.directory.exists()


def test_native_configuration_cancellation_cleans_its_owned_copy(configured):
    capture = configured.capture()
    event = threading.Event()
    capture.cancellation = event
    with pytest.raises(InvocationInterrupted):
        with capture:
            capture.view(["native"])
            event.set()
            capture.verify(["native"])
    assert not capture.directory.exists()
