"""Synthetic Docker proof of metadata visibility and independent pytest roots.

This is an investigation control, not the production metadata compiler or scheduler.
"""

import argparse
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from pytest_telescope import runtime


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True)
    requirements = root / "requirements.txt"
    requirements.write_text("pytest-asyncio==1.3.0\ntyping_extensions==4.15.0\n")
    prepared = runtime.build_runtime(root, requirements)
    outcomes = []
    try:
        for package in ("core", "addon"):
            declaration = (
                '[build-system]\nrequires = ["setuptools==80.9.0"]\n'
                'build-backend = "setuptools.build_meta"\n'
                f'[project]\nname = "example-{package}"\nversion = "1.0.0"\n'
                f'[project.entry-points."example.plugins"]\n{package} = "{package}:value"\n'
            )
            write(root, f"declarations/{package}/pyproject.toml", declaration)
            project = tomllib.loads(declaration)["project"]
            directory = f"metadata/{package}/example_{package}-1.0.0.dist-info"
            write(
                root,
                directory + "/METADATA",
                f"Metadata-Version: 2.4\nName: {project['name']}\nVersion: {project['version']}\n",
            )
            write(
                root,
                directory + "/entry_points.txt",
                "[example.plugins]\n"
                + f"{package} = {project['entry-points']['example.plugins'][package]}\n",
            )
            write(root, directory + "/INSTALLER", "pytest-telescope-static-v1\n")
            for scenario in ("complete", "metadata-only", "source-only"):
                snapshot = root / f"snapshots/{package}-{scenario}"
                snapshot.mkdir(parents=True)
                if scenario != "metadata-only":
                    write(snapshot, f"packages/{package}/{package}.py", f"value = '{package}'\n")
                suite = f"tests/{package}"
                write(
                    snapshot,
                    suite + "/pytest.ini",
                    f"[pytest]\npythonpath = ../../packages/{package}\nasyncio_mode = strict\nasyncio_default_fixture_loop_scope = function\n",
                )
                write(
                    snapshot,
                    suite + "/conftest.py",
                    f"import os\nassert os.getcwd() == '/project/tests/{package}'\nimport pytest\n@pytest.fixture\ndef context(): return '{package}'\n",
                )
                checks = (
                    f"    assert metadata.version('example-{package}') == '1.0.0'\n"
                    f"    point = next(iter(metadata.entry_points(group='example.plugins', name='{package}')))\n"
                )
                if scenario == "complete":
                    checks += f"    assert point.load() == '{package}'\n"
                elif scenario == "metadata-only":
                    checks += "    with pytest.raises(ModuleNotFoundError): point.load()\n"
                else:
                    checks = f"    with pytest.raises(metadata.PackageNotFoundError): metadata.version('example-{package}')\n    assert not metadata.entry_points(group='example.plugins', name='{package}')\n"
                other = "addon" if package == "core" else "core"
                checks += f"    with pytest.raises(metadata.PackageNotFoundError): metadata.version('example-{other}')\n"
                write(
                    snapshot,
                    suite + "/test_selected.py",
                    "from importlib import metadata\nimport pytest\nimport pytest_asyncio\n@pytest_asyncio.fixture\nasync def asynchronous(): return 7\n@pytest.mark.asyncio\nasync def test_context(context, asynchronous):\n"
                    + f"    assert context == '{package}' and asynchronous == 7\n"
                    + checks,
                )
                write(
                    snapshot,
                    suite + "/test_excluded.py",
                    "raise RuntimeError('structurally excluded synthetic module')\n",
                )
                metadata_path = root / (
                    f"metadata/{package}" if scenario != "source-only" else "metadata/empty"
                )
                metadata_path.mkdir(parents=True, exist_ok=True)
                for tree in (snapshot, metadata_path):
                    tree.chmod(0o755)
                    for path in tree.rglob("*"):
                        path.chmod(0o755 if path.is_dir() else 0o444)
                token = f"pytest-telescope-proof-{os.getpid()}-{package}-{scenario}"
                command = [
                    "docker",
                    "run",
                    "--name",
                    token,
                    "--rm",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--user=65534:65534",
                    "--cpus=1",
                    "--memory=512m",
                    "--memory-swap=512m",
                    "--pids-limit=128",
                    "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                    "--workdir=/project/" + suite,
                    "--mount",
                    f"type=bind,src={snapshot},dst=/project,readonly",
                    "--mount",
                    f"type=bind,src={metadata_path},dst=/metadata,readonly",
                    "--entrypoint=python",
                    prepared["image"],
                    "-I",
                    "-c",
                    "import sys; sys.path.insert(0, '/metadata'); import pytest; sys.exit(pytest.main(['-p', 'pytest_asyncio.plugin', '-p', 'no:cacheprovider', '-q', '-c', 'pytest.ini', '--rootdir=.', 'test_selected.py']))",
                ]
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    (root / f"{package}-{scenario}.log").write_text(
                        completed.stdout + completed.stderr
                    )
                    assert completed.returncode == 0, (
                        f"failed {package}-{scenario}: inspect proof log"
                    )
                    outcomes.append({"package": package, "scenario": scenario, "passed": True})
                finally:
                    subprocess.run(["docker", "rm", "-f", token], capture_output=True, timeout=30)
        print(json.dumps({"scenarios": outcomes}, indent=2))
    finally:
        runtime.command(["docker", "image", "rm", prepared["image"]])
        # Evidence keeps its fixture and logs; only owned invocation scratch is removed.
        shutil.rmtree(root / ".pytest-telescope", ignore_errors=True)


if __name__ == "__main__":
    main()
