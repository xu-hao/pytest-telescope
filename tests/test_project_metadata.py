"""Behavioral controls for the bounded, backend-free static metadata projection."""

import hashlib
import importlib.metadata
import stat

import pytest
from packaging.requirements import Requirement

from pytest_telescope import project_metadata as metadata
from pytest_telescope.model import DeclarationError, UnsupportedCapability, expand


def write_project(root, text, directory="core"):
    path = root / directory / "pyproject.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"format": "pep621-static-v1", "metadata": path.relative_to(root).as_posix()}


def load(root, entries):
    return metadata.load_distributions(
        root, entries, lambda names, label: expand(root, names, label)
    )


def simple(name="Sample_Core", version="v1.0.0"):
    return f'[project]\nname = "{name}"\nversion = "{version}"\n'


def discover(path):
    return list(importlib.metadata.distributions(path=[str(path)]))


def test_complete_projection_standard_discovery_and_selection(tmp_path, monkeypatch):
    core = write_project(
        tmp_path,
        simple()
        + """
 description = "Example core package"
 requires-python = ">=3.11"
 dependencies = ["base-lib>=1; python_version >= '3.10'"]
 dynamic = []
 [project.optional-dependencies]
 Fast_Mode = ["helper>=2; sys_platform == 'linux' or sys_platform == 'darwin'"]
 [project.scripts]
 sample = "sample_core:main"
 [project.gui-scripts]
 sample-gui = "sample_core:main"
 [project.entry-points."example.plugins"]
 core = "sample_core:VALUE"
 [build-system]
 requires = ["unknown-backend"]
 build-backend = "unknown_backend"
 [tool.example]
 identity = 1
 """,
    )
    addon = write_project(tmp_path, simple("Example_Addon", "2.0"), "addon")
    source = tmp_path / "core" / "sample_core.py"
    source.write_text('VALUE = "hello"\ndef main(): return 3\n')
    (tmp_path / "core" / "build.txt").write_text("identity")
    core["build_inputs"] = ["core/build.txt"]
    distributions = load(tmp_path, {"core": core, "addon": addon})
    assert distributions["core"].name == "sample-core"
    assert distributions["core"].inputs == ["core/build.txt", "core/pyproject.toml"]
    assert "core/sample_core.py" not in distributions["core"].inputs
    assert distributions["core"].source_hashes == {
        "core/pyproject.toml": hashlib.sha256(
            (tmp_path / "core/pyproject.toml").read_bytes()
        ).hexdigest()
    }
    destination = tmp_path / "metadata"
    metadata.materialize_metadata(distributions, ["core"], destination)
    (found,) = discover(destination)
    assert found.metadata["Name"] == "sample-core"
    assert found.version == "1.0.0"
    assert found.metadata["Summary"] == "Example core package"
    assert found.metadata["Requires-Python"] == ">=3.11"
    assert found.metadata.get_all("Provides-Extra") == ["fast-mode"]
    assert found.requires == [
        'base-lib>=1; python_version >= "3.10"',
        'helper>=2; (sys_platform == "linux" or sys_platform == "darwin") and extra == "fast-mode"',
    ]
    extra = Requirement(found.requires[1])
    assert extra.marker.evaluate({"extra": "fast-mode", "sys_platform": "linux"})
    assert extra.marker.evaluate({"extra": "fast-mode", "sys_platform": "darwin"})
    assert not extra.marker.evaluate({"extra": "other", "sys_platform": "linux"})
    assert not extra.marker.evaluate({"extra": "fast-mode", "sys_platform": "win32"})
    assert {(entry.group, entry.name) for entry in found.entry_points} == {
        ("console_scripts", "sample"),
        ("gui_scripts", "sample-gui"),
        ("example.plugins", "core"),
    }
    with pytest.raises(ModuleNotFoundError):
        next(iter(found.entry_points.select(group="example.plugins"))).load()
    monkeypatch.syspath_prepend(str(source.parent))
    assert next(iter(found.entry_points.select(group="example.plugins"))).load() == "hello"
    monkeypatch.syspath_prepend(str(destination))
    assert importlib.metadata.version("sample-core") == "1.0.0"
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        importlib.metadata.version("example-addon")
    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == ["sample_core-1.0.0.dist-info/METADATA", "sample_core-1.0.0.dist-info/entry_points.txt"]
    records = metadata.metadata_records(distributions, ["core", "core"])
    assert records == [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in sorted(distributions["core"].files.items())
    ]
    for path in [destination, *destination.rglob("*")]:
        info = path.stat()
        assert stat.S_IMODE(info.st_mode) == (0o755 if path.is_dir() else 0o444)
        assert info.st_mtime_ns == 0


