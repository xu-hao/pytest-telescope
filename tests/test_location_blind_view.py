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
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts
from test_isolated_child_imports import (
    DECLARED,
    environment,
    install_editable,
    put,
    run,
    toy_project,
)

from pytest_telescope import controller, sandbox

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
    cache = tmp_path / "shared-results"
    reports = []
    for name in ("checkout-a", "checkout-b"):
        project = toy_project(tmp_path / name)
        put(project, "tests/test_where.py", WHERE_TESTS.format(expected=expected))
        reports.append(run(interpreter, project, "--cache-dir", str(cache)))
        # Results, failure evidence and inventories never record the checkout.
        assert_nothing_recorded_names(project, reports[-1][1], cache)
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
import re
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
    tables = ("mounts", "mountstats", "cmdline", "maps", "status", "cgroup")
    values = [read("/proc/self/" + name) for name in tables]
    values += [read("/proc/1/cmdline"), read("/proc/1/environ")]
    values += [os.readlink("/proc/self/" + name) for name in ("cwd", "exe", "root")]
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            values.append(os.readlink("/proc/self/fd/" + descriptor))
        except OSError:
            pass
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
    # The activation script names the environment by its path in the sandbox, in
    # whatever quoting the interpreter that created the environment wrote.
    assignment = r"^\\s*(?:export )?VIRTUAL_ENV=(['\\"]?)" + re.escape(prefix) + r"\\1$"
    activate = read(prefix + "/bin/activate").decode()
    assert re.search(assignment, activate, re.MULTILINE), activate
    # A console script starts the environment's interpreter by its path in the sandbox.
    completed = subprocess.run(["toy-script"], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == prefix + " /project/src/toypkg/__init__.py\\n"


def test_no_file_below_the_project_names_the_checkout():
    # Every regular file the sandbox shows below /project, the environment's included,
    # except the environment's own cached bytecode, which the next test states.
    files = [
        os.path.join(directory, leaf)
        for directory, _, leaves in os.walk("/project")
        for leaf in leaves
    ]
    files = [
        path for path in files
        if os.path.isfile(path) and not os.path.islink(path) and not environment_bytecode(path)
    ]
    assert "/project/" + ENVIRONMENT + "/bin/activate" in files
    assert named(files) == []
    assert [path for path in files if naming(read(path))] == []


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


def cache_bytecode(interpreter, *modules):
    """Start the scratch environment's interpreter the way its owner would have.

    An interpreter's start caches the bytecode of its startup hook, and an import caches
    that module's. These tests need those caches to exist before the run, whatever the
    job that runs them allows: an isolated start with an explicit environment ignores an
    ambient request not to write bytecode. Only modules of the scratch environment are
    imported, so nothing is written anywhere else.
    """
    subprocess.run(
        [str(interpreter), "-I", "-c", "".join(f"import {name}\n" for name in modules)],
        check=True,
        timeout=60,
        env={"PATH": os.defpath},
    )


def cached(site_packages, module):
    return sorted((site_packages / "__pycache__").glob(module + ".*.pyc"))


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
    # The finder's bytecode is cached from the original, which names the checkout.
    cache_bytecode(interpreter)
    assert cached(site_packages, "__editable___toypkg_0_1_finder")
    before = host_files(site_packages)
    cache = tmp_path / "results"
    code, report = run(interpreter, project, "--cache-dir", str(cache))
    assert code == 0, failed_lines(report)
    assert report["route_counts"]["scoped"]["executed"] == 8, report
    # The environment's own files are shown rewritten, never modified.
    assert host_files(site_packages) == before
    assert_nothing_recorded_names(project, report, cache)


# An interpreter quotes the environment's path in its activation script only when the
# path needs it. The name without a space is written bare after `VIRTUAL_ENV=`; the name
# with one is quoted, and is percent-encoded in the installed origin record.
@pytest.mark.parametrize("name", ["kestrel checkout", "kestrel-checkout"])
def test_an_environment_inside_the_project_does_not_reveal_the_checkout_location(tmp_path, name):
    root = tmp_path / name
    project, interpreter, site_packages = blind_project(root, root / ".venv")
    put(site_packages, "toyextra.py", "def where():\n    pass\n")
    script = project / ".venv" / "bin" / "toy-script"
    script.write_text(
        f"#!{project / '.venv' / 'bin' / 'python'}\n"
        "import sys\nimport toypkg\nprint(sys.prefix, toypkg.__file__)\n"
    )
    script.chmod(0o755)
    # Cache the module's bytecode on the host, as importing it there would. It records
    # the module's host path, which is what the open channel's test reads.
    cache_bytecode(interpreter, "toyextra")
    assert cached(site_packages, "__editable___toypkg_0_1_finder")
    [bytecode] = cached(site_packages, "toyextra")
    assert os.fsencode(project) in bytecode.read_bytes()
    write_blind_tests(project, ".venv")
    before = host_files(project / ".venv")
    cache = tmp_path / "results"
    code, report = run(interpreter, project, "--cache-dir", str(cache))
    assert code == 0, failed_lines(report)
    assert report["route_counts"]["scoped"]["executed"] == 11, report
    assert host_files(project / ".venv") == before
    assert_nothing_recorded_names(project, report, cache)


def test_checkouts_with_their_own_environment_share_one_identity_and_results(tmp_path):
    # Each checkout has its own environment inside it, at a path without a space. Every
    # file of the environment that names the checkout reads /project in both, so the
    # two environments are one sandbox identity and the second checkout reuses.
    cache = tmp_path / "shared-results"
    identities, reports = [], []
    for name in ("checkout-a", "checkout-b"):
        project = toy_project(tmp_path / name)
        interpreter, _ = environment(project / ".venv")
        put(project, "tests/test_where.py", WHERE_TESTS.format(expected="project"))
        identities.append(sandbox.describe(project, interpreter).identity)
        reports.append(run(interpreter, project, "--cache-dir", str(cache)))
        assert_nothing_recorded_names(project, reports[-1][1], cache)
    (first_code, first), (second_code, second) = reports
    assert first_code == 0 and second_code == 0, reports
    assert identities[0] == identities[1]
    assert {key: value for key, value in first["counts"].items() if value} == {
        "executed": 2,
        "selected": 2,
    }
    assert {key: value for key, value in second["counts"].items() if value} == {
        "reused": 2,
        "selected": 2,
    }


SHOWN_TEST = """\
import pytest

pytestmark = pytest.mark.telescope(inputs=[])


def test_shown_lines():
    with open({path!r}, "rb") as stream:
        shown = stream.read().decode().splitlines()
    assert shown == {expected!r}
"""


def shown_lines(tmp_path, name, lines):
    """Run a scoped test that compares an installed metadata file with `lines`.

    Each line is a pair: what the host file says, with `{root}` for the checkout's path
    and `{url}` for its file URL, and what the sandbox must show for it.
    """
    project = toy_project(tmp_path / name)
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, "pth")
    root, url = str(project), project.as_uri()
    record = site_packages / "toypkg-0.1.dist-info" / "locations.txt"
    record.write_text(
        "".join(line.replace("{root}", root).replace("{url}", url) + "\n" for line, _ in lines)
    )
    expected = [line.replace("{root}", root).replace("{url}", url) for _, line in lines]
    put(project, "tests/test_shown.py", SHOWN_TEST.format(path=str(record), expected=expected))
    before = host_files(site_packages)
    code, report = run(interpreter, project)
    assert code == 0, [failure for unit in report["units"] for failure in unit["failures"]]
    assert report["route_counts"]["scoped"]["executed"] == 1, report
    assert host_files(site_packages) == before


