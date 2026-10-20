"""Bounded hosted proof of ordinary-user unit quotas and private Bubblewrap scratch."""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PAYLOAD = r"""
import ctypes, errno, json, os, resource, subprocess, sys, time
from pathlib import Path
started=time.monotonic()
assert Path('/project/data.txt').read_text() == TOKEN
for name in ['/project/undeclared.txt', HOST_SECRET]:
    assert not os.path.exists(name)
    try: os.stat(name)
    except FileNotFoundError: pass
    else: raise AssertionError('unexpected visible path')
    libc=ctypes.CDLL(None,use_errno=True)
    assert libc.open(name.encode(),os.O_RDONLY)==-1 and ctypes.get_errno()==errno.ENOENT
try: Path('/project/data.txt').write_text('changed')
except OSError as exc: assert exc.errno==errno.EROFS
else: raise AssertionError('writable project')
st=os.statvfs('/tmp')
assert st.f_blocks*st.f_frsize==8388608 and st.f_files==1024
assert st.f_flag & os.ST_NOEXEC and st.f_flag & os.ST_NOSUID
mount=next(line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines() if line.split()[4]=='/tmp')
assert {'noexec','nosuid','nodev'} <= set(mount[5].split(','))
Path('/tmp/same-name').write_text(TOKEN)
exe=Path('/tmp/native')
exe.write_bytes(Path(sys.executable).resolve().read_bytes());exe.chmod(0o755)
try: subprocess.run([str(exe),'-c','pass'],check=True)
except PermissionError: pass
else: raise AssertionError('executable scratch')
exe.unlink()
byte_failure=False
try:
    with open('/tmp/fill','wb',buffering=0) as f:
        for _ in range(256): f.write(b'x'*65536)
except OSError as exc:
    assert exc.errno==errno.ENOSPC;byte_failure=True
finally: Path('/tmp/fill').unlink(missing_ok=True)
assert byte_failure
created=[];inode_failure=False
try:
    for i in range(1100):
        p=Path('/tmp')/str(i);p.touch();created.append(p)
except OSError as exc:
    assert exc.errno==errno.ENOSPC;inode_failure=True
finally:
    for p in created:p.unlink()
assert inode_failure
r,w=os.pipe();children=[];pid_failure=False
try:
    for _ in range(160):
        try:pid=os.fork()
        except OSError as exc:
            assert exc.errno==errno.EAGAIN;pid_failure=True;break
        if pid==0:
            os.close(w);os.read(r,1);os._exit(0)
        children.append(pid)
finally:
    os.close(r);os.close(w)
    for pid in children:assert os.waitpid(pid,0)[1]==0
assert pid_failure and children
memory=subprocess.run([sys.executable,'-I','-B','-c','x=bytearray(768*1024*1024)'],capture_output=True,timeout=15)
assert memory.returncode==-9,memory.returncode
cpu_start=time.process_time()
while time.process_time()-cpu_start < .4:
    sum(i*i for i in range(1000))
assert Path('/tmp/same-name').read_text()==TOKEN
import socket
assert [name for _,name in socket.if_nameindex()]==['lo']
assert not any(x.split()[1]=='00000000' for x in Path('/proc/net/route').read_text().splitlines()[1:])
print(json.dumps(dict(token=TOKEN,started=started,finished=time.monotonic(),cpu_work=time.process_time()-cpu_start,
    readonly=True,native_denied=True,noexec=True,tmpfs_bytes=True,tmpfs_inodes=True,pid_ceiling=True,
    forked=len(children),memory_child_signal=9,network_none=True,private_scratch=True)))
"""


def call(argv, **kwargs):
    try:
        return subprocess.run(
            argv, check=True, text=True, capture_output=True, timeout=90, **kwargs
        )
    except subprocess.CalledProcessError as exc:
        print(
            json.dumps(
                dict(
                    command=Path(argv[0]).name,
                    exit_code=exc.returncode,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                )
            ),
            file=sys.stderr,
            flush=True,
        )
        raise


def systemctl(*args):
    return call(["systemctl", "--user", *args]).stdout.strip()


def group_values(path):
    return {
        name: (path / name).read_text().strip()
        for name in [
            "cpu.max",
            "cpu.stat",
            "memory.max",
            "memory.swap.max",
            "memory.events",
            "pids.max",
            "pids.current",
            "pids.events",
            "cgroup.controllers",
        ]
    }


