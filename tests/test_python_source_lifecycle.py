"""Source-view projection failures retain bounded worker and cache lifecycle."""

import json
import os
from pathlib import Path

import pytest

from pytest_telescope import controller, python_source, runtime
from pytest_telescope.model import InfrastructureError


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("value=7\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope.python]\nprofile="isolated-source-v1"\nsource_roots=["src"]\n'
    )
    child_code = (
        "import example,pathlib; assert example.value==7; "
        f"p=pathlib.Path({python_source.SLOT!r}); "
        "assert p.stat().st_atime_ns==p.stat().st_mtime_ns==0"
    )
    (tmp_path / "test_example.py").write_text(
        "import pytest,subprocess,sys\n"
        'pytestmark=pytest.mark.telescope(inputs=["src/example.py"])\n'
        "def test_child():\n"
        f' result=subprocess.run([sys.executable,"-I","-c",{child_code!r}],capture_output=True)\n'
        " assert result.returncode==0\n"
    )
    prepared = runtime.build_runtime(tmp_path)
    try:
        yield tmp_path, prepared
    finally:
        runtime._image_info(prepared["image"], runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", prepared["image"]])


def test_projection_is_data_only_readonly_epoch_and_bounded(tmp_path):
    settings = {"profile": python_source.ISOLATED, "source_roots": ["src", "."]}
    path = python_source.materialize(tmp_path, settings, True)
    assert path.stat().st_atime_ns == path.stat().st_mtime_ns == 0
    assert path.read_bytes() == b"/project/src\n/project\n/metadata\n"
    assert path.stat().st_mode & 0o777 == 0o444
    assert path.stat().st_mtime_ns == 0
    assert (
        python_source.materialize(tmp_path, {"profile": "default", "source_roots": []}, False)
        is None
    )


def test_projection_storage_failure_is_typed(tmp_path, monkeypatch):
    original = os.open

    def unavailable(path, *args, **kwargs):
        if Path(path).name == "source-view.pth":
            raise OSError("synthetic storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(python_source.os, "open", unavailable)
    with pytest.raises(InfrastructureError):
        python_source.materialize(
            tmp_path, {"profile": python_source.ISOLATED, "source_roots": []}, False
        )


@pytest.mark.parametrize("failure", ["once", "exhaustion", "corrupt"])
def test_projection_failure_retry_and_exhaustion_are_owned(project, monkeypatch, failure):
    root, _ = project
    original = python_source.materialize
    calls = []

    def interrupted(directory, settings, metadata):
        calls.append(directory)
        if failure == "once" and len(calls) > 1:
            return original(directory, settings, metadata)
        if failure == "corrupt":
            path = original(directory, settings, metadata)
            path.chmod(0o600)
            path.write_bytes(b"/not-the-declared-view\n")
            path.chmod(0o444)
            return path
        raise InfrastructureError("synthetic projection outage")

    monkeypatch.setattr(python_source, "materialize", interrupted)
    result = controller.run(root)
    assert len(calls) == (3 if failure == "once" else 2)
    assert len(set(calls)) == len(calls)
    assert all(not directory.exists() for directory in calls)
    assert result["status"] == ("passed" if failure == "once" else "infrastructure_error")
    if failure != "once":
        assert not list((root / ".pytest-telescope/cache").glob("*.json"))
        assert not list((root / ".pytest-telescope/cache/inventory").glob("*.json"))
    assert not runtime.command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=" + runtime.LABEL + "project=" + runtime._project(root),
        ]
    ).strip()


@pytest.mark.parametrize("state", ["missing", "changed"])
def test_invalid_prepared_slot_refuses_before_worker_or_warm_reuse(project, monkeypatch, state):
    root, _ = project
    assert controller.run(root)["status"] == "passed"
    path = root / ".pytest-telescope/runtime.json"
    data = json.loads(path.read_bytes())
    if state == "missing":
        data.pop("source_slot")
    else:
        data["source_slot"] = "0" * 64
    path.write_text(json.dumps(data))

    def forbidden(*args, **kwargs):
        raise AssertionError("worker must not start for invalid runtime slot")

    monkeypatch.setattr(runtime, "execute", forbidden)
    result = controller.run(root)
    assert result["status"] == "infrastructure_error"
    assert result["counts"]["reused"] == 0