def test_frozen_bytes_and_inert_configuration_identity(tmp_path):
    entry = write_project(tmp_path, simple() + "[tool.example]\nvalue = 1\n")
    before = load(tmp_path, {"core": entry})
    (tmp_path / entry["metadata"]).write_text(simple() + "[tool.example]\nvalue = 2\n")
    after = load(tmp_path, {"core": entry})
    assert before["core"].files == after["core"].files
    assert before["core"].source_hashes != after["core"].source_hashes
    destination = tmp_path / "frozen"
    metadata.materialize_metadata(before, ["core"], destination)
    assert discover(destination)[0].version == "1.0.0"


@pytest.mark.parametrize(
    "field,value",
    [
        ("readme", '"README.md"'),
        ("license", '"MIT"'),
        ("license-files", '["LICENSE"]'),
        ("authors", '[{name="Example"}]'),
        ("maintainers", '[{name="Example"}]'),
        ("classifiers", '["Programming Language :: Python"]'),
        ("keywords", '["example"]'),
        ("urls", '{Home="https://example.invalid"}'),
    ],
)
def test_unsupported_fields_refuse_explicitly(tmp_path, field, value):
    entry = write_project(tmp_path, simple() + f"{field} = {value}\n")
    with pytest.raises(UnsupportedCapability) as raised:
        load(tmp_path, {"core": entry})
    assert raised.value.capability == metadata.CAPABILITY


@pytest.mark.parametrize(
    "text",
    [
        '[build-system]\nbuild-backend="backend"\n',
        '[project]\nname="example"\n',
        simple() + 'dynamic=["description"]\n',
        simple() + '[build-system]\nbackend-path=["."]\n',
    ],
)
def test_dynamic_missing_and_backend_metadata_never_execute(tmp_path, text):
    sentinel = tmp_path / "executed"
    (tmp_path / "backend.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    )
    entry = write_project(tmp_path, text)
    with pytest.raises(UnsupportedCapability):
        load(tmp_path, {"core": entry})
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "text",
    [
        "invalid TOML",
        '[project]\nname="example"\nversion=3\n',
        simple("../bad"),
        simple(version="not/a/version"),
        simple() + 'description="injected\\nName: other"\n',
        simple() + 'requires-python="not a specifier"\n',
        simple() + 'dependencies=["not a requirement !"]\n',
        simple() + 'dependencies=["good\\nVersion: 2"]\n',
        simple() + 'dynamic="version"\n',
        simple() + '[project.optional-dependencies]\n"../bad"=["helper"]\n',
        simple() + "[project.optional-dependencies]\nfast_mode=[]\nfast-mode=[]\n",
        simple() + '[project.entry-points.console_scripts]\nfoo="sample:main"\n',
        simple() + '[project.entry-points."bad]group"]\nfoo="sample:main"\n',
        simple() + '[project.scripts]\n"bad=name"="sample:main"\n',
        simple() + '[project.scripts]\n"../bad"="sample:main"\n',
        simple() + '[project.scripts]\nfoo="sample:main\\nother = sample:main"\n',
        simple() + '[project.scripts]\nfoo="sample"\n',
        simple() + '[project.entry-points."example.plugins"]\nfoo="sample:main()"\n',
        simple() + "[project.scripts]\nfoo=3\n",
    ],
)
def test_invalid_declarations_cannot_inject_metadata(tmp_path, text):
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": write_project(tmp_path, text)})


def test_normalized_duplicate_names_refuse(tmp_path):
    one = write_project(tmp_path, simple("Example_Core"))
    two = write_project(tmp_path, simple("example.core"), "addon")
    with pytest.raises(DeclarationError, match="duplicate normalized"):
        load(tmp_path, {"one": one, "two": two})


@pytest.mark.parametrize("mutation", ["missing", "symlink", "directory", "oversize", "encoding"])
def test_source_must_be_bounded_regular_utf8(tmp_path, mutation):
    entry = write_project(tmp_path, simple())
    path = tmp_path / entry["metadata"]
    path.unlink()
    if mutation == "symlink":
        other = tmp_path / "other.toml"
        other.write_text(simple())
        path.symlink_to(other)
    elif mutation == "directory":
        path.mkdir()
    elif mutation == "oversize":
        path.write_bytes(b"x" * (metadata.MAX_SOURCE_BYTES + 1))
    elif mutation == "encoding":
        path.write_bytes(b"\xff")
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": entry})


