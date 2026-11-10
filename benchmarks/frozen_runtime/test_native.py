"""Explicitly invoked host-runtime controls; these are outside the functional gate."""

import copy
import os
import shutil
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, file_digest
from frozen_tool_runtime_fixtures import PAYLOADS, native_install
from frozen_tool_runtime_manifest import RuntimeManifest


@pytest.fixture
def project(tmp_path):
    return ToolProject(tmp_path / "project", tmp_path / "installed")


def installed_node():
    value = os.environ.get("TELESCOPE_TEST_NODE")
    if value is None or not Path(value).is_file():
        pytest.fail("required installed Node prerequisite is absent; set TELESCOPE_TEST_NODE")
    return Path(value).resolve(strict=True)


def node_runtime(project, *, browser=False):
    node = installed_node()
    scripts = project.external / "driver"
    scripts.mkdir()
    for name in ("command.js", "browser.js", "child.py"):
        shutil.copyfile(PAYLOADS / name, scripts / name)
        (scripts / name).chmod(0o755 if name.endswith(".js") else 0o644)
    runtime = RuntimeManifest(project, "frontend")
    if not node.is_relative_to(Path("/usr")):
        runtime.add_files("node", node.parent)
    runtime.add_tree("driver", scripts)
    runtime.executable(node)
    env = runtime.executable(Path("/usr/bin/env"), "env")
    chrome = None
    if browser:
        value = os.environ.get("TELESCOPE_TEST_CHROMIUM")
        if value is None or not Path(value).is_file():
            pytest.fail(
                "required full Chromium prerequisite is absent; set TELESCOPE_TEST_CHROMIUM"
            )
        chrome = Path(value).resolve(strict=True)
        if chrome.name != "chrome" or "headless_shell" in str(chrome):
            pytest.fail(
                "the full Chromium control requires chrome, not a headless-shell substitute"
            )
        runtime.add_tree("browser", chrome.parent)
        runtime.executable(chrome)
        crashpad = chrome.parent / "chrome_crashpad_handler"
        if crashpad.exists():
            runtime.executable(crashpad)
        # Named font inputs are runtime resources; arbitrary /usr/share is never copied.
        runtime.add_tree("fontconfig", Path("/etc/fonts"))
        runtime.add_tree("fontconfig_defaults", Path("/usr/share/fontconfig/conf.avail"))
        runtime.add_tree("fonts", Path("/usr/share/fonts"))
        runtime.add_tree("local_fonts", Path("/usr/local/share/fonts"))
    runtime.finish(
        commands={"node": node},
        scripts=[
            (scripts / "command.js", env, "node"),
            (scripts / "browser.js", env, "node"),
        ],
    )
    return runtime, scripts, node, chrome


def test_native_origin_resolution_uses_same_frozen_library_at_both_paths(project, tmp_path):
    installation = native_install(tmp_path / "native")
    runtime = RuntimeManifest(project, "native")
    runtime.add_tree("install", installation)
    executable = runtime.executable(installation / "bin/owned-probe")
    runtime.finish()
    project.module(
        "test_native.py",
        f"""import stat
import subprocess
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['native'])
def test_native():
    preload = Path('/etc/ld.so.preload')
    assert preload.read_bytes() == b''
    assert stat.S_IMODE(preload.stat().st_mode) == 0o444
    with pytest.raises(OSError):
        preload.write_bytes(b'cannot replace controlled preload')
    for command in ({str(executable)!r}, {runtime.logical(executable)!r}):
        result = subprocess.run([command, 'argument with spaces'], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'native-one argument with spaces\\n'
        failure = subprocess.run([command], capture_output=True, timeout=10)
        assert failure.returncode == 23
""",
    )
    assert_counts(project.run(timeout=120), executed=1)
    assert_counts(project.run(timeout=120), reused=1)


def test_closed_library_search_hides_unpinned_competing_candidate(project, tmp_path):
    directory = tmp_path / "native"
    installation = native_install(directory, alternate=True)
    alternative = directory / "unselected-library/libownedprobe.so"
    assert alternative.is_file()
    runtime = RuntimeManifest(project, "native")
    runtime.add_tree("install", installation)
    executable = runtime.executable(installation / "bin/owned-probe")
    runtime.finish(library_pins={"libownedprobe.so": installation / "lib/libownedprobe.so"})
    project.module(
        "test_winner.py",
        f"""from pathlib import Path
import subprocess
import pytest
pytestmark = pytest.mark.telescope(tools=['native'])
def test_winner():
    assert not Path({str(alternative)!r}).exists()
    for command in ({str(executable)!r}, {runtime.logical(executable)!r}):
        result = subprocess.run([command, 'chosen'], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'native-one chosen\\n'
""",
    )
    assert_counts(project.run(timeout=120), executed=1)
    # A new unselected source candidate cannot become a view winner or affect its key.
    alternative.write_bytes(b"unselected bytes changed after the successful run")
    assert_counts(project.run(timeout=120), reused=1)


