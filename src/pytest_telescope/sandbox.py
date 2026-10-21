"""Bubblewrap filesystem views over the invoking interpreter.

The sandbox decides only what a scoped test can see. It sets no resource limits and
measures no resource usage: every sandbox is a plain child process of the invocation,
so it stays inside whatever limits the caller applies to pytest.
"""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .model import DeclarationError, InfrastructureError, UnsupportedCapability, fingerprint
from .result import validate_batch, validate_collection, validate_result

try:
    import fcntl
except ImportError:  # Importing the optional adapter remains safe on other hosts.
    fcntl = None

BACKEND = "bubblewrap"
POLICY = "bubblewrap-view-v1"
MAX_OUTPUT = 1_048_576
MAX_COLLECTION = 67_108_864
MAX_PROBE = 4 * 1_048_576
MAX_INVOCATIONS = 64
WORKER = Path(__file__).with_name("worker.py")
REQUEST = "/telescope/request.json"
WORKER_PATH = "/telescope/worker.py"
READY = "TELESCOPE_SANDBOX_READY"
SYSTEM_DIRECTORIES = ("/usr", "/etc")
SYSTEM_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# Paths the sandbox mounts itself; an environment directory may not overlap them.
MOUNT_POINTS = ("/project", "/metadata", "/telescope", "/proc", "/dev")
# User, mount and PID namespaces only. A PID namespace with its own /proc keeps other
# processes' root and working directories, which are host paths, out of the view. The
# worker is PID 1, so it reaps orphans and every remaining process ends with it.
NAMESPACES = ("--unshare-user", "--unshare-pid", "--as-pid-1", "--die-with-parent")
PROBE = r"""
import json, re, sys, sysconfig
from importlib import metadata

def normalize(name):
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()

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
}, sort_keys=True))
"""


class CleanupError(InfrastructureError):
    """Terminal owned-resource failure: callers must not start another sandbox."""


class InvocationInterrupted(CleanupError):
    """Terminal controller interruption after bounded owned-resource cleanup."""


@dataclass(frozen=True)
class Sandbox:
    """A validated view of the invoking interpreter, ready to start workers."""

    bwrap: str
    executable: str
    binds: tuple
    path: str
    identity: str
    description: dict


def _host_environment():
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}


def _terminate(process):
    # Killing the process group reaches bwrap and, while it shares that group, the
    # sandbox's PID 1; --die-with-parent covers the rest of the sandbox.
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


def capture(args, timeout, *, env, cwd=None, limit=MAX_OUTPUT, cancellation=None):
    """Run one child with bounded merged output; never retain more than the limit.

    A timeout of None sets no deadline: the caller's own job bounds that session.
    """
    process = None
    output = bytearray()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
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
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CleanupError("owned frozen copy cleanup failed") from exc


