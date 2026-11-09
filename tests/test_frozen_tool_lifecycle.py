"""Deterministic I/O faults around actual owned capture, with public run outcomes."""

import errno
from contextlib import contextmanager
from pathlib import Path

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put

from pytest_telescope import tool_capture
from pytest_telescope.sandbox import CleanupError


@pytest.fixture
def fixture(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    source = project.add_tree("first", files={"value.txt": "first"})
    project.module("test_value.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_value():
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
""")
    return project, source / "value.txt"


def capture_attempts(monkeypatch):
    original = tool_capture.Capture._capture
    attempts = []

    def capture(self, deadline):
        # Retry staging must be empty before any tool directory is reconstructed.
        assert list(self.directory.iterdir()) == []
        attempts.append(self.directory)
        return original(self, deadline)

    monkeypatch.setattr(tool_capture.Capture, "_capture", capture)
    return attempts


@pytest.mark.parametrize("persistent", [False, True])
def test_capture_io_outage_retries_at_most_twice_from_fresh_owned_staging(fixture, monkeypatch, persistent):
    project, source = fixture
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    original = tool_capture._parent
    failures = 0

    @contextmanager
    def parent(filename):
        nonlocal failures
        if Path(filename) == source and (persistent or failures == 0):
            failures += 1
            raise OSError(errno.EIO, "synthetic owned fixture I/O outage")
        with original(filename) as descriptor:
            yield descriptor

    attempts = capture_attempts(monkeypatch)
    monkeypatch.setattr(tool_capture, "_parent", parent)
    report = project.run()
    assert len(attempts) == 2
    assert failures == (2 if persistent else 1)
    if persistent:
        assert report["status"] == "infrastructure_error", report
        assert cache_bytes(project.root) == previous
    else:
        assert_counts(report, reused=1)
    assert all(not directory.exists() for directory in attempts)


def test_source_replaced_after_copy_is_detected_and_recaptured_before_cache_hit(fixture, monkeypatch):
    project, source = fixture
    assert_counts(project.run(), executed=1)
    original = tool_capture._measure
    retired = project.external / "retired-owned-input"
    replaced = False

    def measure(filename, expected, budget, destination=None, *, frozen=False):
        nonlocal replaced
        result = original(filename, expected, budget, destination, frozen=frozen)
        if Path(filename) == source and destination is not None and not replaced:
            source.rename(retired)
            put(source.parent, source.name, "first")
            replaced = True
        return result

    attempts = capture_attempts(monkeypatch)
    monkeypatch.setattr(tool_capture, "_measure", measure)
    assert_counts(project.run(), reused=1)
    assert replaced and len(attempts) == 2
    assert source.stat().st_ino != retired.stat().st_ino
    assert all(not directory.exists() for directory in attempts)


@pytest.mark.parametrize("persistent", [False, True])
def test_corrupt_owned_copy_is_never_used_or_published(fixture, monkeypatch, persistent):
    project, source = fixture
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    original = tool_capture._measure
    corruptions = 0

    def measure(filename, expected, budget, destination=None, *, frozen=False):
        nonlocal corruptions
        result = original(filename, expected, budget, destination, frozen=frozen)
        if Path(filename) == source and destination is not None and (persistent or corruptions == 0):
            corruptions += 1
            destination.chmod(0o644)
            destination.write_text("corrupt owned staging")
            destination.chmod(0o444)
        return result

    attempts = capture_attempts(monkeypatch)
    monkeypatch.setattr(tool_capture, "_measure", measure)
    report = project.run()
    assert len(attempts) == 2
    assert corruptions == (2 if persistent else 1)
    if persistent:
        assert report["status"] == "infrastructure_error", report
        assert cache_bytes(project.root) == previous
    else:
        assert_counts(report, reused=1)
    assert source.read_text() == "first"


def test_capture_deadline_has_a_bounded_infrastructure_end(fixture, monkeypatch):
    project, _ = fixture
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    original = tool_capture.Budget.tick

    def tick(self, *, size=0, visit=False):
        self.deadline = float("-inf")
        return original(self, size=size, visit=visit)

    attempts = capture_attempts(monkeypatch)
    monkeypatch.setattr(tool_capture.Budget, "tick", tick)
    report = project.run()
    assert report["status"] == "infrastructure_error", report
    assert len(attempts) <= 2
    assert cache_bytes(project.root) == previous


def test_cleanup_uncertainty_is_terminal_and_preserves_previous_records(fixture, monkeypatch):
    project, source = fixture
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    project.module("test_new.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_new(): pass
""")
    original = tool_capture._remove
    attempted = []

    def remove(directory):
        attempted.append(directory)
        raise CleanupError("synthetic owned capture cleanup refusal")

    try:
        with monkeypatch.context() as fault:
            attempts = capture_attempts(fault)
            fault.setattr(tool_capture, "_remove", remove)
            report = project.run()
        assert report["status"] == "infrastructure_error", report
        assert len(attempts) == len(attempted) == 1
        assert cache_bytes(project.root) == previous
        assert source.read_text() == "first"
    finally:
        # The injected refusal did not remove these exact capture-owned directories.
        for directory in set(attempted):
            if directory.exists():
                original(directory)


def test_unselected_source_outage_does_not_touch_an_unrelated_tool_scope(fixture, monkeypatch):
    project, source = fixture
    second = project.add_tree("second")
    project.module("test_second.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['second'])
def test_second():
    assert Path('/tools/second/install/value.txt').read_text() == 'second'
""")
    original = tool_capture._parent
    forbidden = []

    @contextmanager
    def parent(filename):
        if Path(filename).is_relative_to(source.parent):
            forbidden.append(filename)
            raise OSError(errno.EIO, "unselected fixture source is unavailable")
        with original(filename) as descriptor:
            yield descriptor

    monkeypatch.setattr(tool_capture, "_parent", parent)
    report = project.run(structural_files={"default": ["tests/test_second.py"]})
    assert_counts(report, executed=1)
    assert not forbidden
    assert (second / "value.txt").read_text() == "second"
