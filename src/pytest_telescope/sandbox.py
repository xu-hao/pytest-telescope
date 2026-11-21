"""Bubblewrap filesystem views over the invoking interpreter.

The sandbox decides only what a scoped test can see. It sets no resource limits and
measures no resource usage: every sandbox is a plain child process of the invocation,
so it stays inside whatever limits the caller applies to pytest.
"""

import base64
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .model import (
    DeclarationError,
    InfrastructureError,
    Unit,
    UnsupportedCapability,
    environment_names,
    fingerprint,
    materialize,
)
from .result import validate_batch, validate_collection, validate_guarded_batch, validate_result

try:
    import fcntl
except ImportError:  # Importing the optional adapter remains safe on other hosts.
    fcntl = None

BACKEND = "bubblewrap"
POLICY = "bubblewrap-view-v3"
MAX_OUTPUT = 1_048_576
MAX_COLLECTION = 67_108_864
MAX_PROBE = 4 * 1_048_576
# Bound abandoned copies, not live owners across concurrent runs of one checkout.
MAX_INVOCATIONS = 64
WORKER = Path(__file__).with_name("worker.py")
REQUEST = "/telescope/request.json"
WORKER_PATH = "/telescope/worker.py"
READY = "TELESCOPE_SANDBOX_READY"
SYSTEM_DIRECTORIES = ("/usr", "/etc")
SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# The one path that shows the frozen project view. Nothing is mounted at the project's
# own host path, so a scoped test cannot tell where its checkout lives.
PROJECT = Path("/project")
# Paths the sandbox mounts itself; an environment directory outside the project may not
# overlap them.
MOUNT_POINTS = (str(PROJECT), "/metadata", "/env", "/telescope", "/proc", "/dev")
UNPACKED_RESERVED = (*MOUNT_POINTS, "/tools", "/tmp")
ENV_MARKER = ".telescope-env.json"
_ENV_LOCKS_GUARD = threading.Lock()
_ENV_LOCKS = {}
# A startup file that names the project directory is shown rewritten; larger ones refuse.
MAX_REWRITE = 1_048_576
# The install records read to tell whether an environment inside the project depends on
# its host location; an environment with more refuses rather than passing unchecked.
MAX_RECORDS = 4096
MAX_RECORD_BYTES = 64 * 1_048_576
# User, mount and PID namespaces only. A PID namespace with its own /proc keeps other
# processes' root and working directories, which are host paths, out of the view.
# Without --as-pid-1, Bubblewrap runs a minimal PID 1 init that reaps orphaned
# descendants continuously; --die-with-parent kills outer bwrap when its caller exits
# (even on SIGKILL), stopping PID 1 so the kernel ends the whole PID namespace.
NAMESPACES = ("--unshare-user", "--unshare-pid", "--die-with-parent")
_BWRAP_OPTION_ARITIES = {
    "--unshare-user": 0,
    "--unshare-user-try": 0,
    "--unshare-pid": 0,
    "--unshare-ipc": 0,
    "--unshare-net": 0,
    "--unshare-uts": 0,
    "--unshare-cgroup": 0,
    "--unshare-cgroup-try": 0,
    "--unshare-all": 0,
    "--share-net": 0,
    "--disable-userns": 0,
    "--assert-userns-disabled": 0,
    "--clearenv": 0,
    "--new-session": 0,
    "--die-with-parent": 0,
    "--as-pid-1": 0,
    "--help": 0,
    "--version": 0,
    "--args": 1,
    "--argv0": 1,
    "--userns": 1,
    "--userns2": 1,
    "--pidns": 1,
    "--uid": 1,
    "--gid": 1,
    "--hostname": 1,
    "--chdir": 1,
    "--unsetenv": 1,
    "--lock-file": 1,
    "--sync-fd": 1,
    "--remount-ro": 1,
    "--exec-label": 1,
    "--file-label": 1,
    "--proc": 1,
    "--dev": 1,
    "--tmpfs": 1,
    "--mqueue": 1,
    "--dir": 1,
    "--seccomp": 1,
    "--add-seccomp-fd": 1,
    "--block-fd": 1,
    "--userns-block-fd": 1,
    "--info-fd": 1,
    "--json-status-fd": 1,
    "--cap-add": 1,
    "--cap-drop": 1,
    "--perms": 1,
    "--size": 1,
    "--setenv": 2,
    "--bind": 2,
    "--bind-try": 2,
    "--dev-bind": 2,
    "--dev-bind-try": 2,
    "--ro-bind": 2,
    "--ro-bind-try": 2,
    "--bind-fd": 2,
    "--ro-bind-fd": 2,
    "--file": 2,
    "--bind-data": 2,
    "--ro-bind-data": 2,
    "--symlink": 2,
    "--chmod": 2,
}
PROBE = r"""
import sys
STARTUP = [(name, getattr(module, "__file__", None)) for name, module in list(sys.modules.items())]
import base64, hashlib, json, os, re, site, sysconfig
from importlib import metadata
from urllib.parse import unquote, urlparse

LIMIT = int(sys.argv[2])
NAME = rb"A-Za-z0-9._~+@%=\x80-\xff-"
# What may end the name before a path that only ends with the directory. `=` is left
# out: it separates a key from the path that is its value, as in `VIRTUAL_ENV=/path`.
# `~` starts a path below a home directory, and names end with `+`, `@` and `%`.
BEFORE = rb"A-Za-z0-9._~+@%\x80-\xff-"

def normalize(name):
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()

def spellings(root):
    # The directory as files spell it: literally, percent-encoded in a file URL with
    # either case of hexadecimal digit, and escaped in a Python or JSON string literal.
    raw = os.fsencode(root)
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~/"
    found = {raw}
    for escape in (b"%%%02X", b"%%%02x"):
        found.add(b"".join(bytes([byte]) if byte in safe else escape % byte for byte in raw))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        found.add(repr(text)[1:-1].encode("utf-8", "surrogatepass"))
        for ascii_only in (True, False):
            found.add(json.dumps(text, ensure_ascii=ascii_only)[1:-1].encode("utf-8", "surrogatepass"))
    return sorted(found, key=lambda value: (-len(value), value))

def boundary(root):
    # The directory as a whole path: not the tail of a longer path, except after the
    # two slashes of a URL or the `=` of a value, and not the head of a longer file name.
    return re.compile(
        rb"(?<![" + BEFORE + rb"])(?<!(?<!:/)/)(?:"
        + b"|".join(re.escape(value) for value in spellings(root))
        + rb")(?![" + NAME + rb"])"
    )

def inside(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)

def candidates(root):
    # Files of the environment that can name the project directory: the .pth files of
    # the site directories and the modules they loaded at startup, such as an editable
    # install's finder; the installed metadata of distributions installed from a
    # location; and, for an environment inside the project, its configuration and
    # scripts, which name the environment's own directory.
    sites = list(dict.fromkeys(
        os.path.realpath(name) for name in site.getsitepackages() if os.path.isdir(name)
    ))
    found = []
    for directory in sites:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if name.endswith(".pth"):
                found.append(("pth", name, path))
            elif name.endswith(".egg-link"):
                found.append(("metadata", name, path))
            elif name.endswith(".dist-info") and os.path.isfile(os.path.join(path, "direct_url.json")):
                try:
                    leaves = sorted(os.listdir(path))
                except OSError:
                    continue
                found += [("metadata", name + "/" + leaf, os.path.join(path, leaf)) for leaf in leaves]
    for name, filename in STARTUP:
        if type(filename) is str and any(
            os.path.realpath(filename).startswith(directory + os.sep) for directory in sites
        ):
            found.append(("module", name, filename))
    if any(
        inside(spelling(name), root)
        for name in (sys.prefix, sys.exec_prefix)
        for spelling in (os.path.normpath, os.path.realpath)
    ):
        found.append(("environment", "pyvenv.cfg", os.path.join(sys.prefix, "pyvenv.cfg")))
        scripts = sysconfig.get_path("scripts")
        try:
            leaves = sorted(os.listdir(scripts))
        except OSError:
            leaves = []
        found += [("environment", "scripts/" + leaf, os.path.join(scripts, leaf)) for leaf in leaves]
    return found

def references(root):
    # Each occurrence of the project directory becomes a placeholder in the identity,
    # so identical checkouts at different paths keep one identity, and /project in the
    # copy the sandbox shows instead of the file.
    pattern = boundary(root)
    identity, rewrites, masks, oversize, seen, total = [], [], [], [], set(), 0
    for kind, name, filename in candidates(root):
        target = os.path.realpath(filename)
        try:
            if not os.path.isfile(target):
                continue
            with open(target, "rb") as stream:
                data = stream.read()
        except OSError:
            continue
        if kind == "environment" and name != "pyvenv.cfg" and b"\0" in data:
            continue
        if not pattern.search(data):
            continue
        identity.append([kind, name, hashlib.sha256(pattern.sub(b"\0project\0", data)).hexdigest()])
        shown = pattern.sub(b"/project", data)
        if shown == data or target in seen:
            continue
        seen.add(target)
        total += len(shown)
        if total > LIMIT:
            oversize.append(name)
            continue
        rewrites.append([target, base64.b64encode(shown).decode("ascii"), os.access(target, os.X_OK)])
        if kind == "module":
            # Bytecode compiled from the original would still name the directory.
            cache = os.path.join(os.path.dirname(target), "__pycache__")
            stem = os.path.basename(target).rpartition(".")[0] + "."
            try:
                leaves = sorted(os.listdir(cache))
            except OSError:
                leaves = []
            masks += [
                os.path.join(cache, leaf) for leaf in leaves
                if leaf.startswith(stem) and leaf.endswith(".pyc")
                and os.path.isfile(os.path.join(cache, leaf))
            ]
    return sorted(identity), rewrites, sorted(set(masks)), sorted(oversize)

def external_editables(root):
    # Editable dependencies keep source outside the read-only project snapshot. The
    # tool itself may be installed editable during development; it is the runner, not
    # an application dependency imported by the scoped test, so leave it out.
    found = set()
    for dist in metadata.distributions():
        try:
            name = normalize(dist.metadata.get("Name", ""))
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
            info = direct.get("dir_info", {})
            parsed = urlparse(direct.get("url", ""))
            if (
                not name or name == "pytest-telescope" or info.get("editable") is not True
                or parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
            ):
                continue
            location = os.path.realpath(unquote(parsed.path))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if os.path.isdir(location) and not inside(location, root):
            found.add((name, location))
    return sorted([list(row) for row in found])

IDENTITY, REWRITES, MASKS, OVERSIZE = references(sys.argv[1])
EXTERNAL_EDITABLES = external_editables(os.path.realpath(sys.argv[1]))
print(json.dumps({
    "executable": sys.executable,
    "prefixes": sorted({sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}),
    "path": [entry for entry in sys.path if entry],
    "identity": {
        "implementation": sys.implementation.name,
        "version": sys.version,
        "hexversion": sys.hexversion,
        "cache_tag": sys.implementation.cache_tag,
        "abiflags": getattr(sys, "abiflags", ""),
        "platform": sysconfig.get_platform(),
    },
    "distributions": sorted(
        [normalize(dist.metadata["Name"]), str(dist.version or "")]
        for dist in metadata.distributions()
    ),
    "references": IDENTITY,
    "rewrites": REWRITES,
    "masks": MASKS,
    "oversize": OVERSIZE,
    "external_editables": EXTERNAL_EDITABLES,
    "startup": {
        "prefix": sys.prefix, "exec_prefix": sys.exec_prefix,
        "base_prefix": sys.base_prefix, "base_exec_prefix": sys.base_exec_prefix,
        "base_executable": getattr(sys, "_base_executable", sys.executable),
        "stdlib": sysconfig.get_path("stdlib"),
        "platstdlib": sysconfig.get_path("platstdlib"),
        "purelib": sysconfig.get_path("purelib"),
        "platlib": sysconfig.get_path("platlib"),
        "platlibdir": sys.platlibdir,
        "sites": site.getsitepackages(),
    },
}, sort_keys=True))
"""


