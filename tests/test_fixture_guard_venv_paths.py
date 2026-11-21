"""Guarded fixture commitments normalize environment roots and refuse outside paths."""

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import pytest_telescope
from pytest_telescope import controller, sandbox

PLUGIN_TEMPLATE = """\
import pytest


@pytest.fixture(scope="module")
def env_plugin_value():
    return {value!r}


@pytest.fixture(autouse=True)
def env_plugin_autouse():
    return {autouse!r}
"""

VENV_DRIVER = """\
import json
import sys
from pathlib import Path

from pytest_telescope import controller, result_store

payload = json.loads(sys.argv[1])
store_dir = Path(payload["store_dir"]) if payload["store_dir"] else None


class FileBackedObjects:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def open(self, bucket):
        self.bucket_dir = self.directory / bucket
        self.bucket_dir.mkdir(parents=True, exist_ok=True)
        return self

    def _target(self, name):
        target = self.bucket_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def read(self, name, limit):
        target = self._target(name)
        if not target.is_file():
            return None
        return target.read_bytes()[: limit + 1]

    def create(self, name, data):
        target = self._target(name)
        try:
            with target.open("xb") as stream:
                stream.write(data)
            return True
        except FileExistsError:
            return False


if store_dir is not None:
    objects = FileBackedObjects(store_dir)
    result_store.BACKENDS["gs"] = objects.open

report = controller.run(
    Path(payload["project"]),
    cache_dir=Path(payload["cache_dir"]) if payload["cache_dir"] else None,
    fixture_guard=payload["fixture_guard"],
    result_store=payload["result_store"],
    force=payload["force"],
    arguments=["-p", "venv_guard_plugin"],
)
print(json.dumps(report))
"""


def _put(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _copy_distribution(name: str, purelib: Path) -> None:
    dist = importlib.metadata.distribution(name)
    for entry in dist.files or ():
        rel = str(entry)
        if rel.startswith("..") or entry.suffix in {".pyc", ".pyo"}:
            continue
        source = Path(dist.locate_file(entry)).resolve()
        if not source.is_file():
            continue
        destination = purelib / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _install_plugin(purelib: Path, *, value: int = 42, autouse: str = "active-v1") -> Path:
    plugin_file = purelib / "venv_guard_plugin.py"
    plugin_file.write_text(
        PLUGIN_TEMPLATE.format(value=value, autouse=autouse),
        encoding="utf-8",
    )
    dist_info = purelib / "venv_guard_plugin-0.1.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: venv-guard-plugin\nVersion: 0.1\n",
        encoding="utf-8",
    )
    return plugin_file


