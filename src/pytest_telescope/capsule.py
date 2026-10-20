"""Bounded immutable runtime files and inert ELF/wheel inspection.

These primitives never execute a copied program, resolve a dependency from PATH, or
inspect application source. Native execution qualification belongs to the backend.
"""

import hashlib
import io
import json
import os
import stat
import struct
import zipfile
from pathlib import Path, PurePosixPath

from .model import InfrastructureError, UnsupportedCapability

MAX_NATIVE = 64 * 1024 * 1024
MAX_NATIVE_FILES = 4096
PLATFORM_LIBRARIES = (
    "/lib/x86_64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu",
    "/lib64",
    "/usr/lib64",
    "/lib",
    "/usr/lib",
)


def _unsupported(message):
    raise UnsupportedCapability("runtime:layout", message)


def _path(name):
    if (
        type(name) is not str
        or not name.startswith("/")
        or "\x00" in name
        or "\\" in name
        or any(part in (".", "..") for part in name.split("/"))
        or str(PurePosixPath(name)) != name
    ):
        _unsupported("runtime requires canonical absolute paths")
    return name.removeprefix("/")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def regular_snapshot(path, limit, *, follow=False):
    """One bounded descriptor read, detecting replacement/growth before accepting bytes."""
    descriptor = None
    try:
        realpath = str(Path(path).resolve(strict=True)) if follow else str(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            _unsupported("runtime input must be a bounded regular file")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=follow)

        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                value.st_mode,
            )

        if (
            len(data) != before.st_size
            or len(data) > limit
            or identity(before) != identity(after)
            or identity(after) != identity(current)
            or (follow and identity(os.stat(realpath)) != identity(after))
        ):
            raise InfrastructureError("runtime source changed during bounded inspection")
        return bytes(data), before, realpath
    except OSError as exc:
        raise InfrastructureError("runtime source changed or is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class Payload:
    """One owned staged runtime, with deterministic content and storage accounting."""

    def __init__(self, root, *, max_bytes=2 * 1024**3, max_entries=100_000):
        self.root = Path(root)
        self.root.mkdir(mode=0o700)
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.bytes_used = 0
        self.records = {}
        self.directories = set()

    def _reserve(self, entries):
        names, directories, size = set(self.records), set(self.directories), self.bytes_used
        for name, length in entries:
            relative = _path(name)
            if relative in names or relative in directories:
                _unsupported("runtime payload has conflicting paths")
            names.add(relative)
            size += length
            for parent in PurePosixPath(relative).parents:
                if str(parent) != ".":
                    if str(parent) in names:
                        _unsupported("runtime payload has a file ancestor")
                    directories.add(str(parent))
        if size > self.max_bytes or len(names) + len(directories) > self.max_entries:
            raise UnsupportedCapability("runtime:storage", "runtime storage envelope exceeded")

    def write(self, name, data, *, mode=0o444, mtime_ns=0):
        self._reserve([(name, len(data))])
        relative = _path(name)
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(mode & 0o555)
        os.utime(target, ns=(mtime_ns, mtime_ns))
        self.records[relative] = dict(
            kind="file", size=len(data), sha256=_digest(data), mode=mode & 0o555, mtime_ns=mtime_ns
        )
        self.bytes_used += len(data)
        self.directories.update(str(p) for p in PurePosixPath(relative).parents if str(p) != ".")

    def copy_file(self, source, target):
        data, before, _ = regular_snapshot(source, self.max_bytes - self.bytes_used)
        self.write(target, data, mode=stat.S_IMODE(before.st_mode), mtime_ns=before.st_mtime_ns)

    def symlink(self, name, target):
        if type(target) is not str or not target or "\x00" in target:
            _unsupported("runtime symlink target is invalid")
        relative = _path(name)
        # Resolve lexically inside the runtime root, never against host filesystem links.
        normalized = os.path.normpath(
            target if target.startswith("/") else str(PurePosixPath(name).parent / target)
        )
        _path(normalized)
        self._reserve([(name, len(os.fsencode(target)))])
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        os.utime(path, ns=(0, 0), follow_symlinks=False)
        self.records[relative] = dict(kind="symlink", target=target, mtime_ns=0)
        self.bytes_used += len(os.fsencode(target))
        self.directories.update(str(p) for p in PurePosixPath(relative).parents if str(p) != ".")

    def install_wheel(self, raw, site):
        _path(site)
        if len(raw) > self.max_bytes:
            raise UnsupportedCapability("runtime:storage", "runtime storage envelope exceeded")
        staged = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > self.max_entries:
                    raise UnsupportedCapability(
                        "runtime:storage", "runtime storage envelope exceeded"
                    )
                for info in infos:
                    name = info.filename
                    if info.is_dir():
                        continue
                    path = PurePosixPath(name)
                    if (
                        name.startswith("/")
                        or str(path) != name
                        or any(part in (".", "..") for part in name.split("/"))
                        or "\\" in name
                        or "\x00" in name
                        or path.suffix.lower() in (".pth", ".pyc", ".pyo")
                        or path.name.lower() in ("sitecustomize.py", "usercustomize.py")
                        or any(part.endswith(".data") for part in path.parts)
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or info.flag_bits & 1
                    ):
                        _unsupported("wheel contains unsupported paths, startup code or layout")
                    staged.append((site + "/" + name, info))
                # Validate the entire archive before writing any member.
                self._reserve([(name, info.file_size) for name, info in staged])
                for name, info in staged:
                    data = archive.read(info)
                    if len(data) != info.file_size:
                        raise InfrastructureError("wheel payload integrity mismatch")
                    self.write(name, data, mode=0o444)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise InfrastructureError("wheel payload integrity or encoding is invalid") from exc
        return _digest(raw)

    def manifest(self):
        return dict(
            schema=1,
            directories=sorted(self.directories),
            files={name: self.records[name] for name in sorted(self.records)},
        )

    def identity(self):
        return _digest(json.dumps(self.manifest(), sort_keys=True, separators=(",", ":")).encode())

    def verify(self):
        observed, directories = set(), set()
        pending = [self.root]
        visited = total = 0
        try:
            while pending:
                directory = pending.pop()
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            visited += 1
                            if visited > self.max_entries:
                                raise InfrastructureError(
                                    "runtime content changed beyond entry bound"
                                )
                            path = directory / entry.name
                            relative = path.relative_to(self.root).as_posix()
                            value = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                            if stat.S_ISDIR(value.st_mode):
                                if relative not in self.directories:
                                    raise InfrastructureError("runtime content changed")
                                directories.add(relative)
                                pending.append(path)
                                continue
                            observed.add(relative)
                            record = self.records.get(relative)
                            if record is None:
                                raise InfrastructureError("runtime content changed")
                            if record["kind"] == "symlink":
                                valid = (
                                    stat.S_ISLNK(value.st_mode)
                                    and os.readlink(path) == record["target"]
                                )
                                total += len(os.fsencode(record["target"]))
                            else:
                                data, value, _ = regular_snapshot(path, self.max_bytes - total)
                                total += len(data)
                                valid = (
                                    value.st_size == record["size"]
                                    and stat.S_IMODE(value.st_mode) == record["mode"]
                                    and _digest(data) == record["sha256"]
                                )
                            if (
                                not valid
                                or value.st_mtime_ns != record["mtime_ns"]
                                or total > self.max_bytes
                            ):
                                raise InfrastructureError("runtime content changed")
                finally:
                    os.close(descriptor)
            if observed != set(self.records) or directories != self.directories:
                raise InfrastructureError("runtime content changed")
        except (OSError, UnsupportedCapability) as exc:
            raise InfrastructureError("runtime content changed or exceeded its bound") from exc


def elf_info(path):
    """Inspect exactly one bounded regular native snapshot, without executing it."""
    data, _, _ = regular_snapshot(path, MAX_NATIVE, follow=True)
    return _elf_bytes(data)


def _elf_bytes(data):
    """Read ELF64 little-endian x86-64 dynamic metadata from admitted bytes."""
    try:
        if data[:7] != b"\x7fELF\x02\x01\x01" or len(data) < 64:
            raise ValueError()
        _, machine, _, _, phoff, _, _, _, phsize, phnum, *_ = struct.unpack_from(
            "<HHIQQQIHHHHHH", data, 16
        )
        if machine != 62 or phsize != 56 or phnum > 4096 or phoff + phsize * phnum > len(data):
            raise ValueError()
        segments = [
            struct.unpack_from("<IIQQQQQQ", data, phoff + index * phsize) for index in range(phnum)
        ]
        dynamic, interpreter = [], None
        for kind, _, offset, _, _, size, _, _ in segments:
            if offset + size > len(data):
                raise ValueError()
            if kind == 3:
                if interpreter is not None or size > 4096:
                    raise ValueError()
                interpreter = data[offset : offset + size].rstrip(b"\0").decode("ascii")
                _path(interpreter)
            elif kind == 2:
                if size > 1024 * 1024 or size % 16:
                    raise ValueError()
                dynamic = [
                    struct.unpack_from("<qQ", data, pos) for pos in range(offset, offset + size, 16)
                ]
        strings = [value for tag, value in dynamic if tag == 5]
        sizes = [value for tag, value in dynamic if tag == 10]
        if not dynamic:
            return dict(interpreter=interpreter, needed=[], rpath=[])
        if len(strings) != 1 or len(sizes) != 1 or sizes[0] > MAX_NATIVE:
            raise ValueError()
        tables = [
            offset + strings[0] - address
            for kind, _, offset, address, _, size, _, _ in segments
            if kind == 1 and address <= strings[0] and strings[0] + sizes[0] <= address + size
        ]
        if len(tables) != 1:
            raise ValueError()
        table = data[tables[0] : tables[0] + sizes[0]]

        def value_at(offset):
            end = table.find(b"\0", offset)
            if offset >= len(table) or end < 0 or end - offset > 4096:
                raise ValueError()
            return table[offset:end].decode("ascii")

        needed = [value_at(value) for tag, value in dynamic if tag == 1]
        runpath = [value_at(value) for tag, value in dynamic if tag == 29]
        rpath = [value_at(value) for tag, value in dynamic if tag == 15]
        if len(runpath) > 1 or len(rpath) > 1 or len(needed) > 256:
            raise ValueError()
        if any("/" in name or not name for name in needed):
            raise ValueError()
        return dict(
            interpreter=interpreter,
            needed=needed,
            rpath=(runpath or rpath or [""])[0].split(":") if runpath or rpath else [],
        )
    except (ValueError, UnicodeError, struct.error, IndexError) as exc:
        raise UnsupportedCapability(
            "runtime:layout", "unsupported or malformed ELF layout"
        ) from exc


def elf_closure(paths):
    pending = [Path(path).absolute() for path in paths]
    result = {}
    while pending:
        path = pending.pop()
        lexical = str(path)
        if lexical in result:
            continue
        if len(result) >= MAX_NATIVE_FILES:
            _unsupported("ELF closure exceeds its file bound")
        data, _, realpath = regular_snapshot(path, MAX_NATIVE, follow=True)
        info = _elf_bytes(data)
        result[lexical] = dict(realpath=realpath, sha256=_digest(data))
        if info["interpreter"]:
            pending.append(Path(info["interpreter"]))
        searches = []
        for entry in info["rpath"]:
            entry = entry.replace("${ORIGIN}", str(Path(realpath).parent)).replace(
                "$ORIGIN", str(Path(realpath).parent)
            )
            if "$" in entry or not entry.startswith("/"):
                _unsupported("ELF search path requires a supported absolute origin")
            searches.append(Path(os.path.normpath(entry)))
        for name in info["needed"]:
            preferred = [directory / name for directory in searches if (directory / name).is_file()]
            candidates = preferred[:1] or [
                Path(directory) / name
                for directory in PLATFORM_LIBRARIES
                if (Path(directory) / name).is_file()
            ]
            if not candidates or len({str(candidate.resolve()) for candidate in candidates}) != 1:
                _unsupported("ELF dependency has no unique supported resolution")
            pending.append(candidates[0])
    return dict(sorted(result.items()))
