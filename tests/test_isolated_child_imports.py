"""Required real-Bubblewrap controls: isolated child interpreters and editable installs.

A project installed into the invoking environment in editable form points that
environment at the checkout's own path. The sandbox shows the frozen declared snapshot
there as well as at /project, so a `python -I` child resolves the project to declared
files only. These tests launch bwrap and are never skipped.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
from dataclasses import replace
from pathlib import Path

import pytest

import pytest_telescope
from pytest_telescope import sandbox

# The shape of the finder module a PEP 660 editable install by setuptools leaves in
# site-packages: a mapping from the top-level package to its absolute checkout path.
FINDER = """\
import sys
from importlib.machinery import PathFinder
from importlib.util import spec_from_file_location
from pathlib import Path

MAPPING = {mapping!r}


class _EditableFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname in MAPPING:
            init = Path(MAPPING[fullname]) / "__init__.py"
            return spec_from_file_location(fullname, init) if init.exists() else None
        parent, _, child = fullname.rpartition(".")
        if parent and parent in MAPPING:
            return PathFinder.find_spec(fullname, path=[MAPPING[parent]])
        return None


def install():
    if not any(finder == _EditableFinder for finder in sys.meta_path):
        sys.meta_path.append(_EditableFinder)
"""

DECLARED = '["src/toypkg/__init__.py", "src/toypkg/worker.py"]'

VIEW_TESTS = """\
import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import toypkg

ROOT = {root!r}
ENVIRONMENT = {environment!r}
LIVE_INODE = {inode!r}
pytestmark = pytest.mark.telescope(inputs={declared})


def child(*arguments):
    return subprocess.run(
        [sys.executable, "-I", *arguments], capture_output=True, text=True, timeout=60
    )


def test_module_child():
    completed = child("-m", "toypkg.worker")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "worker 7\\n"


def test_code_child():
    completed = child("-c", "import toypkg; print(toypkg.VALUE, toypkg.__file__)")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "7 " + ROOT + "/src/toypkg/__init__.py\\n"
    sibling = child("-c", "import toypkg.sibling")
    assert sibling.returncode != 0 and "ModuleNotFoundError" in sibling.stderr


def test_view():
    # The parent still imports declared source through pytest pythonpath.
    assert toypkg.__file__ == "/project/src/toypkg/__init__.py"
    assert Path.cwd() == Path("/project")
    # One frozen snapshot, shown at /project and at the checkout's own path; only an
    # environment that lives inside the checkout is added at its own host path.
    assert sorted(os.listdir("/project")) == ["pyproject.toml", "src", "tests"]
    shown = sorted(os.listdir(ROOT))
    if ENVIRONMENT:
        shown.remove(ENVIRONMENT)
        assert sys.prefix == ROOT + "/" + ENVIRONMENT
    assert shown == ["pyproject.toml", "src", "tests"]
    for base in ("/project", ROOT):
        assert sorted(os.listdir(base + "/src/toypkg")) == ["__init__.py", "worker.py"]
        for name in ("src/toypkg/sibling.py", "notes.txt"):
            assert not os.path.lexists(base + "/" + name)
    # The checkout path shows the frozen copy, never the live checkout.
    declared = os.stat(ROOT + "/src/toypkg/__init__.py")
    assert declared.st_ino != LIVE_INODE
    assert stat.S_IMODE(declared.st_mode) == 0o444
    for target in (ROOT + "/src/toypkg/__init__.py", ROOT + "/new.txt"):
        with pytest.raises(OSError) as refused:
            Path(target).write_text("changed")
        assert refused.value.errno in (errno.EROFS, errno.EACCES)
"""

VALUE_TEST = """\
import subprocess
import sys

import pytest

# Wholly scoped: the module is never imported on the host.
pytestmark = pytest.mark.telescope(inputs={declared})


def test_value():
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "toypkg.worker"], capture_output=True, text=True, timeout=60
    )
    assert completed.stdout == "worker 7\\n", completed.stderr
