"""Hosted qualification of installed CPython assembly, not runtime/backend admission.

The transport has only a bounded JSON manifest and hash-named regular blobs. It
never extracts archive paths or follows payload symlinks on the preparing host.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from pytest_telescope import capsule, python_capsule
from pytest_telescope.controller import runner_identity

MAX_BYTES = 2 * 1024**3
MAX_ENTRIES = 100_000
MAX_MANIFEST = 32 * 1024**2
MAX_ARCHIVE = 512 * 1024**2
SCRIPT = Path(__file__).resolve()
WORKFLOW = SCRIPT.parents[1] / ".github/workflows/python-capsule-proof.yml"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def bindings():
    return dict(
        head=os.environ["GITHUB_SHA"],
        run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        workflow_sha256=digest(WORKFLOW.read_bytes()),
        script_sha256=digest(SCRIPT.read_bytes()),
        runner=runner_identity(),
        wheel_sha256=os.environ["TELESCOPE_WHEEL_SHA256"],
    )


def regular(path, limit=MAX_BYTES):
    return capsule.regular_snapshot(path, limit)[0]


def validate_manifest(value):
    assert type(value) is dict and set(value) == {"schema", "directories", "files"}
    assert type(value["schema"]) is int and value["schema"] == 1
    dirs, files = value["directories"], value["files"]
    assert type(dirs) is list and type(files) is dict
    assert len(dirs) + len(files) <= MAX_ENTRIES
    assert dirs == sorted(set(dirs)) and not set(dirs) & set(files)
    total = 0
    for name in [*dirs, *files]:
        assert type(name) is str and name and capsule._path("/" + name) == name
        assert len(name.encode()) <= 4096
        assert all(str(p) in dirs for p in PurePosixPath(name).parents if str(p) != ".")
    for name, record in files.items():
        assert type(record) is dict
        if record.get("kind") == "file":
            assert set(record) == {"kind", "size", "sha256", "mode", "mtime_ns"}
            assert type(record["size"]) is int and 0 <= record["size"] <= MAX_BYTES
            assert type(record["mode"]) is int and record["mode"] & ~0o555 == 0
            assert re.fullmatch("[a-f0-9]{64}", record["sha256"])
            total += record["size"]
        else:
            assert set(record) == {"kind", "target", "mtime_ns"}
            assert record["kind"] == "symlink" and record["mtime_ns"] == 0
            assert type(record["target"]) is str and "\x00" not in record["target"]
            assert 0 < len(os.fsencode(record["target"])) <= 4096
            total += len(os.fsencode(record["target"]))
        assert type(record["mtime_ns"]) is int and 0 <= record["mtime_ns"] < 2**63
    assert total <= MAX_BYTES
    # Resolve every component against the manifest, never host filesystem links.
    for name, record in files.items():
        if record["kind"] != "symlink":
            continue
        pending = "/" + name
        for _ in range(64):
            parts = PurePosixPath(pending).parts[1:]
            for index in range(len(parts)):
                key = "/".join(parts[: index + 1])
                candidate = files.get(key)
                if candidate and candidate["kind"] == "symlink":
                    target = candidate["target"]
                    base = (
                        target
                        if target.startswith("/")
                        else str(PurePosixPath("/" + key).parent / target)
                    )
                    pending = os.path.normpath(
                        str(PurePosixPath(base).joinpath(*parts[index + 1 :]))
                    )
                    capsule._path(pending)
                    break
            else:
                assert pending.lstrip("/") in files or pending.lstrip("/") in dirs
                break
        else:
            raise AssertionError("transport symlink cycle")
    return total


def export(result, archive, *, preparation_resources=None):
    result.payload.verify()
    manifest = result.payload.manifest()
    validate_manifest(manifest)
    directory_metadata = {}
    for name in ["", *manifest["directories"]]:
        value = (result.payload.root / name).lstat()
        assert stat.S_ISDIR(value.st_mode)
        directory_metadata[name] = dict(
            mode=stat.S_IMODE(value.st_mode), mtime_ns=value.st_mtime_ns
        )
    metadata = dict(
        directory_metadata=directory_metadata,
        schema=1,
        bindings=bindings(),
        manifest=manifest,
        identity=result.identity(),
        source=result.source,
        executable=result.executable,
        site=result.site,
        wheel_hashes=result.wheel_hashes,
        timings=result.timings,
        bytes=result.payload.bytes_used,
    )
    raw = json_bytes(metadata)
    assert len(raw) <= MAX_MANIFEST
    written = set()
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", raw)
        for name, record in manifest["files"].items():
            if record["kind"] == "symlink" or record["sha256"] in written:
                continue
            data = regular(result.payload.root / name, record["size"])
            assert len(data) == record["size"] and digest(data) == record["sha256"]
            output.writestr("files/" + record["sha256"], data)
            written.add(record["sha256"])
    result.payload.verify()
    receipt = dict(
        bindings=bindings(),
        archive_sha256=digest(regular(archive, MAX_ARCHIVE)),
        manifest_sha256=digest(raw),
        identity=result.identity(),
        bytes=result.payload.bytes_used,
        entries=len(manifest["files"]) + len(manifest["directories"]),
        timings=result.timings,
        preparation_resources=preparation_resources,
    )
    archive.with_suffix(".json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def restore(archive, destination):
    receipt = json.loads(regular(archive.with_suffix(".json"), 65536))
    assert receipt["bindings"] == bindings()
    archive_bytes = regular(archive, MAX_ARCHIVE)
    assert digest(archive_bytes) == receipt["archive_sha256"]
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as source:
        infos = source.infolist()
        assert len(infos) <= MAX_ENTRIES + 1
        names = [item.filename for item in infos]
        assert len(set(names)) == len(names)
        assert all(not item.is_dir() and not item.flag_bits & 1 for item in infos)
        assert sum(item.file_size for item in infos) <= MAX_BYTES + MAX_MANIFEST
        info = source.getinfo("manifest.json")
        assert info.file_size <= MAX_MANIFEST
        raw = source.read(info)
        assert digest(raw) == receipt["manifest_sha256"]
        metadata = json.loads(raw)
        assert set(metadata) == {
            "schema",
            "bindings",
            "manifest",
            "identity",
            "source",
            "executable",
            "site",
            "wheel_hashes",
            "timings",
            "bytes",
            "directory_metadata",
        }
        assert metadata["schema"] == 1 and metadata["bindings"] == bindings()
        manifest = metadata["manifest"]
        total = validate_manifest(manifest)
        directories = metadata["directory_metadata"]
        assert type(directories) is dict and set(directories) == set(manifest["directories"]) | {""}
        for value in directories.values():
            assert type(value) is dict and set(value) == {"mode", "mtime_ns"}
            assert type(value["mode"]) is int and 0 <= value["mode"] <= 0o777
            assert type(value["mtime_ns"]) is int and 0 <= value["mtime_ns"] < 2**63
        expected = {
            "files/" + rec["sha256"] for rec in manifest["files"].values() if rec["kind"] == "file"
        }
        assert set(names) == expected | {"manifest.json"}
        for record in manifest["files"].values():
            if record["kind"] == "file":
                info = source.getinfo("files/" + record["sha256"])
                assert info.file_size == record["size"] and info.file_size <= MAX_BYTES
        payload = capsule.Payload(destination)
        # No links exist until all regular files/directories have been created.
        for name, record in manifest["files"].items():
            if record["kind"] == "file":
                info = source.getinfo("files/" + record["sha256"])
                assert info.file_size == record["size"]
                data = source.read(info)
                assert len(data) == record["size"] and digest(data) == record["sha256"]
                payload.write("/" + name, data, mode=record["mode"], mtime_ns=record["mtime_ns"])
        for name, record in manifest["files"].items():
            if record["kind"] == "symlink":
                payload.symlink("/" + name, record["target"])
        for name, value in directories.items():
            directory = payload.root / name
            directory.chmod(value["mode"])
            os.utime(directory, ns=(value["mtime_ns"], value["mtime_ns"]))
        assert payload.manifest() == manifest and payload.bytes_used == total == metadata["bytes"]
        payload.verify()
    result = python_capsule.PythonCapsule(
        payload,
        metadata["executable"],
        metadata["site"],
        metadata["source"],
        metadata["wheel_hashes"],
        metadata["timings"],
    )
    assert result.identity() == metadata["identity"] == receipt["identity"]
    return result


def resources():
    spec = importlib.util.spec_from_file_location(
        "owned_resource_proof", SCRIPT.with_name("probe_bubblewrap_resources.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAYLOAD = r"""
