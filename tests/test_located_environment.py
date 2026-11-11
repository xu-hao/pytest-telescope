"""Required real-Bubblewrap controls: an environment inside the project that depends on
its host location refuses before any scoped session starts.

The sandbox shows an environment inside the project only below /project. A conda-style
environment lists, in `conda-meta/*.json`, every installed file its install location was
written into (`prefix_placeholder`). Unless the sandbox shows such a file rewritten, it
names a host path that does not exist in the view, so the environment cannot run there.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_isolated_child_imports import environment, put, run, toy_project

from pytest_telescope import sandbox
from pytest_telescope.model import UnsupportedCapability

SCOPED_TEST = """\
import pytest

pytestmark = pytest.mark.telescope(inputs=[])


def test_runs():
    assert True
"""


def record(prefix, name, paths):
    """Record an installed conda package the way conda leaves it in `conda-meta`."""
    meta = prefix / "conda-meta"
    meta.mkdir(exist_ok=True)
    entries = []
    for path, mode in paths:
        entry = {"_path": path, "path_type": "hardlink", "sha256": "0" * 64}
        if mode is not None:
            entry.update(prefix_placeholder="/opt/build/placeholder_prefix", file_mode=mode)
        entries.append(entry)
    document = {
        "name": name,
        "version": "1.0",
        "files": [path for path, _ in paths],
        "paths_data": {"paths_version": 1, "paths": entries},
    }
    (meta / f"{name}-1.0-0.json").write_text(json.dumps(document))


def located_files(prefix):
    """Files with the environment's install location written into them, as conda does."""
    put(prefix, "lib/libtoy.so", "")
    (prefix / "lib/libtoy.so").write_bytes(b"\x7fELF\0\0" + os.fsencode(prefix) + b"/ssl\0")
    put(prefix, "lib/pkgconfig/toy.pc", f"prefix={prefix}\nlibdir=${{prefix}}/lib\n")


def refusal(interpreter, project, cache):
    completed = subprocess.run(
        [str(interpreter), "-m", "pytest_telescope", "run", "--cache-dir", str(cache)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        pytest.fail(completed.stdout + completed.stderr)
    return completed.returncode, report


def recorded(cache):
    return sorted(path for path in cache.rglob("*") if path.is_file()) if cache.exists() else []


@pytest.mark.parametrize(
    "path,mode", [("lib/libtoy.so", "binary"), ("lib/pkgconfig/toy.pc", "text")]
)
def test_an_environment_inside_the_project_that_names_its_location_refuses(tmp_path, path, mode):
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".env"
    interpreter, _ = environment(prefix)
    located_files(prefix)
    record(prefix, "toy", [("lib/libtoy.so", None), ("lib/pkgconfig/toy.pc", None)])
    record(prefix, "located", [(path, mode)])
    put(project, "tests/test_scoped.py", SCOPED_TEST)
    cache = tmp_path / "results"
    code, report = refusal(interpreter, project, cache)
    assert code == 4, report
    assert report["kind"] == "refusal" and report["status"] == "unsupported", report
    assert report["capability"] == "sandbox:environment", report
    message = report["error"]
    assert "environment .env " in message and path in message, message
    assert "place the environment outside the project directory" in message, message
    assert str(project) not in message and str(tmp_path) not in message, message
    # The refusal comes from identifying the environment, which starts no sandbox, and
    # nothing ran or was recorded.
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
    assert str(refused.value) == message
    assert recorded(cache) == []


def test_a_script_shown_rewritten_does_not_refuse(tmp_path):
    # Its install location is written into the script, but the sandbox shows the script
    # rewritten to /project, so the environment runs there.
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".env"
    interpreter, _ = environment(prefix)
    script = prefix / "bin" / "toy-tool"
    script.write_text(f"#!{prefix / 'bin' / 'python'}\nprint('toy')\n")
    script.chmod(0o755)
    record(prefix, "toy-tool", [("bin/toy-tool", "text")])
    put(project, "tests/test_scoped.py", SCOPED_TEST)
    code, report = run(interpreter, project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 1, report


@pytest.mark.parametrize("layout", ["plain", "records-without-location", "outside-the-project"])
def test_environments_that_do_not_depend_on_their_location_inside_the_project_run(tmp_path, layout):
    project = toy_project(tmp_path / "checkout")
    prefix = tmp_path / "env" if layout == "outside-the-project" else project / ".env"
    interpreter, _ = environment(prefix)
    if layout != "plain":
        located_files(prefix)
        record(prefix, "toy", [("lib/libtoy.so", None), ("lib/pkgconfig/toy.pc", None)])
    if layout == "outside-the-project":
        record(prefix, "located", [("lib/libtoy.so", "binary"), ("lib/pkgconfig/toy.pc", "text")])
    put(project, "tests/test_scoped.py", SCOPED_TEST)
    code, report = run(interpreter, project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 1, report


def test_too_many_install_records_refuse(tmp_path):
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".env"
    interpreter, _ = environment(prefix)
    for index in range(sandbox.MAX_RECORDS + 1):
        record(prefix, f"toy{index}", [(f"lib/toy{index}.py", None)])
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
    assert "environment .env " in str(refused.value)
    assert str(project) not in str(refused.value)
    # One fewer is within the bound, and those records name no location.
    next((prefix / "conda-meta").glob("toy0-*.json")).unlink()
    assert sandbox.describe(project, interpreter).executable == "/project/.env/bin/python"


def test_install_records_larger_than_the_bound_refuse(tmp_path):
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".env"
    interpreter, _ = environment(prefix)
    record(prefix, "toy", [("lib/toy.py", None)])
    padding = sandbox.MAX_RECORD_BYTES - (prefix / "conda-meta/toy-1.0-0.json").stat().st_size
    with open(prefix / "conda-meta/large-1.0-0.json", "wb") as stream:
        stream.truncate(padding + 1)
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
    assert "environment .env " in str(refused.value)
    assert str(project) not in str(refused.value)


def test_unreadable_install_records_refuse(tmp_path):
    project = toy_project(tmp_path / "checkout")
    prefix = project / ".env"
    interpreter, _ = environment(prefix)
    put(prefix, "conda-meta/broken-1.0-0.json", "{not json")
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
    assert Path(".env/conda-meta/broken-1.0-0.json").name in str(refused.value)


def test_a_link_inside_the_project_to_a_located_environment_outside_it_refuses(tmp_path):
    # The environment already lives outside the project; the link inside it is what
    # starts the interpreter below /project, so the remedy names the link.
    project = toy_project(tmp_path / "checkout")
    prefix = tmp_path / "shared-env"
    interpreter, _ = environment(prefix)
    located_files(prefix)
    record(prefix, "located", [("lib/libtoy.so", "binary")])
    (project / ".env").symlink_to("../shared-env")
    put(project, "tests/test_scoped.py", SCOPED_TEST)
    cache = tmp_path / "results"
    code, report = refusal(project / ".env" / "bin" / "python", project, cache)
    assert code == 4, report
    assert report["capability"] == "sandbox:environment", report
    message = report["error"]
    assert message.startswith("the environment .env is a link inside the project "), message
    assert "start its interpreter by its real path outside the project" in message, message
    assert "remove the link" in message, message
    assert "place the environment outside the project directory" not in message, message
    assert str(project) not in message and str(tmp_path) not in message, message
    assert recorded(cache) == []
    # Started by its real path, the same environment is outside the project and runs.
    code, report = run(interpreter, project)
    assert code == 0, report
    assert report["route_counts"]["scoped"]["executed"] == 1, report
