"""Prototype fresh-vs-fresh benchmark; not a production execution backend."""

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEPENDENCIES = {"pytest": "8.3.5", "packaging": "24.2", "pluggy": "1.5.0", "iniconfig": "2.0.0"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_record(path):
    info = path.lstat()
    common = {"mtime_ns": info.st_mtime_ns, "mode": info.st_mode & 0o7777}
    if stat.S_ISLNK(info.st_mode):
        return {**common, "symlink": os.readlink(path)}
    return {**common, "sha256": digest(path)}


def resolve_frozen_link(root, relative):
    pending = list(Path(relative).parts)
    resolved = []
    followed = 0
    while pending:
        part = pending.pop(0)
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        path = root.joinpath(*resolved, part)
        if path.is_symlink():
            followed += 1
            if followed > 64:
                raise RuntimeError("runtime symlink cycle")
            target = Path(os.readlink(path))
            if target.is_absolute():
                resolved = []
            pending = list(target.parts) + pending
        else:
            resolved.append(part)
    target = root.joinpath(*resolved)
    if not target.exists():
        raise RuntimeError("runtime symlink target is absent from frozen closure")
    return target


def runtime_manifest(destination, verify_source=False):
    result = {}
    for path in sorted(destination.rglob("*")):
        if path.is_symlink() or path.is_file():
            relative = str(path.relative_to(destination))
            result[relative] = runtime_record(path)
            if path.is_symlink():
                resolve_frozen_link(destination, relative)
            if verify_source:
                assert result[relative] == runtime_record(Path("/") / relative), relative
    return result


def copy_runtime(destination):
    """Freeze only this clean interpreter/venv plus their ELF dependencies, once."""
    started = time.perf_counter()
    prefixes = sorted({Path(sys.base_prefix), Path(sys.prefix)})
    if any(str(p) in {"/", "/usr", "/usr/local"} for p in prefixes):
        raise RuntimeError("benchmark requires a dedicated interpreter prefix")
    for prefix in prefixes:
        shutil.copytree(prefix, destination / str(prefix).lstrip("/"), symlinks=True)
    native = [Path(sys.executable).resolve()]
    for prefix in prefixes:
        native.extend(p for p in prefix.rglob("*.so*") if p.is_file())
    linked = subprocess.run(
        ["ldd", *map(str, sorted(set(native)))], capture_output=True, text=True, timeout=60
    )
    if linked.returncode or "not found" in linked.stdout:
        raise RuntimeError("native runtime closure could not be established")

    def copy_native(source):
        source = Path(os.path.normpath(source))
        target = destination / str(source).lstrip("/")
        if target.exists() or target.is_symlink():
            return
        for parent in target.parents:
            if parent == destination:
                break
            if parent.is_symlink():
                raise RuntimeError("native closure path crosses a copied directory symlink")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
            shutil.copystat(source, target, follow_symlinks=False)
            link = Path(os.readlink(source))
            copy_native(link if link.is_absolute() else source.parent / link)
        else:
            shutil.copy2(source, target)

    for name in sorted(set(re.findall(r"(?<!\S)(/[^\s():]+)", linked.stdout))):
        source = Path(name)
        if source.is_file():
            copy_native(source)
    for name in ["project", "tmp", "proc", "dev"]:
        (destination / name).mkdir(exist_ok=True)
    files = [p for p in destination.rglob("*") if not p.is_symlink() and p.is_file()]
    manifest = runtime_manifest(destination, verify_source=True)
    return {
        "seconds": time.perf_counter() - started,
        "files": len(files),
        "source_and_clone_bytes_mtime_mode_equal": True,
        "bytecode_policy": "Preserve copied runtime timestamps and modes; both executions use -I -B.",
        "bytes": sum(p.stat().st_size for p in files),
        "identity": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
    }


def snapshot(source, destination):
    started = time.perf_counter()
    destination.mkdir()
    manifest = {}
    for name in ["pytest.ini", "tests/test_work.py", "data.txt"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        target.chmod(0o444)
        os.utime(target, (0, 0))
        manifest[name] = digest(target)
    for p in [*destination.rglob("*"), destination]:
        if p.is_dir():
            os.utime(p, (0, 0))
    return time.perf_counter() - started, manifest


def command(runtime, view, scratch, payload, *, bwrap="/usr/bin/bwrap"):
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
    }
    argv = [
        str(bwrap),
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        str(runtime),
        "/",
        "--ro-bind",
        str(view),
        "/project",
        "--bind",
        str(scratch),
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        "/project",
        "--clearenv",
    ]
    for key, value in env.items():
        argv.extend(["--setenv", key, value])
    return [*argv, "--", sys.executable, "-I", "-B", *payload]


def execute(argv, cwd, scratch):
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
    }
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    host = time.process_time()
    started = time.perf_counter()
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    record = {
        "argv": argv,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "wall_seconds": wall,
        "controller_cpu_seconds": time.process_time() - host,
        "child_cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
    }
    if result.returncode:
        raise RuntimeError(json.dumps(record))
    return record


