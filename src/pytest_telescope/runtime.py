"""Clean public runtime construction and bounded Docker workers."""

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import python_source, runtime_profiles
from .model import DeclarationError, InfrastructureError, UnsupportedCapability, fingerprint
from .result import validate_batch, validate_collection, validate_result

try:
    import fcntl
except ImportError:  # Importing the optional adapter remains safe on other hosts.
    fcntl = None

BASE = "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
POLICY = "docker-nonroot-ro-none-v4"
DEFAULT_REQUIREMENTS = (
    "pytest==9.1.1\niniconfig==2.3.0\npackaging==25.0\npluggy==1.6.0\npygments==2.19.2\n"
)
MAX_OUTPUT = 1_048_576
MAX_COLLECTION = 67_108_864
MAX_IMAGE_SIZE = 2_147_483_648
MAX_IMAGES = 4
MAX_INVOCATIONS = 64
LABEL = "org.pytest-telescope."


class CleanupError(InfrastructureError):
    """Terminal owned-resource failure: callers must not start another worker."""


class InvocationInterrupted(CleanupError):
    """Terminal controller interruption after bounded owned-resource cleanup."""


def _terminate(process):
    # The client can exit while its descendants still hold output descriptors.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise CleanupError("local container client cleanup deadline exceeded") from exc
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _capture(args, timeout, *, merged=False, on_start=None, limit=MAX_OUTPUT, cancellation=None):
    """Bound both streams while running; never retain unrestricted stderr."""
    process = None
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merged else subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        if on_start is not None:
            on_start(process)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            if not merged:
                selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                if cancellation is not None and cancellation.is_set():
                    raise InvocationInterrupted("worker cancelled; owned cleanup required")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("container command deadline exceeded")
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit + 1 - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise InfrastructureError(
                            f"container command output exceeded {limit // 1_048_576} MiB"
                        )
                    if key.data:
                        output.extend(chunk)
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfrastructureError(
            "container service unavailable or command deadline exceeded"
        ) from exc
    finally:
        if process is not None:
            _terminate(process)


def command(args, timeout=30):
    code, output = _capture(args, timeout)
    if code:
        raise InfrastructureError(
            "container command failed; verify Docker availability and runtime setup"
        )
    return output