import bz2, ctypes, errno, hashlib, importlib.metadata, json, lzma, os, socket, sqlite3, ssl, subprocess, sys, zlib
from pathlib import Path
assert sys.version_info[:2] == (3, 12)
assert sys.prefix == '/opt/telescope/python'
assert importlib.metadata.version('pytest') == '8.3.5'
assert importlib.metadata.version('pytest-telescope') == '0.1.0a4'
assert Path('/project/data.txt').read_text() == 'declared-value'
for missing in ['/project/undeclared.txt', HOST_SECRET, '/etc/passwd', '/bin/sh', HOST_INSTALLATION]:
    assert not os.path.exists(missing), missing
    try: os.stat(missing)
    except FileNotFoundError: pass
    else: raise AssertionError('unexpected path metadata')
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.open(missing.encode(), os.O_RDONLY) == -1 and ctypes.get_errno() == errno.ENOENT
try: Path('/project/data.txt').write_text('changed')
except OSError as exc: assert exc.errno == errno.EROFS
else: raise AssertionError('writable source')
assert [name for _, name in socket.if_nameindex()] == ['lo']
assert not any(row.split()[1] == '00000000' for row in Path('/proc/net/route').read_text().splitlines()[1:])
st = os.statvfs('/tmp')
assert st.f_flag & os.ST_NOEXEC and st.f_blocks * st.f_frsize == 8388608 and st.f_files == 1024
assert {'noexec','nosuid','nodev'} <= set(next(row.split()[5] for row in Path('/proc/self/mountinfo').read_text().splitlines() if row.split()[4] == '/tmp').split(','))
for module in (zlib, bz2, lzma): assert module.decompress(module.compress(b'sealed' * 100)) == b'sealed' * 100
assert sqlite3.connect(':memory:').execute('select 7').fetchone() == (7,)
assert ssl.OPENSSL_VERSION and hashlib.sha256(b'sealed').hexdigest()
child = subprocess.run([sys.executable, '-I', '-B', '-c', "import ssl,sqlite3,importlib.metadata,os;assert not os.path.exists('/project/undeclared.txt');assert importlib.metadata.version('pytest')=='8.3.5';print('child-ok')"], capture_output=True, text=True, timeout=10)
assert child.returncode == 0 and child.stdout == 'child-ok\n', (child.returncode, child.stderr)
for code in (0, 7):
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', f'import sys;sys.exit({code})'])
    assert child.wait(timeout=10) == code and child.poll() == code
