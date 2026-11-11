"""Required real-Bubblewrap controls: a scoped session cannot tell where its checkout lives.

Scoped results are shared between identical checkouts at different paths, so nothing a
scoped test can observe may depend on that path. The sandbox shows the frozen project at
/project and nothing at the checkout's own path, and shows the environment's files that
name the checkout rewritten to /project. These tests launch bwrap and are never skipped.

Two channels are still open and are stated by a test each, rather than left unchecked:
the source directory of each bind in `/proc/self/mountinfo`, and the raw bytes of an
in-project environment's own cached bytecode.
"""

import json
import os
import py_compile
import subprocess
from pathlib import Path

import pytest
from test_isolated_child_imports import (
    DECLARED,
    environment,
    install_editable,
    put,
    run,
    toy_project,
)

WHERE_TESTS = """\
from pathlib import Path

import pytest

# Wholly scoped: collected and run only inside the sandbox.
pytestmark = pytest.mark.telescope(inputs=[])
CHECKOUT = Path(__file__).parents[1].name


def test_working_directory():
    assert Path.cwd().name == {expected!r}


@pytest.mark.parametrize("checkout", [CHECKOUT])
def test_parameter(checkout):
    assert checkout == {expected!r}
"""


def case_identities(report):
    return sorted(unit["case_id"] for unit in report["units"])


def run_checkouts(tmp_path, expected):
    """Run identical checkouts at two paths with one environment and one results directory."""
    interpreter, _ = environment(tmp_path / "env")
    reports = []
    for name in ("checkout-a", "checkout-b"):
        project = toy_project(tmp_path / name)
        put(project, "tests/test_where.py", WHERE_TESTS.format(expected=expected))
        reports.append(run(interpreter, project, "--cache-dir", str(tmp_path / "shared-results")))
    return reports


def test_identical_checkouts_observe_one_location_and_share_results(tmp_path):
    (first_code, first), (second_code, second) = run_checkouts(tmp_path, "project")
    failures = [failure for unit in first["units"] for failure in unit["failures"]]
    assert first_code == 0 and second_code == 0, failures
    # The working directory and a parameter built from the checkout directory's name are
    # the same at both checkouts, so the second checkout's reuse is correct.
    assert {key: value for key, value in first["counts"].items() if value} == {
        "executed": 2,
        "selected": 2,
    }
    assert {key: value for key, value in second["counts"].items() if value} == {
        "reused": 2,
        "selected": 2,
    }
    assert case_identities(first) == case_identities(second)
    assert first["inventory_digest"] == second["inventory_digest"]


def test_a_test_that_expects_its_checkout_name_fails_at_every_checkout(tmp_path):
    # A scoped test cannot pass at one checkout and be reused at another: it sees
    # /project at both, so an expectation of either checkout's name fails at both.
    (first_code, first), (second_code, second) = run_checkouts(tmp_path, "checkout-a")
    for code, report in ((first_code, first), (second_code, second)):
        assert code == 1, report
        assert {key: value for key, value in report["counts"].items() if value} == {
            "failed": 2,
            "selected": 2,
        }
    assert case_identities(first) == case_identities(second)


