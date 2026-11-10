"""Guard lifecycle and infrastructure failure controls with real Bubblewrap."""

import json
from contextlib import contextmanager

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import InfrastructureError


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


def records(root):
    cache = root / ".pytest-telescope/cache"
    return [json.loads(path.read_text()) for path in cache.glob("*.json")]


@pytest.mark.requires_fixture_guard
@pytest.mark.parametrize("failure_phase", ["setup", "teardown"])
def test_actual_fixture_failure_is_not_reused(tmp_path, failure_phase):
    configure(tmp_path)
    fixture = (
        '    raise RuntimeError("synthetic setup")\n    yield\n'
        if failure_phase == "setup"
        else '    yield\n    raise RuntimeError("synthetic teardown")\n'
    )
    put(
        tmp_path,
        "tests/test_failure.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture
def broken():
"""
        + fixture
        + "def test_item(broken): pass\n",
    )
    first = controller.run(tmp_path, fixture_guard="available-v1")
    second = controller.run(tmp_path, fixture_guard="available-v1")
    for report in (first, second):
        assert report["status"] == "failed" and report["fixture_guard"]["state"] == "admitted"
        assert report["counts"]["reused"] == 0
        assert not report["groups"][0]["cache_eligible"]
        assert any(
            phase["phase"] == failure_phase and phase["outcome"] == "failed"
            for phase in report["units"][0]["reports"]
        )
    assert not any(record["schema"] == 8 for record in records(tmp_path))


@pytest.mark.requires_fixture_guard
def test_retry_then_success_and_exhaustion_are_bounded(tmp_path, monkeypatch):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_retry.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    original = sandbox.execute
    attempts = []

    def once(*args, request, **kwargs):
        if request["mode"] == "guard-batch":
            attempts.append("dispatch")
            if len(attempts) == 1:
                kwargs["on_session_start"]()  # Synthetic started session; no worker is launched.
                raise InfrastructureError("synthetic unavailable worker")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", once)
    recovered = controller.run(tmp_path, fixture_guard="available-v1")
    assert attempts == ["dispatch", "dispatch"]
    assert recovered["status"] == "passed" and recovered["groups"][0]["attempts"] == 2
    assert recovered["counts"]["executed"] == 1
    attempts.clear()

    def unavailable(*args, request, **kwargs):
        if request["mode"] == "guard-batch":
            attempts.append("dispatch")
            kwargs["on_session_start"]()  # Synthetic started session; no worker is launched.
            raise InfrastructureError("synthetic unavailable worker")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", unavailable)
    exhausted = controller.run(tmp_path, fixture_guard="available-v1", force=True)
    assert attempts == ["dispatch", "dispatch"]
    assert exhausted["status"] == "infrastructure_error"
    assert exhausted["groups"][0]["attempts"] == 2 and not exhausted["groups"][0]["cache_eligible"]


@pytest.mark.requires_fixture_guard
def test_cancellation_drains_and_does_not_dispatch_next_group(tmp_path, monkeypatch):
    configure(tmp_path)
    for name in ("a", "b"):
        put(
            tmp_path,
            f"tests/test_{name}.py",
            "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
        )
    original = sandbox.execute
    attempts = []

    def cancel(*args, request, **kwargs):
        if request["mode"] == "guard-batch":
            attempts.append(request["group_id"])
            kwargs["on_session_start"]()  # Synthetic started session; no worker is launched.
            raise sandbox.InvocationInterrupted("synthetic cancellation")
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", cancel)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert len(attempts) == 1 and report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "admitted"
    assert sum(group["attempts"] for group in report["groups"]) == 1
    assert not any(group["cache_eligible"] for group in report["groups"])
    assert not any(record["schema"] == 8 for record in records(tmp_path))


@pytest.mark.requires_fixture_guard
def test_outer_cleanup_failure_preserves_observed_pass_without_publication(tmp_path, monkeypatch):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_item.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    original = sandbox.invocation
    first = True

    @contextmanager
    def fail_outer(root):
        nonlocal first
        outer = first
        first = False
        with original(root) as directory:
            yield directory
        if outer:
            raise sandbox.CleanupError("synthetic outer cleanup incident")

    monkeypatch.setattr(sandbox, "invocation", fail_outer)
    report = controller.run(tmp_path, fixture_guard="available-v1", workers=1)
    assert (
        report["status"] == "infrastructure_error"
        and report["fixture_guard"]["state"] == "admitted"
    )
    assert report["counts"]["executed"] == 1
    assert report["units"][0]["result_status"] == "passed"
    assert not report["groups"][0]["cache_eligible"]
    assert not any(record["schema"] == 8 for record in records(tmp_path))
    monkeypatch.setattr(sandbox, "invocation", original)
    resumed = controller.run(tmp_path, fixture_guard="available-v1")
    assert resumed["status"] == "passed" and resumed["counts"]["executed"] == 1


@pytest.mark.requires_fixture_guard
def test_real_descendant_process_is_bounded_and_not_cached(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_descendant.py",
        """import subprocess
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): subprocess.Popen(["sleep", "20"])
""",
    )
    report = controller.run(tmp_path, fixture_guard="available-v1", timeout=4)
    assert report["status"] == "infrastructure_error"
    assert report["fixture_guard"]["state"] == "admitted"
    assert report["groups"][0]["attempts"] == 2
    assert not report["groups"][0]["cache_eligible"]
    assert not any(record["schema"] == 8 for record in records(tmp_path))
    scratch = tmp_path / ".pytest-telescope/scratch"
    assert not list(scratch.iterdir())
