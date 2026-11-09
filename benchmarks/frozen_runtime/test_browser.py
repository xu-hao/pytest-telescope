"""Explicitly invoked full-browser benchmark; never part of functional readiness."""

import pytest
from frozen_tool_fixtures import assert_counts, file_digest
from frozen_tool_program_fixture import ProgramConfiguration, capture_evidence
from frozen_tool_worker_fixture import WorkerConfiguration


@pytest.fixture(autouse=True)
def evidence(tmp_path, monkeypatch):
    with capture_evidence(monkeypatch, tmp_path):
        yield


def test_full_chromium_real_navigation_click_through_isolated_cli(tmp_path, monkeypatch):
    frontend = ProgramConfiguration(WorkerConfiguration(tmp_path, monkeypatch), browser=True)
    driver = frontend.scripts / "browser.js"
    original = driver.read_bytes()
    assert original.startswith(b"#!/usr/bin/env node\n")
    frontend.project.module("test_browser.py", f"""import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['worker', 'frontend'])
def test_browser(capfd):
    assert Path({str(frontend.chrome)!r}).name == 'chrome'
    completed = subprocess.run([
        sys.executable, '-I', {frontend.logical(frontend.scripts / 'child.py')!r},
        {str(driver)!r}, {str(frontend.chrome)!r}, 'kept argument', '',
    ], capture_output=True, text=True, timeout=80)
    if completed.returncode != 0:
        observation = {{'existing_child_returncode': completed.returncode}}
        for name in ('stdout', 'stderr'):
            content = getattr(completed, name).encode()
            observation[name] = {{'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'truncated': len(content) > 65536, 'base64': base64.b64encode(content[:65536]).decode('ascii')}}
        with capfd.disabled():
            os.write(2, ('OWNED_BROWSER_CHILD_OBSERVATION ' + json.dumps(observation) + '\\n').encode('ascii'))
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {{'clicked': 'clicked', 'args': ['kept argument', ''], 'node_sha256': {file_digest(frontend.node)!r}}}
""")
    assert_counts(frontend.project.run(timeout=240), executed=1)
    assert driver.read_bytes() == original
    assert_counts(frontend.project.run(timeout=240), reused=1)