BLIND_TESTS = """\
import json
import os
import site
import subprocess
import sys
import traceback
from importlib import metadata
from pathlib import Path

import pytest

import toypkg

# The checkout's host path and directory name, spelled so this file does not name them.
HOST = bytes.fromhex({host!r}).decode()
NAME = bytes.fromhex({name!r}).decode()
ENVIRONMENT = {environment!r}
pytestmark = pytest.mark.telescope(inputs={declared})


def naming(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    value = str(value)
    return any(
        needle in value
        for needle in (HOST, NAME, NAME.replace(" ", "%20"), NAME.replace(" ", "\\\\040"))
    )


def named(values):
    return [str(value)[:300] for value in values if naming(value)]


def read(path):
    with open(path, "rb") as stream:
        return stream.read()


def test_the_checkout_path_does_not_exist():
    assert not os.path.lexists(HOST)
    parent = os.path.dirname(HOST)
    assert not os.path.isdir(parent) or NAME not in os.listdir(parent)
    assert os.getcwd() == "/project" and __file__ == "/project/tests/test_blind.py"


def test_environment_variables():
    assert named([*os.environ, *os.environ.values(), read("/proc/self/environ")]) == []
    assert os.environ["PWD"] == "/project"
    assert not {{"OLDPWD", "VIRTUAL_ENV", "PYTHONPATH"}} & set(os.environ)
    if ENVIRONMENT:
        assert os.environ["PATH"].split(":")[0] == "/project/" + ENVIRONMENT + "/bin"


def test_interpreter_state(request):
    values = [
        sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix,
        *sys.path, *sys.argv, *getattr(sys, "orig_argv", []), *sys.path_importer_cache,
        os.getcwd(), __file__, toypkg.__file__,
        request.config.rootpath, request.config.inipath, *request.config.args,
        *request.config.invocation_params.args, request.node.nodeid, request.node.path,
    ]
    for name, module in list(sys.modules.items()):
        spec = getattr(module, "__spec__", None)
        values += [getattr(module, "__file__", None), getattr(module, "__cached__", None)]
        values += [getattr(spec, "origin", None), *(getattr(module, "__path__", None) or [])]
        if name.startswith("__editable__"):
            values += [repr(value) for key, value in vars(module).items() if key != "__builtins__"]
    assert named(values) == []
    assert toypkg.__file__ == "/project/src/toypkg/__init__.py"
    if ENVIRONMENT:
        assert sys.prefix == "/project/" + ENVIRONMENT
        assert sys.executable == "/project/" + ENVIRONMENT + "/bin/python"


def test_tracebacks_and_source_lines():
    texts = []
    for call in (toypkg.boom, lambda: json.loads("{{")):
        try:
            call()
        except ValueError:
            texts.append(traceback.format_exc())
    assert named(texts) == []
    assert 'File "/project/src/toypkg/__init__.py", line 5, in boom' in texts[0]


def mount_rows():
    return [line.split(" ") for line in read("/proc/self/mountinfo").decode().splitlines()]


def test_process_and_mount_tables():
    values = [read("/proc/self/" + name) for name in ("mounts", "cmdline", "maps", "status")]
    values += [read("/proc/1/cmdline")]
    values += [os.readlink("/proc/self/" + name) for name in ("cwd", "exe", "root")]
    # Every mountinfo field except the fourth, the bind's source directory.
    values += [" ".join(row[:3] + row[4:]) for row in mount_rows()]
    assert named(values) == []
    assert "/project" in [row[4] for row in mount_rows()]


def test_open_channel_mountinfo_names_the_source_directory_of_each_bind():
    # OPEN CHANNEL. The fourth mountinfo field is the bind source's path within its
    # filesystem. The frozen copy lives below the checkout's own state directory, so
    # that field names the checkout. No other field does, as the test above shows.
    sources = [row[3] for row in mount_rows() if row[4] == "/project" or row[4].startswith("/project/")]
    assert named(sources) != []


def installed_files():
    for directory in dict.fromkeys([*site.getsitepackages(), *sys.path]):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if name.endswith((".pth", ".egg-link", ".py")) and os.path.isfile(path):
                yield path
            elif name.endswith((".dist-info", ".egg-info")) and os.path.isdir(path):
                yield from (os.path.join(path, leaf) for leaf in sorted(os.listdir(path)))
            elif name == "__pycache__":
                yield from (os.path.join(path, leaf) for leaf in sorted(os.listdir(path)))


def environment_bytecode(path):
    # The one kind of installed file left out below: bytecode an environment inside the
    # project cached for its own modules. A separate test states that open channel.
    return bool(ENVIRONMENT) and path.startswith("/project/" + ENVIRONMENT + "/") and (
        "/__pycache__/" in path
    )


def test_installed_metadata_and_startup_files():
    files = [
        path for path in installed_files()
        if os.path.isfile(path) and not environment_bytecode(path)
    ]
    assert named(files) == []
    assert [path for path in files if naming(read(path))] == []
    distribution = metadata.distribution("toypkg")
    origin = json.loads(distribution.read_text("direct_url.json"))
    assert origin == {{"url": "file:///project", "dir_info": {{"editable": True}}}}
    assert named([distribution.locate_file(""), *map(str, distribution.files or [])]) == []
    links = [path for path in files if path.endswith(".egg-link")]
    assert [read(path) for path in links] == [b"/project/src\\n."]


CHILD = '''
import json, os, sys
import toypkg
print(json.dumps({{
    "environment": dict(os.environ), "path": sys.path, "prefix": sys.prefix,
    "executable": sys.executable, "cwd": os.getcwd(), "package": toypkg.__file__,
    "modules": sorted(str(getattr(module, "__file__", None)) for module in sys.modules.values()),
    "mounts": open("/proc/self/mounts").read(),
}}))
'''


def test_isolated_child_interpreters():
    for flags in (["-I"], ["-I", "-O"]):
        completed = subprocess.run(
            [sys.executable, *flags, "-c", CHILD], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        assert named([completed.stdout, completed.stderr]) == []
        assert json.loads(completed.stdout)["package"] == "/project/src/toypkg/__init__.py"
"""

