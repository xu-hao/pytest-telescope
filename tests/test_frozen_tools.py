"""Public static declaration and manifest controls for explicit frozen tools."""

import json

import pytest

from frozen_tool_fixtures import ToolProject, put
from pytest_telescope.model import DeclarationError
from pytest_telescope.plan import make_plan


@pytest.fixture
def project(tmp_path):
    return ToolProject(tmp_path / "project", tmp_path / "installed")


def test_registered_and_default_tools_do_not_scope_an_ordinary_case(project):
    project.definitions["unused"] = {
        "format": "frozen-tool-v1", "manifest": "tool-locks/not-present.json",
    }
    project.configure('[tool.pytest_telescope.defaults]\ntools = ["unused"]\n')
    project.module("test_plain.py", "def test_plain(): pass\n")
    preview = make_plan(project.root)
    assert preview["sandbox"]["required"] is False
    assert preview["tools"] == {}
    assert preview["suites"][0]["ordinary_files"] == ["tests/test_plain.py"]


def test_empty_tools_and_inherited_selections_have_exact_static_routes(project):
    project.add_tree("first")
    project.add_tree("second")
    project.configure('[tool.pytest_telescope.bundles.shared]\ntools = ["second"]\n')
    project.module("test_module.py", "import pytest\npytestmark = pytest.mark.telescope(tools=[])\ndef test_empty(): pass\n")
    project.module("test_mixed.py", """import pytest
def test_plain(): pass
@pytest.mark.telescope(tools=['first', 'first'], bundles=['shared'])
class TestTools:
    @pytest.mark.telescope(tools=[])
    def test_inherits(self): pass
""")
    preview = make_plan(project.root)
    assert preview["sandbox"]["required"] is True
    assert preview["tools"] == project.definitions
    assert preview["suites"][0]["scoped_files"] == ["tests/test_module.py"]
    assert preview["suites"][0]["ordinary_files"] == ["tests/test_mixed.py"]
    rows = {(row["file"], row["line"]): row for row in preview["declarations"]}
    assert rows[("tests/test_module.py", 2)]["tools"] == []
    assert rows[("tests/test_mixed.py", 3)]["tools"] == ["first", "second"]
    assert rows[("tests/test_mixed.py", 5)]["tools"] == []
    assert all(row["route"] == "scoped" for row in rows.values())
    assert str(project.external) not in json.dumps(preview)


def test_structural_narrowing_does_not_prepare_an_excluded_tool(project):
    project.definitions["unused"] = {
        "format": "frozen-tool-v1", "manifest": "tool-locks/not-present.json",
    }
    project.configure()
    project.module("test_plain.py", "def test_plain(): pass\n")
    project.module("test_excluded.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['unused'])\ndef test_external(): pass\n")
    preview = make_plan(project.root, structural_files={"default": ["tests/test_plain.py"]})
    assert preview["inventory"] == ["tests/test_plain.py"]
    assert preview["tools"] == {}
    assert preview["sandbox"]["required"] is False


@pytest.mark.parametrize("declaration", [
    "tools='first'", "tools=[1]", "tools=[True]", "tools=['unknown']",
    "tool=['first']", "tools=['first'] + []",
])
def test_invalid_tool_marker_values_refuse_statically(project, declaration):
    project.add_tree("first")
    project.module("test_bad.py", f"import pytest\n@pytest.mark.telescope({declaration})\ndef test_bad(): pass\n")
    with pytest.raises(DeclarationError):
        make_plan(project.root)


@pytest.mark.parametrize("definition", [
    {"format": "frozen-tool-v1"},
    {"format": "frozen-tool-v1", "manifest": "tool.json", "mount": "/opt"},
    {"format": True, "manifest": "tool.json"},
    {"format": "frozen-tool-v1", "manifest": "/outside/tool.json"},
    {"format": "frozen-tool-v1", "manifest": "../tool.json"},
    {"format": "frozen-tool-v1", "manifest": ".pytest-telescope/tool.json"},
])
def test_registration_has_exact_safe_fields(project, definition):
    project.definitions["first"] = definition
    project.configure()
    project.module("test_bad.py", "import pytest\n@pytest.mark.telescope(tools=['first'])\ndef test_bad(): pass\n")
    with pytest.raises(DeclarationError):
        make_plan(project.root)


def test_plan_never_executes_a_declared_script(project):
    source = project.add_tree("first", files={"run": "#!/bin/sh\nexit 93\n"})
    (source / "run").chmod(0o755)
    project.refresh("first")
    project.module("test_static.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\nraise RuntimeError('must not import while planning')\n")
    preview = make_plan(project.root)
    assert preview["case_inventory"] == "requires pytest collection"
    assert preview["tools"] == project.definitions
    assert not (project.root / ".pytest-telescope").exists()


def test_manifest_path_symlink_refuses_without_reading_its_target(project):
    project.add_tree("first")
    source = project.root / "tool-locks/first.json"
    source.unlink()
    source.symlink_to(put(project.external, "private-marker.json", "private-test-marker"))
    project.module("test_bad.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\ndef test_bad(): pass\n")
    with pytest.raises(DeclarationError):
        make_plan(project.root)