def test_source_parent_symlink_refuses(tmp_path):
    entry = write_project(tmp_path, simple())
    (tmp_path / "alias").symlink_to(tmp_path / "core", target_is_directory=True)
    entry["metadata"] = "alias/pyproject.toml"
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": entry})


def test_strict_entry_and_unknown_selection(tmp_path):
    entry = write_project(tmp_path, simple())
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": dict(entry, surprise=True)})
    with pytest.raises(UnsupportedCapability):
        load(tmp_path, {"core": dict(entry, format="dynamic")})
    with pytest.raises(UnsupportedCapability):
        load(tmp_path, {"core": dict(entry, metadata="setup.py")})
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": dict(entry, metadata="../pyproject.toml")})
    distributions = load(tmp_path, {"core": entry})
    with pytest.raises(DeclarationError, match="unknown selected"):
        metadata.metadata_records(distributions, ["absent"])
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        metadata.materialize_metadata(distributions, ["core"], destination)
    empty = tmp_path / "empty"
    metadata.materialize_metadata(distributions, [], empty)
    assert discover(empty) == []


def test_distribution_and_combined_item_caps(tmp_path):
    with pytest.raises(DeclarationError, match="256"):
        load(tmp_path, {str(number): {} for number in range(257)})
    dependencies = ",".join('"example"' for _ in range(4096))
    entry = write_project(
        tmp_path,
        simple() + f'dependencies=[{dependencies}]\n[project.scripts]\nexample="example:main"\n',
    )
    with pytest.raises(DeclarationError, match="too many dependencies"):
        load(tmp_path, {"core": entry})


def test_generated_size_caps_and_destination_validation(tmp_path, monkeypatch):
    entry = write_project(tmp_path, simple())
    distributions = load(tmp_path, {"core": entry})
    monkeypatch.setattr(metadata, "MAX_DISTRIBUTION_BYTES", 1)
    with pytest.raises(DeclarationError, match="size limit"):
        load(tmp_path, {"core": entry})
    destination = tmp_path / "too-large"
    with pytest.raises(DeclarationError, match="size limit"):
        metadata.materialize_metadata(distributions, ["core"], destination)
    assert not destination.exists()
    monkeypatch.setattr(metadata, "MAX_DISTRIBUTION_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(metadata, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(DeclarationError, match="total size limit"):
        load(tmp_path, {"core": entry})
    monkeypatch.setattr(metadata, "MAX_TOTAL_BYTES", 16 * 1024 * 1024)
    distributions["core"].files["../outside"] = b"bad"
    with pytest.raises(DeclarationError):
        metadata.materialize_metadata(distributions, ["core"], destination)
    assert not destination.exists()


def test_frozen_materialization_survives_source_mutation_and_deletion(tmp_path):
    entry = write_project(tmp_path, simple())
    before = load(tmp_path, {"core": entry})
    path = tmp_path / entry["metadata"]
    path.write_text(simple(version="2.0"))
    after = load(tmp_path, {"core": entry})
    assert metadata.metadata_records(before, ["core"]) != metadata.metadata_records(after, ["core"])
    path.unlink()
    with pytest.raises(DeclarationError):
        load(tmp_path, {"core": entry})
    destination = tmp_path / "frozen"
    metadata.materialize_metadata(before, ["core"], destination)
    assert discover(destination)[0].version == "1.0.0"


def test_plain_module_and_extra_entry_points(tmp_path):
    entry = write_project(
        tmp_path,
        simple()
        + '[project.entry-points."example.plugins"]\nmodule="example_plugin"\nextras="example_plugin:main [helper]"\n',
    )
    distributions = load(tmp_path, {"core": entry})
    destination = tmp_path / "metadata"
    metadata.materialize_metadata(distributions, ["core"], destination)
    entries = discover(destination)[0].entry_points
    assert entries["module"].module == "example_plugin"
    assert entries["extras"].extras == ["helper"]


def test_exact_minimal_metadata_and_empty_static_strings(tmp_path):
    entry = write_project(tmp_path, simple() + 'description=""\nrequires-python=""\n')
    distributions = load(tmp_path, {"core": entry})
    assert distributions["core"].files == {
        "sample_core-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.3\nName: sample-core\nVersion: 1.0.0\nSummary: \nRequires-Python: \n\n"
        )
    }