"""


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def toy_project(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    put(root, "src/toypkg/__init__.py", "VALUE = 7\n")
    put(
        root,
        "src/toypkg/worker.py",
        "import toypkg\n\nif __name__ == '__main__':\n    print('worker', toypkg.VALUE)\n",
    )
    put(root, "src/toypkg/sibling.py", "UNDECLARED = True\n")
    put(root, "notes.txt", "only in the live checkout\n")
    return root.resolve()


def environment(directory):
    """A virtual environment that also sees this one's packages, as a developer's would."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(directory)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    interpreter = directory / "bin" / "python"
    purelib = subprocess.run(
        [str(interpreter), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    shared = {sysconfig.get_path("purelib"), str(Path(pytest_telescope.__file__).parent.parent)}
    (Path(purelib) / "shared.pth").write_text("".join(path + "\n" for path in sorted(shared)))
    return interpreter, Path(purelib)


def install_editable(site_packages, project, form):
    """Install toypkg from `project` in editable form, as an installer leaves it."""
    record = site_packages / "toypkg-0.1.dist-info"
    record.mkdir()
    (record / "METADATA").write_text("Metadata-Version: 2.1\nName: toypkg\nVersion: 0.1\n")
    (record / "direct_url.json").write_text(
        json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
    )
    if form == "finder":
        mapping = {"toypkg": str(project / "src" / "toypkg")}
        (site_packages / "__editable___toypkg_0_1_finder.py").write_text(
            FINDER.format(mapping=mapping)
        )
        (site_packages / "__editable__.toypkg-0.1.pth").write_text(
            "import __editable___toypkg_0_1_finder; __editable___toypkg_0_1_finder.install()\n"
        )
    else:
        (site_packages / "__editable__.toypkg-0.1.pth").write_text(f"{project / 'src'}\n")


def run(interpreter, project, *arguments):
    completed = subprocess.run(
        [str(interpreter), "-m", "pytest_telescope", "run", "--explain", *arguments],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        pytest.fail(completed.stdout + completed.stderr)
    assert report["schema"] == 3 and report["sandbox"]["required"] is True, report
    return completed.returncode, report


def write_view_tests(project, environment_name=None):
    put(
        project,
        "tests/test_child.py",
        VIEW_TESTS.format(
            root=str(project),
            environment=environment_name,
            inode=os.stat(project / "src/toypkg/__init__.py").st_ino,
            declared=DECLARED,
        ),
    )


@pytest.mark.parametrize("mode", ["case", "batch"])
@pytest.mark.parametrize("form", ["finder", "pth"])
def test_isolated_children_import_declared_source_through_an_editable_install(tmp_path, form, mode):
    project = toy_project(tmp_path / "checkout")
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, form)
    write_view_tests(project)
    code, report = run(interpreter, project, "--execution-mode", mode)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 3, report
    profiles = [group["profile"] for group in report["groups"]]
    assert profiles == (["case-v1"] * 3 if mode == "case" else ["batch-v1"]), report


def test_an_environment_inside_the_project_root_keeps_both_views(tmp_path):
    project = toy_project(tmp_path / "checkout")
    interpreter, site_packages = environment(project / ".venv")
    install_editable(site_packages, project, "finder")
    write_view_tests(project, ".venv")
    code, report = run(interpreter, project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 3, report


SUBJECT = """\
import os
import sys

ROOT = {root!r}
LOCATION = {location!r}

# Runs at collection, inside the sandbox: both views and the environment are in place.
assert sys.prefix == ROOT + "/" + LOCATION, sys.prefix
assert sorted(os.listdir("/project")) == ["test_subject.py", "tools"]
assert sorted(os.listdir("/project/tools")) == ["helper.txt"]
assert open(ROOT + "/tools/helper.txt").read() == "declared\\n"
expected = ["test_subject.py", "tools"] + ([LOCATION] if "/" not in LOCATION else [])
assert sorted(os.listdir(ROOT)) == sorted(expected)
expected = ["helper.txt"] + (["env"] if LOCATION == "tools/env" else [])
assert sorted(os.listdir(ROOT + "/tools")) == sorted(expected)


def test_subject():
    pass
"""


def tree(directory):
    """Every entry under a directory with its type, mode, size, inode and times."""
    entries = {}
    for parent, names, files in os.walk(directory):
        for name in [".", *names, *files]:
            path = Path(parent, name)
            status = os.lstat(path)
            entries[str(path.relative_to(directory))] = (
                stat.S_IFMT(status.st_mode),
                stat.S_IMODE(status.st_mode),
                status.st_size,
                status.st_ino,
                status.st_mtime_ns,
                status.st_ctime_ns,
            )
    return entries


@pytest.mark.parametrize("location", [".venv", "tools/env"])
def test_bubblewrap_never_writes_into_the_frozen_snapshot(tmp_path, location):
    root = tmp_path / "checkout"
    root.mkdir()
    root = root.resolve()
    interpreter, _ = environment(root / location)
    box = replace(sandbox.describe(root, interpreter), bwrap=shutil.which("bwrap"))
    with sandbox.invocation(root) as directory:
        view = directory / "project"
        put(view, "test_subject.py", SUBJECT.format(root=str(root), location=location))
        put(view, "tools/helper.txt", "declared\n")
        for path in (view / "test_subject.py", view / "tools/helper.txt"):
            path.chmod(0o444)
        for path in (view / "tools", view):
            path.chmod(0o755)
        before = tree(view)
        with sandbox.invocation(root) as worker_directory:
            inventory = sandbox.execute(
                box,
                view,
                ["-p", "no:cacheprovider", "test_subject.py"],
                {},
                60,
                root=root,
                invocation_directory=worker_directory,
                request={"mode": "collection", "registry": [], "selectors": []},
            )
        assert inventory["exit_code"] == 0, inventory["failures"]
        assert tree(view) == before


def test_changing_child_imported_declared_source_reruns_the_case(tmp_path):
    project = toy_project(tmp_path / "checkout")
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, "finder")
    put(project, "tests/test_value.py", VALUE_TEST.format(declared=DECLARED))
    first = run(interpreter, project)
    assert first[0] == 0 and first[1]["counts"]["executed"] == 1, first
    second = run(interpreter, project)
    assert second[0] == 0 and second[1]["counts"]["reused"] == 1, second
    # Only the child imports worker.py; changing it reruns the case with the new bytes.
    put(
        project,
        "src/toypkg/worker.py",
        "import toypkg\n\nif __name__ == '__main__':\n    print('worker', toypkg.VALUE + 1)\n",
    )
    third = run(interpreter, project)
    assert third[0] == 1 and third[1]["counts"]["failed"] == 1, third
    assert third[1]["route_counts"]["scoped"]["reused"] == 0, third


def test_a_moved_checkout_reruns_cases_its_editable_install_no_longer_reaches(tmp_path):
    project = toy_project(tmp_path / "checkout")
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, "finder")
    put(project, "tests/test_value.py", VALUE_TEST.format(declared=DECLARED))
    first = run(interpreter, project)
    assert first[0] == 0 and first[1]["counts"]["executed"] == 1, first
    # The results directory moves with the checkout; the editable install does not.
    moved = project.rename(tmp_path / "moved")
    second = run(interpreter, moved)
    assert second[0] == 1 and second[1]["counts"]["failed"] == 1, second
    assert second[1]["route_counts"]["scoped"]["reused"] == 0, second


@pytest.mark.parametrize("editable", [False, True])
def test_identical_checkouts_at_different_paths_share_results(tmp_path, editable):
    cache = tmp_path / "shared-results"
    test = (
        VALUE_TEST.format(declared=DECLARED)
        if editable
        else "from pathlib import Path\nimport pytest\n\n\n"
        f"@pytest.mark.telescope(inputs={DECLARED})\n"
        "def test_value():\n"
        "    assert Path('src/toypkg/__init__.py').read_text() == 'VALUE = 7\\n'\n"
    )
    outcomes = []
    for name in ("first", "second"):
        project = toy_project(tmp_path / name)
        interpreter, site_packages = environment(tmp_path / (name + "-env"))
        if editable:
            install_editable(site_packages, project, "finder")
        put(project, "tests/test_value.py", test)
        code, report = run(interpreter, project, "--cache-dir", str(cache))
        assert code == 0, report
        outcomes.append({key: value for key, value in report["counts"].items() if value})
    assert outcomes == [{"executed": 1, "selected": 1}, {"reused": 1, "selected": 1}]