class CleanupError(InfrastructureError):
    """Terminal owned-resource failure: callers must not start another sandbox."""


class InvocationInterrupted(CleanupError):
    """Terminal controller interruption after bounded owned-resource cleanup."""


@dataclass(frozen=True)
class Sandbox:
    """A validated view of the invoking interpreter, ready to start workers.

    `executable`, `path` and the destinations in `binds` are paths inside the sandbox.
    `overlays` are the environment's files that name the project directory, each as its
    host path, the content the sandbox shows there instead, and whether it is executable.
    `links` are the host's symbolic links on the way to the environment, each as the
    link text the sandbox shows and its path inside the sandbox.
    """

    bwrap: str
    executable: str
    binds: tuple
    path: str
    identity: str
    description: dict
    overlays: tuple = ()
    links: tuple = ()
    rootfs: Path | None = None


def _host_environment():
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}


def _terminate(process):
    # Killing the process group reaches outer bwrap, its PID 1 init, and the worker;
    # when PID 1 exits, the kernel ends every remaining process in the PID namespace.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise CleanupError("sandbox process cleanup deadline exceeded") from exc
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _bwrap_option_split(args):
    if not args or os.path.basename(os.fspath(args[0])) != "bwrap":
        return None
    index = 1
    while index < len(args):
        token = args[index]
        if token == "--":
            return index if index > 1 else None
        if token == "--args":
            return None
        arity = _BWRAP_OPTION_ARITIES.get(token)
        if arity is None or index + arity >= len(args):
            return None
        index += 1 + arity
    return None


