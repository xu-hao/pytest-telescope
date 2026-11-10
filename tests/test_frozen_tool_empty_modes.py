"""An empty declared files root needs no directory-read permission."""

import json
import os
import stat

import pytest
from frozen_tool_fixtures import ToolProject, manifest
from frozen_tool_permission_fixture import (
    errors,
    identity,
    make_capture,
    record_material,
    restore_owned,
)


def test_empty_mode_zero_files_root_capture_verify_and_cleanup(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    source = project.external / "owned"
    source.mkdir(mode=0o755)
    source_identity = identity(source)
    records, failures = {}, []
    entered = False
    locked = False
    try:
        project.documents["owned"] = manifest(
            [
                {
                    "id": "install",
                    "source": str(source),
                    "coverage": "files",
                    "preserve_source": False,
                    "entries": [{"path": ".", "kind": "directory", "mode": 0}],
                }
            ]
        )
        project.definitions["owned"] = {
            "format": "frozen-tool-v1",
            "manifest": "tool-locks/owned.json",
        }
        project.save("owned")
        project.configure()
        stage = tmp_path / "capture"
        stage.mkdir()
        os.chmod(source, 0, follow_symlinks=False)
        locked = True
        assert os.geteuid() != 0, "permission control requires an actual non-root owner"
        # Opening for directory reads proves EACCES without listing any entries.
        with pytest.raises(PermissionError) as denied:
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.close(descriptor)
        assert denied.value.errno == 13
        assert identity(source) == source_identity
        assert stat.S_IMODE(source.lstat().st_mode) == 0
        capture = make_capture(project.root, stage)
        with record_material(capture.directory, records):
            try:
                capture.__enter__()
                entered = True
                capture.view(["owned"])
                capture.verify(["owned"])
                copied = capture.directory / "owned/install"
                assert stat.S_IMODE(copied.lstat().st_mode) == 0
            except BaseException as error:
                failures.append(errors(error))
            finally:
                if entered:
                    try:
                        capture.__exit__(None, None, None)
                    except BaseException as error:
                        failures.append(errors(error))
        assert identity(source) == source_identity
        assert stat.S_IMODE(source.lstat().st_mode) == 0
        diagnostic = {
            "mode": 0,
            "selected_entries": 1,
            "failures": failures,
            "copy_exists": os.path.lexists(capture.directory / "owned/install"),
        }
        print(json.dumps(diagnostic))
        assert not failures, diagnostic
        assert not capture.directory.exists()
    finally:
        try:
            restore_owned(records)
        finally:
            try:
                if locked:
                    assert identity(source) == source_identity
                    assert stat.S_IMODE(source.lstat().st_mode) == 0
            finally:
                restore_owned({str(source): source_identity})
