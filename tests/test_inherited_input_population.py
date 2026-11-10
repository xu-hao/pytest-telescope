"""Case-unit composition below the declared limit, distinct from structural files."""

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import model
from pytest_telescope.result import dimension_identity, owner_identity


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
    # This explicit static fixture uses the declared lineage and its sole literal
    # alternative; it does not claim that pytest collected or executed the case.
    owners = sorted({owner_identity(row["file"], row["owner"]) for row in manifest.registry})
    parameter_origin = next(row for row in manifest.registry if row["kind"] == "parameter")
    item = {
        "file": "tests/test_owned.py",
        "case_id": "0" * 64,
        "origins": sorted(row["id"] for row in manifest.registry),
        "owners": owners,
        "parameters": [
            {
                "dimension": dimension_identity(
                    parameter_origin["file"], parameter_origin["parameter"]["position"]
                ),
                "index": parameter_origin["parameter"]["index"],
            }
        ],
        "aliases": sorted({"0" * 64, *owners}),
    }
    unit = model.case_unit(manifest, item, manifest.suites[0])
    explicit = {"data/a", "data/b", "data/c", "data/" + parameter}
    assert set(unit.inputs) == explicit | {"tests/test_owned.py", "pyproject.toml"}
    assert not unit.absent and unit.cache
