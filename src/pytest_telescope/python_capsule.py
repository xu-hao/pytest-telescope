"""Prepare a clean CPython file closure without installing application code.

Assembly is inert after querying the explicitly selected trusted interpreter. A
backend must independently qualify the resulting bytes in an empty namespace.
"""

import hashlib
import json
import os
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from . import capsule, process
from .model import InfrastructureError, UnsupportedCapability, fingerprint

PREFIX = "/opt/telescope/python"
QUERY = r"""
import importlib.machinery, json, os, platform, sys, sysconfig
print(json.dumps(dict(schema=1, implementation=sys.implementation.name,
    version=list(sys.version_info[:3]), executable=os.path.realpath(sys.executable),
    base_prefix=os.path.realpath(sys.base_prefix), base_exec_prefix=os.path.realpath(sys.base_exec_prefix),
    stdlib=os.path.realpath(sysconfig.get_path('stdlib')),
    platstdlib=os.path.realpath(sysconfig.get_path('platstdlib')),
    extension_suffixes=importlib.machinery.EXTENSION_SUFFIXES,
    abi=sysconfig.get_config_var('SOABI'), platform=sys.platform,
    machine=platform.machine(), libc=os.confstr('CS_GNU_LIBC_VERSION')), sort_keys=True))
"""
QUERY_FIELDS = {
    "schema",
    "implementation",
    "version",
    "executable",
    "base_prefix",
    "base_exec_prefix",
    "stdlib",
    "platstdlib",
    "extension_suffixes",
    "abi",
    "platform",
    "machine",
    "libc",
}
EXCLUDED = {"site-packages", "dist-packages", "sitecustomize.py", "usercustomize.py"}


def _refuse(message):
    raise UnsupportedCapability("runtime:layout", message)


def describe(interpreter):
    interpreter = Path(interpreter)
    if not interpreter.is_absolute() or str(interpreter) != os.path.normpath(str(interpreter)):
        _refuse("select a canonical absolute CPython executable")
    data, mode, _ = capsule.regular_snapshot(interpreter, capsule.MAX_NATIVE, follow=True)
    if not mode.st_mode & 0o111 or mode.st_mode & (stat.S_ISUID | stat.S_ISGID):
        _refuse("CPython must be an executable without set-ID permissions")
    capsule._elf_bytes(data)
    code, output = process.capture(
        [str(interpreter), "-I", "-S", "-B", "-c", QUERY],
        30,
        env={"LC_ALL": "C.UTF-8", "PATH": "/nonexistent"},
        limit=65536,
    )
    if code:
        raise InfrastructureError("explicit CPython metadata query failed")
    try:
        source = json.loads(output)
        if type(source) is not dict or set(source) != QUERY_FIELDS:
            raise ValueError()
        version = source["version"]
        if (
            type(source["schema"]) is not int
            or source["schema"] != 1
            or source["implementation"] != "cpython"
            or source["platform"] != "linux"
            or source["machine"] != "x86_64"
            or type(source["libc"]) is not str
            or not source["libc"].startswith("glibc ")
            or type(version) is not list
            or len(version) != 3
            or any(type(value) is not int for value in version)
            or version[0] != 3
            or version[1] not in (11, 12, 13)
            or version[2] < 0
            or type(source["abi"]) is not str
            or not source["abi"]
        ):
            raise ValueError()
        for name in ("executable", "base_prefix", "base_exec_prefix", "stdlib", "platstdlib"):
            capsule._path(source[name])
            if source[name] == "/" or any(ord(char) < 32 for char in source[name]):
                raise ValueError()
        expected = f"python{version[0]}.{version[1]}"
        for name in ("stdlib", "platstdlib"):
            if Path(source[name]).name != expected or not any(
                Path(source[name]).is_relative_to(source[prefix])
                for prefix in ("base_prefix", "base_exec_prefix")
            ):
                raise ValueError()
        suffixes = source["extension_suffixes"]
        if (
            type(suffixes) is not list
            or not 1 <= len(suffixes) <= 8
            or any(
                type(value) is not str
                or not value.startswith(".")
                or "/" in value
                or len(value) > 128
                for value in suffixes
            )
        ):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise UnsupportedCapability(
            "runtime:layout", "unsupported CPython metadata or layout"
        ) from exc
    return source


@dataclass
class PythonCapsule:
    payload: capsule.Payload
    executable: str
    site: str
    source: dict
    wheel_hashes: list[str]
    timings: dict

    def identity(self):
        return fingerprint(
            dict(
                schema=1,
                payload=self.payload.identity(),
                source=self.source,
                executable=self.executable,
                site=self.site,
                wheels=self.wheel_hashes,
            )
        )


