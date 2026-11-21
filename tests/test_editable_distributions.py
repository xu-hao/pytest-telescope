"""Behavioral controls for isolated editable-v1 project distributions."""

import json
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pytest_telescope import controller, project_metadata, result_store, sandbox
from pytest_telescope.model import DeclarationError, UnsupportedCapability, expand


class MemoryObjects:
    """In-memory create-if-absent store for remote result-store parity checks."""

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


def package_pyproject(name: str, version: str = "1.0.0", *, extra: str = "") -> str:
    return (
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        'description = "Isolated editable package"\n'
        'readme = "README.md"\n'
        f"{extra}\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{name.replace("-", "_")}"]\n'
    )


def make_workspace(root: Path) -> Path:
    put(
        root,
        "pyproject.toml",
        """[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.distributions.alpha]
format = "editable-v1"
metadata = "packages/alpha/pyproject.toml"
build_inputs = ["packages/alpha/README.md", "packages/alpha/src/alpha_pkg/__init__.py"]

[tool.pytest_telescope.distributions.beta]
format = "editable-v1"
metadata = "packages/beta/pyproject.toml"
build_inputs = ["packages/beta/README.md", "packages/beta/src/beta_pkg/__init__.py"]

[tool.pytest_telescope.distributions.static_meta]
format = "pep621-static-v1"
metadata = "packages/static_meta/pyproject.toml"
""",
    )
    put(
        root,
        "packages/alpha/pyproject.toml",
        package_pyproject(
            "alpha-pkg",
            "1.2.0",
            extra=(
                '[project.scripts]\nalpha-cli = "alpha_pkg.cli:main"\n\n'
                '[project.entry-points."telescope.demo"]\nalpha = "alpha_pkg.cli:PLUGIN_TAG"\n'
            ),
        ),
    )
    put(root, "packages/alpha/README.md", "# Alpha\n")
    put(root, "packages/alpha/src/alpha_pkg/__init__.py", "VERSION_VALUE = 12\n")
    put(
        root,
        "packages/alpha/src/alpha_pkg/cli.py",
        "import alpha_pkg\n\nPLUGIN_TAG = 'alpha-ready'\n\n"
        "def main():\n    print('alpha', alpha_pkg.VERSION_VALUE)\n\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    put(root, "packages/alpha/src/alpha_pkg/secret.py", "SECRET = 'undeclared'\n")

    put(
        root,
        "packages/beta/pyproject.toml",
        package_pyproject(
            "beta-pkg",
            "2.0.0",
            extra='[project.entry-points."telescope.demo"]\nbeta = "beta_pkg.mod:TAG"\n',
        ),
    )
    put(root, "packages/beta/README.md", "# Beta\n")
    put(root, "packages/beta/src/beta_pkg/__init__.py", "BETA = True\n")
    put(root, "packages/beta/src/beta_pkg/mod.py", "TAG = 'beta'\n")

    put(
        root,
        "packages/static_meta/pyproject.toml",
        '[project]\nname = "static-addon"\nversion = "3.1.0"\n'
        '[project.entry-points."telescope.demo"]\nstatic = "alpha_pkg.cli:PLUGIN_TAG"\n',
    )
    return root


def test_editable_metadata_validation_and_build_input_digests(tmp_path):
    root = make_workspace(tmp_path)
    entries = {
        "alpha": {
            "format": "editable-v1",
            "metadata": "packages/alpha/pyproject.toml",
            "build_inputs": ["packages/alpha/README.md"],
        },
        "static_meta": {
            "format": "pep621-static-v1",
            "metadata": "packages/static_meta/pyproject.toml",
        },
    }
    loaded = project_metadata.load_distributions(
        root, entries, lambda names, label: expand(root, names, label)
    )
    assert loaded["alpha"].format == "editable-v1"
    assert loaded["alpha"].name == "alpha-pkg"
    assert loaded["alpha"].directory == "packages/alpha"
    assert loaded["static_meta"].format == "pep621-static-v1"

    before_records = project_metadata.metadata_records(loaded, ["alpha"])
    assert {row["path"] for row in before_records} == {
        "packages/alpha/README.md",
        "packages/alpha/pyproject.toml",
    }
    put(root, "packages/alpha/README.md", "# Alpha updated\n")
    reloaded = project_metadata.load_distributions(
        root, entries, lambda names, label: expand(root, names, label)
    )
    assert project_metadata.metadata_records(reloaded, ["alpha"]) != before_records

    # backend-path, missing static version, dynamic version, non-pyproject, and escaping paths refuse.
    put(
        root,
        "packages/bad_backend/pyproject.toml",
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        'backend-path = ["."]\n[project]\nname = "bad-pkg"\nversion = "1.0.0"\n',
    )
    with pytest.raises(UnsupportedCapability) as backend_err:
        project_metadata.load_distributions(
            root,
            {
                "bad": {
                    "format": "editable-v1",
                    "metadata": "packages/bad_backend/pyproject.toml",
                }
            },
            lambda names, label: expand(root, names, label),
        )
    assert backend_err.value.capability == "metadata:editable-v1"

    put(
        root,
        "packages/dyn/pyproject.toml",
        '[project]\nname = "dyn-pkg"\ndynamic = ["version"]\n',
    )
    with pytest.raises(UnsupportedCapability) as dyn_err:
        project_metadata.load_distributions(
            root,
            {"dyn": {"format": "editable-v1", "metadata": "packages/dyn/pyproject.toml"}},
            lambda names, label: expand(root, names, label),
        )
    assert dyn_err.value.capability == "metadata:editable-v1"

    with pytest.raises(DeclarationError):
        project_metadata.load_distributions(
            root,
            {
                "escape": {
                    "format": "editable-v1",
                    "metadata": "packages/alpha/pyproject.toml",
                    "build_inputs": ["../outside.txt"],
                }
            },
            lambda names, label: expand(root, names, label),
        )


def test_editable_v1_isolates_metadata_entrypoints_and_isolated_child_imports(tmp_path):
    root = make_workspace(tmp_path)
    base_probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json, re; from importlib import metadata; "
            "print(json.dumps(sorted({re.sub(r'[-_.]+', '-', d.metadata['Name']).lower() "
            "for d in metadata.distributions()})))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    expected_base = sorted(json.loads(base_probe.stdout))

    put(
        root,
        "tests/test_alpha.py",
        f"""import errno
import importlib.metadata as md
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BASE = set({expected_base!r})

pytestmark = pytest.mark.telescope(
    distributions=["alpha", "static_meta"],
    inputs=["packages/alpha/src/alpha_pkg/cli.py"],
)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def test_environment_and_metadata_isolation():
    assert sys.executable == "/env/bin/python"
    assert sys.prefix == "/env"
    assert os.environ["PATH"].split(":")[0] == "/env/bin"
    with pytest.raises(OSError) as ro_err:
        Path("/env/illegal.txt").write_text("nope")
    assert ro_err.value.errno in (errno.EROFS, errno.EACCES)

    visible = {{normalize(d.metadata["Name"]) for d in md.distributions()}}
    assert visible == BASE | {{"alpha-pkg", "static-addon"}}
    assert md.version("alpha-pkg") == "1.2.0"
    assert md.version("static-addon") == "3.1.0"
    with pytest.raises(md.PackageNotFoundError):
        md.version("beta-pkg")

    demo_eps = md.entry_points(group="telescope.demo")
    assert {{ep.name for ep in demo_eps}} == {{"alpha", "static"}}
    assert demo_eps["alpha"].load() == "alpha-ready"


def test_isolated_child_imports_only_declared_modules_and_distributions():
    ok = subprocess.run(
        [sys.executable, "-I", "-m", "alpha_pkg.cli"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout == "alpha 12\\n"

    script = subprocess.run(["alpha-cli"], capture_output=True, text=True, timeout=30)
    assert script.returncode == 0 and script.stdout == "alpha 12\\n", script.stderr

    loc = subprocess.run(
        [sys.executable, "-I", "-c", "import alpha_pkg.cli as c; print(c.__file__)"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert loc.returncode == 0, loc.stderr
    assert loc.stdout.strip() == "/project/packages/alpha/src/alpha_pkg/cli.py"

    missing_mod = subprocess.run(
        [sys.executable, "-I", "-m", "alpha_pkg.secret"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing_mod.returncode != 0
    assert "No module named alpha_pkg.secret" in missing_mod.stderr

    missing_mod_import = subprocess.run(
        [sys.executable, "-I", "-c", "import alpha_pkg.secret"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing_mod_import.returncode != 0
    assert "ModuleNotFoundError" in missing_mod_import.stderr

    missing_pkg = subprocess.run(
        [sys.executable, "-I", "-m", "beta_pkg.mod"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing_pkg.returncode != 0
    assert "ModuleNotFoundError" in missing_pkg.stderr

    child_meta = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.metadata as m; "
            "assert m.version('alpha-pkg') == '1.2.0'; "
            "m.version('beta-pkg')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child_meta.returncode != 0
    assert "PackageNotFoundError" in child_meta.stderr
""",
    )
    report = controller.run(root)
    assert report["status"] == "passed", report
    assert report["counts"]["executed"] == 2


def test_editing_unselected_sibling_pyproject_preserves_full_cache_reuse(tmp_path):
    root = make_workspace(tmp_path)
    put(
        root,
        "tests/test_alpha_only.py",
        """import subprocess
import sys
import pytest

pytestmark = pytest.mark.telescope(
    distributions=["alpha"],
    inputs=["packages/alpha/src/alpha_pkg/cli.py"],
)


def test_alpha_runs():
    done = subprocess.run(
        [sys.executable, "-I", "-m", "alpha_pkg.cli"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0 and done.stdout == "alpha 12\\n"
""",
    )
    first = controller.run(root)
    assert first["status"] == "passed" and first["counts"]["executed"] == 1

    # Mutate unselected sibling beta's pyproject.toml and README.md: alpha reuses 100%
    # of both collection and execution cache records without starting a worker.
    put(root, "packages/beta/pyproject.toml", package_pyproject("beta-pkg", "9.9.9"))
    put(root, "packages/beta/README.md", "# Beta mutated\n")

    second = controller.run(root)
    assert second["status"] == "passed", second
    assert second["counts"]["reused"] == 1 and second["counts"]["executed"] == 0
    assert second["collection"]["outcome"] == "reused"

    # Mutating alpha's declared build_inputs invalidates and re-executes alpha.
    put(root, "packages/alpha/README.md", "# Alpha changed build input\n")
    third = controller.run(root)
    assert third["status"] == "passed"
    assert third["counts"]["executed"] == 1 and third["counts"]["reused"] == 0


@pytest.mark.requires_fixture_guard
@pytest.mark.parametrize(
    "mode,guard",
    [
        ("case", None),
        ("batch", None),
        ("batch", "available-v1"),
    ],
)
def test_editable_v1_across_case_batch_and_guarded_batch_modes_reuses_cached_env(
    tmp_path, monkeypatch, mode, guard
):
    store = MemoryObjects()
    monkeypatch.setitem(result_store.BACKENDS, "gs", store.open)
    monkeypatch.setattr(result_store, "RETRY_DELAYS", (0, 0))

    root = make_workspace(tmp_path / "checkout-one")
    put(root, "packages/alpha/src/alpha_pkg/second.py", "SECOND = 99\n")
    put(
        root,
        "tests/test_modes.py",
        """import subprocess
import sys
import pytest

pytestmark = pytest.mark.telescope(distributions=["alpha"])


@pytest.mark.telescope(inputs=["packages/alpha/src/alpha_pkg/cli.py"])
def test_group_one_a():
    res = subprocess.run([sys.executable, "-I", "-m", "alpha_pkg.cli"], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "alpha 12\\n"


@pytest.mark.telescope(inputs=["packages/alpha/src/alpha_pkg/cli.py"])
def test_group_one_b():
    res = subprocess.run([sys.executable, "-I", "-m", "alpha_pkg.cli"], capture_output=True, text=True)
    assert res.returncode == 0


@pytest.mark.telescope(inputs=["packages/alpha/src/alpha_pkg/second.py"])
def test_group_two():
    res = subprocess.run(
        [sys.executable, "-I", "-c", "import alpha_pkg.second as s; print(s.SECOND)"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0 and res.stdout == "99\\n"
""",
    )
    cache_dir = tmp_path / "cache"
    builds = []
    real_capture = sandbox.capture

    def counting_capture(argv, *args, **kwargs):
        if "-m" in argv and "venv" in argv and "/env" in argv:
            builds.append(list(argv))
        return real_capture(argv, *args, **kwargs)

    monkeypatch.setattr(sandbox, "capture", counting_capture)

    first = controller.run(
        root,
        cache_dir=cache_dir,
        execution_mode=mode,
        fixture_guard=guard,
        workers=2,
        result_store="gs://editable-bucket/prefix",
    )
    assert first["status"] == "passed", first
    assert first["counts"]["executed"] == 3
    if guard is not None:
        assert first["fixture_guard"]["state"] == "admitted"
    # Multiple groups sharing distributions=["alpha"] build /env once and reuse it.
    assert len(builds) == 1
    env_dirs = [
        entry
        for entry in (cache_dir / "environments").iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    ]
    assert len(env_dirs) == 1
    assert stat.S_IMODE(env_dirs[0].stat().st_mode) == 0o555

    # Local cache hit starts zero builds and zero execution sandboxes.
    second = controller.run(
        root,
        cache_dir=cache_dir,
        execution_mode=mode,
        fixture_guard=guard,
        workers=2,
    )
    assert second["status"] == "passed"
    assert second["counts"]["reused"] == 3 and second["counts"]["executed"] == 0
    assert len(builds) == 1

    # Fresh checkout at another location reuses from remote result store.
    other = make_workspace(tmp_path / "checkout-two")
    put(other, "packages/alpha/src/alpha_pkg/second.py", "SECOND = 99\n")
    put(other, "tests/test_modes.py", (root / "tests/test_modes.py").read_text(encoding="utf-8"))
    remote_hit = controller.run(
        other,
        execution_mode=mode,
        fixture_guard=guard,
        workers=2,
        result_store="gs://editable-bucket/prefix",
    )
    assert remote_hit["status"] == "passed", remote_hit
    assert remote_hit["counts"]["reused"] == 3 and remote_hit["counts"]["executed"] == 0
    expected_groups = 3 if mode == "case" else 2
    assert remote_hit["result_store"]["reused_groups"] == {"local": 0, "remote": expected_groups}


def test_editable_v1_runtime_conflict_with_base_environment_refuses(tmp_path):
    root = tmp_path / "project"
    put(
        root,
        "pyproject.toml",
        """[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.distributions.collide]
format = "editable-v1"
metadata = "packages/collide/pyproject.toml"
build_inputs = ["packages/collide/README.md"]
""",
    )
    put(root, "packages/collide/pyproject.toml", package_pyproject("pytest", "9.9.0"))
    put(root, "packages/collide/README.md", "# Collides with base pytest\n")
    put(
        root,
        "tests/test_collide.py",
        """import pytest

pytestmark = pytest.mark.telescope(distributions=["collide"])


def test_never_runs():
    assert False
""",
    )
    with pytest.raises(UnsupportedCapability) as caught:
        controller.run(root)
    assert caught.value.capability == "project-metadata:runtime-conflict"

    completed = subprocess.run(
        [sys.executable, "-m", "pytest_telescope", "run"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 4, report
    assert report["kind"] == "refusal" and report["status"] == "unsupported"
    assert report["capability"] == "project-metadata:runtime-conflict"