def counters(text):
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "fresh_probe", Path(__file__).parents[1] / "benchmarks/bubblewrap_fresh.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit(config, token):
    helper = load_helper()
    own = Path("/sys/fs/cgroup") / next(
        x.split(":", 2)[2].lstrip("/")
        for x in Path("/proc/self/cgroup").read_text().splitlines()
        if x.startswith("0::")
    )
    before = group_values(own)
    assert before["cpu.max"] == "50000 100000", before
    assert before["memory.max"] == "536870912" and before["memory.swap.max"] == "0"
    assert before["pids.max"] == "128"
    assert own.parent == Path(config["worker_cgroup"])
    parent = group_values(own.parent)
    assert parent["cpu.max"] == "100000 100000" and parent["memory.max"] == "1073741824"
    assert parent["pids.max"] == "256"
    aggregate = group_values(own.parent.parent)
    assert aggregate["cpu.max"] == "200000 100000" and aggregate["memory.max"] == "2147483648"
    assert aggregate["pids.max"] == "320"
    root = Path(config["output"])
    (root / f"ready-{token}").write_text("ready")
    deadline = time.monotonic() + 10
    while not (root / "release").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("owned unit readiness timeout")
        time.sleep(0.01)
    payload = f"TOKEN={token!r}\nHOST_SECRET={config['secret']!r}\n" + PAYLOAD
    argv = helper.command(
        Path(config["runtime"]),
        root / f"view-{token}",
        Path("/tmp"),
        ["-c", payload],
        bwrap=config["bwrap"],
    )
    result = call(argv)
    observed = json.loads(result.stdout)
    after = group_values(own)
    assert int(after["pids.current"]) == 1, after
    assert counters(after["pids.events"])["max"] > counters(before["pids.events"])["max"]
    assert (
        counters(after["memory.events"])["oom_kill"] > counters(before["memory.events"])["oom_kill"]
    )
    assert (
        counters(after["cpu.stat"])["nr_throttled"] > counters(before["cpu.stat"])["nr_throttled"]
    )
    cpu = counters(after["cpu.stat"])["usage_usec"] - counters(before["cpu.stat"])["usage_usec"]
    assert cpu >= observed["cpu_work"] * 1_000_000
    print(
        json.dumps(
            dict(
                observed=observed,
                before=before,
                after=after,
                parent=parent,
                aggregate=aggregate,
                cgroup_cpu_usec=cpu,
                cgroup=str(own),
                uid=os.getuid(),
                security=Path("/proc/self/attr/current").read_text().strip(),
            )
        )
    )


def controller(config):
    helper = load_helper()
    root = Path(config["output"])
    root.mkdir()
    runtime = root / "runtime"
    preparation = helper.copy_runtime(runtime)
    config["runtime"] = str(runtime)
    secret = root / "undeclared.txt"
    secret.write_text("synthetic omitted source")
    config["secret"] = str(secret)
    for token in ["A", "B"]:
        view = root / f"view-{token}"
        view.mkdir()
        (view / "data.txt").write_text(token)
        (view / "data.txt").chmod(0o444)
        os.utime(view / "data.txt", (0, 0))
        os.utime(view, (0, 0))
    config["worker_cgroup"] = "/sys/fs/cgroup" + systemctl(
        "show", config["worker_slice"], "-p", "ControlGroup", "--value"
    )
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config))
    active = []
    report = dict(status="incomplete", preparation=preparation, units=[], uid=os.getuid())
    try:
        for token in ["A", "B"]:
            name = config["prefix"] + token + ".service"
            argv = [
                "systemd-run",
                "--user",
                "--wait",
                "--pipe",
                "--unit=" + name,
                "--slice=" + config["worker_slice"],
                "-p",
                "CPUQuota=50%",
                "-p",
                "MemoryMax=536870912",
                "-p",
                "MemorySwapMax=0",
                "-p",
                "TasksMax=128",
                "-p",
                "PrivateUsers=yes",
                "-p",
                "TemporaryFileSystem=/tmp:rw,noexec,nosuid,nodev,size=8M,nr_inodes=1024,mode=1777",
                "-p",
                "OOMPolicy=continue",
                "-p",
                "KillMode=control-group",
                "-p",
                "RuntimeMaxSec=45",
                "-p",
                "TimeoutStopSec=5",
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--unit",
                token,
                "--config",
                str(config_path),
            ]
            active.append(
                (
                    name,
                    subprocess.Popen(
                        argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    ),
                )
            )
        deadline = time.monotonic() + 15
        while not all((root / f"ready-{token}").exists() for token in ["A", "B"]):
            if any(p.poll() is not None for _, p in active):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("owned worker startup deadline")
            time.sleep(0.02)
        (root / "release").write_text("go")
        for name, process in active:
            stdout, stderr = process.communicate(timeout=50)
            row = dict(unit=name, exit_code=process.returncode, stdout=stdout, stderr=stderr)
            report["units"].append(row)
        assert all(row["exit_code"] == 0 for row in report["units"]), report["units"]
        rows = [json.loads(row["stdout"]) for row in report["units"]]
        a, b = (row["observed"] for row in rows)
        assert max(a["started"], b["started"]) < min(a["finished"], b["finished"])
        assert {a["token"], b["token"]} == {"A", "B"}
        report["overlap"] = True
        report["runtime_unchanged"] = (
            hashlib.sha256(
                json.dumps(
                    helper.runtime_manifest(runtime, verify_source=True), sort_keys=True
                ).encode()
            ).hexdigest()
            == preparation["identity"]
        )
        assert report["runtime_unchanged"]
        report["status"] = "passed"
    finally:
        for name, process in active:
            subprocess.run(["systemctl", "--user", "stop", name], capture_output=True, timeout=15)
            if process.poll() is None:
                process.communicate(timeout=15)
        report["worker_slice_empty"] = (
            Path(config["worker_cgroup"]) / "pids.current"
        ).read_text().strip() == "0"
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        assert report["worker_slice_empty"]
    print(json.dumps(report))


