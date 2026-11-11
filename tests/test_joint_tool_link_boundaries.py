"""Portable data links stay distinct from authenticated project helper origins."""

import json
import os

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes
from test_helper_registry import consumer, project_at

from pytest_telescope import controller, sandbox
from pytest_telescope.model import UnsupportedCapability

PAYLOAD = "owned data [not valid Python source\n"


def linked_project(tmp_path, kind, literal_suffix=""):
    project = ToolProject(tmp_path / "project", tmp_path / "owned-tools")
    source = project.add_tree("frontend", preserve=True, files={"payload.py": PAYLOAD})
    project.bind_link("frontend", path="dependency.py")
    target = source / "payload.py" if kind == "file" else source
    target_text = str(target) + literal_suffix
    alias = project.root / "dependency.py"
    alias.unlink()
    alias.symlink_to(target_text)
    project.documents["frontend"]["links"][0].update(
        target=target_text, subpath="payload.py" if kind == "file" else "."
    )
    project.save("frontend")
    project.module(
        "test_data.py",
        f"""from pathlib import Path
import os
import pytest
pytestmark = pytest.mark.telescope(tools=[])
@pytest.mark.telescope(tools=['frontend'])
def test_consumer():
    assert not os.path.lexists({str(project.root)!r})
    link = Path('/project/dependency.py')
    assert link.is_symlink()
    assert os.readlink(link) == {target_text!r}
    data = link if {kind == "file"!r} else link / 'payload.py'
    assert data.read_text() == {PAYLOAD!r}
    assert Path('/tools/frontend/install/payload.py').read_text() == {PAYLOAD!r}
def test_nonconsumer():
    assert not Path('/tools/frontend').exists()
    assert not Path({str(source)!r}).exists()
    assert not os.path.lexists({str(project.root)!r})
    link = Path('/project/dependency.py')
    assert not link.is_symlink()
    assert not link.exists()
""",
    )
    return project, source


@pytest.mark.parametrize(
    ("kind", "guard", "literal_suffix"),
    [
        ("file", None, ""),
        pytest.param("directory", "available-v1", "", marks=pytest.mark.requires_fixture_guard),
        ("directory", None, "/./"),
    ],
)
def test_py_named_tool_links_remain_data_in_exact_consumer_views(
    tmp_path, kind, guard, literal_suffix
):
    project, _ = linked_project(tmp_path, kind, literal_suffix)
    cold = project.run(execution_mode="batch", fixture_guard=guard)
    print(json.dumps({"data_link": {"kind": kind, "suffix": literal_suffix, "report": cold}}))
    assert_counts(cold, executed=2)
    assert cold["schema"] == (5 if guard else 4)
    assert_counts(project.run(execution_mode="batch", fixture_guard=guard), reused=2)


def test_changed_captured_tool_link_text_refuses_before_launch_or_reuse(tmp_path, monkeypatch):
    project, source = linked_project(tmp_path, "directory")
    assert_counts(project.run(), executed=2)
    previous = cache_bytes(project.root)
    assert previous
    snapshot = controller.snapshot
    execute = sandbox.execute
    mutations, modes, pools = [], [], []

    def changed_snapshot(manifest, unit, destination):
        records = snapshot(manifest, unit, destination)
        alias = destination / "dependency.py"
        record = next(row for row in records if row["path"] == "dependency.py")
        assert record == {"path": "dependency.py", "kind": "symlink", "target": str(source)}
        # Keep the resolved target and every byte unchanged. The captured literal
        # text alone changes, so no unrelated layout or stale-byte check can hide it.
        alias.unlink()
        alias.symlink_to(str(source) + "/.")
        mutations.append("captured-link-text")
        pools.append(destination)
        return records

    def observed(*args, request, **kwargs):
        modes.append(request["mode"])
        return execute(*args, request=request, **kwargs)

    monkeypatch.setattr(controller, "snapshot", changed_snapshot)
    monkeypatch.setattr(sandbox, "execute", observed)
    report = project.run()
    print(json.dumps({"changed_link": {"modes": modes, "report": report}}))
    assert mutations == ["captured-link-text"]
    assert modes == [], report
    assert report["status"] == "infrastructure_error", report
    assert report["counts"]["executed"] == report["counts"]["reused"] == 0
    assert all(unit["attempts"] == 0 for unit in report["units"])
    assert cache_bytes(project.root) == previous
    assert not list((project.root / ".pytest-telescope/cache").rglob("*.pending"))
    assert pools and all(not pool.exists() for pool in pools)
    assert os.readlink(project.root / "dependency.py") == str(source)


def relative_link_project(tmp_path, text):
    """A project whose tool link has relative text, computed from the pinned source."""
    project = ToolProject(tmp_path / "project", tmp_path / "owned-tools")
    source = project.add_tree("frontend", preserve=True, files={"payload.py": PAYLOAD})
    project.bind_link("frontend", path="dependency.py")
    relative = text(source, project.root)
    alias = project.root / "dependency.py"
    alias.unlink()
    alias.symlink_to(relative)
    assert alias.resolve() == source.resolve()
    project.documents["frontend"]["links"][0].update(target=relative)
    project.save("frontend")
    project.module(
        "test_relative.py",
        f"""from pathlib import Path
import os
import pytest
pytestmark = pytest.mark.telescope(tools=['frontend'])
def test_consumer():
    link = Path('/project/dependency.py')
    assert os.readlink(link) == {relative!r}
    assert (link / 'payload.py').read_text() == {PAYLOAD!r}
    assert not os.path.lexists({str(project.root)!r})
""",
    )
    return project


def test_a_relative_project_link_that_reaches_its_target_from_the_frozen_view_is_kept(tmp_path):
    # Enough parent steps to reach the filesystem root from the checkout also reach it
    # from /project, so the unchanged link text names the pinned target in both places.
    project = relative_link_project(
        tmp_path,
        lambda source, root: "../" * (len(root.parts) - 1) + str(source).lstrip("/"),
    )
    assert_counts(project.run(), executed=1)
    assert_counts(project.run(), reused=1)


def test_a_relative_project_link_that_depends_on_the_checkout_location_refuses(tmp_path):
    # The shortest relative text reaches the target only from the checkout's own path.
    # At /project it would name another directory, and the link is never rewritten.
    project = relative_link_project(tmp_path, os.path.relpath)
    with pytest.raises(UnsupportedCapability) as refused:
        controller.run(project.root, explain=True, timeout=20)
    assert refused.value.capability == "tool:layout"


def test_relative_helper_alias_preserves_exact_raw_link_text(tmp_path):
    root = project_at(tmp_path)
    (root / "helpers/alias.py").symlink_to("./support.py")
    support = root / "helpers/support.py"
    support.write_text(
        "import os\n" + support.read_text() + "\nassert os.readlink(__file__) == './support.py'\n"
    )
    consumer(
        root,
        inputs=["helpers/__init__.py", "helpers/alias.py", "helpers/support.py", "data/answer.txt"],
    )
    module = root / "tests/test_consumer.py"
    module.write_text(module.read_text().replace("from helpers.support", "from helpers.alias"))
    cold = controller.run(root, timeout=20)
    print(json.dumps({"helper_link": cold}))
    assert_counts(cold, executed=1)
    assert_counts(controller.run(root, timeout=20), reused=1)
