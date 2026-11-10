"""Search-only declared directories remain supported without changing source modes."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from frozen_tool_permission_fixture import (
    PermissionFixture,
    errors,
    identity,
    record_material,
    restore_owned,
)

import pytest_telescope
from pytest_telescope import controller, sandbox


@pytest.mark.parametrize("interior,mode", [(False, 0o111), (True, 0o111), (False, 0o555), (True, 0o755)],
                         ids=["root-search-only", "interior-search-only", "root-readable", "interior-readable"])
def test_files_directory_permissions_survive_capture_view_verify_and_cleanup(tmp_path, interior, mode):
    fixture = PermissionFixture(tmp_path, interior, mode)
    records, failures = {}, []
    stage = tmp_path / "capture"
    stage.mkdir()
    entered = False
    try:
        capture = fixture.capture(stage)
        assert os.geteuid() != 0, "permission control requires an actual non-root owner"
        if mode == 0o111:
            with pytest.raises(PermissionError) as denied:
                with os.scandir(fixture.protected) as listing:
                    list(listing)
            assert denied.value.errno == 13
        assert fixture.file.read_bytes() == b"owned readable bytes\n"
        with record_material(capture.directory, records):
            try:
                capture.__enter__()
                entered = True
                capture.view(["owned"])
                capture.verify(["owned"])
                copied = capture.directory / "owned/install" / ("inner" if interior else "")
                assert stat.S_IMODE(copied.lstat().st_mode) == mode & 0o555
            except BaseException as error:
                failures.append(errors(error))
            finally:
                if entered:
                    try:
                        capture.__exit__(None, None, None)
                    except BaseException as error:
                        failures.append(errors(error))
        assert fixture.state() == fixture.before
        diagnostic = {"mode": mode, "interior": interior, "failures": failures,
                      "copy_exists": os.path.lexists(capture.directory / "owned/install")}
        print(json.dumps(diagnostic))
        assert not failures, diagnostic
        assert not capture.directory.exists()
    finally:
        try:
            restore_owned(records)
        finally:
            fixture.finish()


def test_abandoned_search_only_capture_recovers_without_source_permission_changes(tmp_path):
    fixture = PermissionFixture(tmp_path, False, 0o111)
    records = {}
    ready = tmp_path / "ready.json"
    try:
        assert os.geteuid() != 0, "permission control requires an actual non-root owner"
        with pytest.raises(PermissionError):
            with os.scandir(fixture.source) as listing:
                list(listing)
        source = str(Path(pytest_telescope.__file__).parent.parent)
        helpers = str(Path(__file__).parent)
        code = ("import sys; from pathlib import Path; sys.path[:0]=" + repr([source, helpers]) + "; "
                "from frozen_tool_permission_fixture import abandon; abandon(Path(sys.argv[1]),Path(sys.argv[2]))")
        processes = []
        original = sandbox.subprocess.Popen

        def popen(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(sandbox.subprocess, "Popen", popen):
            code_status, output = sandbox.capture([sys.executable, "-I", "-B", "-c", code,
                                                  str(fixture.project.root), str(ready)], 10,
                                                 env=dict(os.environ), limit=4096)
        assert code_status == 0, output
        assert ready.stat().st_size <= 4096
        received = json.loads(ready.read_bytes())
        assert len(processes) == 1 and processes[0].poll() == 0
        assert received["pid"] == processes[0].pid
        directory = Path(received["directory"])
        assert directory.parent == fixture.project.root / ".pytest-telescope/scratch"
        assert directory.name.startswith("invocation-") and type(received["pid"]) is int
        records = received["records"]
        assert len(records) <= 32 and all(Path(name).is_relative_to(directory) for name in records)
        assert received["copy_exists"], received
        print(json.dumps({"capture_failure": received["failure"], "recorded_members": len(records)}))
        cache = fixture.project.root / ".pytest-telescope/cache"
        cache.mkdir()
        with controller.lock(cache / ".lock", 10):
            controller.sandbox.recover(fixture.project.root)
        assert not directory.exists()
        assert fixture.state() == fixture.before
        with sandbox.invocation(fixture.project.root) as fresh:
            assert fresh.exists()
        assert not fresh.exists()
    finally:
        try:
            # Even an early child failure has a separate bounded ownership receipt.
            # This fixed list is not a scan and does not claim capture readiness.
            owner = ready.with_suffix(".owner.json")
            if not records and owner.exists():
                assert owner.stat().st_size <= 4096
                owned = json.loads(owner.read_bytes())
                directory = Path(owned["directory"])
                assert directory.parent == fixture.project.root / ".pytest-telescope/scratch"
                try:
                    current = identity(directory)
                except FileNotFoundError:
                    current = None
                if current is not None:
                    assert current == owned["identity"]
                    for suffix in ("", "owner.lock", "tools", "tools/.views", "tools/owned",
                                   "tools/owned/install", "tools/owned/install/value"):
                        path = directory / suffix
                        try:
                            records[str(path)] = identity(path)
                        except FileNotFoundError:
                            pass
            assert fixture.state() == fixture.before
        finally:
            try:
                restore_owned(records)
            finally:
                fixture.finish()