def unit_state(name, *, detail=False):
    fields = {"LoadState", "ActiveState", "ControlGroup"}
    if detail:
        fields |= {"FragmentPath", "DropInPaths", "Transient"}
    result = subprocess.run(
        ["systemctl", "--user", "show", name, "--property=" + ",".join(sorted(fields))],
        text=True,
        capture_output=True,
        timeout=15,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if set(values) != fields:
        raise RuntimeError("owned unit state unavailable: " + result.stderr)
    if result.returncode and values["LoadState"] != "not-found":
        raise RuntimeError("owned unit state query failed: " + result.stderr)
    if values["LoadState"] == "not-found":
        assert values["ActiveState"] == "inactive" and not values["ControlGroup"], values
    elif values["LoadState"] != "loaded":
        raise RuntimeError("unexpected owned unit load state")
    if detail and values["Transient"] not in {"yes", "no"}:
        raise RuntimeError("unexpected unit transient state")
    return values


def stop_owned(name):
    state = unit_state(name)
    if state["LoadState"] == "loaded" and state["ActiveState"] not in {"inactive", "failed"}:
        systemctl("stop", name)
        state = unit_state(name)
    if state["ActiveState"] not in {"inactive", "failed"}:
        raise RuntimeError("owned unit did not stop")
    return state


def cleanup(config):
    prefix = config["prefix"]
    evidence = dict(services={}, cgroups={}, slices={}, status="incomplete")
    errors = []
    for name in [prefix + "A.service", prefix + "B.service", prefix + "controller.service"]:
        try:
            evidence["services"][name] = stop_owned(name)
        except Exception as exc:
            errors.append(str(exc))
    # The launcher is outside the proof slice: every owned process must now be gone.
    for field, name in [
        ("aggregate_cgroup", prefix + ".slice"),
        ("worker_cgroup", config["worker_slice"]),
    ]:
        try:
            value = config.get(field)
            if value is None:
                state = unit_state(name)
                if not state["ControlGroup"]:
                    assert state["ActiveState"] in {"inactive", "failed"}, state
                    evidence["cgroups"][field] = dict(inactive_without_cgroup=True)
                    continue
                value = "/sys/fs/cgroup" + state["ControlGroup"]
                assert Path(value).name == name, state
            path = Path(value)
            try:
                path.stat()
            except FileNotFoundError:
                evidence["cgroups"][field] = dict(path=str(path), removed=True)
            else:
                value = (path / "pids.current").read_text().strip()
                if value != "0":
                    raise RuntimeError("owned cgroup remains populated")
                evidence["cgroups"][field] = dict(path=str(path), pids_current=0)
        except Exception as exc:
            errors.append(str(exc))
    for name in [config["worker_slice"], prefix + ".slice"]:
        try:
            evidence["slices"][name] = stop_owned(name)
        except Exception as exc:
            errors.append(str(exc))
    evidence["errors"] = errors
    evidence["status"] = "failed" if errors else "passed"
    Path(config["output"]).with_suffix(".cleanup.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(json.dumps(evidence))
    if errors:
        raise RuntimeError("owned cleanup evidence incomplete")


def ownership_preflight(run_id):
    prefix = "telescope52" + run_id
    directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    observations = []
    for name in [
        prefix + ".slice",
        prefix + "-workers.slice",
        prefix + "controller.service",
        prefix + "A.service",
        prefix + "B.service",
    ]:
        path = directory / name
        row = dict(
            name=name,
            path=str(path),
            path_exists=path.exists(),
            path_is_symlink=path.is_symlink(),
            manager=unit_state(name, detail=True),
        )
        observations.append(row)
        print(json.dumps(dict(ownership_observation=row)), flush=True)
    empty_state = dict(
        ActiveState="inactive", ControlGroup="", FragmentPath="", DropInPaths="", Transient="no"
    )
    for row in observations:
        state = row["manager"]
        allowed_load = {"not-found", "loaded"} if row["name"].endswith(".slice") else {"not-found"}
        if (
            row["path_exists"]
            or row["path_is_symlink"]
            or state["LoadState"] not in allowed_load
            or any(state[field] != value for field, value in empty_state.items())
        ):
            raise RuntimeError("proof resource name already owned")


def launch(args):
    prefix = "telescope52" + args.run_id
    config = dict(
        prefix=prefix,
        output=str(args.output),
        bwrap=str(args.bwrap),
        worker_slice=prefix + "-workers.slice",
    )
    directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    ownership_preflight(args.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    config_path = args.output.with_suffix(".launch.json")
    config_path.write_text(json.dumps(config))
    controller_name = prefix + "controller.service"
    try:
        for name, cpu, memory, tasks in [
            (prefix, "200%", 2147483648, 320),
            (prefix + "-workers", "100%", 1073741824, 256),
        ]:
            path = directory / (name + ".slice")
            if path.exists():
                raise RuntimeError("owned slice name already exists")
            path.write_text(
                f"[Unit]\nDescription=Owned Bubblewrap resource proof\n[Slice]\nCPUQuota={cpu}\nMemoryMax={memory}\nMemorySwapMax=0\nTasksMax={tasks}\n"
            )
            files.append(path)
        systemctl("daemon-reload")
        systemctl("start", prefix + ".slice", config["worker_slice"])
        config["aggregate_cgroup"] = "/sys/fs/cgroup" + systemctl(
            "show", prefix + ".slice", "-p", "ControlGroup", "--value"
        )
        config["worker_cgroup"] = "/sys/fs/cgroup" + systemctl(
            "show", config["worker_slice"], "-p", "ControlGroup", "--value"
        )
        assert Path(config["worker_cgroup"]).parent == Path(config["aggregate_cgroup"])
        config_path.write_text(json.dumps(config))
        print(
            json.dumps(
                dict(
                    launcher_uid=os.getuid(),
                    systemd=call(["systemd", "--version"]).stdout.splitlines()[0],
                    config=config,
                )
            ),
            flush=True,
        )
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--wait",
                "--pipe",
                "--unit=" + controller_name,
                "--slice=" + prefix + ".slice",
                "-p",
                "CPUQuota=100%",
                "-p",
                "MemoryMax=536870912",
                "-p",
                "MemorySwapMax=0",
                "-p",
                "TasksMax=64",
                "-p",
                "RuntimeMaxSec=120",
                "-p",
                "TimeoutStopSec=5",
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--controller",
                "--config",
                str(config_path),
            ],
            timeout=150,
        )
        if result.returncode:
            raise RuntimeError(f"owned controller exit {result.returncode}")
    finally:
        try:
            cleanup(config)
        finally:
            for path in files:
                path.unlink()
            systemctl("daemon-reload")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bwrap", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--unit")
    parser.add_argument("--controller", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--diagnostic-only", action="store_true")
    args = parser.parse_args()
    if args.diagnostic_only:
        ownership_preflight(args.run_id)
    elif args.cleanup:
        cleanup(json.loads(args.config.read_text()))
    elif args.unit:
        unit(json.loads(args.config.read_text()), args.unit)
    elif args.controller:
        controller(json.loads(args.config.read_text()))
    else:
        launch(args)


if __name__ == "__main__":
    main()
