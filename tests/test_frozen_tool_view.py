"""Required real-Bubblewrap controls for frozen data views and consuming identities."""

import json
import pytest

from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, canonical, digest, put


@pytest.fixture
def project(tmp_path):
    return ToolProject(tmp_path / "project", tmp_path / "installed")


def test_real_view_preserves_project_link_text_and_readonly_frozen_target(project):
    source = project.add_tree("frontend", preserve=True, files={
        "value.txt": "frontend", ".hidden": "included", "nested/data.txt": "nested",
    })
    (source / "alias.txt").symlink_to("value.txt")
    project.refresh("frontend")
    project.bind_link("frontend")
    project.pin_lock("frontend")
    live_inode = (source / "value.txt").stat().st_ino
    put(project.external, "not-declared.txt", "unrelated external data")
    project.module("test_link.py", f"""import errno
import os
import stat
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['frontend'], inputs=['node_modules/nested/data.txt'])
def test_link():
    for base in (Path('/project'), Path({str(project.root)!r})):
        link = base / 'node_modules'
        assert stat.S_ISLNK(link.lstat().st_mode)
        assert os.readlink(link) == {str(source)!r}
        assert (link / 'nested/data.txt').read_text() == 'nested'
        assert (base / 'package-lock.json').read_text() == 'fixture-lock\\n'
    logical = Path('/tools/frontend/install')
    original = Path({str(source)!r})
    for root in (logical, original):
        assert sorted(path.name for path in root.iterdir()) == ['.hidden', 'alias.txt', 'nested', 'value.txt']
        assert (root / 'value.txt').read_text() == 'frontend'
        assert (root / '.hidden').read_text() == 'included'
        assert (root / 'alias.txt').is_symlink()
        assert os.readlink(root / 'alias.txt') == 'value.txt'
        assert (root / 'alias.txt').read_text() == 'frontend'
        assert stat.S_IMODE((root / 'value.txt').stat().st_mode) == 0o444
        assert (root / 'value.txt').stat().st_ino != {live_inode}
        for target in (root / 'value.txt', root / 'new.txt'):
            with pytest.raises(OSError) as refusal:
                target.write_text('attempted change')
            assert refusal.value.errno in (errno.EROFS, errno.EACCES)
    assert not Path({str(project.external / 'not-declared.txt')!r}).exists()
""")
    assert_counts(project.run(), executed=1)
    assert (source / "value.txt").read_text() == "frontend"
    assert (source / "value.txt").stat().st_ino == live_inode
    assert not (source / "new.txt").exists()
    scratch = project.root / ".pytest-telescope" / "scratch"
    assert not scratch.exists() or not list(scratch.iterdir())


def test_nonconsumer_has_no_new_tool_namespace_or_external_alias(project):
    source = project.add_tree("first", preserve=True)
    project.module("test_views.py", f"""from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=[])
@pytest.mark.telescope(tools=['first'])
def test_consumer():
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
    assert Path({str(source / 'value.txt')!r}).read_text() == 'first'
def test_nonconsumer():
    assert not Path('/tools/first').exists()
    assert not Path({str(source)!r}).exists()
""")
    assert_counts(project.run(), executed=2)
    assert_counts(project.run(), reused=2)


def test_omitting_selected_tool_does_not_relax_external_project_link(project):
    project.add_tree("first", preserve=True)
    project.bind_link("first")
    project.module("test_missing.py", """import pytest
pytestmark = pytest.mark.telescope(inputs=['node_modules/value.txt'])
def test_missing(): pass
""")
    report = project.run()
    assert report["status"] in {"declaration_error", "unsupported"}, report
    assert not cache_bytes(project.root)


