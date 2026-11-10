"""Small real declarations distinguish raw patterns from resolved membership."""

import pytest
from frozen_tool_fixtures import ToolProject, put

from pytest_telescope import model
from pytest_telescope.model import DeclarationError


def project(tmp_path, *, inputs=(), absent=(), bundles=(), distributions=(), config=""):
    value = ToolProject(tmp_path / "project", tmp_path / "external")
    value.configure(config)
    value.module("test_owned.py", f"""import pytest
pytestmark = pytest.mark.telescope(inputs={list(inputs)!r}, absent={list(absent)!r},
    bundles={list(bundles)!r}, distributions={list(distributions)!r})
def test_owned(): pass
""")
    return value


def resolved(value):
    manifest = model.load(value.root)
    (unit,) = manifest.declarations.values()
    return unit


@pytest.mark.parametrize("count", [3, 4, 5], ids=["below", "exact", "above"])
def test_direct_expanded_membership_boundary(tmp_path, monkeypatch, count):
    value = project(tmp_path, inputs=["data/*.txt"])
    names = [f"data/value{index}.txt" for index in range(count)]
    for name in names:
        put(value.root, name, "owned\n")
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    if count > 4:
        with pytest.raises(DeclarationError, match="expanded declarations"):
            resolved(value)
    else:
        assert resolved(value).inputs == names


@pytest.mark.parametrize("extras", [0, 1, 2], ids=["below", "exact", "above"])
def test_bundle_direct_and_absence_use_final_union(tmp_path, monkeypatch, extras):
    names = ["data/a", "data/b", *(f"extra/{index}" for index in range(extras))]
    value = project(tmp_path, inputs=["data/b", *names[2:]], absent=["missing"],
                    bundles=["shared"], config='[tool.pytest_telescope.bundles.shared]\ninputs=["data/a", "data/b"]\n')
    for name in names:
        put(value.root, name, "owned\n")
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    if extras == 2:
        with pytest.raises(DeclarationError, match="effective declaration"):
            resolved(value)
    else:
        unit = resolved(value)
        assert unit.inputs == sorted(names) and unit.absent == ["missing"]


@pytest.mark.parametrize("filename", ["value[x].txt", "value?.txt", "value*.txt"])
@pytest.mark.parametrize("extras", [0, 1, 2], ids=["below", "exact", "above"])
def test_resolved_distribution_paths_are_literal_and_counted_once(
    tmp_path, monkeypatch, filename, extras
):
    extra = [f"extra/{index}" for index in range(extras)]
    value = project(tmp_path, inputs=extra, absent=["missing"], distributions=["owned"], config='''
[tool.pytest_telescope.distributions.owned]
format = "pep621-static-v1"
metadata = "metadata/pyproject.toml"
build_inputs = ["data/*"]
''')
    put(value.root, "metadata/pyproject.toml", '[project]\nname="owned-static"\nversion="1.0"\n')
    put(value.root, "data/" + filename, "literal bytes\n")
    for name in extra:
        put(value.root, name, "owned\n")
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    if extras == 2:
        with pytest.raises(DeclarationError, match="effective declaration"):
            resolved(value)
    else:
        unit = resolved(value)
        assert unit.inputs == sorted(["metadata/pyproject.toml", "data/" + filename, *extra])
        assert unit.absent == ["missing"]


def test_duplicate_references_do_not_charge_resolved_members_twice(tmp_path, monkeypatch):
    value = project(tmp_path, inputs=["data/a", "data/a", "data/b"],
                    absent=["missing", "missing"], bundles=["shared", "shared"],
                    config='[tool.pytest_telescope.bundles.shared]\ninputs=["data/a", "data/b"]\n')
    for name in ("a", "b"):
        put(value.root, "data/" + name, "owned\n")
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    unit = resolved(value)
    assert unit.inputs == ["data/a", "data/b"] and unit.absent == ["missing"]


@pytest.mark.parametrize("count", [4, 5], ids=["raw-exact", "raw-above"])
def test_raw_pattern_bound_remains_distinct_from_one_resolved_file(tmp_path, monkeypatch, count):
    patterns = ["data/*", "data/x*", "data/*.txt", "data/x.txt", "data/[x].txt"]
    value = project(tmp_path, inputs=patterns[:count])
    put(value.root, "data/x.txt", "owned\n")
    monkeypatch.setattr(model, "MAX_INPUT_PATHS", 4)
    if count == 5:
        with pytest.raises(DeclarationError, match="effective declaration"):
            resolved(value)
    else:
        assert resolved(value).inputs == ["data/x.txt"]