def _read(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InfrastructureError("runtime state must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InfrastructureError("runtime state exceeded its size limit")
    return data


def _durable(path, data):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _start_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + fields[19]
    except (OSError, IndexError):
        return None


def _project(root):
    return hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()


def _state(root):
    if fcntl is None:
        raise InfrastructureError("Linux Docker backend requires advisory file locking")
    state = Path(root).resolve() / ".pytest-telescope"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.is_symlink() or not state.is_dir():
        raise InfrastructureError("runtime state must be an owned directory")
    return state


def _local_endpoint():
    if os.environ.get("DOCKER_CONTEXT") or not os.environ.get("DOCKER_HOST"):
        endpoint = (
            command(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                timeout=10,
            )
            .decode()
            .strip()
        )
    else:
        endpoint = os.environ["DOCKER_HOST"]
    if not endpoint.startswith("unix:///"):
        raise InfrastructureError("the bind-mount backend requires a local Unix Docker endpoint")
    return endpoint


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
                    raise CleanupError("invocation registration lock deadline exceeded")
                time.sleep(0.02)
        yield
    finally:
        os.close(descriptor)


def _journal(root, directory):
    data = json.loads(_read(directory / "journal.json", 16384))
    token = directory.name.removeprefix("invocation-")
    project = _project(root)
    name = "pytest-telescope-" + project[:12] + "-" + token
    if (
        not re.fullmatch(r"[0-9a-f]{32}", token)
        or data.get("version") != 1
        or data.get("project") != project
        or data.get("name") != name
        or data.get("token") != token
        or data.get("snapshot") != "scratch/" + directory.name + "/project"
        or type(data.get("owner_pid")) is not int
        or type(data.get("owner_start")) is not str
        or type(data.get("cleaned")) is not bool
    ):
        raise CleanupError("invalid owned invocation journal; recovery stopped")
    return data


def _stop_client(data):
    pid, started = data.get("client_pid"), data.get("client_start")
    if type(pid) is int and type(started) is str and _start_identity(pid) == started:
        try:
            if os.getpgid(pid) != pid:
                raise CleanupError("owned container client process group changed")
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while _start_identity(pid) == started:
            if time.monotonic() >= deadline:
                raise CleanupError("owned container client cleanup deadline exceeded")
            time.sleep(0.02)


def _stop_container(data):
    name = data["name"]
    try:
        _stop_client(data)
        ids = (
            command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    "name=^/" + name + "$",
                    "--format",
                    "{{.ID}}",
                ],
                timeout=10,
            )
            .decode()
            .splitlines()
        )
        if not ids:
            return
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
            raise ValueError("ambiguous container identity")
        info = json.loads(command(["docker", "inspect", ids[0]], timeout=10))[0]
        labels = info["Config"]["Labels"]
        if (
            info["Id"] != ids[0]
            or info["Name"] != "/" + name
            or labels.get(LABEL + "project") != data["project"]
            or labels.get(LABEL + "invocation") != data["token"]
        ):
            raise ValueError("container ownership mismatch")
        command(["docker", "rm", "-f", ids[0]], timeout=10)
    except (InfrastructureError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise CleanupError("worker cleanup failed for owned invocation " + name) from exc


def recover(root):
    """Recover only stale, strictly named, durably journaled project invocations."""
    state = _state(root)
    scratch = state / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    if scratch.is_symlink():
        raise CleanupError("scratch directory must not be a symlink")
    with _allocation(state):
        with os.scandir(scratch) as entries:
            paths = []
            for entry in entries:
                if len(paths) >= MAX_INVOCATIONS:
                    raise CleanupError("invocation journal budget exceeded; recovery stopped")
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
            raise CleanupError("incomplete invocation journal; recovery stopped") from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if not directory.exists():
                continue
            data = _journal(root, directory)
            if _start_identity(data["owner_pid"]) == data["owner_start"]:
                continue
            if not data["cleaned"]:
                _stop_container(data)
            shutil.rmtree(directory)
        except (OSError, ValueError, TypeError) as exc:
            raise CleanupError("owned invocation recovery failed") from exc
        finally:
            os.close(descriptor)


@contextmanager
def invocation(root):
    """Journal before snapshot construction; preserve failed cleanup for recovery."""
    state = _state(root)
    scratch = state / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    if scratch.is_symlink():
        raise CleanupError("scratch directory must not be a symlink")
    with _allocation(state):
        with os.scandir(scratch) as entries:
            for count, _ in enumerate(entries, 1):
                if count >= MAX_INVOCATIONS:
                    raise CleanupError("invocation journal budget exhausted")
        token = uuid.uuid4().hex
        directory = scratch / ("invocation-" + token)
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory / "owner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        project = _project(root)
        data = {
            "version": 1,
            "project": project,
            "token": token,
            "name": "pytest-telescope-" + project[:12] + "-" + token,
            "owner_pid": os.getpid(),
            "owner_start": _start_identity(os.getpid()),
            "snapshot": "scratch/" + directory.name + "/project",
            "cleaned": True,
        }
        _durable(directory / "journal.json", data)
    previous = {}

    def interrupted(signum, frame):
        raise InvocationInterrupted("controller interrupted; invocation " + data["name"])

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
            data = _journal(root, directory)
            if not data["cleaned"]:
                _stop_container(data)
            shutil.rmtree(directory)
        except (OSError, ValueError, TypeError) as exc:
            raise CleanupError("owned invocation cleanup failed for " + data["name"]) from exc
        finally:
            os.close(descriptor)
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def worker_source():
    return _read(Path(__file__).with_name("worker.py"), 131072)


def _inspect_owned_image(image, project, *, attempts=1):
    if type(image) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise InfrastructureError("runtime did not return an immutable image ID")
    # Only the cleanup caller requests a second read. Current-runtime inspection
    # retains its existing outer retry owner, and validation failures never retry.
    for attempt in range(attempts):
        try:
            output = command(["docker", "image", "inspect", image])
            break
        except CleanupError:
            raise
        except InfrastructureError:
            if attempt + 1 == attempts:
                raise
    try:
        response = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise InfrastructureError("runtime image inspection returned invalid JSON") from exc
    if type(response) is not list or len(response) != 1 or type(response[0]) is not dict:
        raise InfrastructureError("runtime image inspection requires one image object")
    info = response[0]
    config = info.get("Config")
    if type(config) is not dict or type(config.get("Labels")) is not dict:
        raise InfrastructureError("runtime image inspection requires explicit ownership labels")
    labels = config["Labels"]
    if (
        info.get("Id") != image
        or labels.get(LABEL + "project") != project
        or type(info.get("Size")) is not int
        or info["Size"] < 0
    ):
        raise InfrastructureError("runtime image ownership or identity mismatch")
    return info


def _image_info(image, project, recipe=None):
    """Qualify only the current execution policy; historical ownership is insufficient."""
    info = _inspect_owned_image(image, project)
    labels = info["Config"]["Labels"]
    if labels.get(LABEL + "policy") != POLICY or (
        recipe is not None and labels.get(LABEL + "recipe") != recipe
    ):
        raise InfrastructureError("runtime image ownership or identity mismatch")
    return info


def _retained_image_info(image, project):
    """Inspect registered historical ownership for cleanup, never for execution."""
    info = _inspect_owned_image(image, project, attempts=2)
    labels = info["Config"]["Labels"]
    policy = labels.get(LABEL + "policy")
    recipe = labels.get(LABEL + "recipe")
    if (
        type(policy) is not str
        or policy not in {"docker-nonroot-ro-none-v2", "docker-nonroot-ro-none-v3", POLICY}
        or type(recipe) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", recipe)
    ):
        raise InfrastructureError("retained runtime image policy or recipe is unrecognized")
    if policy == POLICY:
        provenance = labels.get(LABEL + "provenance")
        profile = labels.get(LABEL + "profile")
        if (
            type(profile) is not str
            or profile not in {runtime_profiles.DEFAULT, runtime_profiles.GIT}
            or type(provenance) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", provenance)
            or labels.get(LABEL + "source-slot") != python_source.SLOT_ID
        ):
            raise InfrastructureError("retained runtime image provenance is invalid")
    return info


def build_runtime(root, requirements=None, *, runtime_profile=None):
    root = Path(root).resolve()
    selection = runtime_profiles.normalize(runtime_profiles.config(root), runtime_profile)
    for probe in range(2):
        try:
            runtime_profiles.validate_backend(selection, command)
            break
        except CleanupError:
            raise
        except InfrastructureError:
            if probe:
                raise
    profile = selection["profile"]
    provenance = runtime_profiles.provenance(profile)
    state = _state(root)
    _local_endpoint()
    recover(root)
    pins = DEFAULT_REQUIREMENTS
    if requirements:
        try:
            content = _read(Path(requirements), 65536).decode()
        except (InfrastructureError, OSError, UnicodeError) as exc:
            raise DeclarationError(
                "runtime requirements require a regular UTF-8 file of at most 64 KiB"
            ) from exc
        extra = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*", line):
                raise DeclarationError(
                    "runtime dependencies require exact package==version pins; no paths or URLs"
                )
            extra.append(line)
        pins += "\n".join(extra) + "\n"
    source = worker_source()
    project = _project(root)
    recipe = hashlib.sha256(
        BASE.encode() + pins.encode() + source + POLICY.encode() + provenance.encode()
    ).hexdigest()
    lock = os.open(state / "runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InfrastructureError("another runtime preparation is active") from exc
        try:
            images = json.loads(_read(state / "images.json", 8192))
        except FileNotFoundError:
            images = []
        if (
            type(images) is not list
            or len(images) > MAX_IMAGES
            or any(
                type(image) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
                for image in images
            )
            or len(set(images)) != len(images)
        ):
            raise InfrastructureError("runtime image registry is invalid")
        # Protect the currently selected image even when it is the oldest entry.
        try:
            previous = json.loads(_read(state / "runtime.json", 16384))
            protected = previous["image"]
        except FileNotFoundError:
            protected = None
        while len(images) >= MAX_IMAGES:
            candidates = [item for item in images if item != protected]
            if not candidates:
                raise InfrastructureError("runtime image quota cannot preserve current preparation")
            old = candidates[0]
            _retained_image_info(old, project)
            try:
                command(["docker", "image", "rm", old], timeout=30)
            except InfrastructureError as exc:
                raise CleanupError(
                    "retained runtime image removal unconfirmed; preparation stopped"
                ) from exc
            images.remove(old)
            _durable(state / "images.json", images)
        verified_archive = None
        attempts = 2 if profile == runtime_profiles.GIT else 1
        for attempt in range(attempts):
            build_failed = False
            with invocation(root) as directory:
                context = directory / "build"
                context.mkdir()
                build_data = _journal(root, directory)
                prefix, environment = f"FROM {BASE}\n", ""
                if profile == runtime_profiles.GIT:
                    if verified_archive is None:
                        verified_archive = runtime_profiles.fetch_archive(
                            context,
                            lambda args, timeout: _owned_capture(
                                directory, build_data, args, timeout
                            ),
                            terminal=CleanupError,
                        )
                    else:
                        (context / "git.deb").write_bytes(verified_archive)
                    prefix, environment = runtime_profiles.dockerfile(context, BASE)
                (context / "worker.py").write_bytes(source)
                (context / "requirements.txt").write_text(pins)
                (context / "Dockerfile").write_text(
                    prefix
                    + environment
                    + "ENV PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1\n"
                    "COPY requirements.txt /opt/telescope/requirements.txt\n"
                    "RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: "
                    "-r /opt/telescope/requirements.txt\n"
                    "RUN python -m pip check\n"
                    "COPY worker.py /opt/telescope/worker.py\n"
                    "RUN chmod 0444 /opt/telescope/worker.py\n"
                    + python_source.dockerfile()
                    + f'LABEL {LABEL}source-slot="{python_source.SLOT_ID}"\n'
                    f'LABEL {LABEL}policy="{POLICY}" {LABEL}recipe="{recipe}" '
                    f'{LABEL}project="{project}" {LABEL}profile="{profile}" '
                    f'{LABEL}provenance="{provenance}"\n'
                    "ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 "
                    "HOME=/tmp TMPDIR=/tmp\n"
                    "USER 65534:65534\nWORKDIR /project\n"
                )
                build_options = (
                    ["--progress=plain", "--iidfile", str(context / "image.id")]
                    if profile == runtime_profiles.GIT
                    else ["--quiet"]
                )
                try:
                    code, output = _owned_capture(
                        directory,
                        build_data,
                        [
                            "docker",
                            "build",
                            *build_options,
                            "--pull",
                            "--network=default",
                            str(context),
                        ],
                        300,
                        merged=profile == runtime_profiles.GIT,
                    )
                except CleanupError:
                    raise
                except InfrastructureError:
                    if attempt + 1 == attempts:
                        raise
                    build_failed = True
                else:
                    if runtime_profiles.integrity_failed(output):
                        raise InfrastructureError(
                            "Git native closure integrity verification failed"
                        )
                    if code:
                        if attempt + 1 == attempts:
                            raise InfrastructureError(
                                "runtime image build exhausted bounded attempts"
                            )
                        build_failed = True
                    else:
                        if profile == runtime_profiles.GIT:
                            try:
                                candidates = [_read(context / "image.id", 128).decode().strip()]
                            except (OSError, ValueError, InfrastructureError) as exc:
                                raise CleanupError(
                                    "runtime build did not preserve its immutable image ID"
                                ) from exc
                        else:
                            candidates = output.decode().splitlines()
                        ids = [
                            line
                            for line in candidates
                            if re.fullmatch(r"sha256:[0-9a-f]{64}", line)
                        ]
                        if len(ids) != 1:
                            raise CleanupError("runtime build returned no unique owned image ID")
                        image = ids[0]
                        for inspection in range(attempts):
                            try:
                                info = _image_info(image, project, recipe)
                                break
                            except (
                                InfrastructureError,
                                ValueError,
                                KeyError,
                                IndexError,
                                TypeError,
                            ) as exc:
                                if inspection + 1 == attempts:
                                    raise CleanupError(
                                        "runtime image ownership inspection exhausted; preparation unaccepted"
                                    ) from exc
                        labels = info["Config"]["Labels"]
                        if (
                            labels.get(LABEL + "profile") != profile
                            or labels.get(LABEL + "provenance") != provenance
                            or labels.get(LABEL + "source-slot") != python_source.SLOT_ID
                        ):
                            raise CleanupError("runtime profile image identity mismatch")
                        if image not in images:
                            images.append(image)
                            _durable(state / "images.json", images)
                        if info["Size"] > MAX_IMAGE_SIZE:
                            if image != protected:
                                command(["docker", "image", "rm", image], timeout=30)
                                images.remove(image)
                                _durable(state / "images.json", images)
                            raise InfrastructureError("runtime image exceeded 2 GiB")
            # A new attempt cannot start before the preceding context has cleaned up.
            if not build_failed:
                break
        metadata = {
            "image": image,
            "recipe": recipe,
            "worker": hashlib.sha256(source).hexdigest(),
            "policy": POLICY,
            "profile": profile,
            "provenance": provenance,
            "source_slot": python_source.SLOT_ID,
        }
        _durable(state / "runtime.json", metadata)
        return metadata
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise InfrastructureError("runtime preparation failed") from exc
    finally:
        os.close(lock)


def identity(root, *, runtime_profile=None):
    selection = runtime_profiles.normalize(runtime_profiles.config(root), runtime_profile)
    runtime_profiles.validate_backend(selection, command)
    profile = selection["profile"]
    provenance = runtime_profiles.provenance(profile)
    try:
        endpoint = _local_endpoint()
        metadata = json.loads(_read(Path(root) / ".pytest-telescope/runtime.json", 16384))
        if (
            metadata["worker"] != hashlib.sha256(worker_source()).hexdigest()
            or metadata["policy"] != POLICY
            or metadata["profile"] != profile
            or metadata["provenance"] != provenance
            or metadata["source_slot"] != python_source.SLOT_ID
        ):
            raise InfrastructureError("runtime is stale; run the runtime command again")
        inspected = _image_info(metadata["image"], _project(root), metadata["recipe"])
        labels = inspected["Config"]["Labels"]
        if (
            labels.get(LABEL + "profile") != profile
            or labels.get(LABEL + "provenance") != provenance
            or labels.get(LABEL + "source-slot") != python_source.SLOT_ID
        ):
            raise InfrastructureError("prepared runtime profile does not match requested identity")
        if (
            profile == runtime_profiles.GIT
            and (inspected["Os"] + "/" + inspected["Architecture"])
            != runtime_profiles.MANIFEST["platform"]
        ):
            raise InfrastructureError("prepared Git image platform is invalid")
        if inspected["Size"] > MAX_IMAGE_SIZE:
            raise InfrastructureError("runtime image exceeds 2 GiB")
        server = json.loads(command(["docker", "info", "--format", "{{json .}}"], timeout=10))
        if type(server["KernelVersion"]) is not str or not server["KernelVersion"]:
            raise ValueError("missing Docker kernel identity")
        rootfs = inspected["RootFS"]
        if (
            type(rootfs) is not dict
            or rootfs.get("Type") != "layers"
            or type(rootfs.get("Layers")) is not list
            or not rootfs["Layers"]
            or any(
                type(layer) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", layer)
                for layer in rootfs["Layers"]
            )
        ):
            raise ValueError("missing immutable runtime filesystem identity")
        config = dict(inspected["Config"])
        config["Labels"] = dict(config["Labels"])
        # The worker cannot inspect this controller-only ownership label.
        del config["Labels"][LABEL + "project"]
        backend = {
            "kernel": server["KernelVersion"],
            "docker": server["ServerVersion"],
            "daemon": server["ID"],
            "endpoint": hashlib.sha256(endpoint.encode()).hexdigest(),
            "security_options": server["SecurityOptions"],
            "default_runtime": server["DefaultRuntime"],
            "runtimes": server["Runtimes"],
        }
        execution = {
            "profile": profile,
            "provenance": provenance,
            "rootfs": rootfs,
            "config": config,
            "platform": {
                "os": inspected["Os"],
                "architecture": inspected["Architecture"],
                "variant": inspected.get("Variant", ""),
            },
            "worker": metadata["worker"],
            "recipe": metadata["recipe"],
            "policy": POLICY,
            "backend": backend,
        }
        return {
            **metadata,
            "execution": execution,
            "platform": inspected["Os"] + "/" + inspected["Architecture"],
            "kernel": backend["kernel"],
            "docker": backend["docker"],
            "endpoint": backend["endpoint"],
        }
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise InfrastructureError(
            "missing or invalid runtime; run the runtime command first"
        ) from exc


def _inspect(root, selection):
    """One bounded identity operation; callers must not wrap it in further retries."""
    for attempt in range(2):
        try:
            return identity(root, runtime_profile=selection["profile"])
        except CleanupError:
            raise
        except InfrastructureError:
            if attempt:
                raise


def runtime_envelope(selection, inspected):
    return {**selection, "identity": fingerprint(inspected["execution"])}


def inspect_runtime(root, *, runtime_profile=None, required_capabilities=None):
    """Read-only prepared identity: no preparation, recovery, collection or cache access."""
    selection = runtime_profiles.normalize(
        runtime_profiles.config(root), runtime_profile, required_capabilities
    )
    return runtime_envelope(selection, _inspect(root, selection))


def _owned_capture(
    directory, data, args, timeout, *, merged=False, limit=MAX_OUTPUT, cancellation=None
):
    data["cleaned"] = False
    _durable(directory / "journal.json", data)

    def started(process):
        data["client_pid"], data["client_start"] = process.pid, _start_identity(process.pid)
        _durable(directory / "journal.json", data)

    return _capture(
        args, timeout, merged=merged, on_start=started, limit=limit, cancellation=cancellation
    )


def execute(
    image,
    snapshot,
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
    python_settings=None,
    limits=None,
    cancellation=None,
    selection_disabled=False,
    allow_unknown_selectors=False,
):
    from .execution import DEFAULT_WORKER

    limits = DEFAULT_WORKER if limits is None else limits
    snapshot = Path(snapshot).resolve()
    root = Path(root).resolve()
    directory = Path(invocation_directory).resolve()
    if (
        directory.parent != _state(root) / "scratch"
        or snapshot.parent.parent != _state(root) / "scratch"
        or snapshot.name != "project"
    ):
        raise InfrastructureError("worker requires an owned invocation snapshot")
    frozen_owner = _journal(root, snapshot.parent)
    if frozen_owner["owner_pid"] != os.getpid() or frozen_owner["owner_start"] != _start_identity(
        os.getpid()
    ):
        raise InfrastructureError("snapshot pool is not owned by this controller")
    data = _journal(root, directory)
    if data["owner_pid"] != os.getpid() or data["owner_start"] != _start_identity(os.getpid()):
        raise InfrastructureError("worker invocation is not owned by this controller")
    if any(key.startswith("TELESCOPE_") for key in environment):
        raise DeclarationError("environment must not override worker protocol")
    settings = (
        {"profile": "default", "source_roots": []} if python_settings is None else python_settings
    )
    metadata_present = False
    if metadata_directory is not None:
        metadata_directory = Path(metadata_directory).resolve()
        if metadata_directory.parent != directory or metadata_directory.name != "metadata":
            raise InfrastructureError("metadata requires an owned worker directory")
        metadata_present = next(metadata_directory.iterdir(), None) is not None
    control = directory / "control"
    control.mkdir(exist_ok=True)
    for filename, text in {
        "hosts": "127.0.0.1 localhost telescope\n::1 localhost\n",
        "hostname": "telescope\n",
        "resolv.conf": "# No network resolver\n",
    }.items():
        path = control / filename
        path.write_text(text)
        path.chmod(0o444)
        os.utime(path, ns=(0, 0))
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
    request_path = control / "request.json"
    budget = 0
    with request_path.open("wb") as stream:
        for chunk in json.JSONEncoder(separators=(",", ":"), allow_nan=False).iterencode(
            {
                **request,
                "args": args,
                "nonce": data["token"],
                "suite_root": suite_root,
                "suite": suite,
                "metadata": metadata_directory is not None,
                "python_paths": python_source.projection(settings, False)
                .decode("utf-8")
                .splitlines(),
                "source_projection": hashlib.sha256(
                    python_source.projection(settings, metadata_present)
                ).hexdigest()
                if settings["profile"] == python_source.ISOLATED
                else None,
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
    os.utime(request_path, ns=(0, 0))
    cmd = [
        "docker",
        "run",
        "--name",
        data["name"],
        "--hostname",
        "telescope",
        "--label",
        LABEL + "project=" + data["project"],
        "--label",
        LABEL + "invocation=" + data["token"],
        "--log-driver=none",
        "--pull=never",
        "--read-only",
        "--network=none",
        "--ipc=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={limits['processes']}",
        f"--memory={limits['memory_bytes']}",
        f"--memory-swap={limits['memory_bytes']}",
        f"--cpus={limits['cpus']}",
        "--ulimit",
        "nofile=256:256",
        "--user",
        "65534:65534",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={limits['scratch_bytes']}",
        "--workdir",
        "/project/" + suite_cwd,
        "--mount",
        f"type=bind,src={snapshot},dst=/project,readonly",
        "--env",
        "TELESCOPE_REQUEST=/telescope-request.json",
        "--mount",
        f"type=bind,src={request_path},dst=/telescope-request.json,readonly",
    ]
    if metadata_directory is not None:
        metadata_directory = Path(metadata_directory).resolve()
        if metadata_directory.parent != directory or metadata_directory.name != "metadata":
            raise InfrastructureError("metadata requires an owned worker directory")
        cmd += ["--mount", f"type=bind,src={metadata_directory},dst=/metadata,readonly"]
    projection = python_source.materialize(control, settings, metadata_present)
    if projection is not None:
        if projection.parent != control or projection.name != "source-view.pth":
            raise InfrastructureError("source projection requires its exact owned control file")
        if _read(projection, python_source.MAX_BYTES) != python_source.projection(
            settings, metadata_present
        ):
            raise InfrastructureError("source projection bytes do not match the declared view")
        os.utime(projection, ns=(0, 0), follow_symlinks=False)
        cmd += ["--mount", f"type=bind,src={projection},dst={python_source.SLOT},readonly"]
    for filename in ("hosts", "hostname", "resolv.conf"):
        cmd += ["--mount", f"type=bind,src={control / filename},dst=/etc/{filename},readonly"]
    for key, value in sorted(environment.items()):
        if value is not None:
            cmd += ["--env", key + "=" + value]
    cmd += ["--entrypoint", "python", image, "-I", "/opt/telescope/worker.py"]
    try:
        code, output = _owned_capture(
            directory,
            data,
            cmd,
            timeout,
            merged=True,
            limit=MAX_COLLECTION if request["mode"] in {"collection", "batch"} else MAX_OUTPUT,
            cancellation=cancellation,
        )
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
                "local metadata conflicts with an installed runtime distribution",
            )
        reports = [
            line[len(b"TELESCOPE_RESULT ") :]
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        if len(reports) != 1:
            raise InfrastructureError("worker exited without one complete result")
        result = json.loads(reports[0])
        if type(result) is dict and result.get("leaked_descendants") is True:
            raise InfrastructureError("worker left descendant processes running")
        valid = (
            validate_collection(result, nonce=data["token"])
            if request["mode"] == "collection"
            else validate_batch(
                result,
                nonce=data["token"],
                group_id=request["group_id"],
                case_ids=request["case_ids"],
            )
            if request["mode"] == "batch"
            else validate_result(
                result,
                nonce=data["token"],
                case_id=request["case_id"],
            )
        )
        if not valid or result["exit_code"] != code:
            raise InfrastructureError("worker result identity, completion or exit status mismatch")
        return result
    except UnsupportedCapability:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise InfrastructureError("worker startup or result validation failed") from exc
    finally:
        _stop_container(data)
        data["cleaned"] = True
        _durable(directory / "journal.json", data)
