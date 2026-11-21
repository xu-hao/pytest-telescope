"""Behavioral controls for unpacked container rootfs views and `/` cache-key parity."""

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path

import pytest

from pytest_telescope import controller, result_store
from pytest_telescope.model import DeclarationError, UnsupportedCapability


class MemoryObjects:
    """In-memory create-if-absent remote result store."""

    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()

    def open(self, bucket):
        return self

    def read(self, name, limit):
        with self.lock:
            data = self.objects.get(name)
        return None if data is None else data[: limit + 1]

    def create(self, name, data):
        with self.lock:
            if name in self.objects:
                return False
            self.objects[name] = data
            return True


def put(root: Path, relative_path: str, content: str) -> Path:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _copy_with_links(source: Path, rootfs: Path, *, target_rel: str | None = None) -> None:
    """Copy a host file/symlink chain or directory into `rootfs` preserving relative links."""
    current = Path(source)
    seen = set()
    while current not in seen:
        seen.add(current)
        rel = Path(target_rel) if target_rel is not None else current.relative_to("/")
        target_rel = None
        dest = rootfs / rel
        if dest.exists() or dest.is_symlink():
            if not current.is_symlink():
                return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if current.is_symlink():
            link_text = os.readlink(current)
            if not dest.is_symlink() and not dest.exists():
                dest.symlink_to(link_text)
            resolved_step = Path(
                os.path.normpath(
                    link_text
                    if os.path.isabs(link_text)
                    else os.path.join(current.parent, link_text)
                )
            )
            current = resolved_step
            continue
        if current.is_dir():
            shutil.copytree(current, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(current, dest)
        return


def _shared_libraries(binaries: list[Path]) -> set[Path]:
    found = set()
    for chunk_start in range(0, len(binaries), 64):
        batch = [str(item) for item in binaries[chunk_start : chunk_start + 64]]
        completed = subprocess.run(
            ["ldd", *batch],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in completed.stdout.splitlines():
            for token in line.replace("(", " ").replace(")", " ").split():
                if token.startswith("/") and os.path.exists(token):
                    found.add(Path(token))
    return found


def _seed_venv_distributions(venv_python: Path) -> None:
    """Copy installed pytest runtime distributions into an owned isolated venv."""
    target_purelib = Path(
        subprocess.run(
            [
                str(venv_python),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    )
    host_purelib = Path(sysconfig.get_path("purelib"))
    wanted_prefixes = (
        "_pytest",
        "pytest",
        "py.py",
        "pluggy",
        "iniconfig",
        "packaging",
        "pygments",
    )
    for entry in host_purelib.iterdir():
        name = entry.name
        lower = name.lower()
        if any(
            lower == prefix or lower.startswith(prefix + "-") or lower.startswith(prefix + ".")
            for prefix in wanted_prefixes
        ):
            dest = target_purelib / name
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(
                    entry,
                    dest,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            elif entry.is_file():
                shutil.copy2(entry, dest)
    subprocess.run(
        [str(venv_python), "-I", "-B", "-c", "import pytest, packaging"],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture(scope="module")
def unpacked_rootfs_fixture(tmp_path_factory):
    """Construct an owned unpacked rootfs with /usr, /etc, /bin, /lib* and /opt/venv."""
    base = tmp_path_factory.mktemp("owned-rootfs")
    rootfs = base / "rootfs"
    (rootfs / "usr").mkdir(parents=True)
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc/telescope-rootfs-marker").write_text("owned-unpacked-rootfs\n", encoding="utf-8")
    if Path("/etc/ld.so.cache").is_file():
        shutil.copy2("/etc/ld.so.cache", rootfs / "etc/ld.so.cache")

    for link in ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
        host_link = Path(link)
        if host_link.is_symlink():
            target = os.readlink(host_link)
            (rootfs / link.lstrip("/")).symlink_to(target)
            target_dir = Path(os.path.normpath(os.path.join("/", target)))
            (rootfs / target_dir.relative_to("/")).mkdir(parents=True, exist_ok=True)
        elif host_link.is_dir():
            (rootfs / link.lstrip("/")).mkdir(parents=True, exist_ok=True)

    base_python = Path(sys._base_executable).resolve()
    stdlib = Path(
        sysconfig.get_path(
            "stdlib", vars={"installed_base": sys.base_prefix, "base": sys.base_prefix}
        )
    )
    _copy_with_links(Path(sys._base_executable), rootfs)
    _copy_with_links(base_python, rootfs)
    shutil.copytree(
        stdlib,
        rootfs / stdlib.relative_to("/"),
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "test", "idlelib", "tkinter", "turtle*"
        ),
    )

    so_files = [base_python, *sorted((rootfs / stdlib.relative_to("/")).rglob("*.so"))]
    for lib in _shared_libraries(so_files):
        _copy_with_links(lib, rootfs)
        _copy_with_links(lib.resolve(), rootfs)

    # Create a real virtual environment on host and transplant it to /opt/venv inside rootfs.
    staging_venv = base / "staging-venv"
    subprocess.run(
        [str(base_python), "-m", "venv", "--without-pip", str(staging_venv)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    _seed_venv_distributions(staging_venv / "bin/python")
    opt_venv = rootfs / "opt/venv"
    shutil.copytree(staging_venv, opt_venv, symlinks=True)

    # Rewrite pyvenv.cfg inside /opt/venv to canonical container paths.
    cfg = opt_venv / "pyvenv.cfg"
    lines = []
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, sep, val = line.partition("=")
        if sep and key.strip().lower() == "command":
            lines.append(f"command = {base_python} -m venv --without-pip /opt/venv")
        else:
            lines.append(line)
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # External data directory inside rootfs only (not present on host /) for frozen-tool checks.
    (rootfs / "opt/container-tool").mkdir(parents=True)
    (rootfs / "opt/container-tool/version.txt").write_text(
        "tool-from-rootfs-1.0\n", encoding="utf-8"
    )
    (rootfs / "opt/container-tool/version.txt").chmod(0o444)
    (rootfs / "opt/container-tool").chmod(0o555)

    return rootfs, base_python


def test_unpacked_rootfs_executes_via_api_cli_and_pyproject_and_resolves_frozen_tools(
    tmp_path, unpacked_rootfs_fixture
):
    rootfs, _ = unpacked_rootfs_fixture
    assert not Path("/opt/container-tool/version.txt").exists()

    project = tmp_path / "project"
    payload = (rootfs / "opt/container-tool/version.txt").read_bytes()
    import hashlib

    tool_manifest = {
        "schema": 1,
        "roots": [
            {
                "id": "bundle",
                "source": "/opt/container-tool",
                "coverage": "tree",
                "preserve_source": True,
                "entries": [
                    {"path": ".", "kind": "directory", "mode": 0o555},
                    {
                        "path": "version.txt",
                        "kind": "file",
                        "mode": 0o444,
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    },
                ],
            }
        ],
        "executables": [],
        "python": [],
        "links": [],
        "locks": [],
        "commands": [],
        "loader": None,
    }
    put(project, "tool-locks/container.json", json.dumps(tool_manifest))
    put(
        project,
        "pyproject.toml",
        """[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.tools.container_data]
format = "frozen-tool-v1"
manifest = "tool-locks/container.json"
""",
    )
    put(
        project,
        "tests/test_rootfs_view.py",
        """import sys
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(tools=["container_data"])


def test_inside_unpacked_rootfs():
    assert sys.executable == "/opt/venv/bin/python"
    assert sys.prefix == "/opt/venv"
    assert Path("/etc/telescope-rootfs-marker").read_text() == "owned-unpacked-rootfs\\n"
    assert Path("/opt/container-tool/version.txt").read_text() == "tool-from-rootfs-1.0\\n"
    assert Path("/tools/container_data/bundle/version.txt").read_text() == "tool-from-rootfs-1.0\\n"
""",
    )

    api_report = controller.run(
        project,
        rootfs=rootfs,
        rootfs_python="/opt/venv/bin/python",
    )
    assert api_report["status"] == "passed", api_report
    assert api_report["counts"]["executed"] == 1

    # CLI flags reuse the exact cached result produced by the API call.
    cli_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest_telescope",
            "run",
            "--rootfs",
            str(rootfs),
            "--rootfs-python",
            "/opt/venv/bin/python",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    cli_report = json.loads(cli_completed.stdout)
    assert cli_completed.returncode == 0, cli_report
    assert cli_report["counts"]["reused"] == 1 and cli_report["counts"]["executed"] == 0

    # Configuration table in pyproject.toml also produces the exact same sandbox identity.
    put(
        project,
        "pyproject.toml",
        f"""[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.execution]
rootfs = {json.dumps(str(rootfs))}
rootfs_python = "/opt/venv/bin/python"

[tool.pytest_telescope.tools.container_data]
format = "frozen-tool-v1"
manifest = "tool-locks/container.json"
""",
    )
    cfg_report = controller.run(project)
    assert cfg_report["status"] == "passed"
    assert cfg_report["sandbox"]["identity"] == api_report["sandbox"]["identity"]
    assert cfg_report["counts"]["reused"] == 1


def test_unpacked_rootfs_and_slash_view_share_identical_identity_and_cache_records(
    tmp_path, monkeypatch, unpacked_rootfs_fixture
):
    rootfs, base_python = unpacked_rootfs_fixture
    store = MemoryObjects()
    monkeypatch.setitem(result_store.BACKENDS, "gs", store.open)
    monkeypatch.setattr(result_store, "RETRY_DELAYS", (0, 0))

    # Create an equivalent venv on `/` (under /var/tmp so it does not live under /tmp)
    # and mirror that exact interpreter path inside `rootfs` as well.
    slash_stage = Path(tempfile.mkdtemp(prefix="telescope-slash-parity-", dir="/var/tmp"))
    try:
        slash_venv = slash_stage / "opt/venv"
        subprocess.run(
            [str(base_python), "-m", "venv", "--without-pip", str(slash_venv)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        _seed_venv_distributions(slash_venv / "bin/python")
        mirrored_venv = rootfs / slash_venv.relative_to("/")
        shutil.copytree(slash_venv, mirrored_venv, symlinks=True)
        slash_interp = str(slash_venv / "bin/python")

        def write_checkout(checkout: Path) -> Path:
            put(checkout, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
            put(checkout, "app/value.py", "ANSWER = 42\n")
            put(
                checkout,
                "tests/test_parity.py",
                """from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=["app/value.py"])


def test_answer():
    assert Path("app/value.py").read_text() == "ANSWER = 42\\n"
""",
            )
            return checkout

        container_checkout = write_checkout(tmp_path / "container-checkout")
        host_checkout = write_checkout(tmp_path / "host-checkout")
        shared_local_cache = tmp_path / "shared-local-cache"

        # 1. Run against `/` (`rootfs=Path("/")`) and upload to remote result store + shared local cache.
        slash_report = controller.run(
            container_checkout,
            cache_dir=shared_local_cache,
            rootfs=Path("/"),
            rootfs_python=slash_interp,
            result_store="gs://parity-bucket/results",
        )
        assert slash_report["status"] == "passed", slash_report
        assert slash_report["counts"]["executed"] == 1
        assert slash_report["result_store"]["uploads"]["created"] == 1

        # Remove the live venv from `/` before running the unpacked rootfs view to prove
        # the unpacked view never touches `/` for the interpreter or its site-packages.
        shutil.rmtree(slash_stage)

        # 2. Local cache hit parity using `rootfs=unpacked` with both `slash_interp` and `/opt/venv/bin/python`.
        local_hit = controller.run(
            host_checkout,
            cache_dir=shared_local_cache,
            rootfs=rootfs,
            rootfs_python=slash_interp,
        )
        assert local_hit["status"] == "passed", local_hit
        assert local_hit["sandbox"]["identity"] == slash_report["sandbox"]["identity"]
        assert local_hit["counts"]["reused"] == 1 and local_hit["counts"]["executed"] == 0

        # 3. Remote result-store hit parity from a cold local cache with `/opt/venv/bin/python`!
        cold_host_checkout = write_checkout(tmp_path / "cold-host-checkout")
        remote_hit = controller.run(
            cold_host_checkout,
            rootfs=rootfs,
            rootfs_python="/opt/venv/bin/python",
            result_store="gs://parity-bucket/results",
        )
        assert remote_hit["status"] == "passed", remote_hit
        assert remote_hit["sandbox"]["identity"] == slash_report["sandbox"]["identity"]
        assert remote_hit["counts"]["reused"] == 1 and remote_hit["counts"]["executed"] == 0
        assert remote_hit["result_store"]["reused_groups"] == {"local": 0, "remote": 1}
        assert remote_hit["result_store"]["lookups"]["hits"] == 1
    finally:
        shutil.rmtree(slash_stage, ignore_errors=True)
        shutil.rmtree(rootfs / "var/tmp", ignore_errors=True)


def test_invalid_or_missing_rootfs_and_rootfs_python_refuse(tmp_path, unpacked_rootfs_fixture):
    rootfs, _ = unpacked_rootfs_fixture
    project = tmp_path / "project"
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(
        project,
        "tests/test_scoped.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_ok(): pass\n",
    )
    regular_file = tmp_path / "not-a-dir"
    regular_file.write_text("file\n", encoding="utf-8")

    # DeclarationError cases: missing directory, non-directory, missing companion, relative/bad path.
    for kwargs in (
        {"rootfs": tmp_path / "does-not-exist", "rootfs_python": "/opt/venv/bin/python"},
        {"rootfs": regular_file, "rootfs_python": "/opt/venv/bin/python"},
        {"rootfs": "", "rootfs_python": "/opt/venv/bin/python"},
        {"rootfs": rootfs, "rootfs_python": None},
        {"rootfs": None, "rootfs_python": "/opt/venv/bin/python"},
        {"rootfs": rootfs, "rootfs_python": "opt/venv/bin/python"},
        {"rootfs": rootfs, "rootfs_python": "/opt/../venv/bin/python"},
        {"rootfs": rootfs, "rootfs_python": "/"},
    ):
        with pytest.raises(DeclarationError):
            controller.run(project, **kwargs)

    # UnsupportedCapability("sandbox:environment") cases: missing interpreter in rootfs,
    # reserved mount-point overlaps, and incomplete unpacked rootfs missing /usr or /etc.
    for bad_python in (
        "/opt/venv/bin/does-not-exist",
        "/project/venv/bin/python",
        "/env/bin/python",
        "/metadata/bin/python",
        "/tools/venv/bin/python",
        "/telescope/bin/python",
        "/tmp/venv/bin/python",
        "/proc/bin/python",
        "/dev/bin/python",
    ):
        with pytest.raises(UnsupportedCapability) as refused:
            controller.run(project, rootfs=rootfs, rootfs_python=bad_python)
        assert refused.value.capability == "sandbox:environment"

    incomplete = tmp_path / "incomplete-rootfs"
    (incomplete / "usr").mkdir(parents=True)
    with pytest.raises(UnsupportedCapability) as missing_etc:
        controller.run(project, rootfs=incomplete, rootfs_python="/usr/bin/python3")
    assert missing_etc.value.capability == "sandbox:environment"

    # CLI refusal exit code 4 check.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest_telescope",
            "run",
            "--rootfs",
            str(tmp_path / "missing-rootfs"),
            "--rootfs-python",
            "/opt/venv/bin/python",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 4, report
    assert report["kind"] == "refusal"
