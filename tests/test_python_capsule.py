"""Prepare real distro Python bytes without launching a staged runtime or sandbox."""

import base64
import hashlib
import importlib
import io
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from pytest_telescope.model import InfrastructureError, UnsupportedCapability

INTERPRETER = Path("/usr/bin/python3")


def prepare(*args, **kwargs):
    return importlib.import_module("pytest_telescope.python_capsule").prepare(*args, **kwargs)


def tiny_wheel():
    files = {
        "capsule_sample/__init__.py": b"VALUE = 17\n",
        "capsule_sample-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: capsule-sample\nVersion: 1.0\n"
        ),
        "capsule_sample-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: capsule-test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    records = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(data)}\n")
    records.append("capsule_sample-1.0.dist-info/RECORD,,\n")
    files["capsule_sample-1.0.dist-info/RECORD"] = "".join(records).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


@pytest.fixture(scope="module")
def assembled(tmp_path_factory):
    root = tmp_path_factory.mktemp("python-capsule")
    metadata = subprocess.run(
        [
            str(INTERPRETER),
            "-I",
            "-S",
            "-c",
            "import json,sys,sysconfig;print(json.dumps(dict("
            "version='%s.%s'%sys.version_info[:2],stdlib=sysconfig.get_path('stdlib'),"
            "executable=sys.executable)))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    source = json.loads(metadata.stdout)
    injected = root / "injected"
    injected.mkdir()
    marker = root / "startup-ran"
    startup = f"from pathlib import Path;Path({str(marker)!r}).write_text('unexpected')\n"
    for name in ("sitecustomize.py", "usercustomize.py"):
        (injected / name).write_text(startup)
    (injected / "startup.pth").write_text("import sitecustomize\n")
    (injected / "host_only_capsule_package.py").write_text("VALUE = 'host-only'\n")
    user_site = root / "user-base" / "lib" / ("python" + source["version"]) / "site-packages"
    user_site.mkdir(parents=True)
    (user_site / "user_only_capsule_package.py").write_text("VALUE = 'user-only'\n")
    raw = tiny_wheel()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTHONPATH", str(injected))
        patch.setenv("PYTHONHOME", str(root / "nonexistent-python-home"))
        patch.setenv("PYTHONUSERBASE", str(root / "user-base"))
        results = []
        for name in ("first", "different-location"):
            inputs = root / (name + "-inputs")
            inputs.mkdir()
            wheel = inputs / "capsule_sample-1.0-py3-none-any.whl"
            wheel.write_bytes(raw)
            results.append(prepare(root / name, INTERPRETER, [wheel]))
    return dict(root=root, source=source, marker=marker, raw=raw, results=results)


def lexical_target(records, name):
    """Resolve only recorded runtime links, never a staging link against the host."""
    visited = set()
    while records[name]["kind"] == "symlink":
        assert name not in visited, "runtime symlink cycle"
        visited.add(name)
        target = records[name]["target"]
        absolute = (
            target if target.startswith("/") else str(PurePosixPath("/" + name).parent / target)
        )
        name = os.path.normpath(absolute).removeprefix("/")
        assert name in records, f"runtime symlink target is not frozen: {name}"
    return name


def test_repeated_real_interpreter_and_wheels_have_location_independent_identity(assembled):
    first, second = assembled["results"]
    assert first.identity() == second.identity()
    assert first.source == second.source
    assert first.wheel_hashes == second.wheel_hashes
    assert hashlib.sha256(assembled["raw"]).hexdigest() in json.dumps(first.wheel_hashes)
    assert first.payload.manifest() == second.payload.manifest()
    assert str(assembled["root"]) not in json.dumps(first.source)
    for result in (first, second):
        result.payload.verify()


def test_prepared_venv_contains_only_explicit_wheel_site_and_no_host_startup(assembled):
    result = assembled["results"][0]
    version = assembled["source"]["version"]
    assert result.executable == "/opt/telescope/python/bin/python"
    assert result.site == f"/opt/telescope/python/lib/python{version}/site-packages"
    records = result.payload.manifest()["files"]
    executable = lexical_target(records, result.executable.removeprefix("/"))
    frozen = result.payload.root / executable
    assert frozen.read_bytes() == INTERPRETER.resolve().read_bytes()
    assert frozen.stat().st_mode & 0o111
    prefix = result.site.removeprefix("/") + "/"
    installed = {name.removeprefix(prefix) for name in records if name.startswith(prefix)}
    assert installed == {
        "capsule_sample/__init__.py",
        "capsule_sample-1.0.dist-info/METADATA",
        "capsule_sample-1.0.dist-info/WHEEL",
        "capsule_sample-1.0.dist-info/RECORD",
    }
    assert (
        result.payload.root / prefix / "capsule_sample/__init__.py"
    ).read_bytes() == b"VALUE = 17\n"
    for name in records:
        path = PurePosixPath(name)
        assert not any(part == "dist-packages" for part in path.parts)
        assert "site-packages" not in path.parts or name.startswith(prefix)
        assert path.suffix != ".pth"
        assert path.name not in ("sitecustomize.py", "usercustomize.py")
        assert "host_only_capsule_package" not in name
        assert "user_only_capsule_package" not in name
    assert not assembled["marker"].exists()
    config = result.payload.root / "opt/telescope/python/pyvenv.cfg"
    values = dict(line.split(" = ", 1) for line in config.read_text().splitlines() if " = " in line)
    assert values["include-system-site-packages"].lower() == "false"
    assert Path(values["home"]).resolve() == INTERPRETER.resolve().parent
    assert str(assembled["root"]) not in config.read_text()


def test_copied_stdlib_preserves_bytes_modes_and_nanosecond_timestamps(assembled):
    result = assembled["results"][0]
    source = Path(assembled["source"]["stdlib"]) / "json/__init__.py"
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    records = result.payload.manifest()["files"]
    matches = [name for name, record in records.items() if record.get("sha256") == digest]
    assert matches, "the actual interpreter's stdlib source was not frozen"
    for name in matches:
        copied = result.payload.root / name
        assert copied.read_bytes() == data
        assert copied.stat().st_mtime_ns == source.stat().st_mtime_ns
        assert stat.S_IMODE(copied.stat().st_mode) == stat.S_IMODE(source.stat().st_mode) & 0o555
    assert any(name.endswith(".so") and "lib-dynload/" in name for name in records)
    assert any(Path(name).name.startswith("libc.so") for name in records)
    for name, record in records.items():
        if record["kind"] == "symlink":
            lexical_target(records, name)


def test_unsupported_interpreter_query_or_layout_is_typed(tmp_path):
    interpreter = tmp_path / "interpreter"
    interpreter.write_text("#!/bin/sh\nprintf '{malformed query'\n")
    interpreter.chmod(0o755)
    with pytest.raises((UnsupportedCapability, InfrastructureError)):
        prepare(tmp_path / "runtime", interpreter, [])


def test_malformed_wheel_archive_is_typed(tmp_path):
    wheel = tmp_path / "bad-1-py3-none-any.whl"
    wheel.write_bytes(b"not a wheel archive")
    with pytest.raises((UnsupportedCapability, InfrastructureError)):
        prepare(tmp_path / "runtime", INTERPRETER, [wheel])
