"""The hosted proof transports only validated manifests and bounded regular blobs."""

import hashlib
import importlib.util
import json
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from pytest_telescope.capsule import Payload
from pytest_telescope.model import InfrastructureError, UnsupportedCapability
from pytest_telescope.python_capsule import PythonCapsule


@pytest.fixture
def transport(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/probe_python_capsules.py"
    spec = importlib.util.spec_from_file_location("capsule_transport_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixed = {
        "head": "1" * 40,
        "workflow_sha256": "2" * 64,
        "script_sha256": "3" * 64,
        "runner": "4" * 64,
        "wheel_sha256": "5" * 64,
    }
    monkeypatch.setattr(module, "bindings", lambda: dict(fixed))
    return module


@pytest.fixture
def exported(tmp_path, transport):
    payload = Payload(tmp_path / "source", max_bytes=4096, max_entries=32)
    payload.write("/usr/bin/python", b"synthetic executable\n", mode=0o555, mtime_ns=123456789)
    payload.write("/usr/lib/value.py", b"VALUE = 7\n", mode=0o444, mtime_ns=987654321012345)
    payload.write("/usr/lib/same.py", b"VALUE = 7\n", mode=0o440, mtime_ns=55)
    payload.symlink("/usr/bin/python3", "python")
    payload.symlink("/opt/telescope/python/bin/python", "/usr/bin/python")
    original = PythonCapsule(
        payload,
        "/opt/telescope/python/bin/python",
        "/opt/telescope/python/lib/python3.12/site-packages",
        {"implementation": "synthetic-cpython", "version": [3, 12, 0]},
        ["6" * 64],
        {"preparation_seconds": 0.25},
    )
    archive = tmp_path / "capsule.zip"
    receipt = transport.export(original, archive)
    return original, archive, receipt


def rewrite(archive, *, manifest_change=None, blob_change=None):
    with zipfile.ZipFile(archive) as source:
        entries = [(item.filename, source.read(item)) for item in source.infolist()]
    metadata = json.loads(entries[0][1])
    if manifest_change is not None:
        manifest_change(metadata["manifest"])
        entries[0] = ("manifest.json", json.dumps(metadata).encode())
    if blob_change is not None:
        entries = blob_change(entries)
    with zipfile.ZipFile(archive, "w") as target:
        for name, value in entries:
            target.writestr(name, value)
    receipt_path = archive.with_suffix(".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt["manifest_sha256"] = hashlib.sha256(entries[0][1]).hexdigest()
    receipt_path.write_text(json.dumps(receipt))


def assert_refused_before_writes(transport, archive, destination):
    with pytest.raises(
        (AssertionError, UnsupportedCapability, InfrastructureError, zipfile.BadZipFile)
    ):
        transport.restore(archive, destination)
    assert not destination.exists(), "invalid transport created a payload before validation"


def test_transport_roundtrip_preserves_exact_payload_and_identity(tmp_path, transport, exported):
    original, archive, receipt = exported
    restored = transport.restore(archive, tmp_path / "restored")
    assert restored.identity() == original.identity() == receipt["identity"]
    assert restored.payload.manifest() == original.payload.manifest()
    assert restored.source == original.source
    assert restored.wheel_hashes == original.wheel_hashes
    assert restored.timings == original.timings
    assert restored.executable == original.executable and restored.site == original.site
    assert restored.payload.bytes_used == original.payload.bytes_used
    for name, record in original.payload.manifest()["files"].items():
        path = restored.payload.root / name
        value = path.lstat()
        assert value.st_mtime_ns == record["mtime_ns"]
        if record["kind"] == "symlink":
            assert stat.S_ISLNK(value.st_mode)
            assert os.readlink(path) == record["target"]
        else:
            assert stat.S_IMODE(value.st_mode) == record["mode"]
            assert path.read_bytes() == (original.payload.root / name).read_bytes()
    restored.payload.verify()
    with zipfile.ZipFile(archive) as source:
        # Identical bytes share one blob even when mode and timestamp differ.
        assert len(source.namelist()) == 3


def test_transport_rejects_altered_archive_checksum_before_writes(tmp_path, transport, exported):
    _, archive, _ = exported
    archive.write_bytes(archive.read_bytes() + b"altered")
    assert_refused_before_writes(transport, archive, tmp_path / "restored")


@pytest.mark.parametrize("kind", ["traversal", "symlink-ancestor"])
def test_transport_rejects_manifest_paths_before_writes(tmp_path, transport, exported, kind):
    _, archive, _ = exported

    def change(manifest):
        record = dict(manifest["files"]["usr/lib/value.py"])
        if kind == "traversal":
            manifest["files"]["../outside"] = record
        else:
            manifest["files"]["usr/bin/python3/child"] = record

    rewrite(archive, manifest_change=change)
    assert_refused_before_writes(transport, archive, tmp_path / "restored")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate"])
def test_transport_rejects_blob_membership_before_writes(tmp_path, transport, exported, kind):
    _, archive, _ = exported

    def change(entries):
        if kind == "missing":
            return entries[:-1]
        if kind == "extra":
            return [*entries, ("files/" + "a" * 64, b"unreferenced")]
        return [*entries, entries[-1]]

    if kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            rewrite(archive, blob_change=change)
    else:
        rewrite(archive, blob_change=change)
    assert_refused_before_writes(transport, archive, tmp_path / "restored")


def test_transport_refuses_advertised_oversized_blob_before_writes(
    tmp_path, transport, exported, monkeypatch
):
    _, archive, _ = exported
    monkeypatch.setattr(transport, "MAX_BYTES", 4096)
    raw = bytearray(archive.read_bytes())
    end = raw.rfind(b"PK\x05\x06")
    offset = struct.unpack_from("<I", raw, end + 16)[0]
    while raw[offset : offset + 4] == b"PK\x01\x02":
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", raw, offset + 28)
        name = bytes(raw[offset + 46 : offset + 46 + name_size]).decode()
        if name.startswith("files/"):
            # Only the advertised size changes; the owned archive stays tiny.
            struct.pack_into("<I", raw, offset + 24, 4097)
            break
        offset += 46 + name_size + extra_size + comment_size
    else:
        raise AssertionError("synthetic archive has no regular blob")
    archive.write_bytes(raw)
    receipt_path = archive.with_suffix(".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    assert_refused_before_writes(transport, archive, tmp_path / "restored")


def test_transport_refuses_payload_entry_bound_before_writes(
    tmp_path, transport, exported, monkeypatch
):
    _, archive, _ = exported
    monkeypatch.setattr(transport, "MAX_ENTRIES", 3)
    # The archive has only three members; the expanded payload exceeds this bound.
    assert_refused_before_writes(transport, archive, tmp_path / "restored")