def test_only_changed_tool_consumer_executes_after_collection_union_refresh(project):
    first = project.add_tree("first", files={"value.txt": "1"})
    project.add_tree("second", files={"value.txt": "2"})
    project.module("test_consumers.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=[])
@pytest.mark.telescope(tools=['first'])
def test_first():
    assert int(Path('/tools/first/install/value.txt').read_text()) > 0
    assert not Path('/tools/second').exists()
@pytest.mark.telescope(tools=['second'])
def test_second():
    assert int(Path('/tools/second/install/value.txt').read_text()) > 0
    assert not Path('/tools/first').exists()
def test_unrelated():
    assert not Path('/tools/first').exists()
    assert not Path('/tools/second').exists()
""")
    assert_counts(project.run(), executed=3)
    assert_counts(project.run(), reused=3)
    put(first, "value.txt", "3")
    previous = cache_bytes(project.root)
    refusal = project.run()
    assert refusal["status"] == "unsupported", refusal
    assert refusal["capability"] == "tool:identity", refusal
    assert cache_bytes(project.root) == previous
    project.refresh("first")
    changed = project.run()
    assert_counts(changed, executed=1, reused=2)
    assert changed["collection"]["suites"][0]["outcome"] != "reused"


@pytest.mark.parametrize("mutation", ["file", "mode", "target", "type", "missing", "extra"])
def test_stale_tree_records_never_reuse_success(project, mutation):
    source = project.add_tree("first", files={"value.txt": "first", "other.txt": "other"})
    (source / "alias.txt").symlink_to("value.txt")
    project.refresh("first")
    project.module("test_tree.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_tree():
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
""")
    assert_counts(project.run(), executed=1)
    previous = cache_bytes(project.root)
    if mutation == "file":
        put(source, "value.txt", "changed")
    elif mutation == "mode":
        (source / "value.txt").chmod(0o600)
    elif mutation == "target":
        (source / "alias.txt").unlink()
        (source / "alias.txt").symlink_to("other.txt")
    elif mutation == "type":
        (source / "value.txt").unlink()
        (source / "value.txt").mkdir()
    elif mutation == "missing":
        (source / "value.txt").unlink()
    else:
        put(source, "unlisted.txt", "new")
    report = project.run()
    assert report["status"] in {"unsupported", "declaration_error"}, report
    if report["status"] == "unsupported":
        assert report["capability"] == "tool:identity", report
    assert cache_bytes(project.root) == previous


def test_lock_bytes_and_complete_tree_pin_both_bind_the_consumer(project):
    source = project.add_tree("first", files={"value.txt": "1"})
    project.pin_lock("first")
    project.module("test_lock.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_lock():
    assert int(Path('/tools/first/install/value.txt').read_text()) > 0
    assert Path('package-lock.json').read_text().startswith('fixture-lock')
""")
    assert_counts(project.run(), executed=1)
    put(project.root, "package-lock.json", "fixture-lock-v2\n")
    report = project.run()
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    put(source, "value.txt", "2")
    project.refresh("first")
    report = project.run()
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    project.pin_lock("first", content="fixture-lock-v2\n")
    assert_counts(project.run(), executed=1)


def test_module_class_function_parameter_default_and_bundle_tools_add_together(project):
    names = ["default", "module", "class", "function", "bundle", "base", "first", "second"]
    for name in names:
        project.add_tree(name)
    project.configure("""[tool.pytest_telescope.defaults]
tools = ['default']
[tool.pytest_telescope.bundles.shared]
tools = ['bundle']
bundles = ['base']
[tool.pytest_telescope.bundles.base]
tools = ['base']
""")
    project.module("test_inheritance.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['module'], bundles=['shared'])
class TestTools:
    pytestmark = pytest.mark.telescope(tools=['class'])
    @pytest.mark.telescope(tools=['function'])
    @pytest.mark.parametrize('name', [
        pytest.param('first', marks=pytest.mark.telescope(tools=['first'])),
        pytest.param('second', marks=pytest.mark.telescope(tools=['second'])),
    ])
    def test_tool(self, name):
        for tool in ('default', 'module', 'class', 'function', 'bundle', 'base', name):
            assert Path('/tools', tool, 'install/value.txt').read_text() == tool
        assert not Path('/tools', 'second' if name == 'first' else 'first').exists()
""")
    assert_counts(project.run(), executed=2)
    assert_counts(project.run(), reused=2)


