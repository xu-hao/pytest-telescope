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


@pytest.mark.parametrize("stage", ["collection", "case", "batch"])
def test_isolated_source_child_cannot_leave_live_descendants(project, stage):
    root, _ = project
    spawn = (
        'subprocess.Popen([sys.executable,"-I","-c",'
        '"import example,time; assert example.value==7; time.sleep(30)"],'
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    (root / "test_example.py").write_text(
        "import pytest,subprocess,sys\n"
        'pytestmark=pytest.mark.telescope(inputs=["src/example.py"])\n'
        + (spawn + "\n" if stage == "collection" else "")
        + "def test_child():\n    "
        + ("pass" if stage == "collection" else spawn)
        + "\n"
    )
    result = controller.run(root, execution_mode="batch" if stage == "batch" else "case")
    assert result["status"] == "infrastructure_error"
    assert not any(group["cache_eligible"] for group in result["groups"])
    assert not list((root / ".pytest-telescope/cache").glob("*.json"))
    # Successful, fully cleaned collection remains valid; leaked execution does not.
    inventories = list((root / ".pytest-telescope/cache/inventory").glob("*.json"))
    assert len(inventories) == (0 if stage == "collection" else 1)
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    assert not runtime.command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=" + runtime.LABEL + "project=" + runtime._project(root),
        ]
    ).strip()


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_source_projection_terminal_cleanup_drains_actual_sibling(project, monkeypatch, mode):
    import threading

    from pytest_telescope.execution import DEFAULT_WORKER

    root, _ = project
    (root / "test_example.py").unlink()
    for name, pause in (("a", 0), ("b", 30), ("c", 0)):
        (root / f"test_{name}.py").write_text(
            "import pytest,subprocess,sys,time\n"
            'pytestmark=pytest.mark.telescope(inputs=["src/example.py"])\n'
            "def test_child():\n"
            ' subprocess.run([sys.executable,"-I","-c","import example; assert example.value==7"],check=True,capture_output=True)\n'
            f" time.sleep({pause})\n"
        )
    original = runtime.execute
    barrier = threading.Barrier(2, timeout=10)
    entered, completed, interrupted = [], [], []

    def execute(*args, **kwargs):
        if kwargs["request"]["mode"] == "collection":
            return original(*args, **kwargs)
        filename = args[2][-1]
        entered.append(filename)
        barrier.wait()
        try:
            result = original(*args, **kwargs)
        except runtime.InvocationInterrupted:
            interrupted.append(filename)
            raise
        completed.append(filename)
        assert filename.endswith("test_a.py") and result["exit_code"] == 0
        raise runtime.CleanupError("synthetic terminal cleanup fault after isolated source worker")

    monkeypatch.setattr(runtime, "execute", execute)
    result = controller.run(
        root, execution_mode=mode, budget={key: value * 2 for key, value in DEFAULT_WORKER.items()}
    )
    assert result["status"] == "infrastructure_error"
    assert len(entered) == 2 and len(completed) == len(interrupted) == 1
    assert result["counts"]["selected"] == result["counts"]["infrastructure_error"] == 3
    assert sum(group["attempts"] for group in result["groups"]) == 2
    assert not any(group["cache_eligible"] for group in result["groups"])
    assert not list((root / ".pytest-telescope/cache").rglob("*.json"))
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    assert not runtime.command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=" + runtime.LABEL + "project=" + runtime._project(root),
        ]
    ).strip()
