"""Real owned capture, ancestor access and abandoned-invocation recovery."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, entries

import pytest_telescope
from pytest_telescope import controller, model, sandbox, tool_capture
from pytest_telescope.model import DeclarationError


def project_with_tool(tmp_path, files=None):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    source = project.add_tree("first", files=files)
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first'])\ndef test_scoped(): pass\n")
    return project, source


@pytest.mark.parametrize("filename", ["-x", " leading", "nested/leaf.txt"])
def test_capture_creates_ancestors_before_valid_entry_names(tmp_path, filename):
    project, source = project_with_tool(tmp_path, {filename: b"exact owned bytes\n"})
    before = entries(source)
    stage = tmp_path / "staging"
    stage.mkdir()
    identities = []
    for _ in range(2):
        with tool_capture.Capture(model.load(project.root), stage, 10, None) as capture:
            copied = capture.directory / "first/install" / filename
            assert copied.read_bytes() == b"exact owned bytes\n"
            assert stat.S_IMODE(copied.stat().st_mode) == 0o444
            capture.verify(("first",))
            identities.append(capture.view(("first",)).identity)
        project.documents["first"]["roots"][0]["entries"].reverse()
        project.save("first")
    assert identities[0] == identities[1]
    assert not (stage / "tools").exists()
    assert entries(source) == before


def test_capture_missing_declared_parent_is_invalid(tmp_path):
    project, _ = project_with_tool(tmp_path, {"nested/value.txt": b"owned"})
    root = project.documents["first"]["roots"][0]
    root["entries"] = [row for row in root["entries"] if row["path"] != "nested"]
    project.save("first")
    with pytest.raises(DeclarationError):
        model.load(project.root)


def test_capture_searchable_unlistable_source_ancestor(tmp_path):
    ancestor = tmp_path / "search-only"
    ancestor.mkdir()
    project = ToolProject(tmp_path / "project", ancestor / "external")
    source = project.add_tree("first")
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first'])\ndef test_scoped(): pass\n")
    manifest = model.load(project.root)
    before = entries(source)
    stage = tmp_path / "staging"
    stage.mkdir()
    ancestor.chmod(0o311)
    try:
        with pytest.raises(PermissionError):
            with os.scandir(ancestor) as listing:
                list(listing)
        assert (source / "value.txt").read_bytes() == b"first"
        with tool_capture.Capture(manifest, stage, 10, None) as capture:
            assert (capture.directory / "first/install/value.txt").read_bytes() == b"first"
            capture.verify(("first",))
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o311
    finally:
        ancestor.chmod(0o755)
        if (stage / "tools").exists():
            tool_capture._remove(stage / "tools")
    assert entries(source) == before
    assert not (stage / "tools").exists()


@pytest.mark.parametrize("selected", [False, True])
def test_recover_abrupt_real_capture_preserves_source(tmp_path, selected):
    project, source = project_with_tool(tmp_path)
    if not selected:
        project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\ndef test_scoped(): pass\n")
    before = entries(source)
    code = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pytest_telescope import model, sandbox, tool_capture
root = Path(sys.argv[2])
with sandbox.invocation(root) as directory:
    with tool_capture.Capture(model.load(root), directory, 10, None):
        print(json.dumps({'directory': str(directory), 'pid': os.getpid()}), flush=True)
        os._exit(19)
'''
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code,
         str(Path(pytest_telescope.__file__).parent.parent), str(project.root)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert child.returncode == 19, child.stderr
    assert len(child.stdout) < 4096
    record = json.loads(child.stdout)
    directory = Path(record["directory"])
    assert directory.parent == project.root / ".pytest-telescope/scratch"
    assert directory.name.startswith("invocation-") and directory.is_dir()
    assert stat.S_IMODE((directory / "tools").stat().st_mode) == 0o555
    try:
        cache = project.root / ".pytest-telescope/cache"
        cache.mkdir(exist_ok=True)
        with controller.lock(cache / ".lock", 20):
            sandbox.recover(project.root)
        assert not directory.exists()
        with sandbox.invocation(project.root) as later:
            with tool_capture.Capture(model.load(project.root), later, 10, None):
                pass
        project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\ndef test_scoped(): pass\n")
        project.module("test_ordinary.py", "def test_ordinary(): pass\n")
        assert_counts(project.run(), executed=2)
    finally:
        # Repair only this finished child-owned residue so a failing oracle does
        # not leak it into another test. Never chmod the declared source.
        if (directory / "tools").exists():
            tool_capture._remove(directory / "tools")
        sandbox.recover(project.root)
    assert entries(source) == before


def test_recover_leaves_active_capture_lock_untouched(tmp_path):
    project, source = project_with_tool(tmp_path)
    before = entries(source)
    with sandbox.invocation(project.root) as directory:
        with tool_capture.Capture(model.load(project.root), directory, 10, None) as capture:
            sandbox.recover(project.root)
            assert directory.exists()
            assert (capture.directory / "first/install/value.txt").read_bytes() == b"first"
    assert not directory.exists()
    assert entries(source) == before