def _make_isolated_venv(directory: Path, *, plugin_value: int = 42) -> tuple[Path, Path]:
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(directory)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    interpreter = directory / "bin" / "python"
    purelib = Path(
        subprocess.check_output(
            [
                str(interpreter),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
            timeout=30,
        ).strip()
    )
    for dist_name in ("pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        try:
            importlib.metadata.distribution(dist_name)
        except importlib.metadata.PackageNotFoundError:
            if dist_name == "pygments":
                continue
            raise
        _copy_distribution(dist_name, purelib)
    pkg_source = Path(pytest_telescope.__file__).resolve().parent
    pkg_dest = purelib / "pytest_telescope"
    pkg_dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(pkg_source.iterdir()):
        if item.is_file() and item.suffix in {".py", ".json"}:
            shutil.copy2(item, pkg_dest / item.name)
    version = importlib.metadata.version("pytest-telescope")
    telescope_info = purelib / f"pytest_telescope-{version}.dist-info"
    telescope_info.mkdir(exist_ok=True)
    (telescope_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: pytest-telescope\nVersion: {version}\n",
        encoding="utf-8",
    )
    _install_plugin(purelib, value=plugin_value)
    return interpreter, purelib


def _make_guarded_project(root: Path) -> Path:
    _put(
        root,
        "pyproject.toml",
        '[project]\nname = "demo-guarded-pkg"\nversion = "0.1.0"\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n'
        '[tool.pytest_telescope.distributions.demo]\nformat = "pep621-static-v1"\n'
        'metadata = "pyproject.toml"\n',
    )
    _put(
        root,
        "tests/conftest.py",
        "import pytest\n\n\n@pytest.fixture(scope='module')\ndef project_label():\n    return 'guarded-ok'\n",
    )
    _put(
        root,
        "tests/test_guarded.py",
        "from importlib import metadata\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "pytestmark = pytest.mark.telescope(\n"
        "    inputs=['pyproject.toml', 'tests/conftest.py'],\n"
        "    distributions=['demo'],\n"
        ")\n\n\n"
        "def test_first(env_plugin_value, project_label, tmp_path, doctest_namespace):\n"
        "    assert env_plugin_value in (42, 99)\n"
        "    assert project_label == 'guarded-ok'\n"
        "    assert metadata.version('demo-guarded-pkg') == '0.1.0'\n"
        "    (tmp_path / 'marker.txt').write_text('one')\n"
        "    doctest_namespace['seen'] = 'first'\n\n\n"
        "def test_second(env_plugin_value, project_label, tmp_path, doctest_namespace):\n"
        "    assert env_plugin_value in (42, 99)\n"
        "    assert project_label == 'guarded-ok'\n"
        "    assert not (tmp_path / 'marker.txt').exists()\n"
        "    assert doctest_namespace['seen'] == 'first'\n",
    )
    return root


def _run_in_venv(
    interpreter: Path,
    project: Path,
    *,
    cache_dir: Path | None = None,
    store_dir: Path | None = None,
    result_store_url: str | None = None,
    force: bool = False,
) -> dict:
    payload = {
        "project": str(project),
        "cache_dir": str(cache_dir) if cache_dir is not None else None,
        "store_dir": str(store_dir) if store_dir is not None else None,
        "fixture_guard": "available-v1",
        "result_store": result_store_url,
        "force": force,
    }
    completed = subprocess.run(
        [str(interpreter), "-I", "-B", "-c", VENV_DRIVER, json.dumps(payload)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.requires_fixture_guard
def test_distinct_venv_paths_share_guarded_local_and_remote_cache_and_invalidate_on_source_change(
    tmp_path: Path,
) -> None:
    venv_a_dir = tmp_path / "alpha" / "venv-one"
    venv_b_dir = tmp_path / "beta" / "nested" / "venv-two"
    python_a, _ = _make_isolated_venv(venv_a_dir, plugin_value=42)
    python_b, purelib_b = _make_isolated_venv(venv_b_dir, plugin_value=42)
    symlink_venv = tmp_path / "symlinked-venv"
    symlink_venv.symlink_to(venv_b_dir, target_is_directory=True)
    python_b_symlink = symlink_venv / "bin" / "python"

    project_a = _make_guarded_project(tmp_path / "checkout-a")
    project_b = _make_guarded_project(tmp_path / "checkout-b")

    box_a = sandbox.describe(project_a, python_a)
    box_b = sandbox.describe(project_b, python_b)
    assert box_a.identity == box_b.identity

    shared_cache = tmp_path / "shared-local-cache"
    store_dir = tmp_path / "remote-store"
    store_url = "gs://guarded-results/prefix"

    cold = _run_in_venv(
        python_a,
        project_a,
        cache_dir=shared_cache,
        store_dir=store_dir,
        result_store_url=store_url,
    )
    assert cold["schema"] == 5 and cold["status"] == "passed"
    assert cold["fixture_guard"]["state"] == "admitted"
    assert cold["counts"]["executed"] == 2 and cold["counts"]["reused"] == 0
    assert cold["result_store"]["uploads"]["created"] == 1
    (cold_group,) = cold["groups"]
    cold_record = json.loads((shared_cache / f"{cold_group['group_id']}.json").read_text())

    # 1. Local cache hit from a distinct virtualenv filesystem path.
    warm_local = _run_in_venv(python_b, project_b, cache_dir=shared_cache)
    assert warm_local["schema"] == 5 and warm_local["status"] == "passed"
    assert warm_local["fixture_guard"]["state"] == "admitted"
    assert warm_local["groups"][0]["group_id"] == cold_group["group_id"]
    assert warm_local["counts"]["reused"] == 2 and warm_local["counts"]["executed"] == 0

    # 2. Remote result store hit from a fresh local cache and symlinked venv prefix.
    remote_cache = tmp_path / "fresh-remote-cache"
    warm_remote = _run_in_venv(
        python_b_symlink,
        project_b,
        cache_dir=remote_cache,
        store_dir=store_dir,
        result_store_url=store_url,
    )
    assert warm_remote["schema"] == 5 and warm_remote["status"] == "passed"
    assert warm_remote["fixture_guard"]["state"] == "admitted"
    assert warm_remote["groups"][0]["group_id"] == cold_group["group_id"]
    assert warm_remote["counts"]["reused"] == 2 and warm_remote["counts"]["executed"] == 0
    assert warm_remote["result_store"]["reused_groups"] == {"local": 0, "remote": 1}
    assert warm_remote["result_store"]["lookups"]["hits"] == 1
    fetched_record = json.loads((remote_cache / f"{cold_group['group_id']}.json").read_text())
    assert fetched_record["result"]["registry_id"] == cold_record["result"]["registry_id"]
    assert fetched_record["result"]["graph_id"] == cold_record["result"]["graph_id"]

    # 3. Changing installed plugin/fixture bytes invalidates registry_id and guarded_group_id
    # even when distribution version metadata (and therefore box.identity) is unchanged.
    _install_plugin(purelib_b, value=99, autouse="active-v2")
    assert sandbox.describe(project_b, python_b).identity == box_a.identity
    changed = _run_in_venv(
        python_b,
        project_b,
        cache_dir=shared_cache,
        store_dir=store_dir,
        result_store_url=store_url,
    )
    assert changed["schema"] == 5 and changed["status"] == "passed"
    assert changed["fixture_guard"]["state"] == "admitted"
    assert changed["groups"][0]["group_id"] != cold_group["group_id"]
    assert changed["counts"]["executed"] == 2 and changed["counts"]["reused"] == 0
    assert changed["result_store"]["lookups"]["misses"] == 1
    assert changed["result_store"]["uploads"]["created"] == 1


@pytest.mark.requires_fixture_guard
def test_fixture_defined_outside_admitted_roots_fails_closed(tmp_path: Path) -> None:
    _put(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    _put(
        tmp_path,
        "tests/conftest.py",
        "import sys\n"
        "from pathlib import Path\n\n"
        "outside = Path('/tmp/untrusted_outside_fixture.py')\n"
        "outside.write_text('import pytest\\n@pytest.fixture\\ndef rogue():\\n    return 1\\n')\n"
        "if '/tmp' not in sys.path:\n"
        "    sys.path.insert(0, '/tmp')\n"
        "from untrusted_outside_fixture import rogue  # noqa: F401\n",
    )
    _put(
        tmp_path,
        "tests/test_outside.py",
        "import pytest\n\n"
        "pytestmark = pytest.mark.telescope(inputs=['tests/conftest.py'])\n\n\n"
        "def test_uses_rogue(rogue):\n"
        "    assert rogue == 1\n",
    )
    with pytest.raises(controller.UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-metadata"


@pytest.mark.requires_fixture_guard
def test_plugin_registered_outside_admitted_roots_fails_closed(tmp_path: Path) -> None:
    _put(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    _put(
        tmp_path,
        "tests/conftest.py",
        "import sys\n"
        "from pathlib import Path\n\n"
        "outside = Path('/tmp/untrusted_outside_plugin.py')\n"
        "outside.write_text('class ExtraPlugin:\\n    pass\\n')\n"
        "if '/tmp' not in sys.path:\n"
        "    sys.path.insert(0, '/tmp')\n"
        "import untrusted_outside_plugin\n\n\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(untrusted_outside_plugin.ExtraPlugin(), 'outside-extra')\n",
    )
    _put(
        tmp_path,
        "tests/test_outside_plugin.py",
        "import pytest\n\n"
        "pytestmark = pytest.mark.telescope(inputs=['tests/conftest.py'])\n\n\n"
        "def test_ok():\n"
        "    assert True\n",
    )
    with pytest.raises(controller.UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-metadata"


def _touch_py(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")
    return path


def test_normalize_fixture_source_path_roles_and_most_specific_root(tmp_path: Path) -> None:
    from pytest_telescope.worker import fixture_source_roots, normalize_fixture_source_path

    project = tmp_path / "project"
    metadata = tmp_path / "metadata"
    venv_a = tmp_path / "envs" / "venv-a"
    venv_b = tmp_path / "deep" / "nested" / "venv-b"
    usr_local = tmp_path / "usr" / "local"
    env_mount = tmp_path / "env"
    base = tmp_path / "usr"

    project_file = _touch_py(project / "tests" / "conftest.py")
    metadata_file = _touch_py(metadata / "demo_pkg" / "helper.py")
    stdlib_file = _touch_py(base / "lib" / "python3.12" / "doctest.py")
    platstdlib_file = _touch_py(venv_a / "lib" / "python3.12" / "_sysconfigdata.py")
    purelib_a = _touch_py(venv_a / "lib" / "python3.12" / "site-packages" / "pkg" / "fix.py")
    purelib_b = _touch_py(venv_b / "lib" / "python3.12" / "site-packages" / "pkg" / "fix.py")
    purelib_local = _touch_py(usr_local / "lib" / "python3.12" / "site-packages" / "pkg" / "fix.py")
    purelib_in_proj = _touch_py(
        project / ".venv" / "lib" / "python3.12" / "site-packages" / "pkg" / "fix.py"
    )
    platlib_a = _touch_py(venv_a / "lib64" / "python3.12" / "site-packages" / "ext" / "fix.py")
    prefix_extra = _touch_py(venv_a / "share" / "hook.py")
    base_extra = _touch_py(base / "share" / "site_hook.py")
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    env_pkg = _touch_py(env_mount / "lib" / py_ver / "site-packages" / "pkg" / "fix.py")
    env_extra = _touch_py(env_mount / "sitecustomize.py")

    roots_a = fixture_source_roots(
        project,
        metadata_root=metadata,
        env_root=env_mount,
        paths={
            "purelib": str(venv_a / "lib" / "python3.12" / "site-packages"),
            "platlib": str(venv_a / "lib64" / "python3.12" / "site-packages"),
            "stdlib": str(base / "lib" / "python3.12"),
            "platstdlib": str(venv_a / "lib" / "python3.12"),
        },
        prefixes=(
            ("prefix", str(venv_a)),
            ("exec_prefix", str(venv_a)),
            ("base_prefix", str(base)),
            ("base_exec_prefix", str(base)),
        ),
    )
    roots_b = fixture_source_roots(
        project,
        metadata_root=metadata,
        env_root=None,
        paths={
            "purelib": str(venv_b / "lib" / "python3.12" / "site-packages"),
            "platlib": str(venv_b / "lib" / "python3.12" / "site-packages"),
            "stdlib": str(base / "lib" / "python3.12"),
            "platstdlib": str(venv_b / "lib" / "python3.12"),
        },
        prefixes=(
            ("prefix", str(venv_b)),
            ("exec_prefix", str(venv_b)),
            ("base_prefix", str(base)),
            ("base_exec_prefix", str(base)),
        ),
    )
    roots_local = fixture_source_roots(
        project,
        metadata_root=metadata,
        env_root=None,
        paths={
            "purelib": str(usr_local / "lib" / "python3.12" / "site-packages"),
            "platlib": str(usr_local / "lib" / "python3.12" / "site-packages"),
            "stdlib": str(usr_local / "lib" / "python3.12"),
            "platstdlib": str(usr_local / "lib" / "python3.12"),
        },
        prefixes=(
            ("prefix", str(usr_local)),
            ("exec_prefix", str(usr_local)),
            ("base_prefix", str(usr_local)),
            ("base_exec_prefix", str(usr_local)),
        ),
    )
    roots_in_proj = fixture_source_roots(
        project,
        metadata_root=metadata,
        env_root=None,
        paths={
            "purelib": str(project / ".venv" / "lib" / "python3.12" / "site-packages"),
            "platlib": str(project / ".venv" / "lib" / "python3.12" / "site-packages"),
            "stdlib": str(base / "lib" / "python3.12"),
            "platstdlib": str(project / ".venv" / "lib" / "python3.12"),
        },
        prefixes=(
            ("prefix", str(project / ".venv")),
            ("exec_prefix", str(project / ".venv")),
            ("base_prefix", str(base)),
            ("base_exec_prefix", str(base)),
        ),
    )

    assert normalize_fixture_source_path(project_file, roots_a) == "project:tests/conftest.py"
    assert normalize_fixture_source_path(project_file, roots_in_proj) == "project:tests/conftest.py"
    assert normalize_fixture_source_path(metadata_file, roots_a) == "metadata:demo_pkg/helper.py"
    assert normalize_fixture_source_path(stdlib_file, roots_a) == "stdlib:doctest.py"
    assert normalize_fixture_source_path(platstdlib_file, roots_a) == "platstdlib:_sysconfigdata.py"
    assert normalize_fixture_source_path(platlib_a, roots_a) == "platlib:ext/fix.py"
    assert normalize_fixture_source_path(prefix_extra, roots_a) == "prefix:share/hook.py"
    assert normalize_fixture_source_path(base_extra, roots_a) == "base_prefix:share/site_hook.py"
    assert normalize_fixture_source_path(env_extra, roots_a) == "env:sitecustomize.py"

    # Most-specific resolved directory root wins across venv-a, venv-b, /usr/local, /env, and /project/.venv.
    expected_purelib = "purelib:pkg/fix.py"
    assert normalize_fixture_source_path(purelib_a, roots_a) == expected_purelib
    assert normalize_fixture_source_path(purelib_b, roots_b) == expected_purelib
    assert normalize_fixture_source_path(purelib_local, roots_local) == expected_purelib
    assert normalize_fixture_source_path(purelib_in_proj, roots_in_proj) == expected_purelib
    assert normalize_fixture_source_path(env_pkg, roots_a) == expected_purelib


def test_normalize_fixture_source_path_resolves_symlinked_prefixes(tmp_path: Path) -> None:
    from pytest_telescope.worker import fixture_source_roots, normalize_fixture_source_path

    project = tmp_path / "project"
    project.mkdir()
    real_venv = tmp_path / "real-storage" / "venv"
    purelib_file = _touch_py(real_venv / "lib" / "python3.12" / "site-packages" / "plug" / "mod.py")
    symlink_prefix = tmp_path / "symlink-venv"
    symlink_prefix.symlink_to(real_venv, target_is_directory=True)

    roots = fixture_source_roots(
        project,
        metadata_root=tmp_path / "metadata",
        env_root=None,
        paths={
            "purelib": str(symlink_prefix / "lib" / "python3.12" / "site-packages"),
            "platlib": str(symlink_prefix / "lib" / "python3.12" / "site-packages"),
            "stdlib": str(real_venv / "stdlib"),
            "platstdlib": str(symlink_prefix / "lib" / "python3.12"),
        },
        prefixes=(
            ("prefix", str(symlink_prefix)),
            ("exec_prefix", str(symlink_prefix)),
            ("base_prefix", str(real_venv / "stdlib")),
            ("base_exec_prefix", str(real_venv / "stdlib")),
        ),
    )

    via_symlink = symlink_prefix / "lib" / "python3.12" / "site-packages" / "plug" / "mod.py"
    assert normalize_fixture_source_path(via_symlink, roots) == "purelib:plug/mod.py"
    assert normalize_fixture_source_path(purelib_file, roots) == "purelib:plug/mod.py"


def test_normalize_fixture_source_path_fails_closed_on_outside_and_invalid_paths(
    tmp_path: Path,
) -> None:
    from pytest_telescope.worker import fixture_source_roots, normalize_fixture_source_path

    project = tmp_path / "project"
    project.mkdir()
    venv = tmp_path / "venv"
    outside_py = _touch_py(tmp_path / "outside" / "rogue.py")
    non_py = project / "data.txt"
    non_py.write_text("hello\n", encoding="utf-8")

    # Even if '/' is supplied in prefixes, it must never act as a catch-all root.
    roots = fixture_source_roots(
        project,
        metadata_root=tmp_path / "metadata",
        env_root=None,
        paths={"purelib": str(venv / "site-packages")},
        prefixes=(("prefix", str(venv)), ("base_prefix", "/")),
    )
    with pytest.raises(ValueError, match="outside admitted roots"):
        normalize_fixture_source_path(outside_py, roots)
    with pytest.raises(ValueError, match="unsupported fixture source"):
        normalize_fixture_source_path(non_py, roots)
    with pytest.raises(ValueError, match="invalid fixture source path"):
        normalize_fixture_source_path("relative/mod.py", roots)
    with pytest.raises(ValueError, match="missing project root"):
        fixture_source_roots(None)
    with pytest.raises(ValueError, match="invalid project root"):
        fixture_source_roots("/")
