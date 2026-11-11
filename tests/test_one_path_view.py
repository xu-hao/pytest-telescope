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


# Mount points the sandbox makes from one source on purpose. None: every bind source,
# including each environment directory, is mounted at exactly one mount point.
SYSTEM_MOUNTS = ()

LINKED_TEST = """\
import os
import sys

import pytest

pytestmark = pytest.mark.telescope(inputs=[])
GIVEN = {given!r}
REAL = {real!r}
SYSTEM_MOUNTS = {system!r}


def mount_rows():
    with open("/proc/self/mountinfo") as stream:
        return [line.split(" ") for line in stream.read().splitlines()]


def test_no_source_is_mounted_twice():
    points = {{}}
    for row in mount_rows():
        if row[4] not in SYSTEM_MOUNTS:
            points.setdefault((row[2], row[3]), []).append(row[4])
    assert {{key: value for key, value in points.items() if len(value) > 1}} == {{}}


def test_the_environment_resolves_to_its_one_mount():
    assert sys.prefix == GIVEN
    assert os.path.realpath(sys.prefix) == REAL
    mounted = [row[4] for row in mount_rows()]
    assert REAL in mounted
    if GIVEN != REAL:
        assert GIVEN not in mounted
    assert os.path.isfile(os.path.join(sys.prefix, "pyvenv.cfg"))
"""


def linked_layout(tmp_path, project, layout):
    """An environment reached through a host symlink.

    Returns the interpreter's environment as given on the host, its real directory as
    the sandbox shows it, and the given directory as the sandbox shows it.
    """
    if layout == "plain-outside":
        prefix = tmp_path / "env"
        environment(prefix)
        return prefix, prefix, str(prefix)
    if layout == "prefix-link":
        environment(tmp_path / "real-env")
        (tmp_path / "env").symlink_to(tmp_path / "real-env")
        return tmp_path / "env", tmp_path / "real-env", str(tmp_path / "env")
    if layout == "ancestor-link":
        environment(tmp_path / "real" / "envs" / "env")
        (tmp_path / "link").symlink_to(tmp_path / "real")
        given = tmp_path / "link" / "envs" / "env"
        return given, tmp_path / "real" / "envs" / "env", str(given)
    if layout == "project-link":
        environment(tmp_path / "outside-env")
        (project / ".venv").symlink_to(tmp_path / "outside-env")
        return project / ".venv", tmp_path / "outside-env", "/project/.venv"
    if layout == "project-relative-link":
        environment(tmp_path / "shared-venv")
        (project / ".venv").symlink_to("../shared-venv")
        return project / ".venv", tmp_path / "shared-venv", "/project/.venv"
    prefix = project / ".venv"
    environment(prefix)
    return prefix, "/project/.venv", "/project/.venv"


@pytest.mark.parametrize(
    "layout",
    [
        "plain-outside",
        "plain-inside",
        "prefix-link",
        "ancestor-link",
        "project-link",
        "project-relative-link",
    ],
)
def test_each_environment_directory_is_mounted_once(tmp_path, layout):
    project = toy_project(tmp_path / "checkout")
    given, real, shown = linked_layout(tmp_path, project, layout)
    put(
        project,
        "tests/test_linked.py",
        LINKED_TEST.format(given=shown, real=str(real), system=SYSTEM_MOUNTS),
    )
    code, report = run(given / "bin" / "python", project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 2, report