def test_real_worker_refuses_mutated_tool_declarations(project):
    project.add_tree("first")
    project.add_tree("second")
    project.module("test_mutated.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
pytestmark.kwargs['tools'].append('second')
def test_mutated(): pass
""")
    report = project.run()
    assert report["status"] == "declaration_error", report
    assert not cache_bytes(project.root)


def test_batch_groups_require_exact_tool_sets_and_preserve_selected_order(project):
    project.add_tree("first")
    project.add_tree("second")
    put(project.root, "tests/conftest.py", "def pytest_collection_modifyitems(items):\n    items.reverse()\n")
    project.configure('[tool.pytest_telescope.defaults]\ninputs = ["tests/conftest.py"]\n')
    project.module("test_groups.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=[])
@pytest.mark.telescope(tools=['first'])
def test_a(): assert Path('/tools/first/install/value.txt').read_text() == 'first'
@pytest.mark.telescope(tools=['first', 'first'])
def test_b(): assert not Path('/tools/second').exists()
@pytest.mark.telescope(tools=['second'])
def test_c(): assert not Path('/tools/first').exists()
""")
    report = project.run(execution_mode="batch")
    assert_counts(report, executed=3)
    assert len(report["groups"]) == 2
    def case(name):
        return digest(canonical(["default", f"tests/test_groups.py::test_{name}"]))

    assert [group["case_ids"] for group in report["groups"]] == [[case("c")], [case("b"), case("a")]]
    units = {row["case_id"]: row for row in report["units"]}
    assert all(units[case]["group_id"] == group["group_id"]
               for group in report["groups"] for case in group["case_ids"])
    repeated = project.run(execution_mode="batch")
    assert_counts(repeated, reused=3)
    assert [group["case_ids"] for group in repeated["groups"]] == [group["case_ids"] for group in report["groups"]]


def test_tool_batch_with_shared_teardown_failure_never_reuses_member_passes(project):
    project.add_tree("first")
    project.module("test_teardown.py", """import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
@pytest.fixture(scope='module')
def shared():
    yield
    raise RuntimeError('owned synthetic teardown failure')
def test_a(shared): pass
def test_b(shared): pass
""")
    for _ in range(2):
        report = project.run(execution_mode="batch")
        assert report["status"] == "failed", report
        assert report["counts"]["reused"] == 0
        assert not report["groups"][0]["cache_eligible"]


def test_tool_cache_false_stays_scoped_and_fresh(project):
    project.add_tree("first")
    project.module("test_uncached.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'], cache=False)
def test_value():
    assert Path.cwd() == Path('/project')
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
""")
    for _ in range(2):
        report = project.run()
        assert_counts(report, executed=1)
        assert report["units"][0]["route"] == "scoped"
        assert not report["groups"][0]["cache_eligible"]
    assert not cache_bytes(project.root)


def test_tool_refusal_never_exposes_external_path_or_contents(project):
    source = project.add_tree("first", files={"value.txt": "innocuous-private-content-marker"})
    project.module("test_refusal.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\ndef test_value(): pass\n")
    put(source, "value.txt", "changed-innocuous-private-marker")
    report = project.run()
    assert report["status"] == "unsupported", report
    serialized = json.dumps(report)
    for marker in (str(source), str(project.external), "innocuous-private", "changed-innocuous"):
        assert marker not in serialized


@pytest.mark.parametrize("link_target", ["missing", "../escape", "/unbound-tool-file", "cycle"])
def test_unpinned_escaping_or_cyclic_tool_links_refuse(project, link_target):
    source = project.add_tree("first")
    (source / "cycle").symlink_to(link_target)
    project.refresh("first")
    project.module("test_bad_link.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\ndef test_value(): pass\n")
    report = project.run()
    assert report["status"] in {"declaration_error", "unsupported"}, report
    assert not cache_bytes(project.root)
