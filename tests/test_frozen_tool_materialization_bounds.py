"""Small actual copies/projections expose aggregate admission and cleanup bounds."""

import os
import shutil
import stat

import pytest
from frozen_tool_fixtures import ToolProject, entries

from pytest_telescope import model, tool_capture, tool_context
from pytest_telescope.model import UnsupportedCapability


def owned_paths(root):
    """Count each owned entry once, without following any fixture link."""
    pending, found = [root], {}
    while pending:
        path = pending.pop()
        info = path.lstat()
        assert info.st_uid == os.getuid()
        found[path.relative_to(root).as_posix()] = (info.st_dev, info.st_ino, info.st_mode)
        assert len(found) <= 128, "tiny fixture exceeded its independent inspection bound"
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return found


def repair_failed_fixture(root):
    """Failure-only test-owned removal; never credited as producer cleanup success."""
    if not root.exists():
        return
    paths = owned_paths(root)
    for name, expected in paths.items():
        path = root / name
        actual = path.lstat()
        assert (actual.st_dev, actual.st_ino, actual.st_mode) == expected
        if stat.S_ISDIR(actual.st_mode):
            path.chmod(stat.S_IMODE(actual.st_mode) | 0o700)
    shutil.rmtree(root)


@pytest.fixture
def sparse_pair(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    sources = []
    for name in ("first", "second"):
        source = project.add_tree(name, preserve=True, files={
            "bin/value" + str(index): b"owned\n" for index in range(8)
        })
        (source / "bin").chmod(0o755)
        (source / "bin/alias").symlink_to("value0")
        project.documents[name]["roots"][0]["coverage"] = "files"
        project.refresh(name)
        sources.append(source)
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first','second'])\ndef test_scoped(): pass\n")
    before = [entries(source) for source in sources]
    stage = tmp_path / "staging"
    stage.mkdir()
    try:
        yield project, sources, stage
    finally:
        # The assertion failure remains RED; this only prevents fixture residue.
        repair_failed_fixture(stage / "tools")
        assert [entries(source) for source in sources] == before


def two_views(capture, sources):
    assert len(owned_paths(capture.directory)) == 26
    first = capture.view(("first",))
    second = capture.view(("second",))
    actual = owned_paths(capture.directory)
    assert len(actual) == 48
    assert len([row for row in actual.values() if stat.S_ISDIR(row[2])]) == 12
    for name, source, view in zip(("first", "second"), sources, (first, second), strict=True):
        copy = capture.directory / name / "install/bin/value0"
        closed = dict((destination, path) for path, destination in view.bindings)[str(source / "bin")]
        original_info, copy_info, projected_info = (source / "bin/value0").stat(), copy.stat(), (closed / "value0").stat()
        assert (original_info.st_dev, original_info.st_ino) != (copy_info.st_dev, copy_info.st_ino)
        assert (copy_info.st_dev, copy_info.st_ino) == (projected_info.st_dev, projected_info.st_ino)
        assert stat.S_IMODE(projected_info.st_mode) == 0o444
        assert os.readlink(closed / "alias") == "value0"
    return actual


def test_two_views_fit_shared_bound_and_reuse_adds_no_paths(sparse_pair, monkeypatch):
    project, sources, stage = sparse_pair
    monkeypatch.setattr(tool_context, "MAX_VISITS", 64)
    with tool_capture.Capture(model.load(project.root), stage, 10, None) as capture:
        previous = two_views(capture, sources)
        identity = capture.view(("first",)).identity
        assert capture.view(("first",)).identity == identity
        assert owned_paths(capture.directory) == previous
    assert not (stage / "tools").exists()


def test_union_reservation_refuses_before_creating_over_bound_paths(sparse_pair, monkeypatch):
    project, sources, stage = sparse_pair
    monkeypatch.setattr(tool_context, "MAX_VISITS", 64)
    with tool_capture.Capture(model.load(project.root), stage, 10, None) as capture:
        previous = two_views(capture, sources)
        refusal = None
        try:
            capture.view(("first", "second"))
        except UnsupportedCapability as error:
            refusal = error
        actual = owned_paths(capture.directory)
        print("OWNED_MATERIALIZATION", {"before": len(previous), "after": len(actual), "limit": 64}, flush=True)
        assert refusal is not None and refusal.capability == "tool:bounds"
        assert actual == previous, "rejected reservation created projection members"
    assert not (stage / "tools").exists()


def test_cleanup_counts_each_of_69_admitted_paths_once(sparse_pair, monkeypatch):
    project, sources, stage = sparse_pair
    monkeypatch.setattr(tool_context, "MAX_VISITS", 69)
    with tool_capture.Capture(model.load(project.root), stage, 10, None) as capture:
        two_views(capture, sources)
        capture.view(("first", "second"))
        actual = owned_paths(capture.directory)
        assert len(actual) == 69
        assert len([row for row in actual.values() if stat.S_ISDIR(row[2])]) == 15
        print("OWNED_MATERIALIZATION", {"admitted": len(actual), "limit": 69}, flush=True)
    assert not (stage / "tools").exists()
