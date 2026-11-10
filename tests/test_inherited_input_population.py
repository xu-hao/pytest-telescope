"""Case-unit composition below the declared limit, distinct from structural files."""

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import model


@pytest.mark.parametrize("parameter", ["c", "d"], ids=["three-explicit", "four-explicit"])
def test_defaults_and_all_marker_levels_compose_unique_explicit_members(
    tmp_path, monkeypatch, parameter
):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    project.configure('[tool.pytest_telescope.defaults]\ninputs=["data/a"]\n')
    for name in "abcd":
        put(project.root, "data/" + name, name)
    project.module(
        "test_owned.py",
        f"""import pytest
pytestmark = pytest.mark.telescope(inputs=["data/b"])
@pytest.mark.telescope(inputs=["data/c"])
class TestOwned:
    @pytest.mark.telescope(inputs=["data/a"])
    @pytest.mark.parametrize("value", [pytest.param(1, marks=pytest.mark.telescope(inputs=["data/{parameter}"]))])
    def test_owned(self, value): pass
""",
    )
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    manifest = model.load(project.root)
    assert len(manifest.registry) == 4
    # This is the real case-unit composition seam with statically discovered
    # origins, not a fabricated claim that pytest collected/executed the case.
    item = {
        "file": "tests/test_owned.py",
        "case_id": "0" * 64,
        "origins": [row["id"] for row in manifest.registry],
    }
    unit = model.case_unit(manifest, item, manifest.suites[0])
    explicit = {"data/a", "data/b", "data/c", "data/" + parameter}
    assert set(unit.inputs) == explicit | {"tests/test_owned.py", "pyproject.toml"}
    assert not unit.absent and unit.cache