def test_a_path_after_a_value_delimiter_is_rewritten(tmp_path):
    shown_lines(
        tmp_path,
        "checkout",
        [
            ("export VIRTUAL_ENV={root}/.venv", "export VIRTUAL_ENV=/project/.venv"),
            ("key={root}/data/answer.txt", "key=/project/data/answer.txt"),
            ("key={root}", "key=/project"),
            ("--prefix={root} --other", "--prefix=/project --other"),
            ("first={root}:second={root}/bin", "first=/project:second=/project/bin"),
            ("key={url}/src", "key=file:///project/src"),
        ],
    )


def test_a_longer_path_that_ends_with_or_starts_with_the_checkout_is_not_rewritten(tmp_path):
    # A path that only has the checkout's path as its tail or head is another directory.
    # After `=`, the path is the checkout only when it starts there. A name may end with
    # `+`, `@`, `%` or `~`, and `~` starts a path below a home directory, so a path after
    # one of them is read as the rest of a longer path and is left as it is.
    kept = [
        "/mnt{root}",
        "/x{root}/src",
        "key=/x{root}/src",
        "key=/mnt{root}",
        "{root}=other/src",
        "{root}-other/src",
        "/opt/c++{root}/src",
        "name@{root}/src",
        "100%{root}/src",
        "~{root}/src",
        "~user{root}/src",
    ]
    shown_lines(
        tmp_path,
        "checkout",
        [(line, line) for line in kept] + [("{root}/src", "/project/src")],
    )


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