def summary(samples):
    values = [sample["total_seconds"] for sample in samples]
    return {
        "n": len(values),
        "median_seconds": statistics.median(values),
        "min_seconds": min(values),
        "max_seconds": max(values),
        "stdev_seconds": statistics.stdev(values),
        "median_child_cpu_seconds": statistics.median(s["child_cpu_seconds"] for s in samples),
        "median_controller_cpu_seconds": statistics.median(
            s["total_controller_cpu_seconds"] for s in samples
        ),
        "median_snapshot_seconds": statistics.median(s["snapshot_seconds"] for s in samples),
        "median_cleanup_seconds": statistics.median(s["cleanup_seconds"] for s in samples),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--diagnostic-only", action="store_true")
    parser.add_argument("--bwrap", type=Path, default=Path("/usr/bin/bwrap"))
    args = parser.parse_args()
    if not 4 <= args.samples <= 20:
        parser.error("samples must be between4 and20")
    args.bwrap = args.bwrap.resolve(strict=True)
    assert args.bwrap.is_file() and not args.bwrap.stat().st_mode & (stat.S_ISUID | stat.S_ISGID)
    args.output.mkdir(parents=True, exist_ok=False)
    import importlib.metadata

    group = next(
        line.split(":", 2)[2]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
    enforced = {
        name: (cgroup / name).read_text().strip() for name in ["cpu.max", "memory.max", "pids.max"]
    }
    quota, period = map(int, enforced["cpu.max"].split())
    assert (
        quota == 2 * period
        and enforced["memory.max"] == "2147483648"
        and enforced["pids.max"] == "128"
    ), enforced
    scratch_stat = os.statvfs(args.scratch)
    assert scratch_stat.f_blocks * scratch_stat.f_frsize == 134217728
    assert scratch_stat.f_files == 32768 and scratch_stat.f_flag & os.ST_NOEXEC
    assert len(os.sched_getaffinity(0)) == 2
    versions = {name: importlib.metadata.version(name) for name in DEPENDENCIES}
    assert versions == DEPENDENCIES, versions
    report = {
        "status": "incomplete",
        "prototype": True,
        "cache": "disabled on all paths",
        "python": sys.version,
        "dependencies": versions,
        "platform": platform.platform(),
        "kernel": platform.release(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "bwrap": subprocess.check_output([str(args.bwrap), "--version"], text=True).strip(),
        "bwrap_sha256": digest(args.bwrap),
        "bwrap_executable": str(args.bwrap),
        "samples": [],
        "enforced_cgroup": enforced,
        "scratch_noexec": True,
        "resources": {
            "cpu": 2,
            "memory_bytes": 2147483648,
            "scratch_bytes": 134217728,
            "scratch_inodes": 32768,
            "processes": 128,
        },
        "limitations": [
            "Hosted Linux prototype numbers, not local workstation numbers or a finished backend.",
            "No result cache; operating-system page caches are uncontrolled and shared by interleaving.",
            "Runtime copy/hash is one-time; per-run project copy/hash and cleanup are separately measured and included in total.",
            "The128MiB/inode bound covers private scratch only; frozen runtime and report output use separate runner disk.",
            "No claim of complete lifecycle, cancellation, cache, metadata or mixed-routing qualification.",
            "The32-case path uses32 fresh sessions and snapshots; its fixture lifetime differs from one ordinary or sandbox batch session.",
        ],
    }
    if args.diagnostic_only:
        report["diagnostic_only"] = True
        report["unit_security"] = {
            "apparmor": Path("/proc/self/attr/current").read_text().strip(),
            "process": {
                line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                for line in Path("/proc/self/status").read_text().splitlines()
                if line.startswith(("NoNewPrivs:", "Seccomp:", "Seccomp_filters:", "Cap"))
            },
        }
    source = args.output / "source"
    (source / "tests").mkdir(parents=True)
    data = b"0123456789abcdef" * 2048
    (source / "data.txt").write_bytes(data)
    (source / "undeclared.txt").write_text("synthetic omitted input\n")
    (source / "pytest.ini").write_text("[pytest]\naddopts = --strict-markers --strict-config\n")
    (source / "tests/test_work.py").write_text(
        "import hashlib\nfrom pathlib import Path\nimport pytest\n"
        "def test_single():\n    assert 2 + 2 == 4\n"
        "@pytest.mark.parametrize('n', range(32))\n"
        "def test_batch(n):\n    assert sum(range(n + 1)) == n * (n + 1) // 2\n"
        "def test_work():\n    data = Path('data.txt').read_bytes()\n"
        "    for _ in range(4096):\n        result = hashlib.sha256(data).hexdigest()\n"
        f"    assert result == {hashlib.sha256(data).hexdigest()!r}\n"
        "    assert sum(n * n for n in range(200000)) == 2666646666700000\n"
    )
    runtime = args.output / "runtime"
    report["runtime_preparation"] = copy_runtime(runtime)
    fixed_view = args.output / "fixed-view"
    _, report["project_manifest"] = snapshot(source, fixed_view)
    preflight = r"""
import ctypes, errno, json, os, socket, subprocess, sys
from pathlib import Path
assert Path('data.txt').read_bytes() == b'0123456789abcdef' * 2048
libc=ctypes.CDLL(None, use_errno=True)
for name in ['undeclared.txt', HOST_SOURCE + '/undeclared.txt']:
    assert not Path(name).exists()
    try: Path(name).read_bytes()
    except FileNotFoundError: pass
    else: raise AssertionError('Python read escaped view')
    try: os.stat(name)
    except FileNotFoundError: pass
    else: raise AssertionError('stat escaped view')
    assert libc.open(name.encode(), os.O_RDONLY) == -1 and ctypes.get_errno() == errno.ENOENT
    assert libc.access(name.encode(), os.F_OK) == -1 and ctypes.get_errno() == errno.ENOENT
    assert libc.stat(name.encode(), ctypes.create_string_buffer(512)) == -1 and ctypes.get_errno() == errno.ENOENT
    child=subprocess.run([sys.executable, '-I', '-B', '-c', 'import os,sys; assert not os.path.exists(sys.argv[1]); open(sys.argv[1]).read()', name], capture_output=True)
    assert child.returncode != 0 and b'FileNotFoundError' in child.stderr
try: os.chmod('data.txt', 0o600)
except OSError as error: assert error.errno == errno.EROFS
else: raise AssertionError('project metadata was writable')
try: Path('data.txt').write_text('changed')
except OSError as error: assert error.errno in (errno.EROFS, errno.EACCES)
else: raise AssertionError('project was writable')
Path('/tmp/private.txt').write_text('private')
assert Path('/tmp/private.txt').read_text() == 'private'
assert [name for _,name in socket.if_nameindex()] == ['lo']
assert not any(line.split()[1] == '00000000' for line in Path('/proc/net/route').read_text().splitlines()[1:])
print(json.dumps({'read':True,'stat':True,'native':True,'subprocess':True,'readonly':True,'scratch':True,'network_namespace':True}))
"""
    scratch = args.scratch / "preflight"
    scratch.mkdir()
    try:
        (source / "data.txt").write_bytes(b"changed after snapshot")
        report["preflight"] = execute(
            command(
                runtime,
                fixed_view,
                scratch,
                ["-c", f"HOST_SOURCE={str(source)!r}\n" + preflight],
                bwrap=args.bwrap,
            ),
            source,
            scratch,
        )
        (source / "data.txt").write_bytes(data)
        if args.diagnostic_only:
            report["status"] = "passed-preflight-only"
            return
        for workload, selector, count in [
            ("single", "tests/test_work.py::test_single", 1),
            ("batch", "tests/test_work.py::test_batch", 32),
            ("work", "tests/test_work.py::test_work", 1),
            ("bare", None, None),
        ]:
            for iteration in range(args.samples):
                modes = ["plain", "bwrap-launch", "bwrap-freeze"]
                if workload == "batch" and iteration < 6:
                    modes.extend(["plain-cases", "bwrap-cases"])
                if iteration % 2:
                    modes.reverse()
                for mode in modes:
                    name = f"{workload}-{iteration}-{mode}"
                    private = args.scratch / name
                    started = time.perf_counter()
                    host_started = time.process_time()
                    private.mkdir()
                    freeze = 0.0
                    cleanup = 0.0
                    view = fixed_view
                    if mode == "bwrap-freeze":
                        view = args.output / name
                        freeze, manifest = snapshot(source, view)
                        assert manifest == report["project_manifest"]
                    payload = (
                        ["-c", "pass"]
                        if selector is None
                        else ["-m", "pytest", "-q", "-p", "no:cacheprovider", selector]
                    )
                    argv = (
                        [sys.executable, "-I", "-B", *payload]
                        if mode == "plain"
                        else command(runtime, view, private, payload, bwrap=args.bwrap)
                    )
                    if mode in {"plain-cases", "bwrap-cases"}:
                        children = []
                        for ordinal in range(32):
                            case_view = args.output / f"{name}-{ordinal}"
                            case_scratch = private / str(ordinal)
                            case_scratch.mkdir()
                            if mode == "bwrap-cases":
                                elapsed, manifest = snapshot(source, case_view)
                                freeze += elapsed
                                assert manifest == report["project_manifest"]
                            case_payload = [
                                "-m",
                                "pytest",
                                "-q",
                                "-p",
                                "no:cacheprovider",
                                f"{selector}[{ordinal}]",
                            ]
                            child = execute(
                                (
                                    [sys.executable, "-I", "-B", *case_payload]
                                    if mode == "plain-cases"
                                    else command(
                                        runtime,
                                        case_view,
                                        case_scratch,
                                        case_payload,
                                        bwrap=args.bwrap,
                                    )
                                ),
                                source,
                                case_scratch,
                            )
                            assert re.search(r"\b1 passed\b", child["stdout"]), child
                            children.append(child)
                            cleanup_started = time.perf_counter()
                            if mode == "bwrap-cases":
                                shutil.rmtree(case_view)
                            cleanup += time.perf_counter() - cleanup_started
                        result = {
                            "children": children,
                            "exit_code": 0,
                            "stdout": "32 passed across32 separate sessions",
                            "stderr": "",
                            "wall_seconds": sum(c["wall_seconds"] for c in children),
                            "child_cpu_seconds": sum(c["child_cpu_seconds"] for c in children),
                            "controller_cpu_seconds": sum(
                                c["controller_cpu_seconds"] for c in children
                            ),
                        }
                    else:
                        result = execute(argv, source, private)
                    if count is not None:
                        assert re.search(rf"\b{count} passed\b", result["stdout"]), result
                    cleanup_started = time.perf_counter()
                    shutil.rmtree(private)
                    if mode == "bwrap-freeze":
                        shutil.rmtree(view)
                    cleanup += time.perf_counter() - cleanup_started
                    result.update(
                        workload=workload,
                        iteration=iteration,
                        mode=mode,
                        snapshot_seconds=freeze,
                        cleanup_seconds=cleanup,
                        total_seconds=time.perf_counter() - started,
                        total_controller_cpu_seconds=time.process_time() - host_started,
                    )
                    report["samples"].append(result)
                    print(
                        json.dumps(
                            {
                                key: result[key]
                                for key in [
                                    "workload",
                                    "iteration",
                                    "mode",
                                    "exit_code",
                                    "total_seconds",
                                    "snapshot_seconds",
                                    "cleanup_seconds",
                                ]
                            }
                        ),
                        flush=True,
                    )
        final_manifest = runtime_manifest(runtime, verify_source=True)
        final_identity = hashlib.sha256(
            json.dumps(final_manifest, sort_keys=True).encode()
        ).hexdigest()
        assert final_identity == report["runtime_preparation"]["identity"]
        report["runtime_unchanged_after_execution"] = True
        report["summary"] = {}
        for workload in ["single", "batch", "work", "bare"]:
            modes = ["plain", "bwrap-launch", "bwrap-freeze"] + (
                ["plain-cases", "bwrap-cases"] if workload == "batch" else []
            )
            rows = {
                mode: summary(
                    [
                        s
                        for s in report["samples"]
                        if s["workload"] == workload and s["mode"] == mode
                    ]
                )
                for mode in modes
            }
            plain_by_iteration = {
                row["iteration"]: row
                for row in report["samples"]
                if row["workload"] == workload and row["mode"] == "plain"
            }
            for mode in modes[1:]:
                selected = [
                    row
                    for row in report["samples"]
                    if row["workload"] == workload and row["mode"] == mode
                ]
                paired_plain = [
                    plain_by_iteration[row["iteration"]]["total_seconds"] for row in selected
                ]
                deltas = [
                    1000 * (row["total_seconds"] - plain)
                    for row, plain in zip(selected, paired_plain, strict=True)
                ]
                ratios = [
                    row["total_seconds"] / plain
                    for row, plain in zip(selected, paired_plain, strict=True)
                ]
                rows[mode]["paired_comparison"] = {
                    "iterations": [row["iteration"] for row in selected],
                    "plain_median_seconds": statistics.median(paired_plain),
                    "median_added_ms": statistics.median(deltas),
                    "min_added_ms": min(deltas),
                    "max_added_ms": max(deltas),
                    "stdev_added_ms": statistics.stdev(deltas),
                    "median_ratio": statistics.median(ratios),
                    "min_ratio": min(ratios),
                    "max_ratio": max(ratios),
                    "stdev_ratio": statistics.stdev(ratios),
                    "median_slowdown_percent": 100 * (statistics.median(ratios) - 1),
                }
            if workload == "batch":
                plain_cases = {
                    row["iteration"]: row
                    for row in report["samples"]
                    if row["workload"] == workload and row["mode"] == "plain-cases"
                }
                wrapped_cases = [
                    row
                    for row in report["samples"]
                    if row["workload"] == workload and row["mode"] == "bwrap-cases"
                ]
                deltas = [
                    1000 * (row["total_seconds"] - plain_cases[row["iteration"]]["total_seconds"])
                    for row in wrapped_cases
                ]
                ratios = [
                    row["total_seconds"] / plain_cases[row["iteration"]]["total_seconds"]
                    for row in wrapped_cases
                ]
                rows["bwrap-cases"]["matched_session_comparison"] = {
                    "iterations": [row["iteration"] for row in wrapped_cases],
                    "plain_median_seconds": statistics.median(
                        row["total_seconds"] for row in plain_cases.values()
                    ),
                    "median_added_ms": statistics.median(deltas),
                    "min_added_ms": min(deltas),
                    "max_added_ms": max(deltas),
                    "stdev_added_ms": statistics.stdev(deltas),
                    "median_ratio": statistics.median(ratios),
                    "min_ratio": min(ratios),
                    "max_ratio": max(ratios),
                    "stdev_ratio": statistics.stdev(ratios),
                    "median_slowdown_percent": 100 * (statistics.median(ratios) - 1),
                }
            report["summary"][workload] = rows
        report["status"] = "passed"
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "detail": str(error)}
        report["status"] = "failed-preflight-or-measurement"
        raise
    finally:
        shutil.rmtree(scratch)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "runtime_preparation": report["runtime_preparation"],
                "summary": report.get("summary"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