def _spawn_process(args, *, env, cwd):
    # Pass bwrap setup options via --args FD so /proc/1/cmdline never exposes host
    # bind source paths inside the PID namespace.
    split = _bwrap_option_split(args)
    args_fd = None
    launch_args = args
    pass_fds = ()
    if split is not None and hasattr(os, "memfd_create"):
        encoded = [os.fsencode(item) for item in args[1:split]]
        if all(b"\0" not in item for item in encoded):
            payload = memoryview(b"\0".join(encoded) + b"\0")
            args_fd = os.memfd_create("bwrap-args", 0)
            try:
                while payload:
                    written = os.write(args_fd, payload)
                    payload = payload[written:]
                os.lseek(args_fd, 0, os.SEEK_SET)
            except BaseException:
                os.close(args_fd)
                raise
            launch_args = [args[0], "--args", str(args_fd), *args[split:]]
            pass_fds = (args_fd,)
    try:
        return subprocess.Popen(
            launch_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
    finally:
        if args_fd is not None:
            os.close(args_fd)


def capture(args, timeout, *, env, cwd=None, limit=MAX_OUTPUT, cancellation=None, on_start=None):
    """Run one child with bounded merged output; never retain more than the limit.

    A timeout of None sets no deadline: the caller's own job bounds that session.
    """
    process = None
    output = bytearray()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        process = _spawn_process(args, env=env, cwd=cwd)
        if on_start is not None:
            on_start()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if cancellation is not None and cancellation.is_set():
                    raise InvocationInterrupted("session cancelled; owned cleanup required")
                remaining = 0.1 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("session deadline exceeded")
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InfrastructureError(
                            f"session output exceeded {max(1, limit // 1_048_576)} MiB"
                        )
        code = process.wait(
            timeout=None if deadline is None else max(0.01, deadline - time.monotonic())
        )
        return code, bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError("session could not start or exceeded its deadline") from exc
    finally:
        if process is not None:
            _terminate(process)


def _state(root):
    if fcntl is None:
        raise InfrastructureError("scoped execution requires Linux with advisory file locking")
    state = Path(root).resolve() / ".pytest-telescope"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise InfrastructureError("telescope state must be an owned directory")
    scratch = state / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    if scratch.is_symlink() or not scratch.is_dir():
        raise CleanupError("scratch directory must be an owned directory")
    return state


@contextmanager
def _allocation(state):
    descriptor = os.open(state / "allocation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 2
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CleanupError("invocation registration lock deadline exceeded") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(descriptor)


def _remove(directory):
    """Remove owned contents, retaining the locked owner until atomic deregistration."""
    try:
        for child in directory.iterdir():
            if child.name == "owner.lock":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Registration and discovery use this same lock. Neither can see the
        # interval between unlink and rmdir; large copies are removed outside it.
        with _allocation(directory.parent.parent):
            (directory / "owner.lock").unlink()
            directory.rmdir()
    except FileNotFoundError as exc:
        if directory.exists():
            raise CleanupError("owned frozen copy cleanup failed") from exc
    except OSError as exc:
        raise CleanupError("owned frozen copy cleanup failed") from exc


def _abandoned(state, limit, message, deadline):
    """Discover at most `limit` unlocked copies while the allocation lock is held.

    Live owners consume caller resources, not the abandoned-copy budget. Stream
    their entries without retaining an unbounded list or waiting for owner locks.
    Recovery must acquire ownership again before removing any discovered copy.
    """
    paths = []
    with os.scandir(state / "scratch") as entries:
        for entry in entries:
            if time.monotonic() > deadline:
                raise CleanupError("invocation recovery budget exceeded; rerun after inspection")
            if not re.fullmatch(r"invocation-[0-9a-f]{32}", entry.name):
                raise CleanupError("unrecognized scratch entry; recovery stopped")
            if not entry.is_dir(follow_symlinks=False):
                raise CleanupError("invocation directory changed type; recovery stopped")
            directory = Path(entry.path)
            try:
                descriptor = os.open(directory / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                raise CleanupError("incomplete invocation directory; recovery stopped") from None
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if len(paths) >= limit:
                    raise CleanupError(message)
                paths.append(directory)
            finally:
                os.close(descriptor)
    return paths


def recover(root):
    """Remove frozen copies left by invocations whose owner no longer holds its lock."""
    state = _state(root)
    deadline = time.monotonic() + 60
    with _allocation(state):
        paths = _abandoned(
            state,
            MAX_INVOCATIONS,
            "invocation directory budget exceeded; recovery stopped",
            deadline,
        )
    for directory in paths:
        if time.monotonic() > deadline:
            raise CleanupError("invocation recovery budget exceeded; rerun after inspection")
        try:
            descriptor = os.open(directory / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            # A concurrent invocation may have completed since directory discovery.
            # Recheck under the lock that makes final unlink/rmdir indivisible.
            with _allocation(state):
                if not directory.exists():
                    continue
                raise CleanupError("incomplete invocation directory; recovery stopped") from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if directory.exists():
                # Restore only abandoned captures, under their owner lock. Normal
                # invocation exit must not retry a terminal Capture cleanup failure.
                tools = directory / "tools"
                if os.path.lexists(tools):
                    from .tool_capture import _remove as remove_tools

                    remove_tools(tools)
                _remove(directory)
        finally:
            os.close(descriptor)


@contextmanager
def ignore_termination_signals():
    """Ignore further SIGINT and SIGTERM in the main thread while draining or cleaning up."""
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def invocation(root):
    """A private scratch directory, locked by its owner and removed afterwards."""
    state = _state(root)
    scratch = state / "scratch"
    with _allocation(state):
        _abandoned(
            state,
            MAX_INVOCATIONS - 1,
            "invocation directory budget exhausted",
            time.monotonic() + 60,
        )
        directory = scratch / ("invocation-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory / "owner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    previous = {}
    completed = False

    def interrupted(signum, frame):
        for handled in previous:
            signal.signal(handled, signal.SIG_IGN)
        raise InvocationInterrupted("controller interrupted; owned sandboxes stopped")

    interrupted._telescope_interrupt = True
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            current = signal.getsignal(signum)
            if current != signal.SIG_IGN:
                previous[signum] = signal.signal(signum, interrupted)
    try:
        yield directory
        completed = True
    finally:
        # Cleanup must finish even when a second termination signal arrives.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            _remove(directory)
        finally:
            os.close(descriptor)
            for signum, handler in previous.items():
                if completed or not getattr(handler, "_telescope_interrupt", False):
                    signal.signal(signum, handler)


def _probe_arguments(bwrap, rootfs, executable, project):
    arguments = [bwrap, *NAMESPACES]
    for directory in SYSTEM_DIRECTORIES:
        arguments += ["--ro-bind", str(rootfs / directory.lstrip("/")), directory]
    for link in SYSTEM_LINKS:
        source = rootfs / link.lstrip("/")
        if os.path.islink(source):
            arguments += ["--symlink", os.readlink(source), link]
        elif os.path.isdir(source):
            arguments += ["--ro-bind", str(source), link]
    arguments += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm"]
    arguments += ["--remount-ro", "/dev", "--tmpfs", "/tmp"]
    skipped = {
        *(name.lstrip("/") for name in SYSTEM_DIRECTORIES),
        *(name.lstrip("/") for name in SYSTEM_LINKS),
        *(name.lstrip("/") for name in UNPACKED_RESERVED),
        "sys",
        "run",
    }
    try:
        entries = sorted(os.listdir(rootfs))
    except OSError as exc:
        raise InfrastructureError("unpacked rootfs directory could not be read") from exc
    for name in entries:
        if name in skipped:
            continue
        source = rootfs / name
        destination = "/" + name
        if os.path.islink(source):
            arguments += ["--symlink", os.readlink(source), destination]
        elif os.path.isdir(source):
            arguments += ["--ro-bind", str(source), destination]
    arguments += [
        "--remount-ro",
        "/",
        "--chdir",
        "/",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "LC_ALL",
        "C",
        "--",
        executable,
        "-I",
        "-B",
        "-c",
        PROBE,
        str(project),
        str(MAX_REWRITE),
    ]
    return arguments


def _probe(executable, project, *, rootfs=None, bwrap=None):
    """Describe an interpreter exactly as an isolated child of it sees its environment.

    The interpreter starts on the host (or inside the unpacked Bubblewrap rootfs view)
    with a minimal environment of its own, so it is told not to write bytecode: describing
    an environment never writes into it, whatever the calling job allows.
    """
    if rootfs is not None:
        bwrap = bwrap or shutil.which("bwrap")
        if bwrap is None:
            raise UnsupportedCapability(
                "sandbox:bubblewrap",
                "scoped tests need Bubblewrap; install the bwrap command (package bubblewrap)",
            )
        argv = _probe_arguments(bwrap, rootfs, executable, project)
    else:
        argv = [executable, "-I", "-B", "-c", PROBE, str(project), str(MAX_REWRITE)]
    code, output = capture(
        argv,
        30,
        env=_host_environment(),
        cwd="/",
        limit=MAX_PROBE,
    )
    if code:
        if rootfs is not None:
            message = _bwrap_message(output)
            if message is not None and not re.search(
                r"Can't (bind )?mount|No such file or directory|Not a directory|execvp", message
            ):
                raise UnsupportedCapability(
                    "sandbox:namespaces",
                    "Bubblewrap could not create the user, mount and PID namespaces that scoped "
                    "tests need: " + message,
                )
        raise UnsupportedCapability(
            "sandbox:environment", "the invoking interpreter could not describe its environment"
        )
    try:
        data = json.loads(output)
        valid = (
            type(data) is dict
            and set(data)
            == {
                "executable",
                "prefixes",
                "path",
                "identity",
                "distributions",
                "references",
                "rewrites",
                "masks",
                "oversize",
                "external_editables",
                "startup",
            }
            and type(data["executable"]) is str
            and all(
                type(data[key]) is list and all(type(entry) is str for entry in data[key])
                for key in ("prefixes", "path")
            )
            and type(data["identity"]) is dict
            and type(data["distributions"]) is list
            and all(
                type(row) is list and len(row) == 2 and all(type(value) is str for value in row)
                for row in data["distributions"]
            )
            and type(data["references"]) is list
            and type(data["external_editables"]) is list
            and all(
                type(row) is list and len(row) == 2 and all(type(value) is str for value in row)
                for row in data["external_editables"]
            )
            and type(data["startup"]) is dict
            and all(
                type(row) is list and len(row) == 3 and all(type(value) is str for value in row)
                for row in data["references"]
            )
            and type(data["rewrites"]) is list
            and all(
                type(row) is list
                and len(row) == 3
                and type(row[0]) is str
                and os.path.isabs(row[0])
                and type(row[1]) is str
                and type(row[2]) is bool
                for row in data["rewrites"]
            )
            and all(
                type(data[key]) is list and all(type(entry) is str for entry in data[key])
                for key in ("masks", "oversize")
            )
            and all(os.path.isabs(name) for name in data["masks"])
        )
    except (ValueError, UnicodeError, RecursionError):
        valid = False
    if not valid:
        raise InfrastructureError("interpreter environment probe returned an invalid description")
    return data


def _inside(path, directory):
    return path == directory or directory in path.parents


def _refuse(message):
    raise UnsupportedCapability("sandbox:environment", message)


def _check_project(project):
    """Refuse a project directory whose live files the sandbox would show.

    The frozen view appears at /project and nothing is mounted at the project's own
    path, so that path only has to stay out of what the sandbox shares with the host:
    the project may not lie inside a system directory, nor contain one.
    """
    system = [Path(os.path.realpath(name)) for name in SYSTEM_DIRECTORIES]
    if any(_inside(project, directory) for directory in system):
        _refuse("the project cannot live inside a system directory shared with the sandbox")
    shared = [*system, *(Path(name) for name in (*SYSTEM_DIRECTORIES, *SYSTEM_LINKS))]
    if any(_inside(directory, project) for directory in shared):
        _refuse("the project directory cannot contain a directory the sandbox shares")


def _shown(path, project):
    """The sandbox path of a host path: /project below the project, itself elsewhere."""
    path = Path(path)
    return PROJECT / path.relative_to(project) if _inside(path, project) else path


def _symlinks(name):
    """The host's symbolic links met while resolving `name`, each as (location, text).

    Every location's parent directory is a real directory, so the links, shown at their
    locations with directories mounted at their real paths, resolve as on the host.
    """
    pending = list(Path(name).parts[1:])
    current, found = Path("/"), []
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if not os.path.islink(candidate):
            current = candidate
            continue
        if len(found) >= 40:
            _refuse("an interpreter environment path has too many levels of symbolic links")
        text = os.readlink(candidate)
        found.append((candidate, text))
        parts = Path(text).parts
        if os.path.isabs(text):
            current, parts = Path("/"), parts[1:]
        pending = [*parts, *pending]
    return found


def _resolve_in_rootfs(rootfs, name):
    """Resolve `name` inside `rootfs` without escaping `rootfs`."""
    pending = [part for part in PurePosixPath(name).parts[1:] if part not in ("", ".")]
    current, found = Path("/"), []
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            continue
        logical = current / part
        candidate = rootfs / logical.relative_to("/")
        if not os.path.islink(candidate):
            current = logical
            continue
        if len(found) >= 40:
            _refuse("an interpreter environment path has too many levels of symbolic links")
        text = os.readlink(candidate)
        found.append((logical, text))
        parts = [item for item in PurePosixPath(text).parts if item not in ("", ".")]
        if os.path.isabs(text):
            current = Path("/")
            parts = parts[1:] if parts and parts[0] == "/" else parts
        pending = [*parts, *pending]
    return current, rootfs / current.relative_to("/"), found


def _check_unpacked_rootfs(rootfs, executable, project):
    if project == Path("/") or _inside(project, rootfs) or _inside(rootfs, project):
        _refuse("the unpacked rootfs cannot overlap the project directory")
    for directory in SYSTEM_DIRECTORIES:
        candidate = rootfs / directory.lstrip("/")
        if os.path.islink(candidate) or not os.path.isdir(candidate):
            _refuse(f"unpacked rootfs requires a {directory} directory")
    given = Path(os.path.normpath(executable))
    real, host_executable, _ = _resolve_in_rootfs(rootfs, executable)
    for path in (given, real):
        if any(
            _inside(path, Path(point)) or _inside(Path(point), path) for point in UNPACKED_RESERVED
        ):
            _refuse("an interpreter environment directory overlaps a sandbox mount point")
    try:
        info = host_executable.lstat()
    except OSError:
        _refuse("rootfs_python does not name an executable file inside rootfs")
    if not stat.S_ISREG(info.st_mode) or not os.access(host_executable, os.X_OK):
        _refuse("rootfs_python does not name an executable file inside rootfs")


def _check_slash_executable(executable):
    given = Path(os.path.normpath(executable))
    real = Path(os.path.realpath(executable))
    for path in (given, real):
        if _inside(Path("/tmp"), path) or any(
            _inside(path, Path(point)) or _inside(Path(point), path) for point in MOUNT_POINTS
        ):
            _refuse("an interpreter environment directory overlaps a sandbox mount point")
    try:
        info = real.lstat()
    except OSError:
        _refuse("rootfs_python does not name an executable file")
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        _refuse("rootfs_python does not name an executable file")


def _link_text(location, text, project):
    """The text of a host link as the sandbox shows it: unchanged where it resolves the
    same way, and otherwise its target's sandbox path."""
    target = Path(os.path.normpath(os.path.join(location.parent, text)))
    if os.path.isabs(text):
        return str(_shown(target, project)) if _inside(target, project) else text
    if _inside(location, project) == _inside(target, project):
        return text
    return str(_shown(target, project))


def _binds(description, project, *, rootfs=None):
    """Map the interpreter's prefixes and import directories into the view, read-only.

    Every directory is mounted once, at its real path, and every host symbolic link on
    the way to it is shown as a link, so a file has one path in the sandbox and
    `os.path.realpath` resolves as on the host. An environment prefix that contains the
    project is refused: binding it would expose the project. An import directory that
    contains or lies inside the project is not bound: the project appears only as the
    frozen declared view at /project. An environment prefix inside the project is bound
    below /project, at the same relative path, so it does not reveal the checkout's
    location either. Every other directory keeps its host path and may not overlap a
    path the sandbox mounts itself. Returns the binds and the links.
    """
    if rootfs is not None:
        system = [_resolve_in_rootfs(rootfs, name)[0] for name in SYSTEM_DIRECTORIES]
        prefixes = {
            os.path.normpath(name)
            for name in [*description["prefixes"], os.path.dirname(description["executable"])]
        }
        binds, found = {}, {}
        for name in [*sorted(prefixes), *description["path"]]:
            if not os.path.isabs(name):
                continue
            given = Path(os.path.normpath(name))
            real, host_real, symlinks = _resolve_in_rootfs(rootfs, name)
            if not os.path.exists(host_real):
                continue
            if any(_inside(path, directory) for path in (given, real) for directory in system):
                continue
            for path in (given, real):
                if any(
                    _inside(path, Path(point)) or _inside(Path(point), path)
                    for point in UNPACKED_RESERVED
                ):
                    _refuse("an interpreter environment directory overlaps a sandbox mount point")
            binds[str(real)] = str(host_real)
            for location, text in symlinks:
                if not any(_inside(location, directory) for directory in system):
                    found[location] = text
        ordered = []
        for destination in sorted(binds, key=lambda value: (len(Path(value).parts), value)):
            source = binds[destination]
            if any(
                _inside(Path(destination), Path(parent))
                and Path(source) == Path(binds[parent]) / Path(destination).relative_to(parent)
                for parent, _ in ordered
            ):
                continue
            ordered.append((destination, source))
        links = {}
        for location, text in found.items():
            if any(
                _inside(location, Path(destination))
                and Path(source) / location.relative_to(destination)
                == rootfs / location.relative_to("/")
                for destination, source in ordered
            ):
                continue
            if any(
                _inside(location, Path(point)) or _inside(Path(point), location)
                for point in UNPACKED_RESERVED
            ):
                _refuse("an interpreter environment directory overlaps a sandbox mount point")
            links[str(location)] = text
        return (
            tuple((source, destination) for destination, source in ordered),
            tuple(
                (links[location], location)
                for location in sorted(links, key=lambda value: (len(Path(value).parts), value))
            ),
        )
    _check_project(project)
    system = [Path(os.path.realpath(name)) for name in SYSTEM_DIRECTORIES]
    prefixes = {
        os.path.normpath(name)
        for name in [*description["prefixes"], os.path.dirname(description["executable"])]
    }
    binds, found = {}, {}
    for name in [*sorted(prefixes), *description["path"]]:
        if not os.path.isabs(name) or not os.path.exists(name):
            continue
        given = Path(os.path.normpath(name))
        real = Path(os.path.realpath(name))
        if any(_inside(path, directory) for path in (given, real) for directory in system):
            continue
        overlaps_project = any(
            _inside(project, path) or _inside(path, project) for path in (given, real)
        )
        if str(given) not in prefixes and overlaps_project:
            continue
        for path in (given, real):
            if _inside(project, path):
                _refuse("the interpreter environment contains the project directory")
            if _inside(path, project):
                continue
            if _inside(Path("/tmp"), path) or any(
                _inside(path, Path(point)) or _inside(Path(point), path) for point in MOUNT_POINTS
            ):
                _refuse("an interpreter environment directory overlaps a sandbox mount point")
        binds[str(_shown(real, project))] = str(real)
        for location, text in _symlinks(given):
            if not any(_inside(location, directory) for directory in system):
                found[location] = text
    ordered = []
    for destination in sorted(binds, key=lambda value: (len(Path(value).parts), value)):
        source = binds[destination]
        if any(
            _inside(Path(destination), Path(parent))
            and Path(source) == Path(binds[parent]) / Path(destination).relative_to(parent)
            for parent, _ in ordered
        ):
            continue
        ordered.append((destination, source))
    links = {}
    for location, text in found.items():
        shown = _shown(location, project)
        # A link inside a mounted directory already appears there, as on the host.
        if any(
            _inside(shown, Path(destination))
            and Path(source) / shown.relative_to(destination) == location
            for destination, source in ordered
        ):
            continue
        if not _inside(location, project) and (
            _inside(Path("/tmp"), location)
            or any(
                _inside(location, Path(point)) or _inside(Path(point), location)
                for point in MOUNT_POINTS
            )
        ):
            _refuse("an interpreter environment directory overlaps a sandbox mount point")
        links[str(shown)] = _link_text(location, text, project)
    return (
        tuple((source, destination) for destination, source in ordered),
        tuple(
            (links[location], location)
            for location in sorted(links, key=lambda value: (len(Path(value).parts), value))
        ),
    )


def _overlays(description, *, rootfs=None):
    """The rewritten startup files and the stale bytecode they replace, by host path."""
    if description["oversize"]:
        _refuse(
            "an environment file that names the project directory is too large to show "
            "rewritten: " + description["oversize"][0][:200]
        )

    def host_name(name):
        return name if rootfs is None else str(rootfs / name.lstrip("/"))

    try:
        rewritten = tuple(
            (host_name(name), base64.b64decode(content, validate=True), executable)
            for name, content, executable in description["rewrites"]
        )
    except ValueError as exc:
        raise InfrastructureError("interpreter environment probe returned invalid files") from exc
    # An empty file is not valid bytecode, so the interpreter compiles the shown source.
    return rewritten + tuple((host_name(name), b"", False) for name in description["masks"])


NORMALIZERS = (os.path.normpath, os.path.realpath)


def _located_file(prefix, record, rewritten):
    """The first file a conda install record lists with its install location written in.

    conda writes the environment's location into each file whose entry has a
    `prefix_placeholder`, in `binary` or `text` mode. A binary file keeps that host path
    in the sandbox. A text file does too, unless the sandbox shows it rewritten.
    """
    listing = record.get("paths_data") if type(record) is dict else None
    paths = listing.get("paths") if type(listing) is dict else None
    if type(paths) is not list:
        raise ValueError("no list of installed paths")
    for entry in paths:
        if type(entry) is not dict or not entry.get("prefix_placeholder"):
            continue
        name = entry.get("_path")
        if type(name) is not str:
            raise ValueError("an installed path without a name")
        if entry.get("file_mode") == "text" and (
            os.path.realpath(os.path.join(prefix, name)) in rewritten
        ):
            continue
        return name
    return None


def _location_reason(prefix, meta, rewritten):
    """Why the environment at `prefix` cannot run at /project, or None if it can."""
    try:
        with os.scandir(meta) as entries:
            records = []
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file():
                    records.append(entry.name)
                    if len(records) > MAX_RECORDS:
                        return (
                            f"has more than {MAX_RECORDS} install records in conda-meta "
                            "to check for its host location"
                        )
    except OSError:
        return "has a conda-meta directory that cannot be read"
    total = 0
    for leaf in sorted(records):
        try:
            with open(meta / leaf, "rb") as stream:
                data = stream.read(MAX_RECORD_BYTES - total + 1)
            total += len(data)
            if total > MAX_RECORD_BYTES:
                return (
                    f"has more than {MAX_RECORD_BYTES // 1_048_576} MiB of install records "
                    "in conda-meta to check for its host location"
                )
            located = _located_file(prefix, json.loads(data), rewritten)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return "has an install record that cannot be read: conda-meta/" + leaf[:200]
        if located is not None:
            return (
                "depends on its host location, which is written into its file "
                + located[:200]
                + ", so it cannot run at /project"
            )
    return None


def _check_location(description, project):
    """Refuse an environment inside the project that depends on its host location.

    The sandbox shows such an environment only below /project. A conda-style
    environment lists in `conda-meta/*.json` the files its install location was written
    into; any one the sandbox does not show rewritten names a path that does not exist
    in the view, so the environment cannot run there. The records are read once, while
    the environment is identified and before any session starts, within fixed bounds;
    an environment beyond them refuses rather than passing unchecked.
    """
    rewritten = {row[0] for row in description["rewrites"]}
    scanned = set()
    for name in sorted(description["prefixes"]):
        given, real = (Path(convert(name)) for convert in NORMALIZERS)
        shown = given if _inside(given, project) else real
        meta = real / "conda-meta"
        if not _inside(shown, project) or not os.path.isdir(meta) or str(meta) in scanned:
            continue
        scanned.add(str(meta))
        reason = _location_reason(real, meta, rewritten)
        if reason is None:
            continue
        where = shown.relative_to(project).as_posix()[:200]
        if not _inside(real, project):
            # The environment already lives outside the project; a link inside the
            # project is what starts its interpreter below /project.
            _refuse(
                f"the environment {where} is a link inside the project to an environment "
                f"that {reason}; start its interpreter by its real path outside the "
                "project, or remove the link"
            )
        _refuse(
            f"the environment {where} inside the project {reason}; place the environment "
            "outside the project directory"
        )


def _project_paths(description, project, *, rootfs=None):
    """Import directories inside the project, outside every environment prefix."""
    normalizers = (
        NORMALIZERS
        if rootfs is None
        else (os.path.normpath, lambda name: str(_resolve_in_rootfs(rootfs, name)[0]))
    )
    prefixes = {Path(convert(name)) for name in description["prefixes"] for convert in normalizers}
    found = set()
    for name in description["path"]:
        if not os.path.isabs(name):
            continue
        for path in {Path(convert(name)) for convert in normalizers}:
            if _inside(path, project) and not any(_inside(path, prefix) for prefix in prefixes):
                found.add(path.relative_to(project).as_posix())
    return sorted(found)


def _closed_mountpoints(directory, closed, *, rootfs=None):
    """Make missing mountpoints privately, preserving the admitted system baseline.

    A readonly bind cannot supply a new destination below itself. Reconstruct only
    the necessary parent directories as readonly tmpfs scaffolding; existing
    siblings remain readonly binds or literal links. No host directory is written
    and no sibling becomes part of a selected frozen tool projection.
    """
    base = Path(directory)
    physical_base = base if rootfs is None else rootfs / base.relative_to("/")
    missing = {}
    arguments = []
    visits = size = 0

    def add(*parts):
        nonlocal size
        size += sum(len(part.encode()) + 1 for part in parts)
        if size > MAX_OUTPUT:
            raise InfrastructureError("system mountpoint arguments exceed their bound")
        arguments.extend(parts)

    def visit():
        nonlocal visits
        visits += 1
        if visits > 4096:
            raise InfrastructureError("system mountpoint entries exceed their bound")

    def physical(path):
        return path if rootfs is None else physical_base / path.relative_to(base)

    def scaffold(parent, targets):
        source_parent = physical(parent)
        info = source_parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise InfrastructureError("system mountpoint parent changed type")
        add("--perms", format(stat.S_IMODE(info.st_mode), "o"), "--tmpfs", str(parent))
        children = {}
        with os.scandir(source_parent) as entries:
            for entry in entries:
                visit()
                children[entry.name] = entry.stat(follow_symlinks=False)
        branches = {target.relative_to(parent).parts[0] for target in targets}
        for name, info in sorted(children.items()):
            child = parent / name
            source_child = physical(child)
            if name in branches:
                if child in targets or not stat.S_ISDIR(info.st_mode):
                    raise InfrastructureError("system mountpoint ancestor changed type")
                scaffold(child, [target for target in targets if target.is_relative_to(child)])
            elif stat.S_ISLNK(info.st_mode):
                add("--symlink", os.readlink(source_child), str(child))
            elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                add("--ro-bind", str(source_child), str(child))
            else:
                raise InfrastructureError("unsupported system mountpoint sibling type")
        for target in targets:
            if target.relative_to(parent).parts[0] not in children:
                add("--dir", str(target))
        add("--remount-ro", str(parent))

    try:
        for name in closed:
            target = Path(name)
            if target == base or not target.is_relative_to(base):
                continue
            parent = base
            for part in target.relative_to(base).parts:
                visit()
                child = parent / part
                try:
                    info = physical(child).lstat()
                except FileNotFoundError:
                    missing[target] = parent
                    break
                if not stat.S_ISDIR(info.st_mode):
                    raise InfrastructureError("closed system mountpoint is not a directory")
                parent = child
        roots = set(missing.values())
        for parent in sorted(roots):
            if not any(parent != other and parent.is_relative_to(other) for other in roots):
                scaffold(parent, [target for target in missing if target.is_relative_to(parent)])
    except OSError as exc:
        raise InfrastructureError("system mountpoint baseline could not be read") from exc
    return arguments


def _base_arguments(box, project=None, *, frozen_prefixes=(), native=False, closed_directories=()):
    """System directories, scratch and the environment, except binds below `project`."""
    arguments = [box.bwrap, *NAMESPACES]
    rootfs = box.rootfs
    for directory in SYSTEM_DIRECTORIES:
        source = directory if rootfs is None else str(rootfs / directory.lstrip("/"))
        if directory != "/etc" or not native:
            arguments += ["--ro-bind", source, directory]
            if native:
                arguments += _closed_mountpoints(directory, closed_directories, rootfs=rootfs)
    prefixes = dict(frozen_prefixes)
    for link in SYSTEM_LINKS:
        source = Path(link) if rootfs is None else rootfs / link.lstrip("/")
        if link in prefixes:
            row = prefixes[link]
            if row["kind"] == "symlink":
                arguments += ["--symlink", row["target"], link]
            elif row["kind"] == "directory":
                arguments += ["--ro-bind", str(source), link]
        elif os.path.islink(source):
            arguments += ["--symlink", os.readlink(source), link]
        elif os.path.isdir(source):
            arguments += ["--ro-bind", str(source), link]
    arguments += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm"]
    arguments += ["--remount-ro", "/dev", "--tmpfs", "/tmp"]
    for source, destination in box.binds:
        if project is None or not _inside(Path(destination), project):
            arguments += ["--ro-bind", source, destination]
    for text, location in box.links:
        if project is None or not _inside(Path(location), project):
            arguments += ["--symlink", text, location]
    return arguments


def _project_arguments(box, view):
    """Show the frozen view read-only at /project, and nowhere else.

    Nothing is mounted at the project's own host path. An environment inside the project
    is bound below /project and needs a mount point there, and bwrap must never create
    one inside the frozen copy on the host. The same holds for a host symbolic link
    inside the project on the way to an environment. /project is then a private tmpfs:
    it binds the view's entries one by one, makes directories only on the way to each
    environment or link, binds the environment, shows the link and is made read-only.
    """
    inside = [
        (source, Path(destination))
        for source, destination in box.binds
        if _inside(Path(destination), PROJECT)
    ]
    links = [
        (text, Path(location)) for text, location in box.links if _inside(Path(location), PROJECT)
    ]
    if not inside and not links:
        return ["--ro-bind", str(view), str(PROJECT)]
    destinations = {destination for _, destination in inside + links}
    ancestors = {
        parent
        for destination in destinations
        for parent in destination.parents
        if _inside(parent, PROJECT) and not any(_inside(parent, other) for other in destinations)
    }
    arguments = ["--tmpfs", str(PROJECT)]
    pending = [PROJECT]
    while pending:
        directory = pending.pop(0)
        frozen = view / directory.relative_to(PROJECT)
        if os.path.lexists(frozen) and (frozen.is_symlink() or not frozen.is_dir()):
            raise InfrastructureError("an environment lies below a frozen file")
        for name in sorted(os.listdir(frozen)) if os.path.lexists(frozen) else []:
            target = directory / name
            if target in destinations:
                raise InfrastructureError("the frozen view contains an environment directory")
            if target in ancestors:
                continue
            source = frozen / name
            if source.is_symlink():
                arguments += ["--symlink", os.readlink(source), str(target)]
            else:
                arguments += ["--ro-bind", str(source), str(target)]
        for child in sorted(path for path in ancestors if path.parent == directory):
            arguments += ["--dir", str(child)]
            pending.append(child)
    for source, destination in inside:
        arguments += ["--ro-bind", source, str(destination)]
    for text, location in links:
        arguments += ["--symlink", text, str(location)]
    return arguments + ["--remount-ro", str(PROJECT)]


def _overlay_arguments(box, directory, *, system=True):
    """Show each rewritten environment file over its original, wherever that appears.

    The copies are written into `directory`, private scratch of the invocation, with a
    modification time no bytecode cache records, and bound read-only over every sandbox
    path that shows the original: its own path in a shared system directory, and its
    place below each environment bind. The host files are never modified.
    """
    shown = [*box.binds]
    if system:
        if box.rootfs is not None:
            shown += [(str(box.rootfs / name.lstrip("/")), name) for name in SYSTEM_DIRECTORIES]
        else:
            shown += [(os.path.realpath(name), name) for name in SYSTEM_DIRECTORIES]
    arguments = []
    for index, (name, content, executable) in enumerate(box.overlays):
        original = Path(name)
        targets = set()
        for source, destination in shown:
            if not _inside(original, Path(source)):
                continue
            target = Path(destination) / original.relative_to(source)
            # The deepest bind over the target decides which host file appears there.
            cover = max(
                (pair for pair in shown if _inside(target, Path(pair[1]))),
                key=lambda pair: len(Path(pair[1]).parts),
            )
            if Path(cover[0]) / target.relative_to(cover[1]) == original:
                targets.add(str(target))
        if not targets:
            continue
        directory.mkdir(mode=0o700, exist_ok=True)
        copy = directory / str(index)
        copy.unlink(missing_ok=True)
        copy.write_bytes(content)
        copy.chmod(0o555 if executable else 0o444)
        # Bytecode the host caches for the original records the original's timestamp.
        # The copy carries a different one, so such bytecode is recompiled from the shown
        # source instead of being loaded for it.
        try:
            recorded = int(os.stat(name).st_mtime) & 0xFFFFFFFF
        except OSError:
            recorded = None
        stamp = 1 if recorded == 0 else 0
        os.utime(copy, (stamp, stamp))
        for target in sorted(targets):
            arguments += ["--ro-bind", str(copy), target]
    return arguments


def _environment(box, forwarded=None):
    variables = {
        "PATH": box.path,
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    for key, value in sorted((forwarded or {}).items()):
        if value is not None:
            variables[key] = value
    arguments = ["--clearenv"]
    for key, value in variables.items():
        arguments += ["--setenv", key, value]
    return arguments


def _bwrap_message(output):
    for line in output.decode("utf-8", "replace").splitlines():
        if line.startswith("bwrap: "):
            return line[:300]
    return None


def _preflight(box):
    """Start the interpreter in the view once; classify a refusal before any test runs.

    The view is the one every session gets, with an empty project: the environment, an
    environment inside the project below /project, and the rewritten startup files.
    """
    with tempfile.TemporaryDirectory(prefix="pytest-telescope-preflight-") as scratch:
        empty = Path(scratch) / "project"
        empty.mkdir()
        argv = [
            *_base_arguments(box, PROJECT),
            *_project_arguments(box, empty),
            *_overlay_arguments(box, Path(scratch) / "startup"),
            "--remount-ro",
            "/",
            "--chdir",
            "/",
            *_environment(box),
            "--",
            box.executable,
            "-I",
            "-c",
            f"print({READY!r})",
        ]
        code, output = capture(argv, 30, env=_host_environment(), limit=MAX_OUTPUT)
    if code == 0 and READY.encode() in output.splitlines():
        return
    message = _bwrap_message(output)
    if message is None:
        _refuse("the invoking interpreter could not start inside the read-only Bubblewrap view")
    if re.search(r"Can't (bind )?mount|No such file or directory|Not a directory", message):
        _refuse("Bubblewrap could not mount the interpreter environment: " + message)
    raise UnsupportedCapability(
        "sandbox:namespaces",
        "Bubblewrap could not create the user, mount and PID namespaces that scoped tests "
        "need: " + message,
    )


def describe(root, executable=None, *, rootfs=None, bwrap=None):
    """The view of an interpreter's environment and its cache identity, without bwrap.

    The identity covers the interpreter build and every installed distribution visible
    to it in isolated mode, so installing or upgrading a package changes it. It also
    covers how the environment reaches into the project, relative to the project: the
    environment files that name the project directory, which the sandbox shows rewritten
    to /project, and the import directories inside it. Without such references the
    project's path is not part of the identity.
    """
    project = Path(root).resolve()
    unpacked = None
    if rootfs is not None:
        resolved_rootfs = Path(rootfs).resolve(strict=True)
        if resolved_rootfs != Path("/"):
            unpacked = resolved_rootfs
            if executable is None:
                raise DeclarationError("unpacked rootfs requires rootfs_python")
            _check_unpacked_rootfs(unpacked, executable, project)
        else:
            _check_project(project)
            if executable is not None:
                _check_slash_executable(executable)
    description = _probe(executable or sys.executable, project, rootfs=unpacked, bwrap=bwrap)
    if description["external_editables"]:
        name, location = description["external_editables"][0]
        _refuse(
            f"editable distribution {name} at {location} is outside the project; "
            "install external dependencies as regular, non-editable distributions"
        )
    binds, links = _binds(description, project, rootfs=unpacked)
    if unpacked is None:
        _check_location(description, project)
    shown_executable = (
        description["executable"]
        if unpacked is not None
        else str(_shown(description["executable"], project))
    )
    folders = [
        os.path.dirname(shown_executable),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    return Sandbox(
        bwrap=bwrap,
        executable=shown_executable,
        binds=binds,
        links=links,
        path=":".join(dict.fromkeys(folders)),
        identity=fingerprint(
            {
                "policy": POLICY,
                "interpreter": description["identity"],
                "distributions": description["distributions"],
                "project": {
                    "startup": description["references"],
                    "paths": _project_paths(description, project, rootfs=unpacked),
                },
            }
        ),
        description=description,
        overlays=_overlays(description, rootfs=unpacked),
        rootfs=unpacked,
    )


def prepare(root, *, rootfs=None, executable=None):
    """Probe the invoking interpreter and check Bubblewrap before any session starts."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise UnsupportedCapability(
            "sandbox:bubblewrap",
            "scoped tests need Bubblewrap; install the bwrap command (package bubblewrap)",
        )
    for attempt in range(2):
        try:
            box = replace(
                describe(root, executable=executable, rootfs=rootfs, bwrap=bwrap), bwrap=bwrap
            )
            _preflight(box)
            return box
        except (CleanupError, UnsupportedCapability, DeclarationError):
            raise
        except InfrastructureError:
            if attempt:
                raise


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree_writable(path):
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    for parent, dirs, _ in os.walk(path, topdown=True):
        os.chmod(parent, 0o700)
        for name in dirs:
            child = os.path.join(parent, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)
    shutil.rmtree(path)


@contextmanager
def _environment_lock(directory, env_key, timeout):
    if fcntl is None:
        raise InfrastructureError("scoped execution requires Linux with advisory file locking")
    key = (str(directory), env_key)
    with _ENV_LOCKS_GUARD:
        thread_lock = _ENV_LOCKS.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not thread_lock.acquire(timeout=max(0.01, timeout)):
        raise InfrastructureError("editable environment lock deadline exceeded")
    lock_path = directory / f".{env_key}.lock"
    descriptor = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InfrastructureError(
                        "editable environment lock deadline exceeded"
                    ) from None
                time.sleep(0.02)
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        thread_lock.release()


def _valid_editable_environment(target, env_key):
    try:
        if target.is_symlink() or not target.is_dir():
            return False
        marker = target / ENV_MARKER
        if marker.is_symlink() or not marker.is_file():
            return False
        if not os.path.lexists(target / "bin/python"):
            return False
        payload = json.loads(marker.read_bytes())
        return payload == {"schema": 1, "key": env_key}
    except (OSError, ValueError, TypeError):
        return False


def _configure_base_venv_bridge(box, root, env_stage):
    startup = box.description.get("startup", {})
    prefix = startup.get("prefix")
    base_prefix = startup.get("base_prefix")
    if not prefix or not base_prefix or prefix == base_prefix:
        return
    box_cfg = (
        box.rootfs / prefix.lstrip("/") if box.rootfs is not None else Path(prefix)
    ) / "pyvenv.cfg"
    include_system = "false"
    try:
        if box_cfg.is_file():
            for line in box_cfg.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    if key.strip().lower() == "include-system-site-packages":
                        include_system = value.strip().lower()
    except OSError as exc:
        raise InfrastructureError("cannot read invoking virtual environment configuration") from exc
    env_cfg = env_stage / "pyvenv.cfg"
    if include_system != "true" and env_cfg.is_file():
        lines = []
        for line in env_cfg.read_text(encoding="utf-8").splitlines():
            if (
                "=" in line
                and line.partition("=")[0].strip().lower() == "include-system-site-packages"
            ):
                lines.append("include-system-site-packages = false")
            else:
                lines.append(line)
        env_cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    project = Path(root).resolve()
    shown_prefix = Path(prefix) if box.rootfs is not None else _shown(Path(prefix), project)
    site_entries = []
    for candidate in (
        startup.get("purelib"),
        startup.get("platlib"),
        *(startup.get("sites") or ()),
    ):
        if type(candidate) is not str or not os.path.isabs(candidate):
            continue
        shown_entry = (
            Path(candidate) if box.rootfs is not None else _shown(Path(candidate), project)
        )
        text = str(shown_entry)
        if _inside(shown_entry, shown_prefix) and text not in site_entries:
            site_entries.append(text)
    if site_entries:
        site_dirs = sorted((env_stage / "lib").glob("python*/site-packages"))
        if not site_dirs:
            raise InfrastructureError(
                "editable virtual environment lacks a site-packages directory"
            )
        (site_dirs[0] / "zzz_telescope_base.pth").write_text(
            "\n".join(site_entries) + "\n", encoding="utf-8"
        )


def _freeze_editable_environment(env_stage, env_key):
    for cache_dir in sorted(env_stage.rglob("__pycache__"), reverse=True):
        if not cache_dir.is_symlink() and cache_dir.is_dir():
            shutil.rmtree(cache_dir)
    for bytecode_file in (*env_stage.rglob("*.pyc"), *env_stage.rglob("*.pyo")):
        if os.path.lexists(bytecode_file):
            bytecode_file.unlink()
    marker = env_stage / ENV_MARKER
    marker.write_bytes(json.dumps({"schema": 1, "key": env_key}, sort_keys=True).encode("utf-8"))
    marker.chmod(0o444)
    for parent, dirs, files in os.walk(env_stage, topdown=False):
        parent_path = Path(parent)
        for name in files:
            item = parent_path / name
            info = item.lstat()
            if not stat.S_ISLNK(info.st_mode):
                item.chmod(0o555 if info.st_mode & 0o111 else 0o444)
            os.utime(item, ns=(0, 0), follow_symlinks=False)
        for name in dirs:
            child = parent_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                os.utime(child, ns=(0, 0), follow_symlinks=False)
            else:
                child.chmod(0o555)
                os.utime(child, ns=(0, 0))
    env_stage.chmod(0o555)
    os.utime(env_stage, ns=(0, 0))


def prepare_editable_environment(
    box,
    distributions,
    *,
    root,
    pool,
    record_map,
    environments_directory,
    invocation_directory,
    timeout,
    cancellation=None,
):
    """Build or reuse an isolated read-only /env for selected editable-v1 distributions."""
    from .project_metadata import EDITABLE_CAPABILITY

    installed = {name for name, _ in box.description["distributions"]}
    if any(distribution.name in installed for distribution in distributions):
        raise UnsupportedCapability(
            "project-metadata:runtime-conflict",
            "local metadata conflicts with a distribution installed in the environment",
        )
    env_key = fingerprint(
        {
            "schema": 1,
            "sandbox": box.identity,
            "distributions": [list(distribution.input_records) for distribution in distributions],
        }
    )
    environments_directory = Path(environments_directory)
    environments_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if environments_directory.is_symlink() or not environments_directory.is_dir():
        raise InfrastructureError("editable environment cache must be an owned directory")
    target = environments_directory / env_key
    if _valid_editable_environment(target, env_key):
        return target
    with _environment_lock(environments_directory, env_key, timeout * 2 + 30):
        if _valid_editable_environment(target, env_key):
            return target
        prefix = f".env-{env_key[:16]}-"
        for leftover in list(environments_directory.iterdir()):
            if leftover.name.startswith(prefix):
                _remove_tree_writable(leftover)
        build_inputs = sorted(
            {item for distribution in distributions for item in distribution.inputs}
        )
        editable_targets = [
            argument
            for distribution in distributions
            for argument in (
                "-e",
                str(PROJECT)
                if distribution.directory == "."
                else f"{PROJECT}/{distribution.directory}",
            )
        ]
        for attempt in range(2):
            if cancellation is not None and cancellation.is_set():
                raise InvocationInterrupted("editable environment build cancelled")
            work = Path(invocation_directory) / ("env-build-" + uuid.uuid4().hex)
            env_stage = environments_directory / (prefix + uuid.uuid4().hex)
            try:
                work.mkdir(mode=0o700)
                build_project = work / "project"
                materialize(
                    pool,
                    Unit("editable-build", [], build_inputs, [], True),
                    build_project,
                    record_map,
                )
                env_stage.mkdir(mode=0o755)

                def run_step(command, failure_message):
                    argv = [
                        *_base_arguments(box, PROJECT),
                        *_project_arguments(box, build_project),
                        *_overlay_arguments(box, work / "startup"),
                        "--bind",
                        str(env_stage),
                        "/env",
                        "--remount-ro",
                        "/",
                        "--chdir",
                        str(PROJECT),
                        *_environment(
                            box,
                            {
                                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                                "PIP_NO_CACHE_DIR": "1",
                                "SOURCE_DATE_EPOCH": "0",
                            },
                        ),
                        "--",
                        *command,
                    ]
                    code, output = capture(
                        argv,
                        timeout,
                        env=_host_environment(),
                        limit=MAX_COLLECTION,
                        cancellation=cancellation,
                    )
                    if code != 0:
                        message = _bwrap_message(output)
                        if message is not None:
                            raise InfrastructureError("sandbox startup failed: " + message)
                        raise UnsupportedCapability(EDITABLE_CAPABILITY, failure_message)

                run_step(
                    [
                        box.executable,
                        "-m",
                        "venv",
                        "--system-site-packages",
                        "--without-pip",
                        "/env",
                    ],
                    "could not create isolated virtual environment for editable distributions",
                )
                _configure_base_venv_bridge(box, root, env_stage)
                run_step(
                    [
                        box.executable,
                        "-m",
                        "pip",
                        "--python",
                        "/env/bin/python",
                        "install",
                        "--no-index",
                        "--no-deps",
                        "--no-build-isolation",
                        "--no-compile",
                        *editable_targets,
                    ],
                    "editable distribution installation failed inside Bubblewrap",
                )
                _freeze_editable_environment(env_stage, env_key)
                if not _valid_editable_environment(target, env_key):
                    os.replace(env_stage, target)
                    _fsync_directory(environments_directory)
                return target
            except (CleanupError, DeclarationError):
                raise
            except (InfrastructureError, OSError) as exc:
                if attempt == 1:
                    if isinstance(exc, InfrastructureError):
                        raise
                    raise InfrastructureError(
                        "editable environment storage operation failed"
                    ) from exc
            finally:
                with ignore_termination_signals():
                    _remove_tree_writable(env_stage)
                    _remove_tree_writable(work)


def execute(
    box,
    view,
    args,
    environment,
    timeout,
    *,
    root,
    request,
    invocation_directory,
    suite_root=".",
    suite_cwd=".",
    suite="default",
    metadata_directory=None,
    env_directory=None,
    tool_view=None,
    cancellation=None,
    on_session_start=None,
    selection_disabled=False,
    allow_unknown_selectors=False,
):
    """Run one worker in a fresh view of `view` and return its validated result."""
    view = Path(view).resolve()
    directory = Path(invocation_directory).resolve()
    scratch = _state(root) / "scratch"
    if directory.parent != scratch or view.parent.parent != scratch or view.name != "project":
        raise InfrastructureError("worker requires an owned frozen view")
    # Validate each effective user name before adding trusted worker controls.
    for name in environment:
        environment_names([name])
    if type(request) is not dict or request.get("mode") not in (
        "collection",
        "execution",
        "batch",
        "guard-capture",
        "guard-inspection",
        "guard-batch",
    ):
        raise InfrastructureError("worker requires an explicit request protocol")
    guarded = request["mode"] in ("guard-capture", "guard-inspection", "guard-batch")
    required = (
        {"mode", "registry", "selectors", "context_id"}
        if request["mode"] == "collection"
        else {"mode", "registry", "group_id", "case_ids", "expected_digest", "context_id"}
        if request["mode"] == "guard-capture"
        else {
            "mode",
            "registry",
            "group_id",
            "case_ids",
            "expected_digest",
            "context_id",
            "registry_id",
        }
        if request["mode"] == "guard-inspection"
        else {
            "mode",
            "registry",
            "group_id",
            "case_ids",
            "expected_digest",
            "context_id",
            "registry_id",
            "graph_id",
            "guard_protocol",
        }
        if request["mode"] == "guard-batch"
        else {"mode", "registry", "group_id", "case_ids", "expected_digest", "context_id"}
        if request["mode"] == "batch"
        else {"mode", "registry", "case_id", "expected_digest", "context_id"}
    )
    if set(request) != required or type(request["registry"]) is not list:
        raise InfrastructureError("worker request fields do not match its protocol")
    if (
        type(request["context_id"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", request["context_id"])
        or (
            request["mode"] in ("batch", "guard-capture", "guard-inspection", "guard-batch")
            and request["context_id"] != request["group_id"]
        )
    ):
        raise InfrastructureError("worker request context does not match its protocol")
    if guarded and (
        type(request["case_ids"]) is not list
        or not 0 < len(request["case_ids"]) <= 4096
        or any(
            type(case) is not str or not re.fullmatch(r"[0-9a-f]{64}", case)
            for case in request["case_ids"]
        )
        or len(set(request["case_ids"])) != len(request["case_ids"])
        or type(request["expected_digest"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", request["expected_digest"])
        or (
            request["mode"] in ("guard-inspection", "guard-batch")
            and (
                type(request["registry_id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", request["registry_id"])
            )
        )
        or (
            request["mode"] == "guard-batch"
            and (
                request["guard_protocol"] != "batch-guard-v1"
                or type(request["graph_id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", request["graph_id"])
            )
        )
    ):
        raise InfrastructureError("invalid guarded worker request")
    if metadata_directory is not None:
        metadata_directory = Path(metadata_directory).resolve()
        if metadata_directory.parent != directory or metadata_directory.name != "metadata":
            raise InfrastructureError("metadata requires an owned worker directory")
    if env_directory is not None:
        env_directory = Path(env_directory).resolve()
        if (
            env_directory.is_symlink()
            or not env_directory.is_dir()
            or not (env_directory / ENV_MARKER).is_file()
        ):
            raise InfrastructureError("worker requires a validated editable environment")
    nonce = uuid.uuid4().hex + uuid.uuid4().hex if guarded else uuid.uuid4().hex
    guard_envelope = None
    if request["mode"] == "guard-capture":
        guard_envelope = {
            "protocol": "fixture-capture-v1",
            "nonce": nonce,
            "base_context_id": request["context_id"],
            "case_ids": request["case_ids"],
        }
    elif request["mode"] == "guard-inspection":
        guard_envelope = {
            "protocol": "fixture-inspection-v1",
            "nonce": nonce,
            "context_id": request["context_id"],
            "group_id": request["group_id"],
            "case_ids": request["case_ids"],
            "registry_id": request["registry_id"],
        }
    control = directory / "control"
    control.mkdir(exist_ok=True)
    request_path = control / "request.json"
    project = Path(root).resolve()
    budget = 0
    with request_path.open("wb") as stream:
        for chunk in json.JSONEncoder(separators=(",", ":"), allow_nan=False).iterencode(
            {
                **request,
                "args": args,
                "nonce": nonce,
                "project_root": str(PROJECT),
                "suite_root": suite_root,
                "suite": suite,
                "metadata": metadata_directory is not None,
                "selection_disabled": selection_disabled,
                "allow_unknown_selectors": allow_unknown_selectors,
                **({"guard_envelope": guard_envelope} if guard_envelope is not None else {}),
            }
        ):
            encoded = chunk.encode()
            budget += len(encoded)
            if budget > MAX_COLLECTION:
                raise InfrastructureError("worker request exceeded 64 MiB")
            stream.write(encoded)
    request_path.chmod(0o444)
    prefixes = ()
    native = False
    if tool_view is not None:
        from .tool_capture import ToolView

        if (
            type(tool_view) is not ToolView
            or tool_view.directory.parent.parent != scratch
            or tool_view.directory.name != "tools"
        ):
            raise InfrastructureError("tools require an owned frozen invocation")
        if tool_view.names and (
            any(
                _inside(Path(destination), Path("/tools"))
                or _inside(Path("/tools"), Path(destination))
                for _, destination in (*box.binds, *box.links)
            )
            or (
                project != PROJECT
                and any(
                    _inside(Path(destination), PROJECT) or _inside(PROJECT, Path(destination))
                    for _, destination in tool_view.bindings
                )
            )
        ):
            raise UnsupportedCapability(
                "tool:layout", "frozen-tool namespace overlaps the invoking environment or project"
            )
        for source, destination in tool_view.bindings:
            if not source.is_relative_to(tool_view.directory) or source.is_symlink():
                raise InfrastructureError("tool binding is outside its owned frozen material")
        prefixes = tool_view.prefixes
        native = tool_view.native
        if type(native) is not bool:
            raise InfrastructureError("tool view has an invalid native profile")
        if native:
            if env_directory is not None:
                raise UnsupportedCapability(
                    "tool:layout",
                    "editable project distributions are unsupported with closed native startup",
                )
            from .tool_startup import environment as startup_environment
            from .tool_startup import signature

            if signature(box) != tool_view.startup_identity:
                raise InfrastructureError("native startup environment changed after projection")
            destinations = {destination: source for source, destination in tool_view.bindings}
            if "/etc" not in tool_view.closed or any(
                name not in destinations for name in tool_view.closed
            ):
                raise InfrastructureError("native view lacks its closed startup projections")
            for destination in tool_view.closed:
                if not destinations[destination].is_dir():
                    raise InfrastructureError("native closed projection is not an owned directory")
            box = replace(box, binds=startup_environment(box, tool_view.closed), links=())
        from .tool_context import validate_environment

        validate_environment(environment, tool_view.names)
    argv = [
        *_base_arguments(
            box,
            PROJECT,
            frozen_prefixes=prefixes,
            native=native,
            closed_directories=() if tool_view is None else tool_view.closed,
        ),
        *_project_arguments(box, view),
        # Before the tool bindings, so a selected frozen entry keeps its exact bytes.
        *_overlay_arguments(box, control / "startup", system=not native),
    ]
    if tool_view is not None:
        for source, destination in tool_view.bindings:
            argv += ["--ro-bind", str(source), destination]
        # A tool that keeps its original path is mounted there once; its /tools path
        # is a link to it.
        for target, location in tool_view.links:
            if not _inside(Path(location), Path("/tools")):
                raise InfrastructureError("tool link is outside the tool namespace")
            argv += ["--symlink", target, location]
    if metadata_directory is not None:
        argv += ["--ro-bind", str(metadata_directory), "/metadata"]
    if env_directory is not None:
        argv += ["--ro-bind", str(env_directory), "/env"]
    path_entries = []
    if tool_view is not None and tool_view.command_path is not None:
        path_entries.append(tool_view.command_path)
    if env_directory is not None:
        path_entries.append("/env/bin")
    path_entries.extend(box.path.split(":"))
    controlled = replace(box, path=":".join(dict.fromkeys(path_entries)))
    worker_executable = "/env/bin/python" if env_directory is not None else box.executable
    startup = [worker_executable, "-I", "-B", WORKER_PATH]
    argv += [
        "--ro-bind",
        str(WORKER),
        WORKER_PATH,
        "--ro-bind",
        str(request_path),
        REQUEST,
        "--remount-ro",
        "/",
        "--chdir",
        str(PROJECT / suite_cwd),
        *_environment(controlled, {**environment, "TELESCOPE_REQUEST": REQUEST}),
        "--",
        *startup,
    ]
    code, output = capture(
        argv,
        timeout,
        env=_host_environment(),
        limit=MAX_COLLECTION
        if request["mode"]
        in {"collection", "batch", "guard-capture", "guard-inspection", "guard-batch"}
        else MAX_OUTPUT,
        cancellation=cancellation,
        on_start=on_session_start,
    )
    try:
        unsupported = [
            line[len(b"TELESCOPE_UNSUPPORTED ") :]
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_UNSUPPORTED ")
        ]
        if (
            code == 4
            and len(unsupported) == 1
            and json.loads(unsupported[0]) == {"capability": "project-metadata:runtime-conflict"}
        ):
            raise UnsupportedCapability(
                "project-metadata:runtime-conflict",
                "local metadata conflicts with a distribution installed in the environment",
            )
        if (
            code == 4
            and len(unsupported) == 1
            and json.loads(unsupported[0]) == {"capability": "execution:fixture-metadata"}
        ):
            raise UnsupportedCapability(
                "execution:fixture-metadata", "fixture metadata cannot be validated"
            )
        reports = [
            line[len(b"TELESCOPE_RESULT ") :]
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        if len(reports) != 1:
            message = _bwrap_message(output)
            if tool_view is not None and tool_view.names and message:
                message = "the frozen tool filesystem view could not start"
            raise InfrastructureError(
                "sandbox startup failed: " + message
                if message
                else "worker exited without one complete result"
            )
        result = json.loads(reports[0])
        if request["mode"] in ("guard-capture", "guard-inspection"):
            if type(result) is dict and result.get("protocol") == "fixture-refusal-v1":
                if code != 4 or result != {
                    "protocol": "fixture-refusal-v1",
                    "nonce": nonce,
                    "context_id": request["context_id"],
                    "mode": request["mode"],
                    "reason": "declaration",
                }:
                    raise InfrastructureError("invalid fixture declaration refusal")
                raise DeclarationError(
                    "case collection or declarations differ from validated inventory"
                )
            from importlib.metadata import version

            from .fixture_guard import (
                framework_definition_ids,
                validate_capture,
                validate_inspection,
            )

            if code != 0:
                raise InfrastructureError("fixture inspection worker failed")
            pytest_version = dict(box.description.get("distributions", ())).get(
                "pytest"
            ) or version("pytest")
            expected_framework = framework_definition_ids(pytest_version)
            if request["mode"] == "guard-capture":
                return validate_capture(
                    result,
                    nonce=nonce,
                    base_context_id=request["context_id"],
                    case_ids=request["case_ids"],
                    framework_ids=expected_framework,
                )
            return validate_inspection(
                result,
                nonce=nonce,
                group_id=request["group_id"],
                case_ids=request["case_ids"],
                registry_id=request["registry_id"],
                framework_ids=expected_framework,
            )
        if type(result) is dict and result.get("leaked_descendants") is True:
            raise InfrastructureError("worker left descendant processes running")
        valid = (
            validate_collection(result, nonce=nonce, context_id=request["context_id"])
            if request["mode"] == "collection"
            else validate_batch(
                result,
                nonce=nonce,
                group_id=request["group_id"],
                case_ids=request["case_ids"],
                context_id=request["context_id"],
            )
            if request["mode"] == "batch"
            else validate_guarded_batch(
                result,
                nonce=nonce,
                group_id=request["group_id"],
                case_ids=request["case_ids"],
                registry_id=request["registry_id"],
                graph_id=request["graph_id"],
            )
            if request["mode"] == "guard-batch"
            else validate_result(
                result, nonce=nonce, case_id=request["case_id"], context_id=request["context_id"]
            )
        )
        if not valid or result["exit_code"] != code:
            raise InfrastructureError("worker result identity, completion or exit status mismatch")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, DeclarationError):
            raise
        raise InfrastructureError("worker result validation failed") from exc