ENVIRONMENT_TESTS = """


def test_environment_inside_the_project():
    prefix = "/project/" + ENVIRONMENT
    assert not naming(read(prefix + "/pyvenv.cfg"))
    scripts = sorted(os.listdir(prefix + "/bin"))
    assert [name for name in scripts if os.path.isfile(prefix + "/bin/" + name)
            and naming(read(prefix + "/bin/" + name))] == []
    # A console script starts the environment's interpreter by its path in the sandbox.
    completed = subprocess.run(["toy-script"], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == prefix + " /project/src/toypkg/__init__.py\\n"


def test_open_channel_bytecode_of_an_environment_inside_the_project():
    # OPEN CHANNEL. Bytecode the environment cached for its own modules records the
    # path each source had when it was compiled, which lies below the checkout. An
    # imported module reports its path in the sandbox; only the cache file's raw bytes
    # still name the checkout.
    import toyextra

    assert toyextra.__file__.startswith("/project/" + ENVIRONMENT + "/")
    assert toyextra.__cached__ and naming(read(toyextra.__cached__))
    assert not naming(toyextra.where.__code__.co_filename)
"""

BOOM = "VALUE = 7\n\n\ndef boom():\n    raise ValueError\n"


def blind_project(root, interpreter_directory=None):
    """A checkout with an editable install in every form, and the environment to run it."""
    project = toy_project(root)
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(project, "src/toypkg/__init__.py", BOOM)
    interpreter, site_packages = environment(interpreter_directory or root.parent / "env")
    # A finder with its startup hook, a legacy path entry and a legacy link file.
    install_editable(site_packages, project, "finder")
    (site_packages / "easy-install.pth").write_text(f"{project / 'src'}\n")
    (site_packages / "toypkg.egg-link").write_text(f"{project / 'src'}\n.")
    return project, interpreter, site_packages


def write_blind_tests(project, environment_name=None):
    put(
        project,
        "tests/test_blind.py",
        BLIND_TESTS.format(
            host=str(project).encode().hex(),
            name=project.name.encode().hex(),
            environment=environment_name,
            declared=DECLARED,
        )
        + (ENVIRONMENT_TESTS if environment_name else ""),
    )


def failed_lines(report):
    return sorted(
        (failure["file"], failure["line"], failure["exception_type"])
        for unit in report["units"]
        for failure in unit["failures"]
    )


def host_files(directory):
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def assert_nothing_recorded_names(project, completed_report, *directories):
    needles = [str(project).encode(), project.name.encode()]
    recorded = json.dumps(
        {key: completed_report[key] for key in ("units", "groups", "collection")}
    ).encode()
    assert not any(needle in recorded for needle in needles)
    for directory in directories:
        for path in directory.rglob("*"):
            if path.is_file():
                assert not any(needle in path.read_bytes() for needle in needles), path


def test_a_scoped_session_cannot_observe_its_checkout_location(tmp_path):
    # A directory name with a space also covers the percent-encoded spelling of the path
    # in the installed origin record.
    project, interpreter, site_packages = blind_project(tmp_path / "kestrel checkout")
    write_blind_tests(project)
    # The host interpreter caches bytecode of its startup hook the first time it starts.
    subprocess.run([str(interpreter), "-c", "pass"], check=True, timeout=60)
    before = host_files(site_packages)
    cache = tmp_path / "results"
    code, report = run(interpreter, project, "--cache-dir", str(cache))
    assert code == 0, failed_lines(report)
    assert report["route_counts"]["scoped"]["executed"] == 8, report
    # The environment's own files are shown rewritten, never modified.
    assert host_files(site_packages) == before
    assert_nothing_recorded_names(project, report, cache)


