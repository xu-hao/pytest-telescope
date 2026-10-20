"""Completed children are reaped; surviving descendants always refuse evidence."""

import json

import pytest

from pytest_telescope import controller, runtime


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots=["tests"]\n')
    metadata = runtime.build_runtime(tmp_path)
    try:
        yield tmp_path
    finally:
        runtime.recover(tmp_path)
        for image in json.loads((tmp_path / ".pytest-telescope/images.json").read_text()):
            runtime.command(["docker", "image", "rm", image], timeout=30)
        assert metadata["image"]


CHILD = r"""
import os, time

def adopted_child(live):
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            grandchild = os.fork()
        except OSError:
            os.write(write_fd, b'failed')
            os._exit(1)
        if grandchild == 0:
            if live:
                os.write(write_fd, b'ready')
                os.close(write_fd)
                time.sleep(30)
            else:
                # Charge measurable child CPU before exiting.
                started = time.process_time()
                while time.process_time() - started < 0.03:
                    pass
                os.write(write_fd, b'complete')
            os._exit(0)
        os._exit(0)
    os.close(write_fd)
    assert os.waitpid(child, 0) == (child, 0)
    with os.fdopen(read_fd, 'rb') as stream:
        assert stream.read() == (b'ready' if live else b'complete')
"""


@pytest.mark.parametrize("mode", ["case", "batch"])
@pytest.mark.parametrize("phase", ["collection", "call"])
@pytest.mark.parametrize("live", [False, True])
def test_completed_adopted_child_vs_live_descendant(project, mode, phase, live):
    source = CHILD + "\nimport pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
    if phase == "collection":
        source += f"adopted_child({live!r})\ndef test_child(): pass\n"
    else:
        source += f"def test_child(): adopted_child({live!r})\n"
    (project / "tests/test_child.py").write_text(source)
    result = controller.run(project, execution_mode=mode)
    if live:
        assert result["status"] == "infrastructure_error", json.dumps(result, indent=2)
        assert result["counts"]["reused"] == 0, json.dumps(result, indent=2)
        assert not any(group["cache_eligible"] for group in result["groups"]), json.dumps(
            result, indent=2
        )
        if phase == "call":
            assert result["groups"][0]["attempts"] == 2, json.dumps(result, indent=2)
    else:
        assert result["status"] == "passed", json.dumps(result, indent=2)
        assert result["counts"]["executed"] == 1, json.dumps(result, indent=2)
        assert result["worker_cpu_seconds"] >= 0.03, json.dumps(result, indent=2)
        warm = controller.run(project, execution_mode=mode)
        assert warm["status"] == "passed" and warm["counts"]["reused"] == 1, json.dumps(
            warm, indent=2
        )


@pytest.mark.parametrize("bound", ["iterations", "deadline", "live", "none"])
def test_completed_child_reaping_is_nonblocking_and_bounded(monkeypatch, bound):
    from pytest_telescope import worker

    calls = []
    clock = [0]

    def monotonic():
        clock[0] += 0.02 if bound == "deadline" else 0
        return clock[0]

    def waitpid(pid, flags):
        assert pid == -1 and flags == worker.os.WNOHANG
        calls.append(1)
        if bound == "none":
            raise ChildProcessError
        return (0 if bound == "live" else 42), 0

    monkeypatch.setattr(worker.os, "waitpid", waitpid)
    monkeypatch.setattr(worker.time, "monotonic", monotonic)
    worker.reap_completed_children()
    assert len(calls) == {"iterations": 4096, "deadline": 2, "live": 1, "none": 1}[bound]
