"""Required real-Bubblewrap controls: a scoped session shows each project file at one path.

The project, and everything inside the project directory, appears only below /project.
Nothing is mounted at the project's host path, and a frozen file is not reachable
through any other directory the session shows: not through the environment, wherever it
lives. These tests launch bwrap and are never skipped.
"""

import pytest
from test_isolated_child_imports import environment, put, run, toy_project

ONE_PATH_TEST = """\
import os
import sys

import pytest

pytestmark = pytest.mark.telescope(inputs=["src/toypkg/__init__.py"])
HOST = bytes.fromhex({host!r}).decode()


def identities(top):
    found = {{}}
    for directory, folders, files in os.walk(top):
        for name in folders + files:
            path = os.path.join(directory, name)
            status = os.lstat(path)
            found.setdefault((status.st_dev, status.st_ino), []).append(path)
    return found


def test_the_project_host_path_does_not_exist():
    assert not os.path.lexists(HOST)


def test_frozen_files_are_reachable_only_below_project():
    frozen = {{
        key: paths
        for key, paths in identities("/project").items()
        if not any(path.startswith(sys.prefix + "/") for path in paths)
    }}
    assert any(paths == ["/project/src/toypkg/__init__.py"] for paths in frozen.values())
    roots = [
        name
        for name in dict.fromkeys(
            [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, *sys.path]
        )
        if name and os.path.isdir(name) and not name.startswith(("/usr", "/etc", "/project"))
    ]
    assert roots
    elsewhere = {{}}
    for root in roots:
        for key, paths in identities(root).items():
            elsewhere.setdefault(key, []).extend(paths)
    assert [frozen[key] + elsewhere[key] for key in frozen if key in elsewhere] == []
"""


@pytest.mark.parametrize("place", ["inside", "outside"])
def test_a_frozen_file_has_one_path_in_the_session(tmp_path, place):
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".venv" if place == "inside" else tmp_path / "env"
    interpreter, _ = environment(prefix)
    put(project, "tests/test_one_path.py", ONE_PATH_TEST.format(host=str(project).encode().hex()))
    code, report = run(interpreter, project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 2, report