def test_an_environment_inside_the_project_does_not_reveal_the_checkout_location(tmp_path):
    root = tmp_path / "kestrel checkout"
    project, interpreter, site_packages = blind_project(root, root / ".venv")
    put(site_packages, "toyextra.py", "def where():\n    pass\n")
    script = project / ".venv" / "bin" / "toy-script"
    script.write_text(
        f"#!{project / '.venv' / 'bin' / 'python'}\n"
        "import sys\nimport toypkg\nprint(sys.prefix, toypkg.__file__)\n"
    )
    script.chmod(0o755)
    # Cache the module's bytecode on the host, as importing it there would.
    subprocess.run([str(interpreter), "-c", "import toyextra"], check=True, timeout=60)
    write_blind_tests(project, ".venv")
    before = host_files(project / ".venv")
    cache = tmp_path / "results"
    code, report = run(interpreter, project, "--cache-dir", str(cache))
    assert code == 0, failed_lines(report)
    assert report["route_counts"]["scoped"]["executed"] == 10, report
    assert host_files(project / ".venv") == before
    assert_nothing_recorded_names(project, report, cache)


BYTECODE_TEST = """\
import os
import subprocess
import sys

import pytest

import __editable___toypkg_0_1_finder as finder

pytestmark = pytest.mark.telescope(inputs={declared})


def test_the_rewritten_finder_is_compiled_from_its_shown_source():
    assert finder.MAPPING == {{"toypkg": "/project/src/toypkg"}}
    # The stale cache file is shown empty, so it is never valid bytecode.
    assert os.path.getsize(finder.__cached__) == 0
    for flags in (["-I"], ["-I", "-O"], ["-I", "-OO"]):
        completed = subprocess.run(
            [sys.executable, *flags, "-c", "import toypkg; print(toypkg.__file__)"],
            capture_output=True, text=True, timeout=60,
        )
        assert completed.stdout == "/project/src/toypkg/__init__.py\\n", completed.stderr
"""


@pytest.mark.parametrize("mode", ["TIMESTAMP", "CHECKED_HASH", "UNCHECKED_HASH"])
def test_stale_bytecode_of_a_rewritten_startup_module_is_never_loaded(tmp_path, mode):
    project = toy_project(tmp_path / "checkout")
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, "finder")
    finder = site_packages / "__editable___toypkg_0_1_finder.py"
    # Bytecode compiled from the original finder, which maps the package to the checkout.
    # An unchecked hash-based file is loaded without comparing it to its source.
    for level in (0, 1, 2):
        py_compile.compile(
            str(finder),
            doraise=True,
            optimize=level,
            invalidation_mode=py_compile.PycInvalidationMode[mode],
        )
    cached = sorted((site_packages / "__pycache__").glob("__editable___toypkg_0_1_finder.*.pyc"))
    assert len(cached) == 3
    assert all(os.fsencode(project / "src" / "toypkg") in path.read_bytes() for path in cached)
    put(project, "tests/test_bytecode.py", BYTECODE_TEST.format(declared=DECLARED))
    before = host_files(site_packages)
    code, report = run(interpreter, project)
    assert code == 0, [failure for unit in report["units"] for failure in unit["failures"]]
    assert report["route_counts"]["scoped"]["executed"] == 1, report
    assert host_files(site_packages) == before


def test_the_environment_identity_covers_the_rewritten_files(tmp_path):
    # Rewritten files are part of what a scoped test sees, so they are part of its key:
    # changing the installed origin record reruns the case.
    project, interpreter, site_packages = blind_project(tmp_path / "checkout")
    put(
        project,
        "tests/test_value.py",
        f"import pytest\nimport toypkg\n\npytestmark = pytest.mark.telescope(inputs={DECLARED})"
        "\n\n\ndef test_value():\n    assert toypkg.VALUE == 7\n",
    )
    first = run(interpreter, project)
    assert first[0] == 0 and first[1]["counts"]["executed"] == 1, first
    second = run(interpreter, project)
    assert second[0] == 0 and second[1]["counts"]["reused"] == 1, second
    record = site_packages / "toypkg-0.1.dist-info" / "direct_url.json"
    record.write_text(json.dumps({"url": Path(project).as_uri(), "dir_info": {}}))
    third = run(interpreter, project)
    assert third[0] == 0 and third[1]["counts"]["executed"] == 1, third