def _copy_host(payload, source):
    """Preserve admitted symlink topology, storing bytes only at real target paths."""
    source = Path(source)
    for _ in range(64):
        components = source.parts[1:]
        current = Path("/")
        for index, part in enumerate(components):
            current /= part
            mode = current.lstat()
            if stat.S_ISLNK(mode.st_mode):
                link = os.readlink(current)
                if len(os.fsencode(link)) > 4096:
                    _refuse("runtime symlink exceeds its bound")
                key = current.as_posix().lstrip("/")
                record = payload.records.get(key)
                if record is None:
                    payload.symlink(str(current), link)
                elif record != dict(kind="symlink", target=link, mtime_ns=0):
                    raise InfrastructureError("runtime alias changed during preparation")
                target = Path(link) if link.startswith("/") else current.parent / link
                source = Path(os.path.normpath(str(target.joinpath(*components[index + 1 :]))))
                break
        else:
            data, mode, _ = capsule.regular_snapshot(source, payload.max_bytes)
            key = source.as_posix().lstrip("/")
            existing = payload.records.get(key)
            expected = dict(
                kind="file",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                mode=stat.S_IMODE(mode.st_mode) & 0o555,
                mtime_ns=mode.st_mtime_ns,
            )
            if existing is None:
                payload.write(
                    str(source), data, mode=stat.S_IMODE(mode.st_mode), mtime_ns=mode.st_mtime_ns
                )
            elif existing != expected:
                raise InfrastructureError("runtime input changed during preparation")
            loadable = False
            if data.startswith(b"\x7fELF"):
                if len(data) < 18:
                    _refuse("truncated native runtime input")
                kind = struct.unpack_from("<H", data, 16)[0]
                if kind not in (1, 2, 3):
                    _refuse("unsupported native runtime object type")
                # ET_REL build objects in distro config directories are inert data.
                loadable = kind in (2, 3)
            return str(source), loadable
    _refuse("runtime symlink chain exceeds its supported bound")


def _stdlib_files(roots, limit):
    pending, seen, output = list(roots), set(), []
    visits = 0
    while pending:
        directory = pending.pop()
        resolved = directory.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        descriptor = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    visits += 1
                    if visits > limit:
                        raise UnsupportedCapability(
                            "runtime:storage", "stdlib entry ceiling exceeded"
                        )
                    if (
                        entry.name in EXCLUDED
                        or entry.name.endswith(".pth")
                        or entry.name.startswith(("sitecustomize.", "usercustomize."))
                    ):
                        continue
                    source = resolved / entry.name
                    mode = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISLNK(mode.st_mode):
                        target = source.resolve(strict=True)
                        if not any(target.is_relative_to(root) for root in roots):
                            # Distro libpython aliases are native inputs, not arbitrary
                            # external Python/data grants. Parse before admission.
                            native, _, _ = capsule.regular_snapshot(target, capsule.MAX_NATIVE)
                            capsule._elf_bytes(native)
                        if target.is_dir():
                            # Directory aliases require a separate explicit layout qualification.
                            _refuse("stdlib directory symlink is unsupported")
                        output.append(source)
                    elif stat.S_ISDIR(mode.st_mode):
                        pending.append(source)
                    elif stat.S_ISREG(mode.st_mode):
                        output.append(source)
                    else:
                        _refuse("stdlib contains a nonregular runtime input")
        finally:
            os.close(descriptor)
    return sorted(output)


def prepare(destination, interpreter, wheels, *, max_bytes=2 * 1024**3, max_entries=100_000):
    """Assemble one staged runtime; publication/retention and admission are caller-owned."""
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or max_bytes > 2 * 1024**3
        or type(max_entries) is not int
        or not 0 < max_entries <= 100_000
    ):
        _refuse("runtime storage ceiling is outside the supported profile")
    if type(wheels) is not list or len(wheels) > 256:
        _refuse("runtime requires a bounded explicit wheel list")
    started = time.monotonic()
    source = describe(interpreter)
    timings = {"query_seconds": time.monotonic() - started}
    payload = capsule.Payload(destination, max_bytes=max_bytes, max_entries=max_entries)
    roots = sorted({Path(source[name]) for name in ("stdlib", "platstdlib")})
    native = [Path(source["executable"])]
    try:
        phase = time.monotonic()
        _copy_host(payload, source["executable"])
        for path in _stdlib_files(roots, max_entries):
            _, executable = _copy_host(payload, path)
            if executable:
                native.append(path)
        timings["stdlib_seconds"] = time.monotonic() - phase
        phase = time.monotonic()
        closure = capsule.elf_closure(native)
        for path, record in closure.items():
            realpath, _ = _copy_host(payload, path)
            if payload.records[realpath.lstrip("/")]["sha256"] != record["sha256"]:
                raise InfrastructureError("native closure changed during preparation")
        timings["native_seconds"] = time.monotonic() - phase
        phase = time.monotonic()
        version = ".".join(map(str, source["version"][:2]))
        executable = PREFIX + "/bin/python"
        site = PREFIX + "/lib/python" + version + "/site-packages"
        payload.symlink(executable, source["executable"])
        payload.symlink(PREFIX + "/bin/python3", "python")
        payload.symlink(PREFIX + "/bin/python" + version, "python")
        config = (
            f"home = {Path(source['executable']).parent}\n"
            "include-system-site-packages = false\n"
            f"version = {'.'.join(map(str, source['version']))}\n"
            f"executable = {source['executable']}\n"
        )
        payload.write(PREFIX + "/pyvenv.cfg", config.encode())
        hashes = []
        for path in wheels:
            raw, _, _ = capsule.regular_snapshot(Path(path), max_bytes)
            hashes.append(payload.install_wheel(raw, site))
        # The fixed empty projection slot is part of this new runtime's identity.
        payload.write(site + "/zz_telescope_source_view.pth", b"")
        for name in payload.directories:
            directory = payload.root / name
            directory.chmod(0o755)
            os.utime(directory, ns=(0, 0))
        os.utime(payload.root, ns=(0, 0))
        timings["venv_wheels_seconds"] = time.monotonic() - phase
        phase = time.monotonic()
        payload.verify()
        timings["verification_seconds"] = time.monotonic() - phase
        timings["total_seconds"] = time.monotonic() - started
        return PythonCapsule(payload, executable, site, source, hashes, timings)
    except OSError as exc:
        raise InfrastructureError("runtime preparation failed within its storage envelope") from exc
