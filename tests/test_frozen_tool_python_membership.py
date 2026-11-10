"""Owned installed-layout metadata validation; no interpreter or plugin is launched."""

import base64
import csv
import hashlib
import io
from types import SimpleNamespace

import pytest
from frozen_tool_fixtures import ToolProject, entries, manifest, put

from pytest_telescope import tool_capture, tool_context, tool_python
from pytest_telescope.model import UnsupportedCapability


def installed_metadata(tmp_path, monkeypatch, mutation):
    project = ToolProject(tmp_path / "project", tmp_path / "installation")
    package = project.external / "owned_plugin"
    metadata = project.external / "owned_plugin-1.0.dist-info"
    runtime = project.external / "runtime"
    source = put(package, "__init__.py", "VALUE = 1\n")
    meta = put(metadata, "METADATA", "Metadata-Version: 2.1\nName: owned-plugin\nVersion: 1.0\n")
    invoking = put(runtime, "python", b"owned metadata-only invoking-path witness")
    # Supply only this validator's invoking-path observation. These bytes are
    # never advertised as an executable runtime or used for worker startup.
    monkeypatch.setattr(tool_python, "sys", SimpleNamespace(executable=str(invoking)))
    rows = []
    for path in (source, meta):
        content = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append(
            [path.relative_to(project.external).as_posix(), "sha256=" + digest, str(len(content))]
        )
    rows.append([metadata.name + "/RECORD", "", ""])
    if mutation == "hash":
        rows[0][1] = "sha256=" + "A" * 43
    elif mutation == "size":
        rows[0][2] = str(source.stat().st_size + 1)
    elif mutation == "missing-self":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(list(rows[0]))
    elif mutation == "unrecorded-source":
        rows.pop(0)
    elif mutation == "hashless-source":
        rows[0][1:] = ["", ""]
    content = io.StringIO(newline="")
    csv.writer(content).writerows(rows)
    put(metadata, "RECORD", content.getvalue())
    document = manifest(
        [
            {
                "id": name,
                "source": str(path),
                "coverage": "tree",
                "preserve_source": True,
                "entries": entries(path),
            }
            for name, path in (("package", package), ("metadata", metadata), ("runtime", runtime))
        ]
    )
    document["python"] = [
        {
            "name": "owned-plugin",
            "version": "1.0",
            "metadata": {"root": "metadata", "path": "."},
            "packages": [{"root": "package", "path": "."}],
            "dependencies": [],
            "runtime": [{"root": "runtime", "path": "python"}],
        }
    ]
    project.documents["owned"] = document
    project.definitions["owned"] = {"format": "frozen-tool-v1", "manifest": "tool-locks/owned.json"}
    project.save("owned")
    tools = tool_context.load_selected(project.root, project.definitions, ["owned"])
    stage = tmp_path / "capture"
    stage.mkdir()
    return tool_capture.Capture(SimpleNamespace(root=project.root, tools=tools), stage, 20, None)


@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "hash",
        "size",
        "missing-self",
        "duplicate",
        "unrecorded-source",
        "hashless-source",
    ],
)
def test_installed_record_admits_only_complete_matching_owned_members(
    tmp_path, monkeypatch, mutation
):
    capture = installed_metadata(tmp_path, monkeypatch, mutation)
    if mutation == "valid":
        with capture:
            assert capture.graphs["owned"].bytes(("package", "__init__.py")) == b"VALUE = 1\n"
    else:
        with pytest.raises(UnsupportedCapability) as caught:
            with capture:
                pytest.fail("incomplete or inconsistent installed metadata was admitted")
        assert caught.value.capability == "tool:layout"
    assert not capture.directory.exists() and not capture.materialized
