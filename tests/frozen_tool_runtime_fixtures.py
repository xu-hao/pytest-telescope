"""Bounded setup of tiny native and normally installed Python test fixtures.

These helpers run only inside an admitted behavioral check. They never install a
browser, fetch tools or change the invoking environment.
"""

import base64
import csv
import hashlib
import io
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from frozen_tool_fixtures import put

import pytest_telescope

PAYLOADS = Path(__file__).with_name("frozen_tool_payloads")
MAX_FIXTURE_OUTPUT = 1024 * 1024


def owned_command(command, directory, label, *, timeout=120, accepted=(0,)):
    """Run one owned process group, retain one bounded log, and always reap it."""
    scratch = directory / "setup-scratch"
    scratch.mkdir(exist_ok=True)
    environment = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }
    log_path = directory / (label + ".log")
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "deadline"
                    break
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(chunk) > MAX_FIXTURE_OUTPUT:
                        failure = "output bound"
                        break
                    else:
                        output.extend(chunk)
                if failure is not None:
                    break
        if failure is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "deadline"
    finally:
        if failure is not None or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=8)
        process.stdout.close()
        with log_path.open("xb") as log:
            log.write(output)
    if failure is not None or process.returncode not in accepted:
        pytest.fail(
            f"owned fixture {label} failed ({failure or process.returncode}); log {log_path.name}"
        )
    return bytes(output)


def native_install(directory, *, alternate=False):
    """Compile only these two owned sources with the already-installed compiler."""
    directory.mkdir()
    install = directory / "installation"
    for name in ("bin", "lib", "source"):
        (install / name).mkdir(parents=True)
    for name in ("native.c", "library.c"):
        shutil.copyfile(PAYLOADS / name, install / "source" / name)
    owned_command(
        [
            "/usr/bin/cc",
            "-fPIC",
            "-shared",
            "-Wl,-soname,libownedprobe.so",
            "-o",
            str(install / "lib/libownedprobe.so"),
            str(install / "source/library.c"),
        ],
        directory,
        "compile-library",
    )
    runpath = "$ORIGIN/../lib"
    if alternate:
        alternative = directory / "unselected-library"
        alternative.mkdir()
        put(alternative, "library.c", 'const char *owned_answer(void) { return "unselected"; }\n')
        owned_command(
            [
                "/usr/bin/cc",
                "-fPIC",
                "-shared",
                "-Wl,-soname,libownedprobe.so",
                "-o",
                str(alternative / "libownedprobe.so"),
                str(alternative / "library.c"),
            ],
            directory,
            "compile-alternative",
        )
        runpath += ":" + str(alternative)
    owned_command(
        [
            "/usr/bin/cc",
            "-o",
            str(install / "bin/owned-probe"),
            str(install / "source/native.c"),
            "-L" + str(install / "lib"),
            "-lownedprobe",
            "-Wl,-rpath," + runpath,
        ],
        directory,
        "compile-program",
    )
    return install


def plugin_wheel(directory):
    """Build one tiny pure-Python wheel locally; no backend or package index."""
    metadata = "owned_plugin-1.0.0.dist-info"
    files = {
        "owned_plugin/__init__.py": b"VALUE = 7\n",
        "owned_plugin/message.txt": b"ready\n",
        metadata
        + "/METADATA": b"Metadata-Version: 2.1\nName: owned-plugin\nVersion: 1.0.0\nRequires-Python: >=3.11\n",
        metadata
        + "/WHEEL": b"Wheel-Version: 1.0\nGenerator: owned-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        metadata + "/entry_points.txt": b"[owned.tools]\nvalue = owned_plugin:VALUE\n",
    }
    encoded = io.StringIO(newline="")
    writer = csv.writer(encoded, lineterminator="\n")
    for name, content in sorted(files.items()):
        hashed = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + hashed, len(content)])
    writer.writerow([metadata + "/RECORD", "", ""])
    files[metadata + "/RECORD"] = encoded.getvalue().encode()
    wheel = directory / "owned_plugin-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return wheel


def installed_plugin(directory):
    """Install the local wheel into a fixture venv, preserving real RECORD metadata."""
    directory.mkdir()
    environment = directory / "environment"
    owned_command(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        directory,
        "create-environment",
    )
    interpreter = environment / "bin/python"
    purelib = Path(
        owned_command(
            [
                str(interpreter),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            directory,
            "locate-site-packages",
            timeout=30,
        )
        .decode()
        .strip()
    )
    wheel = plugin_wheel(directory)
    owned_command(
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--python",
            str(interpreter),
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            str(wheel),
        ],
        directory,
        "install-owned-wheel",
    )
    shared = {
        str(Path(pytest.__file__).parent.parent),
        str(Path(pytest_telescope.__file__).parent.parent),
    }
    put(purelib, "owned-test-support.pth", "".join(path + "\n" for path in sorted(shared)))
    return interpreter, purelib
