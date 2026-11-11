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
    view = sandbox.describe(project, interpreter)
    destinations = {destination for _, destination in view.binds}
    # The environment keeps its place in the project, which the sandbox shows at /project.
    assert (str(project / ".venv"), "/project/.venv") in view.binds
    assert view.executable == "/project/.venv/bin/python"
    assert view.path.split(":")[0] == "/project/.venv/bin"
    assert not any(
        Path(destination) == project or project in Path(destination).parents
        for destination in destinations
    )
    assert not {"/project", "/project/src", str(tmp_path)} & destinations


def test_an_environment_containing_the_project_is_refused(tmp_path):
    interpreter, _ = environment(tmp_path / "env")
    project = tmp_path / "env" / "work"
    project.mkdir()
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"


def editable_finder(site_packages, target):
    """The two files a setuptools editable install leaves: a startup hook and its mapping."""
    (site_packages / "__editable___toypkg_0_1_finder.py").write_text(
        f"MAPPING = {{'toypkg': {str(target)!r}}}\n\n\ndef install():\n    pass\n"
    )
    (site_packages / "__editable__.toypkg-0.1.pth").write_text(
        "import __editable___toypkg_0_1_finder; __editable___toypkg_0_1_finder.install()\n"
    )


def test_identity_covers_how_the_environment_reaches_into_the_project(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    plain = sandbox.describe(project, interpreter).identity
    editable_finder(site_packages, project / "src" / "toypkg")
    mapped = sandbox.describe(project, interpreter).identity
    assert mapped != plain
    editable_finder(site_packages, project / "lib" / "toypkg")
    assert sandbox.describe(project, interpreter).identity not in {plain, mapped}
    # A mapping to another checkout does not reach this project.
    editable_finder(site_packages, tmp_path / "other" / "src" / "toypkg")
    assert sandbox.describe(project, interpreter).identity == plain
    # A path entry into the project reaches it too.
    (site_packages / "__editable___toypkg_0_1_finder.py").unlink()
    (site_packages / "__editable__.toypkg-0.1.pth").write_text(f"{project / 'src'}\n")
    assert sandbox.describe(project, interpreter).identity not in {plain, mapped}


def test_identity_does_not_depend_on_the_checkout_path(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # Without an editable install the project path is not part of the identity.
    assert (
        sandbox.describe(first, interpreter).identity
        == sandbox.describe(second, interpreter).identity
    )
    # With one, only where it points relative to the project counts.
    editable_finder(site_packages, first / "src" / "toypkg")
    reaching_first = sandbox.describe(first, interpreter).identity
    editable_finder(site_packages, second / "src" / "toypkg")
    assert sandbox.describe(second, interpreter).identity == reaching_first


@pytest.mark.parametrize("root", ["/", "/usr/share/app", "/etc/app"])
def test_a_project_root_whose_live_files_the_sandbox_would_show_is_refused(tmp_path, root):
    interpreter, _ = environment(tmp_path / "env")
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(root, interpreter)
    assert refused.value.capability == "sandbox:environment"


# Nothing is mounted at the project's own path, so that path may be anywhere outside the
# shared system directories, including a path the sandbox mounts something else at.
@pytest.mark.parametrize(
    "root",
    ["/project", "/project/app", "/tmp", "/tmp/app", "/dev/shm", "/dev/shm/app", "/metadata/app"],
)
def test_a_project_root_at_any_other_location_is_accepted(tmp_path, root):
    interpreter, _ = environment(tmp_path / "env")
    assert len(sandbox.describe(root, interpreter).identity) == 64


def shown(view):
    """The content the sandbox shows over each environment file, by file name."""
    return {Path(name).name: content for name, content, _ in view.overlays}


def test_environment_files_that_name_the_project_are_shown_rewritten(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    editable_finder(site_packages, project / "src" / "toypkg")
    (site_packages / "paths.pth").write_text(f"{project / 'src'}\n{project}\n")
    record = install(site_packages, "toypkg", "0.1")
    (record / "direct_url.json").write_text(f'{{"url": "{project.as_uri()}"}}')
    (site_packages / "toypkg.egg-link").write_text(f"{project}\n.")
    before = {path: path.read_bytes() for path in site_packages.rglob("*") if path.is_file()}
    assert shown(sandbox.describe(project, interpreter)) == {
        "__editable___toypkg_0_1_finder.py": (
            b"MAPPING = {'toypkg': '/project/src/toypkg'}\n\n\ndef install():\n    pass\n"
        ),
        "paths.pth": b"/project/src\n/project\n",
        "direct_url.json": b'{"url": "file:///project"}',
        "toypkg.egg-link": b"/project\n.",
        # Bytecode the probe's own start cached from the original finder: shown empty.
        "__editable___toypkg_0_1_finder." + sys.implementation.cache_tag + ".pyc": b"",
    }
    # The environment itself is never modified.
    assert {
        path: path.read_bytes()
        for path in site_packages.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    } == {path: content for path, content in before.items()}


def test_only_the_whole_project_directory_is_rewritten(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    project.mkdir()
    # A sibling whose name starts with the project's, a longer path that merely ends
    # with it, and another checkout are different directories and stay as they are.
    lines = [
        f"{project}-two/src",
        f"/mnt{project}/src",
        f"{tmp_path / 'other'}/src",
        f"{project}.bak",
    ]
    (site_packages / "others.pth").write_text("".join(line + "\n" for line in lines))
    view = sandbox.describe(project, interpreter)
    assert view.overlays == ()
    (site_packages / "mixed.pth").write_text(f"{project}\n{project}/\n{project}-two\n")
    assert shown(sandbox.describe(project, interpreter)) == {
        "mixed.pth": f"/project\n/project/\n{project}-two\n".encode()
    }


def test_a_project_at_project_needs_no_rewritten_files(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    editable_finder(site_packages, Path("/project/src/toypkg"))
    view = sandbox.describe("/project", interpreter)
    assert view.overlays == ()
    # The reference is still part of the identity.
    (site_packages / "__editable__.toypkg-0.1.pth").unlink()
    (site_packages / "__editable___toypkg_0_1_finder.py").unlink()
    assert sandbox.describe("/project", interpreter).identity != view.identity


def test_an_oversized_environment_file_naming_the_project_is_refused(tmp_path):
    interpreter, site_packages = environment(tmp_path / "env")
    project = tmp_path / "project"
    project.mkdir()
    (site_packages / "large.pth").write_text("# padding\n" * 120_000 + f"{project}\n")
    with pytest.raises(UnsupportedCapability) as refused:
        sandbox.describe(project, interpreter)
    assert refused.value.capability == "sandbox:environment"
    # A large file that does not name the project is no obstacle.
    (site_packages / "large.pth").write_text("# padding\n" * 120_000)
    assert sandbox.describe(project, interpreter).overlays == ()
