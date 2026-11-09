"""Strict public frozen-tool refusal and small bounded-input controls."""

import copy
import json
import os
import socket

import pytest
from frozen_tool_fixtures import ToolProject, cache_bytes, put


@pytest.fixture
def project(tmp_path):
    fixture = ToolProject(tmp_path / "project", tmp_path / "installed")
    fixture.add_tree("first")
    fixture.module("test_tool.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\ndef test_tool(): pass\n")
    return fixture


@pytest.mark.parametrize("kind", ["duplicate", "nonfinite", "utf8", "array", "null", "truncated"])
def test_invalid_manifest_encoding_is_a_private_declaration_refusal(project, kind):
    valid = json.dumps(project.documents["first"])
    encoding = {
        "duplicate": valid.replace('"schema": 1', '"schema": 1, "schema": 1').encode(),
        "nonfinite": valid.replace('"schema": 1', '"schema": NaN').encode(),
        "utf8": b'\xff', "array": b'[]', "null": b'null', "truncated": b'{',
    }[kind]
    put(project.root, "tool-locks/first.json", encoding)
    report = project.run()
    assert report["status"] == "declaration_error", report
    assert str(project.external) not in json.dumps(report)
    assert not cache_bytes(project.root)


@pytest.mark.parametrize("change", [
    "unknown-top", "missing-top", "schema-bool", "unknown-root", "coverage",
    "preserve-type", "mode-bool", "size-float", "hash", "unknown-entry",
    "duplicate-entry", "duplicate-root", "entry-parent", "entry-absolute",
])
def test_manifest_records_have_exact_types_keys_and_paths(project, change):
    document = project.documents["first"]
    root = document["roots"][0]
    file_record = next(row for row in root["entries"] if row["kind"] == "file")
    if change == "unknown-top":
        document["mounts"] = []
    elif change == "missing-top":
        del document["commands"]
    elif change == "schema-bool":
        document["schema"] = True
    elif change == "unknown-root":
        root["destination"] = "/opt/test"
    elif change == "coverage":
        root["coverage"] = "automatic"
    elif change == "preserve-type":
        root["preserve_source"] = 1
    elif change == "mode-bool":
        file_record["mode"] = True
    elif change == "size-float":
        file_record["size"] = 5.0
    elif change == "hash":
        file_record["sha256"] = "not-a-digest"
    elif change == "unknown-entry":
        file_record["fallback"] = True
    elif change == "duplicate-entry":
        root["entries"].append(copy.deepcopy(file_record))
    elif change == "duplicate-root":
        document["roots"].append(copy.deepcopy(root))
    elif change == "entry-parent":
        file_record["path"] = "../value.txt"
    else:
        file_record["path"] = "/value.txt"
    project.save("first")
    report = project.run()
    assert report["status"] == "declaration_error", report
    assert not cache_bytes(project.root)


def test_unsupported_manifest_version_has_a_stable_capability(project):
    project.documents["first"]["schema"] = 2
    project.save("first")
    report = project.run()
    assert report["kind"] == "refusal", report
    assert report["status"] == "unsupported", report
    assert report["capability"] == "tool:frozen-v1", report


@pytest.mark.parametrize("source", ["/", "/usr", "/etc", "/home"])
def test_tree_mode_cannot_snapshot_a_broad_system_or_home_root(project, source):
    project.documents["first"]["roots"][0]["source"] = source
    project.save("first")
    report = project.run()
    assert report["status"] == "unsupported", report
    assert report["capability"] == "tool:layout", report
    assert not cache_bytes(project.root)


@pytest.mark.parametrize("special", ["fifo", "socket"])
def test_special_file_is_refused_without_opening_or_waiting_for_it(project, special, monkeypatch):
    path = project.external / "first/value.txt"
    path.unlink()
    if special == "fifo":
        os.mkfifo(path)
    else:
        with monkeypatch.context() as owned_directory:
            owned_directory.chdir(path.parent)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.bind(path.name)
    report = project.run()
    assert report["status"] in {"declaration_error", "unsupported"}, report
    assert not cache_bytes(project.root)


def test_files_mode_cannot_claim_a_complete_lock_tree(project):
    project.pin_lock("first")
    project.documents["first"]["roots"][0]["coverage"] = "files"
    project.save("first")
    report = project.run()
    assert report["status"] in {"declaration_error", "unsupported"}, report
    assert not cache_bytes(project.root)


def test_declared_oversize_file_refuses_before_reading_a_giant_fixture(project):
    record = next(row for row in project.documents["first"]["roots"][0]["entries"] if row["kind"] == "file")
    record["size"] = 536_870_913
    project.save("first")
    report = project.run()
    assert report["status"] == "unsupported", report
    assert report["capability"] == "tool:bounds", report


def test_too_many_registered_tools_refuses_before_capture(project):
    for number in range(65):
        project.definitions[f"tool{number}"] = {
            "format": "frozen-tool-v1", "manifest": f"tool-locks/tool{number}.json",
        }
    project.configure()
    report = project.run()
    assert report["status"] == "unsupported", report
    assert report["capability"] == "tool:bounds", report


def test_files_mode_exposes_only_explicit_entries_and_ignores_unselected_siblings(project):
    source = project.external / "first"
    put(source, "unselected.txt", "unselected")
    project.documents["first"]["roots"][0]["coverage"] = "files"
    project.save("first")
    project.module("test_tool.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_tool():
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
    assert not Path('/tools/first/install/unselected.txt').exists()
""")
    first = project.run()
    assert first["status"] == "passed" and first["counts"]["executed"] == 1, first
    put(source, "unselected.txt", "changed unrelated sibling")
    second = project.run()
    assert second["status"] == "passed" and second["counts"]["reused"] == 1, second
