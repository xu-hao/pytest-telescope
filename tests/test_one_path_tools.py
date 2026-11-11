"""Required real-Bubblewrap controls: a frozen tool that keeps its original path is
mounted once, there, and `/tools/NAME/ROOT` is a symlink that resolves to it."""

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts

SESSION_TESTS = """\
import os
import subprocess

import pytest

pytestmark = pytest.mark.telescope(tools=["frontend"])
SOURCE = {source!r}


def mount_rows():
    with open("/proc/self/mountinfo") as stream:
        return [line.split(" ") for line in stream.read().splitlines()]


def test_no_source_is_mounted_twice():
    points = {{}}
    for row in mount_rows():
        points.setdefault((row[2], row[3]), []).append(row[4])
    assert {{key: value for key, value in points.items() if len(value) > 1}} == {{}}


def test_each_tool_file_has_one_real_path():
    found = {{}}
    for top in ("/tools/frontend", SOURCE):
        for directory, _, files in os.walk(top, followlinks=True):
            for name in files:
                path = os.path.join(directory, name)
                info = os.stat(path)
                found.setdefault((info.st_dev, info.st_ino), set()).add(os.path.realpath(path))
    assert found
    assert {{key: value for key, value in found.items() if len(value) > 1}} == {{}}


def test_the_tool_path_resolves_to_its_original_path():
    assert os.path.realpath("/tools/frontend/install/bin/tool") == SOURCE + "/bin/tool"


def test_a_tool_that_names_its_install_path_runs():
    done = subprocess.run(["/tools/frontend/install/bin/tool"], capture_output=True, text=True)
    assert (done.returncode, done.stdout) == (0, "configured\\n"), done.stderr
"""


@pytest.mark.parametrize("coverage", ["tree", "files"])
def test_a_preserved_tool_is_mounted_once_at_its_original_path(tmp_path, coverage):
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    source = project.external / "frontend"
    project.add_tree(
        "frontend",
        preserve=True,
        files={
            "bin/tool": f'#!/bin/sh\n. "{source}/etc/tool.conf"\necho "$VALUE"\n',
            "etc/tool.conf": "VALUE=configured\n",
        },
    )
    (source / "bin/tool").chmod(0o755)
    project.refresh("frontend")
    project.documents["frontend"]["roots"][0]["coverage"] = coverage
    project.save("frontend")
    project.module("test_preserved.py", SESSION_TESTS.format(source=str(source)))
    assert_counts(project.run(), executed=4)


def test_a_preserved_tool_source_inside_the_project_refuses(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    project.add_tree("frontend", preserve=True)
    inside = project.root / "vendor" / "frontend"
    inside.mkdir(parents=True)
    root = project.documents["frontend"]["roots"][0]
    root["source"] = str(inside)
    project.save("frontend")
    project.module(
        "test_inside.py",
        "import pytest\npytestmark = pytest.mark.telescope(tools=['frontend'])\n"
        "def test_value(): pass\n",
    )
    report = project.run()
    assert report["kind"] == "refusal", report
    assert report["capability"] == "tool:layout", report
    assert report["error"] == "frozen-tool source is not a supported narrow input", report
