"""Owned frontend programs with explicit existing Node/full-browser prerequisites."""

import pytest

from frozen_tool_fixtures import assert_counts, cache_bytes, file_digest, put
from frozen_tool_program_fixture import ProgramConfiguration, capture_evidence
from frozen_tool_worker_fixture import WorkerConfiguration


@pytest.fixture(autouse=True)
def evidence(tmp_path, monkeypatch):
    with capture_evidence(monkeypatch, tmp_path):
        yield


@pytest.fixture
def frontend(tmp_path, monkeypatch):
    return ProgramConfiguration(WorkerConfiguration(tmp_path, monkeypatch))


def command_case(frontend):
    frontend.project.module("test_frontend.py", f"""import json
import signal
import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['worker', 'frontend'])
def test_frontend(tmp_path):
    for script in ({str(frontend.scripts / 'command.js')!r}, {frontend.logical(frontend.scripts / 'command.js')!r}):
        command = [sys.executable, '-I', {frontend.logical(frontend.scripts / 'child.py')!r}, script]
        result = subprocess.run(command + ['space value', '', '--literal'], capture_output=True, text=True, timeout=75)
        assert result.returncode == 0, result.stderr
        answer = json.loads(result.stdout)
        assert answer['args'] == ['space value', '', '--literal']
        assert answer['node_sha256'] == {file_digest(frontend.node)!r}
        assert answer['is_link'] is True
        assert answer['command_target'] == {frontend.logical(frontend.node)!r}
        assert answer['command_directory'].startswith('/tools/.commands/')
        assert answer['directory_mode'] & 0o222 == 0
        assert subprocess.run(command + ['exit'], capture_output=True, timeout=75).returncode == 23
        assert subprocess.run(command + ['signal'], capture_output=True, timeout=75).returncode == -signal.SIGTERM
    output = tmp_path / 'frontend-output.txt'
    built = subprocess.run([sys.executable, '-I', {frontend.logical(frontend.scripts / 'child.py')!r}, {frontend.logical(frontend.scripts / 'build.js')!r}, str(output)], capture_output=True, timeout=75)
    assert (built.returncode, built.stdout, built.stderr) == (0, b'built\\n', b'')
    assert output.read_bytes() == b'FROZEN FRONTEND INPUT\\n'
""")


def test_env_node_unchanged_script_arguments_exit_signal_and_warm_reuse(frontend):
    script = frontend.scripts / "command.js"
    before = script.read_bytes()
    assert before.startswith(b"#!/usr/bin/env node\n")
    command_case(frontend)
    assert_counts(frontend.project.run(timeout=180), executed=1)
    assert script.read_bytes() == before
    assert_counts(frontend.project.run(timeout=180), reused=1)


def test_command_selection_change_invalidates_only_consumer(frontend):
    command_case(frontend)
    frontend.project.module("test_unrelated.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_unrelated():
    assert not Path('/tools/frontend').exists()
""")
    assert_counts(frontend.project.run(timeout=180), executed=2)
    assert_counts(frontend.project.run(timeout=180), reused=2)
    # An additional explicitly selected name changes command context, while the
    # unchanged env shebang still resolves node to exactly the same executable.
    frontend.document["commands"].append({"name": "owned-node-alias", "file": frontend.reference(frontend.node)})
    frontend.save()
    assert_counts(frontend.project.run(timeout=180), executed=1, reused=1)


@pytest.mark.parametrize("fault", ["missing_command", "path_override"])
def test_env_node_conflict_refuses_before_project_import(frontend, fault):
    if fault == "missing_command":
        frontend.document["commands"] = []
        frontend.save()
        marker = "pytestmark = pytest.mark.telescope(tools=['worker', 'frontend'])"
    else:
        marker = "pytestmark = pytest.mark.telescope(tools=['worker', 'frontend'], env=['PATH'])"
    frontend.project.module("test_refused.py", "import pytest\n" + marker + "\nraise RuntimeError('invalid command context reached import')\ndef test_refused(): pass\n")
    report = frontend.project.run(timeout=180)
    assert report["status"] in {"unsupported", "declaration_error"}, report
    assert not cache_bytes(frontend.project.root)


def test_full_chromium_real_navigation_click_through_isolated_cli(tmp_path, monkeypatch):
    frontend = ProgramConfiguration(WorkerConfiguration(tmp_path, monkeypatch), browser=True)
    driver = frontend.scripts / "browser.js"
    original = driver.read_bytes()
    assert original.startswith(b"#!/usr/bin/env node\n")
    frontend.project.module("test_browser.py", f"""import json
import subprocess
import sys
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker', 'frontend'])
def test_browser():
    assert Path({str(frontend.chrome)!r}).name == 'chrome'
    completed = subprocess.run([
        sys.executable, '-I', {frontend.logical(frontend.scripts / 'child.py')!r},
        {str(driver)!r}, {str(frontend.chrome)!r}, 'kept argument', '',
    ], capture_output=True, text=True, timeout=80)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {{'clicked': 'clicked', 'args': ['kept argument', ''], 'node_sha256': {file_digest(frontend.node)!r}}}
""")
    assert_counts(frontend.project.run(timeout=240), executed=1)
    assert driver.read_bytes() == original
    assert_counts(frontend.project.run(timeout=240), reused=1)


def test_changed_driver_refuses_stale_pin_without_reuse(frontend):
    command_case(frontend)
    assert_counts(frontend.project.run(timeout=180), executed=1)
    previous = cache_bytes(frontend.project.root)
    driver = frontend.scripts / "command.js"
    put(frontend.scripts, driver.name, driver.read_bytes() + b"\n// changed owned input\n", 0o755)
    report = frontend.project.run(timeout=180)
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    assert cache_bytes(frontend.project.root) == previous