binary = Path('/tmp/copied-python'); binary.write_bytes(Path(sys.executable).resolve().read_bytes()); binary.chmod(0o755)
try: subprocess.run([str(binary), '-c', 'pass'], check=True)
except PermissionError: pass
else: raise AssertionError('executable scratch')
binary.unlink()
import pytest
assert pytest.main(['-q','-p','no:cacheprovider','--basetemp=/tmp/pytest','/project/test_sample.py']) == 0
print('CAPSULE_RESULT ' + json.dumps(dict(version=list(sys.version_info[:3]), executable=sys.executable, prefix=sys.prefix, source_visible=True, missing_native_denied=True, native_extensions=True, isolated_child=True, child_statuses=True, readonly=True, network_none=True, noexec=True, pytest_cases=2)))
"""


def unit(config, token):
    helper = resources()
    own = Path("/sys/fs/cgroup") / next(
        row.split(":", 2)[2].lstrip("/")
        for row in Path("/proc/self/cgroup").read_text().splitlines()
        if row.startswith("0::")
    )
    before = helper.group_values(own)
    assert before["cpu.max"] == "50000 100000" and before["memory.max"] == "536870912"
    assert before["memory.swap.max"] == "0" and before["pids.max"] == "128"
    assert own.parent == Path(config["worker_cgroup"])
    parent, aggregate = helper.group_values(own.parent), helper.group_values(own.parent.parent)
    assert (
        parent["cpu.max"] == "100000 100000"
        and parent["memory.max"] == "1073741824"
        and parent["pids.max"] == "256"
    )
    assert (
        aggregate["cpu.max"] == "200000 100000"
        and aggregate["memory.max"] == "3221225472"
        and aggregate["pids.max"] == "320"
    )
    item = config["capsules"][token]
    payload = (
        "HOST_SECRET="
        + repr(config["secret"])
        + "\nHOST_INSTALLATION="
        + repr(config["host_installation"])
        + "\n"
        + PAYLOAD
    )
    argv = [
        config["bwrap"],
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/",
    ]
    # A fresh root supplies mountpoints without altering or broadening the closure.
    for entry in item["mounts"]:
        if entry["kind"] == "symlink":
            argv += ["--symlink", entry["target"], "/" + entry["name"]]
        else:
            argv += ["--ro-bind", str(Path(item["root"]) / entry["name"]), "/" + entry["name"]]
    argv += [
        "--ro-bind",
        config["view"],
        "/project",
        "--bind",
        "/tmp",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--remount-ro",
        "/",
        "--chdir",
        "/project",
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PATH",
        "/opt/telescope/python/bin",
        "--setenv",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "1",
        "--",
        item["executable"],
        "-I",
        "-B",
        "-c",
        payload,
    ]
    result = subprocess.run(argv, text=True, capture_output=True, timeout=40)
    after = helper.group_values(own)
    print(
        json.dumps(
            dict(
                token=token,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                before=before,
                after=after,
                pool=parent,
                aggregate=aggregate,
            )
        ),
        flush=True,
    )
    assert result.returncode == 0
    records = [
        json.loads(line.removeprefix("CAPSULE_RESULT "))
        for line in result.stdout.splitlines()
        if line.startswith("CAPSULE_RESULT ")
    ]
    assert len(records) == 1 and records[0]["pytest_cases"] == 2
    assert after["pids.current"] == "1"


def controller(config):
    helper = resources()
    root = Path(config["output"])
    root.mkdir()
    own = Path("/sys/fs/cgroup") / next(
        row.split(":", 2)[2].lstrip("/")
        for row in Path("/proc/self/cgroup").read_text().splitlines()
        if row.startswith("0::")
    )
    values = helper.group_values(own)
    assert (
        values["cpu.max"] == "100000 100000"
        and values["memory.max"] == "2147483648"
        and values["memory.swap.max"] == "0"
        and values["pids.max"] == "64"
    )
    capsules = {}
    report = dict(
        status="incomplete", bindings=bindings(), controller=values, units=[], preparation={}
    )
    active = []
    try:
        capsules["A"] = restore(Path(config["distro"]), root / "distro")
        wheels = sorted(Path(config["wheels"]).glob("*.whl"))
        capsules["B"] = python_capsule.prepare(
            root / "dedicated", Path(config["interpreter"]), wheels
        )
        assert capsules["A"].source["base_prefix"] == "/usr"
        assert capsules["B"].source["base_prefix"] != "/usr"
        for token, result in capsules.items():
            report["preparation"][token] = dict(
                identity=result.identity(),
                bytes=result.payload.bytes_used,
                entries=len(result.payload.records) + len(result.payload.directories),
                source=result.source,
                timings=result.timings,
            )
        config["capsules"] = {
            token: dict(
                root=str(result.payload.root),
                executable=result.executable,
                mounts=[
                    dict(
                        name=path.name,
                        kind="symlink",
                        target=result.payload.records[path.name]["target"],
                    )
                    if path.is_symlink()
                    else dict(name=path.name, kind="readonly")
                    for path in sorted(result.payload.root.iterdir())
                ],
            )
            for token, result in capsules.items()
        }
        view = root / "view"
        view.mkdir()
        (view / "data.txt").write_text("declared-value")
        (view / "test_sample.py").write_text(
            "import pytest\nfrom pathlib import Path\n@pytest.fixture(scope='module')\ndef value(): return Path('/project/data.txt').read_text()\n@pytest.mark.parametrize('number',[1,2])\ndef test_value(value,number): assert value=='declared-value' and number in (1,2)\n"
        )
        for path in view.iterdir():
            path.chmod(0o444)
            os.utime(path, ns=(0, 0))
        os.utime(view, ns=(0, 0))
        secret = root / "undeclared.txt"
        secret.write_text("synthetic omitted input")
        config.update(view=str(view), secret=str(secret), host_installation=str(Path(sys.prefix)))
        assert Path(config["host_installation"]).exists()
        cfg = root / "unit-config.json"
        cfg.write_text(json.dumps(config))
        for token in ["A", "B"]:
            name = config["prefix"] + token + ".service"
            argv = [
                "systemd-run",
                "--user",
                "--wait",
                "--pipe",
                "--unit=" + name,
                "--slice=" + config["worker_slice"],
            ]
            for value in [
                "CPUQuota=50%",
                "MemoryMax=536870912",
                "MemorySwapMax=0",
                "TasksMax=128",
                "PrivateUsers=yes",
                "TemporaryFileSystem=/tmp:rw,noexec,nosuid,nodev,size=8M,nr_inodes=1024,mode=1777",
                "KillMode=control-group",
                "RuntimeMaxSec=60",
                "TimeoutStopSec=5",
            ]:
                argv += ["-p", value]
            argv += [sys.executable, "-I", "-B", str(SCRIPT), "--unit", token, "--config", str(cfg)]
            active.append(
                (
                    name,
                    subprocess.Popen(
                        argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    ),
                )
            )
        for name, child in active:
            stdout, stderr = child.communicate(timeout=70)
            report["units"].append(
                dict(unit=name, exit_code=child.returncode, stdout=stdout, stderr=stderr)
            )
        assert all(row["exit_code"] == 0 for row in report["units"])
        for result in capsules.values():
            result.payload.verify()
        report["runtime_unchanged"] = True
        report["status"] = "passed"
    finally:
        for name, child in active:
            helper.stop_owned(name)
            if child.poll() is None:
                child.communicate(timeout=10)
        report["worker_slice_empty"] = (
            Path(config["worker_cgroup"]) / "pids.current"
        ).read_text().strip() == "0"
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
        assert report["worker_slice_empty"]


def launch(args):
    helper = resources()
    # Probe in the invoking admission context before any external service. This
    # launcher-only prerequisite gives no host /usr mount to either test payload.
    preflight = subprocess.run(
        [
            str(args.bwrap),
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--chdir",
            "/",
            "--",
            "/usr/bin/true",
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    print(
        json.dumps(
            dict(
                entry_preflight=dict(
                    exit_code=preflight.returncode, stdout=preflight.stdout, stderr=preflight.stderr
                )
            )
        ),
        flush=True,
    )
    assert preflight.returncode == 0
    # Same reviewed ownership classification; distinct exact prefix from prior probes.
    run_id = "native54" + args.run_id
    helper.ownership_preflight(run_id)
    prefix = "telescope52" + run_id
    config = dict(
        prefix=prefix,
        worker_slice=prefix + "-workers.slice",
        output=str(args.output),
        bwrap=str(args.bwrap),
        distro=str(args.archive),
        wheels=str(args.wheels),
        interpreter=str(args.interpreter),
    )
    directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    cfg = args.output.with_suffix(".launch.json")
    cfg.write_text(json.dumps(config))
    try:
        for name, cpu, memory, tasks in [
            (prefix, "200%", 3221225472, 320),
            (prefix + "-workers", "100%", 1073741824, 256),
        ]:
            path = directory / (name + ".slice")
            with path.open("x") as stream:
                stream.write(
                    f"[Unit]\nDescription=Owned installed Python capsule proof\n[Slice]\nCPUQuota={cpu}\nMemoryMax={memory}\nMemorySwapMax=0\nTasksMax={tasks}\n"
                )
            files.append(path)
        helper.systemctl("daemon-reload")
        helper.systemctl("start", prefix + ".slice", config["worker_slice"])
        config["aggregate_cgroup"] = "/sys/fs/cgroup" + helper.systemctl(
            "show", prefix + ".slice", "-p", "ControlGroup", "--value"
        )
        config["worker_cgroup"] = "/sys/fs/cgroup" + helper.systemctl(
            "show", config["worker_slice"], "-p", "ControlGroup", "--value"
        )
        assert Path(config["worker_cgroup"]).parent == Path(config["aggregate_cgroup"])
        cfg.write_text(json.dumps(config))
        argv = [
            "systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--unit=" + prefix + "controller.service",
            "--slice=" + prefix + ".slice",
        ]
        for value in [
            "CPUQuota=100%",
            "MemoryMax=2147483648",
            "MemorySwapMax=0",
            "TasksMax=64",
            "RuntimeMaxSec=240",
            "TimeoutStopSec=5",
            "KillMode=control-group",
        ]:
            argv += ["-p", value]
        for name in ["GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "TELESCOPE_WHEEL_SHA256"]:
            argv += ["--setenv=" + name + "=" + os.environ[name]]
        argv += [sys.executable, "-I", "-B", str(SCRIPT), "--controller", "--config", str(cfg)]
        result = subprocess.run(argv, timeout=260)
        assert result.returncode == 0
    finally:
        try:
            helper.cleanup(config)
        finally:
            for path in files:
                path.unlink()
            helper.systemctl("daemon-reload")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--controller", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--unit")
    for name in ["output", "archive", "wheels", "interpreter", "bwrap", "config"]:
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.prepare:
        membership = Path("/proc/self/cgroup").read_text()
        own = Path("/sys/fs/cgroup") / next(
            row.split(":", 2)[2].lstrip("/")
            for row in membership.splitlines()
            if row.startswith("0::")
        )
        assert (
            str(own)
            == "/sys/fs/cgroup/system.slice/telescope54prepare"
            + os.environ["GITHUB_RUN_ID"]
            + ".service"
        )
        observed = resources().group_values(own)
        assert observed["cpu.max"] == "100000 100000" and observed["memory.max"] == "2147483648"
        assert observed["memory.swap.max"] == "0" and observed["pids.max"] == "64"
        preparation_resources = dict(
            cgroup=str(own),
            observed=observed,
            bindings=bindings(),
            storage=dict(
                payload_bytes=MAX_BYTES,
                archive_bytes=MAX_ARCHIVE,
                entries=MAX_ENTRIES,
                aggregate_disk_quota_enforced=False,
            ),
        )
        args.archive.with_suffix(".preparation.json").write_text(
            json.dumps(preparation_resources, indent=2) + "\n"
        )
        result = python_capsule.prepare(
            args.output, args.interpreter, sorted(args.wheels.glob("*.whl"))
        )
        print(
            json.dumps(export(result, args.archive, preparation_resources=preparation_resources)),
            flush=True,
        )
    elif args.cleanup:
        resources().cleanup(json.loads(args.config.read_text()))
    elif args.unit:
        unit(json.loads(args.config.read_text()), args.unit)
    elif args.controller:
        controller(json.loads(args.config.read_text()))
    else:
        launch(args)


if __name__ == "__main__":
    main()