def recover(root):
    """Remove frozen copies left by invocations whose owner no longer holds its lock."""
    state = _state(root)
    scratch = state / "scratch"
    with _allocation(state):
        with os.scandir(scratch) as entries:
            paths = []
            for entry in entries:
                if len(paths) >= MAX_INVOCATIONS:
                    raise CleanupError("invocation directory budget exceeded; recovery stopped")
                if not re.fullmatch(r"invocation-[0-9a-f]{32}", entry.name):
                    raise CleanupError("unrecognized scratch entry; recovery stopped")
                if not entry.is_dir(follow_symlinks=False):
                    raise CleanupError("invocation directory changed type; recovery stopped")
                paths.append(Path(entry.path))
    deadline = time.monotonic() + 60
    for directory in paths:
        if time.monotonic() > deadline:
            raise CleanupError("invocation recovery budget exceeded; rerun after inspection")
        try:
            descriptor = os.open(directory / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            # A concurrent invocation may have completed since directory discovery.
            if not directory.exists():
                continue
            raise CleanupError("incomplete invocation directory; recovery stopped") from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if directory.exists():
                _remove(directory)
        finally:
            os.close(descriptor)


@contextmanager
def invocation(root):
    """A private scratch directory, locked by its owner and removed afterwards."""
    state = _state(root)
    scratch = state / "scratch"
    with _allocation(state):
        with os.scandir(scratch) as entries:
            for count, _ in enumerate(entries, 1):
                if count >= MAX_INVOCATIONS:
                    raise CleanupError("invocation directory budget exhausted")
        directory = scratch / ("invocation-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory / "owner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    previous = {}

    def interrupted(signum, frame):
        raise InvocationInterrupted("controller interrupted; owned sandboxes stopped")

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, interrupted)
    try:
        yield directory
    finally:
        # Cleanup must finish even when a second termination signal arrives.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            _remove(directory)
        finally:
            os.close(descriptor)
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def _probe(executable):
    """Describe an interpreter exactly as an isolated child of it sees its environment."""
    code, output = capture(
        [executable, "-I", "-c", PROBE],
        30,
        env=_host_environment(),
        cwd="/",
        limit=MAX_PROBE,
    )
    if code:
        raise UnsupportedCapability(
            "sandbox:environment", "the invoking interpreter could not describe its environment"
        )
    try:
        data = json.loads(output)
        valid = (
            type(data) is dict
            and set(data) == {"executable", "prefixes", "path", "identity", "distributions"}
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


def _binds(description, project):
    """Map the interpreter's prefixes and import directories into the view, read-only.

    An environment prefix that contains the project is refused: binding it would expose
    the project. An import directory that contains or lies inside the project is not
    bound, because project files are visible only through declarations.
    """
    system = [Path(os.path.realpath(name)) for name in SYSTEM_DIRECTORIES]
    if any(_inside(project, directory) for directory in system):
        _refuse("the project cannot live inside a system directory shared with the sandbox")
    prefixes = {
        os.path.normpath(name)
        for name in [*description["prefixes"], os.path.dirname(description["executable"])]
    }
    binds = {}
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
            if _inside(Path("/tmp"), path) or any(
                _inside(path, Path(point)) or _inside(Path(point), path) for point in MOUNT_POINTS
            ):
                _refuse("an interpreter environment directory overlaps a sandbox mount point")
        for destination in {given, real}:
            binds[str(destination)] = str(real)
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
    return tuple((source, destination) for destination, source in ordered)


def _base_arguments(box):
    arguments = [box.bwrap, *NAMESPACES]
    for directory in SYSTEM_DIRECTORIES:
        arguments += ["--ro-bind", directory, directory]
    for link in SYSTEM_LINKS:
        if os.path.islink(link):
            arguments += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            arguments += ["--ro-bind", link, link]
    arguments += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm"]
    arguments += ["--remount-ro", "/dev", "--tmpfs", "/tmp"]
    for source, destination in box.binds:
        arguments += ["--ro-bind", source, destination]
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
    """Start the interpreter in the view once; classify a refusal before any test runs."""
    argv = [
        *_base_arguments(box),
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


def describe(root, executable=None):
    """The view of an interpreter's environment and its cache identity, without bwrap.

    The identity covers the interpreter build and every installed distribution visible
    to it in isolated mode, so installing or upgrading a package changes it.
    """
    description = _probe(executable or sys.executable)
    folders = [os.path.dirname(description["executable"]), "/usr/local/bin", "/usr/bin", "/bin"]
    return Sandbox(
        bwrap=None,
        executable=description["executable"],
        binds=_binds(description, Path(root).resolve()),
        path=":".join(dict.fromkeys(folders)),
        identity=fingerprint(
            {
                "policy": POLICY,
                "interpreter": description["identity"],
                "distributions": description["distributions"],
            }
        ),
        description=description,
    )


def prepare(root):
    """Probe the invoking interpreter and check Bubblewrap before any session starts."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise UnsupportedCapability(
            "sandbox:bubblewrap",
            "scoped tests need Bubblewrap; install the bwrap command (package bubblewrap)",
        )
    for attempt in range(2):
        try:
            box = replace(describe(root), bwrap=bwrap)
            _preflight(box)
            return box
        except (CleanupError, UnsupportedCapability):
            raise
        except InfrastructureError:
            if attempt:
                raise


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
    cancellation=None,
    selection_disabled=False,
    allow_unknown_selectors=False,
):
    """Run one worker in a fresh view of `view` and return its validated result."""
    view = Path(view).resolve()
    directory = Path(invocation_directory).resolve()
    scratch = _state(root) / "scratch"
    if directory.parent != scratch or view.parent.parent != scratch or view.name != "project":
        raise InfrastructureError("worker requires an owned frozen view")
    if any(key.startswith("TELESCOPE_") for key in environment):
        raise DeclarationError("environment must not override worker protocol")
    if type(request) is not dict or request.get("mode") not in ("collection", "execution", "batch"):
        raise InfrastructureError("worker requires an explicit request protocol")
    required = (
        {"mode", "registry", "selectors"}
        if request["mode"] == "collection"
        else {"mode", "registry", "group_id", "case_ids", "expected_digest"}
        if request["mode"] == "batch"
        else {"mode", "registry", "case_id", "expected_digest"}
    )
    if set(request) != required or type(request["registry"]) is not list:
        raise InfrastructureError("worker request fields do not match its protocol")
    if metadata_directory is not None:
        metadata_directory = Path(metadata_directory).resolve()
        if metadata_directory.parent != directory or metadata_directory.name != "metadata":
            raise InfrastructureError("metadata requires an owned worker directory")
    nonce = uuid.uuid4().hex
    control = directory / "control"
    control.mkdir(exist_ok=True)
    request_path = control / "request.json"
    budget = 0
    with request_path.open("wb") as stream:
        for chunk in json.JSONEncoder(separators=(",", ":"), allow_nan=False).iterencode(
            {
                **request,
                "args": args,
                "nonce": nonce,
                "suite_root": suite_root,
                "suite": suite,
                "metadata": metadata_directory is not None,
                "selection_disabled": selection_disabled,
                "allow_unknown_selectors": allow_unknown_selectors,
            }
        ):
            encoded = chunk.encode()
            budget += len(encoded)
            if budget > MAX_COLLECTION:
                raise InfrastructureError("worker request exceeded 64 MiB")
            stream.write(encoded)
    request_path.chmod(0o444)
    argv = [*_base_arguments(box), "--ro-bind", str(view), "/project"]
    if metadata_directory is not None:
        argv += ["--ro-bind", str(metadata_directory), "/metadata"]
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
        "/project/" + suite_cwd,
        *_environment(box, {**environment, "TELESCOPE_REQUEST": REQUEST}),
        "--",
        box.executable,
        "-I",
        WORKER_PATH,
    ]
    code, output = capture(
        argv,
        timeout,
        env=_host_environment(),
        limit=MAX_COLLECTION if request["mode"] in {"collection", "batch"} else MAX_OUTPUT,
        cancellation=cancellation,
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
        reports = [
            line[len(b"TELESCOPE_RESULT ") :]
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        if len(reports) != 1:
            message = _bwrap_message(output)
            raise InfrastructureError(
                "sandbox startup failed: " + message
                if message
                else "worker exited without one complete result"
            )
        result = json.loads(reports[0])
        if type(result) is dict and result.get("leaked_descendants") is True:
            raise InfrastructureError("worker left descendant processes running")
        valid = (
            validate_collection(result, nonce=nonce)
            if request["mode"] == "collection"
            else validate_batch(
                result, nonce=nonce, group_id=request["group_id"], case_ids=request["case_ids"]
            )
            if request["mode"] == "batch"
            else validate_result(result, nonce=nonce, case_id=request["case_id"])
        )
        if not valid or result["exit_code"] != code:
            raise InfrastructureError("worker result identity, completion or exit status mismatch")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, DeclarationError):
            raise
        raise InfrastructureError("worker result validation failed") from exc
