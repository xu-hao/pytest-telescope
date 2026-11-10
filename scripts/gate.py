"""Bounded gate stages and installed-candidate provenance (standard library only)."""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import sysconfig
import time
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

MAX_OUTPUT = 8 * 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024
MAX_PACKAGE = 64 * 1024 * 1024
MAX_MEMBER = 16 * 1024 * 1024
MAX_MEMBERS = 4096


def stable(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_identity(path, maximum=MAX_MEMBER):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ValueError("gate input is not a bounded regular file")
    result = hashlib.sha256()
    count = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if stable(os.fstat(stream.fileno())) != stable(before):
            raise ValueError("gate input changed while opening")
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > maximum:
                raise ValueError("gate input exceeded its bound")
            result.update(chunk)
        if stable(os.fstat(stream.fileno())) != stable(before) or stable(path.lstat()) != stable(
            before
        ):
            raise ValueError("gate input changed while reading")
    return {"bytes": count, "sha256": result.hexdigest()}


def package_files(root, *, source=False):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("candidate package directory is unavailable")
    files, directories, pending = {}, set(), [root]
    visits = total = 0
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            visits += 1
            if visits > MAX_MEMBERS:
                raise ValueError("candidate package membership exceeds its bound")
            if source and (path.name == "__pycache__" or path.suffix == ".pyc"):
                continue
            kind = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(kind):
                directories.add(relative)
                pending.append(path)
            elif stat.S_ISREG(kind) and path.suffix not in (".pyc", ".pyo"):
                identity = file_identity(path)
                total += identity["bytes"]
                if total > MAX_PACKAGE:
                    raise ValueError("candidate package bytes exceed their bound")
                files[relative] = identity
            else:
                raise ValueError("candidate package has bytecode, links or special files")
    return {"files": files, "directories": sorted(directories)}


def wheel_identity(path):
    identity = file_identity(path, MAX_PACKAGE)
    files, directories, versions = {}, set(), []
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or len({row.filename for row in members}) != len(members):
            raise ValueError("wheel has excessive or duplicate members")
        for row in members:
            name = PurePosixPath(row.filename)
            if (
                not name.parts
                or name.is_absolute()
                or ".." in name.parts
                or "\\" in row.filename
                or name.as_posix() != row.filename.removesuffix("/")
            ):
                raise ValueError("wheel member path is invalid")
            total += row.file_size
            if row.file_size > MAX_MEMBER or total > MAX_PACKAGE:
                raise ValueError("wheel metadata exceeds its bounds")
            mode = row.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("wheel contains a nonregular member")
            if name.parts[0] == "pytest_telescope":
                relative = name.relative_to("pytest_telescope").as_posix()
                if relative == ".":
                    continue
                for parent in PurePosixPath(relative).parents:
                    if str(parent) != ".":
                        directories.add(str(parent))
                if row.is_dir():
                    directories.add(relative)
                else:
                    if name.suffix in (".pyc", ".pyo"):
                        raise ValueError("candidate wheel contains producer bytecode")
                    content = archive.read(row)
                    files[relative] = {
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
            elif name.name == "METADATA" and name.parent.name.endswith(".dist-info"):
                message = BytesParser().parsebytes(archive.read(row))
                if (
                    message.defects
                    or message.get_all("Name", []) != ["pytest-telescope"]
                    or len(message.get_all("Version", [])) != 1
                ):
                    raise ValueError("candidate wheel metadata disagrees")
                versions.append(message["Version"])
    if (
        len(versions) != 1
        or "__init__.py" not in files
        or file_identity(path, MAX_PACKAGE) != identity
    ):
        raise ValueError("candidate wheel is incomplete or changed")
    return {
        **identity,
        "package": {"files": files, "directories": sorted(directories)},
        "version": versions[0],
    }


def source_identity(root):
    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, timeout=30
        )
        if len(result.stdout) > MAX_METADATA or len(result.stderr) > MAX_METADATA:
            raise ValueError("source identity output exceeds its bound")
        return result.stdout.decode().strip()

    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("installed gate requires a clean candidate checkout")
    head, tree = git("rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    return {"head": head, "tree": tree}


def interpreter_identity():
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "hexversion": sys.hexversion,
        "binary": file_identity(Path(sys.executable).resolve(strict=True), 512 * 1024 * 1024),
    }


def node_identity(value):
    if not value or not Path(value).is_absolute():
        raise ValueError("select an existing absolute Node executable")
    actual = Path(value).resolve(strict=True)
    mode = stat.S_IMODE(actual.stat().st_mode)
    if not mode & 0o111 or not os.access(actual, os.R_OK | os.X_OK):
        raise ValueError("selected Node file is not executable")
    return {
        "path": str(Path(value)),
        "actual": str(actual),
        "mode": mode,
        "file": file_identity(actual, 512 * 1024 * 1024),
    }


def candidate_binding(root, wheel, node):
    root, wheel = Path(root).resolve(), Path(wheel).resolve()
    identity = wheel_identity(wheel)
    with (root / "pyproject.toml").open("rb") as stream:
        project = stream.read(MAX_METADATA + 1)
    if len(project) > MAX_METADATA:
        raise ValueError("candidate project metadata exceeds its bound")
    version = tomllib.loads(project.decode())["project"]["version"]
    if (
        identity["version"] != version
        or package_files(root / "src/pytest_telescope", source=True) != identity["package"]
    ):
        raise ValueError("wheel bytes or version differ from the candidate checkout")
    return {
        "schema": 1,
        "root": str(root),
        "source": source_identity(root),
        "wheel_path": str(wheel),
        "wheel": identity,
        "interpreter": interpreter_identity(),
        "node": node_identity(node),
    }


def verify_candidate(binding, *, first_import=False):
    if (
        type(binding) is not dict
        or set(binding)
        != {"schema", "root", "source", "wheel_path", "wheel", "interpreter", "node"}
        or type(binding["schema"]) is not int
        or binding["schema"] != 1
    ):
        raise ValueError("invalid candidate binding")
    if (
        candidate_binding(binding["root"], binding["wheel_path"], binding["node"]["path"])
        != binding
    ):
        raise ValueError("candidate source, wheel, interpreter or Node changed")
    if first_import and any(
        name == "pytest_telescope" or name.startswith("pytest_telescope.") for name in sys.modules
    ):
        raise ValueError("candidate producer was imported before its guard")
    spec = importlib.util.find_spec("pytest_telescope")
    if spec is None or spec.origin is None or spec.submodule_search_locations is None:
        raise ValueError("candidate package has no supported installed origin")
    origin = Path(spec.origin)
    locations = [Path(value) for value in spec.submodule_search_locations]
    allowed = {
        Path(sysconfig.get_path(name)).resolve() / "pytest_telescope"
        for name in ("purelib", "platlib")
    }
    package = origin.parent
    if (
        origin.name != "__init__.py"
        or package not in allowed
        or locations != [package]
        or not package.resolve().is_relative_to(Path(sys.prefix).resolve())
    ):
        raise ValueError("candidate import is not the exact interpreter's installed package")
    distribution = importlib.metadata.distribution("pytest-telescope")
    if (
        distribution.version != binding["wheel"]["version"]
        or Path(distribution.locate_file("pytest_telescope/__init__.py")) != origin
        or package_files(package) != binding["wheel"]["package"]
    ):
        raise ValueError("installed candidate membership, bytes or version disagree")
    for name, module in tuple(sys.modules.items()):
        if name == "pytest_telescope" or name.startswith("pytest_telescope."):
            filename = getattr(module, "__file__", None)
            if filename is None or not Path(filename).is_relative_to(package):
                raise ValueError("loaded producer module has a substituted origin")
            relative = Path(filename).relative_to(package).as_posix()
            if relative not in binding["wheel"]["package"]["files"]:
                raise ValueError("loaded producer module is outside the exact wheel")
    return str(origin)


def write_json(path, value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(content) > MAX_METADATA:
        raise ValueError("gate evidence exceeds its metadata bound")
    with Path(path).open("xb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest()


def read_json(path, expected):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate gate evidence field")
            result[key] = value
        return result

    with Path(path).open("rb") as stream:
        content = stream.read(MAX_METADATA + 1)
    if len(content) > MAX_METADATA or hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("gate evidence is missing, changed or oversized")

    def invalid_constant(value):
        raise ValueError("nonfinite gate evidence value")

    return json.loads(content, object_pairs_hook=unique, parse_constant=invalid_constant)


class Stages:
    """One bounded owned process group and exclusive complete evidence per stage."""

    def __init__(self, output, root):
        self.output, self.root = Path(output), Path(root)

    def run(self, label, *args, timeout=300, env=None):
        command = [sys.executable, *args]
        started = time.monotonic()
        total, error, process = 0, None, None
        status, eof = None, False
        with (self.output / (label + ".log")).open("xb") as log:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError("gate stage deadline exceeded")
                        for key, _ in selector.select(0.1):
                            content = os.read(key.fd, 65536)
                            if not content:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(content)
                            if total > MAX_OUTPUT:
                                raise ValueError("gate stage output exceeded 8 MiB")
                            log.write(content)
                            log.flush()
                            sys.stdout.buffer.write(content)
                            sys.stdout.buffer.flush()
                    eof = True
                    status = process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
                if status:
                    raise RuntimeError("gate stage failed")
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError("gate stage left owned process-group descendants")
            except BaseException as exc:
                error = type(exc).__name__ + ": " + str(exc)[:1024]
                raise
            finally:
                try:
                    if process is not None:
                        if error is not None:
                            for signum in (signal.SIGTERM, signal.SIGKILL):
                                try:
                                    os.killpg(process.pid, signum)
                                except ProcessLookupError:
                                    break
                                deadline = time.monotonic() + 10
                                while time.monotonic() < deadline:
                                    process.poll()
                                    try:
                                        os.killpg(process.pid, 0)
                                    except ProcessLookupError:
                                        break
                                    time.sleep(0.05)
                                else:
                                    continue
                                break
                            else:
                                raise RuntimeError("owned stage process group did not stop")
                            process.wait(timeout=10)
                        process.stdout.close()
                        status = process.returncode
                except BaseException as exc:
                    error = "owned stage cleanup failed: " + str(exc)[:1024]
                    raise
                finally:
                    write_json(
                        self.output / (label + "-result.json"),
                        {
                            "argv": command,
                            "exit_status": status,
                            "error": error,
                            "seconds": time.monotonic() - started,
                            "output_bytes": total,
                            "output_complete": total <= MAX_OUTPUT and eof,
                        },
                    )
