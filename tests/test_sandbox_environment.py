"""The interpreter environment a scoped view binds and its cache identity; no sandbox."""

import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from pytest_telescope import sandbox
from pytest_telescope.model import UnsupportedCapability


def environment(directory):
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
    return interpreter, Path(purelib)


def install(site_packages, name, version):
    """Record an installed distribution the way an installer leaves its metadata."""
    record = site_packages / f"{name}-{version}.dist-info"
    record.mkdir()
    (record / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return record


def test_identity_covers_the_installed_distributions(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    project.mkdir()
    original = sandbox.describe(project, interpreter)
    assert original.identity == sandbox.describe(project, interpreter).identity
    record = install(site_packages, "example_extra", "1.0")
    installed = sandbox.describe(project, interpreter)
    assert installed.identity != original.identity
    (record / "METADATA").write_text("Metadata-Version: 2.1\nName: example_extra\nVersion: 2.0\n")
    upgraded = sandbox.describe(project, interpreter)
    assert upgraded.identity not in {original.identity, installed.identity}
    for path in sorted(record.iterdir()):
        path.unlink()
    record.rmdir()
    assert sandbox.describe(project, interpreter).identity == original.identity


def test_identity_differs_between_interpreter_environments(tmp_path):
    first, first_site = environment(tmp_path / "first")
    second, second_site = environment(tmp_path / "second")
    project = tmp_path / "project"
    project.mkdir()
    # Identical builds with identical distributions share an identity regardless of path.
    assert sandbox.describe(project, first).identity == sandbox.describe(project, second).identity
    install(second_site, "example_extra", "1.0")
    assert sandbox.describe(project, first).identity != sandbox.describe(project, second).identity


def test_view_binds_the_environment_read_only_at_its_host_paths(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    project.mkdir()
    view = sandbox.describe(project, interpreter)
    destinations = {destination for _, destination in view.binds}
    assert str(tmp_path / "env") in destinations
    assert view.executable == str(interpreter)
    assert view.path.split(":")[0] == str(interpreter.parent)
    assert not any(
        Path(destination) == project or project in Path(destination).parents
        for destination in destinations
    )
    stdlib = sysconfig.get_path("stdlib")
    if not stdlib.startswith("/usr/"):
        assert any(
            Path(stdlib) == Path(destination) or Path(destination) in Path(stdlib).parents
            for destination in destinations
        )


def test_project_import_paths_are_not_bound_but_its_virtual_environment_is(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    interpreter, site_packages = environment(project / ".venv")
    # Editable installs point the environment at project source, or at the project itself.
    (site_packages / "editable.pth").write_text(f"{project / 'src'}\n{project}\n{tmp_path}\n")
    destinations = {destination for _, destination in sandbox.describe(project, interpreter).binds}
    assert str(project / ".venv") in destinations
    assert str(project / "src") not in destinations
    assert str(project) not in destinations
    assert str(tmp_path) not in destinations


def test_an_environment_containing_the_project_is_refused(tmp_path):
    interpreter, _ = environment(tmp_path / "env")
    project = tmp_path / "env" / "work"
    project.mkdir()
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