LATER_BYTECODE_SUBJECT = """\
import os

import __editable___toypkg_0_1_finder as finder

# Runs at collection, inside the sandbox. The cache file is the host's, unmasked, and
# was compiled from the original finder; the module still comes from the shown source.
assert os.path.getsize(finder.__cached__) > 0
assert finder.MAPPING == {"toypkg": "/project/src/toypkg"}, finder.MAPPING


def test_subject():
    pass
"""


def test_bytecode_cached_after_the_environment_was_described_is_not_loaded(tmp_path):
    project = toy_project(tmp_path / "checkout")
    interpreter, site_packages = environment(tmp_path / "env")
    install_editable(site_packages, project, "finder")
    box = replace(sandbox.describe(project, interpreter), bwrap=shutil.which("bwrap"))
    # Describing the environment cached nothing, so there was no bytecode to mask.
    assert not (site_packages / "__pycache__").exists()
    assert not [name for name, _, _ in box.overlays if name.endswith(".pyc")]
    # A later start of the environment's interpreter caches the original finder's bytecode.
    cache_bytecode(interpreter)
    [bytecode] = cached(site_packages, "__editable___toypkg_0_1_finder")
    assert os.fsencode(project / "src" / "toypkg") in bytecode.read_bytes()
    with sandbox.invocation(project) as directory:
        view = directory / "project"
        put(view, "test_subject.py", LATER_BYTECODE_SUBJECT)
        (view / "test_subject.py").chmod(0o444)
        view.chmod(0o755)
        with sandbox.invocation(project) as worker_directory:
            inventory = sandbox.execute(
                box,
                view,
                ["-p", "no:cacheprovider", "test_subject.py"],
                {},
                60,
                root=project,
                invocation_directory=worker_directory,
                request={
                    "mode": "collection",
                    "context_id": "d" * 64,
                    "registry": [],
                    "selectors": [],
                },
            )
    assert inventory["exit_code"] == 0, inventory["failures"]


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


TOOL_SHARED_TEST = """\
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.telescope(tools=["frontend"], inputs=["node_modules/value.txt"])


def test_tool_consumer_runs_at_project():
    link = Path(__file__).parents[1] / "node_modules"
    assert Path.cwd() == Path("/project") and link.is_symlink()
    assert (link / "value.txt").read_text() == "frontend"
    assert Path("/tools/frontend/install/value.txt").read_text() == "frontend"
"""

TOOL_BLIND_TEST = """\
import os
from pathlib import Path

import pytest

HOST = bytes.fromhex({host!r}).decode()
NAME = bytes.fromhex({name!r}).decode()
pytestmark = pytest.mark.telescope(tools=["frontend"], inputs=["node_modules/value.txt"])


def test_tool_consumer_view_does_not_name_the_checkout():
    values = [*os.environ, *os.environ.values(), open("/proc/self/mounts").read()]
    # Every mountinfo field except the fourth, the open channel stated elsewhere.
    for line in open("/proc/self/mountinfo").read().splitlines():
        fields = line.split(" ")
        values.append(" ".join(fields[:3] + fields[4:]))
    link = Path("/project/node_modules")
    values += [os.readlink(link), *map(str, Path("/tools").rglob("*"))]
    values += [str(path.resolve()) for path in (link, link / "value.txt")]
    values += [path.read_text() for path in Path("/project/tool-locks").iterdir()]
    assert [value[:300] for value in values if HOST in value or NAME in value] == []
    assert not os.path.lexists(HOST)
"""


def test_frozen_tool_views_do_not_name_the_checkout_and_are_shared_between_checkouts(tmp_path):
    first = ToolProject(tmp_path / "kestrel-checkout-a", tmp_path / "owned-tools")
    first.add_tree("frontend", preserve=True)
    first.bind_link("frontend")
    first.module("test_tool_shared.py", TOOL_SHARED_TEST)
    # An identical second checkout of the same project, using the same installed tool.
    second = tmp_path / "kestrel-checkout-b"
    shutil.copytree(first.root, second, symlinks=True)
    cache = tmp_path / "shared-results"
    reports = []
    for root in (first.root, second):
        # Only this module differs between the checkouts: it spells its own location.
        put(
            root,
            "tests/test_tool_blind.py",
            TOOL_BLIND_TEST.format(host=str(root).encode().hex(), name=root.name.encode().hex()),
        )
        reports.append(controller.run(root, explain=True, timeout=20, cache_dir=cache))
    assert_counts(reports[0], executed=2)
    # The identical consumer is reused at the second checkout; its tool view, project
    # link and key do not depend on where the checkout lives.
    assert_counts(reports[1], executed=1, reused=1)
    assert_nothing_recorded_names(first.root, reports[0], cache)
    assert_nothing_recorded_names(second, reports[1], cache)
