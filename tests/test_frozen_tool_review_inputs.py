"""Owned declaration and inventory controls from independent source review."""

import json

import pytest

from pytest_telescope import model
from pytest_telescope.model import UnsupportedCapability
from pytest_telescope.plan import make_plan

from frozen_tool_fixtures import ToolProject, put


def bundle_project(tmp_path, files, consumers):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    for index in range(files):
        put(project.root, f"package/value{index}.txt", b"owned\n")
    project.configure('[tool.pytest_telescope.bundles.shared]\ninputs = ["package"]\n')
    project.module("test_many.py", "import pytest\n" + "".join(
        f"@pytest.mark.telescope(bundles=['shared'], inputs=['tests/test_many.py'])\n"
        f"def test_{index}(): pass\n" for index in range(consumers)
    ))
    return project


def test_shared_bundle_uses_one_bounded_expansion(tmp_path, monkeypatch):
    project = bundle_project(tmp_path, 80, 12)
    monkeypatch.setattr(model, "MAX_VISITS", 300)
    plan = make_plan(project.root)
    expected = {f"package/value{index}.txt" for index in range(80)}
    assert len(plan["declarations"]) == 12
    assert all(expected <= set(row["inputs"]) for row in plan["declarations"])
    assert plan["tools"] == {}


def test_many_consumers_of_one_real_bundle_fit_discovery_budget(tmp_path):
    project = bundle_project(tmp_path, 2000, 100)
    plan = make_plan(project.root)
    assert len(plan["declarations"]) == 100
    assert all("package/value1999.txt" in row["inputs"] for row in plan["declarations"])
    assert plan["tools"] == {}


@pytest.mark.parametrize("filename", ["value[x].txt", "value?.txt", "value*.txt"])
def test_distribution_resolved_filenames_are_not_glob_patterns(tmp_path, filename):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    put(project.root, "metadata/pyproject.toml", '[project]\nname="owned-static"\nversion="1.0"\n')
    put(project.root, "data/" + filename, b"literal owned member\n")
    project.configure('''[tool.pytest_telescope.distributions.owned]
format = "pep621-static-v1"
metadata = "metadata/pyproject.toml"
build_inputs = ["data/*"]
''')
    project.module("test_literal.py", "import pytest\n@pytest.mark.telescope(distributions=['owned'])\ndef test_literal(): pass\n")
    manifest = model.load(project.root)
    expected = "data/" + filename
    assert expected in manifest.distributions["owned"].inputs
    assert expected in next(iter(manifest.declarations.values())).inputs
    assert expected in make_plan(project.root)["inputs"]


@pytest.mark.parametrize("namespace", [".docker", ".kube", ".netrc"])
@pytest.mark.parametrize("position", ["source", "entry"])
def test_explicit_dummy_credential_namespaces_refuse_before_capture(tmp_path, namespace, position):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    source = project.add_tree("first")
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first'])\ndef test_scoped(): pass\n")
    if position == "source":
        renamed = source.with_name(namespace)
        source.rename(renamed)
        project.documents["first"]["roots"][0]["source"] = str(renamed)
    else:
        put(source, namespace, b"synthetic dummy, never credentials\n")
    project.refresh("first")
    with pytest.raises(UnsupportedCapability) as refused:
        model.load(project.root)
    assert refused.value.capability == "tool:layout"
    assert not (project.root / ".pytest-telescope").exists()


def test_declared_shm_descendant_policy_is_parser_only(tmp_path):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    project.add_tree("first")
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first'])\ndef test_scoped(): pass\n")
    project.documents["first"]["roots"][0]["source"] = "/dev/shm/owned-synthetic-declaration/install"
    project.save("first")
    manifest = model.load(project.root)
    assert manifest.tools["first"].document["roots"][0]["source"] == "/dev/shm/owned-synthetic-declaration/install"
    assert not (project.root / ".pytest-telescope").exists()


@pytest.mark.parametrize("source", ["/dev/shm", "/dev/null", "/proc/owned", "/sys/owned"])
def test_device_roots_remain_protected_in_parser(tmp_path, source):
    project = ToolProject(tmp_path / "project", tmp_path / "external")
    project.add_tree("first")
    project.module("test_scoped.py", "import pytest\npytestmark=pytest.mark.telescope(tools=['first'])\ndef test_scoped(): pass\n")
    project.documents["first"]["roots"][0]["source"] = source
    project.save("first")
    with pytest.raises(UnsupportedCapability) as refused:
        model.load(project.root)
    assert refused.value.capability == "tool:layout"
    assert source not in json.dumps({"capability": refused.value.capability, "error": str(refused.value)})
