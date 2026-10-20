"""Observable bounded, deterministic runtime payload preparation."""

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

from pytest_telescope.model import InfrastructureError, UnsupportedCapability


def wheel(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return stream.getvalue()


def test_repeated_clean_wheel_projection_has_identical_identity(tmp_path):
    from pytest_telescope.capsule import Payload

    raw = wheel(
        {
            "sample/__init__.py": "VALUE = 7\n",
            "sample-1.dist-info/METADATA": "Name: sample\nVersion: 1\n",
        }
    )
    first = Payload(tmp_path / "first", max_bytes=65536, max_entries=100)
    second = Payload(tmp_path / "other-location", max_bytes=65536, max_entries=100)
    first.install_wheel(raw, "/opt/telescope/python/lib/python3.12/site-packages")
    second.install_wheel(raw, "/opt/telescope/python/lib/python3.12/site-packages")
    assert first.manifest() == second.manifest()
    assert first.identity() == second.identity()
    assert first.bytes_used == second.bytes_used
    first.verify()
    target = first.root / "opt/telescope/python/lib/python3.12/site-packages/sample/__init__.py"
    target.chmod(0o644)
    target.write_text("VALUE = 8\n")
    with pytest.raises(InfrastructureError, match="changed"):
        first.verify()


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "a/../../outside",
        "startup.pth",
        "sitecustomize.py",
        "sample-1.data/scripts/run",
        "sample.pyc",
    ],
)
def test_wheel_refuses_escape_startup_or_generated_code_before_writes(tmp_path, name):
    from pytest_telescope.capsule import Payload

    payload = Payload(tmp_path / "runtime", max_bytes=65536, max_entries=100)
    with pytest.raises(UnsupportedCapability):
        payload.install_wheel(wheel({"valid.py": "ok", name: "bad"}), "/site")
    assert list(payload.root.iterdir()) == []
    assert not (tmp_path / "outside").exists()


def test_bounds_are_checked_before_wheel_materialization(tmp_path):
    from pytest_telescope.capsule import Payload

    payload = Payload(tmp_path / "runtime", max_bytes=16, max_entries=10)
    with pytest.raises(UnsupportedCapability, match="storage"):
        payload.install_wheel(wheel({"large.py": "x" * 17}), "/site")
    assert list(payload.root.iterdir()) == []


def test_host_copy_keeps_bytecode_relevant_metadata_and_detects_mutation(tmp_path):
    from pytest_telescope.capsule import Payload

    source = tmp_path / "source.py"
    source.write_text("VALUE=1\n")
    source.chmod(0o640)
    os.utime(source, ns=(123456789012345, 123456789012345))
    payload = Payload(tmp_path / "runtime", max_bytes=65536, max_entries=100)
    payload.copy_file(source, "/stdlib/source.py")
    target = payload.root / "stdlib/source.py"
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert target.stat().st_mode & 0o777 == 0o440
    before = payload.identity()
    payload.verify()
    os.utime(target, ns=(0, 0))
    with pytest.raises(InfrastructureError, match="changed"):
        payload.verify()
    assert before == payload.identity()


def test_explicit_elf_closure_contains_real_interpreter_loader_and_libraries():
    from pytest_telescope.capsule import elf_closure

    closure = elf_closure([Path(sys.executable).resolve()])
    assert str(Path(sys.executable).resolve()) in closure
    assert any("ld-linux" in value for value in closure)
    assert any(Path(value).name.startswith("libc.so") for value in closure)
    assert all(Path(value).is_file() for value in closure)
    assert not any("site-packages" in value for value in closure)


def test_invalid_elf_is_typed_and_does_not_execute_payload(tmp_path):
    from pytest_telescope.capsule import elf_closure

    marker = tmp_path / "executed"
    executable = tmp_path / "tool"
    executable.write_text(f"#!/bin/sh\ntouch {marker}\n")
    executable.chmod(0o755)
    with pytest.raises(UnsupportedCapability, match="ELF"):
        elf_closure([executable])
    assert not marker.exists()
