"""Pinned synthetic bytecode headers and source relations; payloads are never executed."""

import base64
import copy
import csv
import hashlib
import importlib.util
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from frozen_tool_fixtures import entries, put
from test_frozen_tool_python_membership import installed_metadata

from pytest_telescope import tool_capture, tool_context
from pytest_telescope.model import UnsupportedCapability


@pytest.mark.parametrize(
    "variant",
    [
        "checked",
        "unchecked",
        "timestamp",
        "unrecorded",
        "stale-source",
        "missing-source",
        "wrong-magic",
        "invalid-flags",
        "wrong-size",
        "changed-pinned-bytes",
    ],
)
def test_owned_bytecode_requires_pinned_header_source_and_payload_identity(
    tmp_path, monkeypatch, variant
):
    initial = installed_metadata(tmp_path, monkeypatch, "valid")
    tool = initial.manifest.tools["owned"]
    document = copy.deepcopy(tool.document)
    roots = {row["id"]: Path(row["source"]) for row in document["roots"]}
    source = put(roots["package"], "worker.py", "VALUE = 2\n")
    original_source = source.read_bytes()
    flags = 1 if variant == "unchecked" else 0 if variant in {"timestamp", "wrong-size"} else 3
    relation = (
        struct.pack("<II", 0, len(original_source) + (variant == "wrong-size"))
        if flags == 0
        else importlib.util.source_hash(original_source)
    )
    magic = b"BAD!" if variant == "wrong-magic" else importlib.util.MAGIC_NUMBER
    if variant == "invalid-flags":
        flags = 2
    # Only the supported 16-byte header is meaningful here. The deliberately
    # inert suffix proves full file identity without pretending to be runnable pyc.
    payload = magic + struct.pack("<I", flags) + relation + b"owned inert payload"
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    put(bytecode.parent, bytecode.name, payload)
    if variant == "stale-source":
        source.write_bytes(b"VALUE = 3\n")
    elif variant == "missing-source":
        source.unlink()
    record = roots["metadata"] / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    base = roots["package"].parent
    if source.exists():
        content = source.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append([source.relative_to(base).as_posix(), "sha256=" + digest, str(len(content))])
    if variant != "unrecorded":
        rows.append([bytecode.relative_to(base).as_posix(), "", ""])
    encoded = io.StringIO(newline="")
    csv.writer(encoded).writerows(rows)
    record.write_text(encoded.getvalue())
    for row in document["roots"]:
        row["entries"] = entries(Path(row["source"]))
    project = initial.manifest.root
    put(project, tool.registration["manifest"], json.dumps(document))
    tools = tool_context.load_selected(project, {"owned": tool.registration}, ["owned"])
    if variant == "changed-pinned-bytes":
        bytecode.write_bytes(payload[:-1] + b"X")
    capture = tool_capture.Capture(
        SimpleNamespace(root=project, tools=tools), initial.directory.parent, 10, None
    )
    if variant in {"checked", "unchecked", "timestamp", "unrecorded"}:
        with capture:
            key = ("package", bytecode.relative_to(roots["package"]).as_posix())
            assert capture.graphs["owned"].bytes(key) == payload
    else:
        with pytest.raises(UnsupportedCapability) as caught:
            with capture:
                pytest.fail("invalid bytecode relation or changed pinned bytes admitted")
        assert caught.value.capability == (
            "tool:identity" if variant == "changed-pinned-bytes" else "tool:layout"
        )
    assert not capture.directory.exists() and not capture.materialized