def test_unchanged_env_node_script_preserves_arguments_exit_signal_and_command_lookup(project):
    runtime, scripts, node, _ = node_runtime(project)
    script = scripts / "command.js"
    original = script.read_bytes()
    expected_hash = file_digest(node)
    project.module(
        "test_command.py",
        f"""import json
import signal
import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['frontend'])
def test_command():
    bootstrap = {runtime.logical(scripts / "child.py")!r}
    for script in ({str(script)!r}, {runtime.logical(script)!r}):
        command = [sys.executable, '-I', bootstrap, script]
        result = subprocess.run(command + ['space value', '', '--literal'], capture_output=True, text=True, timeout=75)
        assert result.returncode == 0, result.stderr
        answer = json.loads(result.stdout)
        assert answer['args'] == ['space value', '', '--literal']
        assert answer['node_sha256'] == {expected_hash!r}
        assert answer['is_link'] is True
        assert answer['command_target'] == {runtime.logical(node)!r}
        assert answer['command_directory'].startswith('/tools/.commands/')
        assert answer['directory_mode'] & 0o222 == 0
        failed = subprocess.run(command + ['exit'], capture_output=True, timeout=75)
        assert failed.returncode == 23
        signalled = subprocess.run(command + ['signal'], capture_output=True, timeout=75)
        assert signalled.returncode == -signal.SIGTERM
""",
    )
    assert_counts(project.run(timeout=120), executed=1)
    assert script.read_bytes() == original
    assert_counts(project.run(timeout=120), reused=1)


def test_missing_env_command_mapping_refuses_before_project_import(project):
    node_runtime(project)
    project.documents["frontend"]["commands"] = []
    project.save("frontend")
    project.module(
        "test_missing_command.py",
        """import pytest
pytestmark = pytest.mark.telescope(tools=['frontend'])
raise RuntimeError('project import must not run with an unresolved command')
def test_missing(): pass
""",
    )
    report = project.run(timeout=120)
    assert report["status"] in {"unsupported", "declaration_error"}, report
    assert not cache_bytes(project.root)


def test_same_command_bytes_with_different_runtime_mapping_refuse_collection_union(project):
    runtime, _, _, _ = node_runtime(project)
    second = copy.deepcopy(runtime.document)
    second["roots"][0]["id"] = "alternate_system"

    def rename(value):
        if isinstance(value, dict):
            if value.get("root") == "system":
                value["root"] = "alternate_system"
            for child in value.values():
                rename(child)
        elif isinstance(value, list):
            for child in value:
                rename(child)

    rename(second)
    project.documents["second"] = second
    project.definitions["second"] = {
        "format": "frozen-tool-v1",
        "manifest": "tool-locks/second.json",
    }
    project.save("second")
    project.configure()
    project.module(
        "test_collision.py",
        """import pytest
@pytest.mark.telescope(tools=['frontend'])
def test_first(): pass
@pytest.mark.telescope(tools=['second'])
def test_second(): pass
""",
    )
    report = project.run(timeout=120)
    assert report["status"] in {"declaration_error", "unsupported"}, report
    assert not cache_bytes(project.root)


def test_full_chromium_navigation_and_click_through_isolated_cli_child(project):
    runtime, scripts, node, chrome = node_runtime(project, browser=True)
    original_driver = (scripts / "browser.js").read_bytes()
    expected_hash = file_digest(node)
    project.module(
        "test_browser.py",
        f"""import json
import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['frontend'])
def test_browser():
    completed = subprocess.run([
        sys.executable, '-I', {runtime.logical(scripts / "child.py")!r},
        {str(scripts / "browser.js")!r}, {str(chrome)!r}, 'kept argument', '',
    ], capture_output=True, text=True, timeout=80)
    assert completed.returncode == 0, completed.stderr
    answer = json.loads(completed.stdout)
    assert answer == {{'clicked': 'clicked', 'args': ['kept argument', ''], 'node_sha256': {expected_hash!r}}}
""",
    )
    assert_counts(project.run(timeout=180), executed=1)
    assert (scripts / "browser.js").read_bytes() == original_driver
    assert_counts(project.run(timeout=180), reused=1)
