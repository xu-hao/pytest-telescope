"""Bounded synthetic mechanism proof; not the production source projection."""

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from pytest_telescope import runtime
from pytest_telescope.model import UnsupportedCapability

SLOT = "/usr/local/lib/python3.12/site-packages/zz_telescope_source_view.pth"
LABEL = "org.pytest-telescope.source-proof"


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o444)
    os.utime(path, ns=(0, 0))
    return path


def validate_view(root, roots):
    for name in roots:
        directory = root / name
        if not directory.exists():
            continue
        for item in directory.iterdir():
            if item.name.split(".")[0] in {"sitecustomize", "usercustomize"} or item.name.endswith(
                (".dist-info", ".egg-info", ".egg-link")
            ):
                raise UnsupportedCapability("python:source-layout", "unsupported startup namespace")


CHECK = r"""
import errno, importlib.metadata as md, json, os, pathlib, shutil, socket, subprocess, sys
scenario = sys.argv[1]
pathlib.Path('/tmp/hostile').mkdir()
pathlib.Path('/tmp/hostile/hostile_path.py').write_text('value=1')
user_site = pathlib.Path('/tmp/user-site/lib/python3.12/site-packages')
user_site.mkdir(parents=True)
(user_site / 'hostile_user.py').write_text('value=1')
expected_source = scenario not in ('metadata-only',)
expected_value = 'addon' if scenario == 'suite-addon' else 'core'
expected_metadata = scenario not in ('baseline', 'source-only')
projected = scenario != 'baseline'
if scenario == 'runtime-conflict':
    selected = {d.metadata['Name'] for d in md.distributions(path=['/metadata'])}
    runtime_paths = [p for p in sys.path if p != '/metadata' and not p.startswith('/project/')]
    installed = {d.metadata['Name'] for d in md.distributions(path=runtime_paths)}
    assert selected & installed == {'pytest'}
    print(json.dumps({'scenario':scenario, 'runtime_collision_detected':True}))
    sys.exit(0)
def child(code, flags=('-I',)):
    result = subprocess.run([sys.executable, *flags, '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, 'child failed'
    return json.loads(result.stdout)
probe = "import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_core') is not None))"
if not projected:
    assert child(probe, ()) is True
    assert child(probe) is False
else:
    assert child(probe) is expected_source
    assert child(probe, ('-I','-S')) is False
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('example_hidden') is None))")
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('hostile_path') is None))")
    assert child("import importlib.util,json; print(json.dumps(importlib.util.find_spec('hostile_user') is None))")
    data = child("import importlib.metadata as m,json; print(json.dumps({'found': any(d.metadata['Name']=='example-core' for d in m.distributions()), 'hidden': any(d.metadata['Name']=='example-hidden' for d in m.distributions())}))")
    assert data == {'found': expected_metadata, 'hidden': False}
    if expected_source:
        assert child("import example_core,json; print(json.dumps(example_core.value))") == expected_value
        command = subprocess.run([sys.executable,'-I','-m','example_core'], capture_output=True,text=True,timeout=10)
        assert command.returncode == 0 and command.stdout.strip() == expected_value
    if expected_metadata:
        assert child("import importlib.metadata as m,json; print(json.dumps(m.version('example-core')))") == '1.0.0'
        entry = "import importlib.metadata as m; p=next(iter(m.entry_points(group='example.plugins',name='core'))); "
        if expected_source:
            assert child(entry + "import json; print(json.dumps(p.load()))") == expected_value
        else:
            result = subprocess.run([sys.executable,'-I','-c',entry+'p.load()'],capture_output=True,timeout=10)
            assert result.returncode != 0
    assert not pathlib.Path('/tmp/source-pth-ran').exists()
    # The old parent scan would see selected metadata as installed after startup.
    # Validate the required separate runtime-only enumeration before implementation.
    selected = {d.metadata['Name'] for d in md.distributions(path=['/metadata'])}
    all_names = {d.metadata['Name'] for d in md.distributions()}
    runtime_paths = [p for p in sys.path if p != '/metadata' and not p.startswith('/project/')]
    runtime_names = {d.metadata['Name'] for d in md.distributions(path=runtime_paths)}
    assert (bool(selected & all_names)) is expected_metadata
    assert not selected & runtime_names
    assert 'pytest' in runtime_names  # A projected pytest alias must still conflict.
    if expected_source:
        try: pathlib.Path('/project/src/example_core/__init__.py').write_text('changed')
        except OSError as e: assert e.errno in (errno.EROFS,errno.EACCES)
        else: raise AssertionError('source write succeeded')
    assert not pathlib.Path('/project/private.txt').exists()
    shutil.copyfile(sys.executable, '/tmp/copied-python')
    pathlib.Path('/tmp/copied-python').chmod(0o755)
    try: subprocess.run(['/tmp/copied-python','-V'],check=True,capture_output=True)
    except PermissionError: pass
    else: raise AssertionError('executable scratch')
    interfaces = {p.name for p in pathlib.Path('/sys/class/net').iterdir()}
    assert interfaces == {'lo'}
    rows = pathlib.Path('/proc/net/route').read_text().splitlines()[1:]
    assert not any(row.split()[1] == '00000000' for row in rows)
    assert sys.flags.isolated == 1 and sys.flags.no_user_site == 1 and sys.flags.ignore_environment == 1
    assert set(p.name for p in pathlib.Path('/proc').iterdir() if p.name.isdigit()) == {str(os.getpid())}
print(json.dumps({'scenario':scenario,'passed':True,'projected':projected,'metadata':expected_metadata}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    prepared = runtime.build_runtime(root)
    outcomes = []
    names = []
    try:
        for scenario in (
            "baseline",
            "complete",
            "source-only",
            "metadata-only",
            "suite-core",
            "suite-addon",
            "runtime-conflict",
        ):
            view = root / scenario / "project"
            view.mkdir(parents=True)
            metadata = root / scenario / "metadata"
            metadata.mkdir()
            source_root = (
                "addon_src"
                if scenario == "suite-addon"
                else "core_src"
                if scenario == "suite-core"
                else "src"
            )
            value = "addon" if scenario == "suite-addon" else "core"
            if scenario != "metadata-only":
                write(view, source_root + "/example_core/__init__.py", f"value = {value!r}\n")
                write(view, source_root + "/example_core/__main__.py", f"print({value!r})\n")
                write(
                    view,
                    source_root + "/ignored.pth",
                    "import pathlib; pathlib.Path('/tmp/source-pth-ran').touch()\n",
                )
            if scenario not in ("baseline", "source-only"):
                write(
                    metadata,
                    "example_core-1.0.0.dist-info/METADATA",
                    "Metadata-Version: 2.3\nName: example-core\nVersion: 1.0.0\n",
                )
                write(
                    metadata,
                    "example_core-1.0.0.dist-info/entry_points.txt",
                    "[example.plugins]\ncore = example_core:value\n",
                )
            if scenario == "runtime-conflict":
                write(
                    metadata,
                    "pytest-0.0.0.dist-info/METADATA",
                    "Metadata-Version: 2.3\nName: pytest\nVersion: 0.0.0\n",
                )
            control = root / scenario / "control"
            script = write(
                control, "check.py", CHECK.replace("/project/src", "/project/" + source_root)
            )
            projection = write(control, "view.pth", "/project/" + source_root + "\n/metadata\n")
            validate_view(view, [source_root])
            for tree in (view, metadata, control):
                for p in sorted(tree.rglob("*"), reverse=True):
                    if p.is_dir():
                        p.chmod(0o755)
                        os.utime(p, ns=(0, 0))
                tree.chmod(0o755)
                os.utime(tree, ns=(0, 0))
            name = "telescope-source-proof-" + token + "-" + scenario
            names.append(name)
            cmd = [
                "docker",
                "run",
                "--name",
                name,
                "--label",
                LABEL + "=" + token,
                "--network=none",
                "--ipc=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65534:65534",
                "--cpus=1",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=128",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--workdir=/tmp",
                "--env=PYTHONPATH=/project/src:/tmp/hostile",
                "--env=PYTHONUSERBASE=/tmp/user-site",
                "--env=HOME=/tmp",
                "--mount",
                f"type=bind,src={view},dst=/project,readonly",
                "--mount",
                f"type=bind,src={metadata},dst=/metadata,readonly",
                "--mount",
                f"type=bind,src={script},dst=/proof.py,readonly",
            ]
            if scenario != "baseline":
                cmd += ["--mount", f"type=bind,src={projection},dst={SLOT},readonly"]
            cmd += ["--entrypoint=python", prepared["image"], "-I", "/proof.py", scenario]
            completed = subprocess.run(cmd, capture_output=True, timeout=45)
            (root / (scenario + ".log")).write_bytes(completed.stdout + completed.stderr)
            assert completed.returncode == 0, "inspect retained scenario log: " + scenario
            info = json.loads(runtime.command(["docker", "inspect", name]))[0]
            assert info["Config"]["Labels"][LABEL] == token
            assert info["HostConfig"]["NetworkMode"] == "none"
            assert info["HostConfig"]["ReadonlyRootfs"]
            outcomes.append(json.loads(completed.stdout))
            runtime.command(["docker", "rm", name])
            names.remove(name)
        for entry in (
            "sitecustomize.py",
            "usercustomize/__init__.py",
            "hidden-1.0.dist-info/METADATA",
        ):
            view = root / ("refusal-" + str(len(outcomes)))
            write(view, "src/" + entry, "raise RuntimeError('must never execute')\n")
            try:
                validate_view(view, ["src"])
            except UnsupportedCapability as error:
                assert error.capability == "python:source-layout"
            else:
                raise AssertionError("startup namespace was accepted")
            outcomes.append({"scenario": entry.split("/")[0], "refused_before_worker": True})
        report = {
            "mechanism": "data-only-system-pth-v1",
            "outcomes": outcomes,
            "image": prepared["image"],
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        for name in names:
            info = json.loads(runtime.command(["docker", "inspect", name]))[0]
            assert info["Config"]["Labels"][LABEL] == token
            runtime.command(["docker", "rm", "-f", name])
        runtime._image_info(prepared["image"], runtime._project(root))
        runtime.command(["docker", "image", "rm", prepared["image"]])
        assert not runtime.command(
            ["docker", "ps", "-aq", "--filter", "label=" + LABEL + "=" + token]
        ).strip()
        remaining = runtime.command(["docker", "image", "ls", "--quiet", "--no-trunc"])
        assert prepared["image"] not in remaining.decode().splitlines()
        print(json.dumps({"owned_cleanup": True}))


if __name__ == "__main__":
    main()
