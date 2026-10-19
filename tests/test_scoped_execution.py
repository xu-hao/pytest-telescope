"""Required real-worker controls for scoped metadata and independent suites."""

import hashlib
import json
from pathlib import Path

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError, UnsupportedCapability

BUDGET = dict(cpus=2, memory_bytes=1073741824, processes=256, scratch_bytes=134217728)


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


@pytest.fixture
def project(tmp_path):
    image = runtime.build_runtime(tmp_path)["image"]
    yield tmp_path
    runtime._image_info(image, runtime._project(tmp_path))
    runtime.command(["docker", "image", "rm", image])


def passed(root, **kwargs):
    report = controller.run(root, explain=True, **kwargs)
    assert report["status"] == "passed", report
    assert report["schema"] == 1 and report["kind"] == "run"
    assert report["inventory_complete"] is True
    assert len(report["inventory_digest"]) == 64
    assert report["counts"]["selected"] == sum(row["selected"] for row in report["units"])
    assert report["counts"]["not-selected"] == sum(not row["selected"] for row in report["units"])
    assert all(len(row["case_id"]) == 64 for row in report["units"])
    return report


def configure_metadata(root):
    put(
        root,
        "pyproject.toml",
        """
[tool.pytest.ini_options]
pythonpath = ["packages/core", "packages/addon"]
[tool.pytest_telescope]
test_roots = ["tests"]
[tool.pytest_telescope.distributions.core]
format = "pep621-static-v1"
metadata = "packages/core/pyproject.toml"
build_inputs = ["packages/core/build.txt"]
[tool.pytest_telescope.distributions.addon]
format = "pep621-static-v1"
metadata = "packages/addon/pyproject.toml"
""",
    )
    for package in ("core", "addon"):
        put(
            root,
            f"packages/{package}/pyproject.toml",
            f'''
[project]
name = "example-{package}"
version = "1.0"
[project.entry-points."example.plugins"]
{package} = "{package}:VALUE"
''',
        )
        put(root, f"packages/{package}/{package}.py", 'VALUE = "example"\nOTHER = "example"\n')
    put(root, "packages/core/build.txt", "build identity\n")
    for package, other in (("core", "addon"), ("addon", "core")):
        put(
            root,
            f"tests/test_{package}.py",
            f'''
import pytest
pytestmark = pytest.mark.telescope(distributions=["{package}"], inputs=["packages/{package}/{package}.py"])
def test_distribution():
    from importlib.metadata import version, entry_points, PackageNotFoundError
    from pathlib import Path
    assert version("example-{package}") in ("1.0", "2.0")
    assert next(iter(entry_points(group="example.plugins", name="{package}"))).load() == "example"
    with pytest.raises(PackageNotFoundError): version("example-{other}")
    assert not Path("packages/{other}/{other}.py").exists()
    with pytest.raises(OSError):
        next(Path("/metadata").rglob("METADATA")).write_text("changed")
''',
        )


def test_metadata_is_selected_and_mutations_invalidate_only_consumers(project):
    configure_metadata(project)
    assert passed(project)["counts"]["executed"] == 2
    assert passed(project)["counts"]["reused"] == 2
    for path, change in [
        (
            "packages/core/pyproject.toml",
            lambda text: text.replace('version = "1.0"', 'version = "2.0"'),
        ),
        ("packages/core/build.txt", lambda text: text + "changed\n"),
        ("packages/core/core.py", lambda text: text + "# implementation identity\n"),
        ("packages/core/pyproject.toml", lambda text: text + "\n[tool.identity]\nvalue = 1\n"),
        ("packages/core/pyproject.toml", lambda text: text.replace("core:VALUE", "core:OTHER")),
    ]:
        file = project / path
        file.write_text(change(file.read_text()))
        result = passed(project)
        assert result["counts"]["executed"] == result["counts"]["reused"] == 1
        assert {row["file"]: row["outcome"] for row in result["units"]} == {
            "tests/test_core.py": "executed",
            "tests/test_addon.py": "reused",
        }


def test_metadata_without_source_and_source_without_metadata(project):
    configure_metadata(project)
    put(
        project,
        "tests/test_core.py",
        """
import pytest
pytestmark = pytest.mark.telescope(distributions=["core"])
def test_metadata_only():
    from importlib.metadata import version, entry_points
    assert version("example-core") == "1.0"
    with pytest.raises(ModuleNotFoundError):
        next(iter(entry_points(group="example.plugins", name="core"))).load()
""",
    )
    put(
        project,
        "tests/test_addon.py",
        """
import pytest
pytestmark = pytest.mark.telescope(distributions=[], inputs=["packages/addon/addon.py"])
def test_source_only():
    from importlib.metadata import version, PackageNotFoundError
    import addon
    assert addon.VALUE == "example"
    with pytest.raises(PackageNotFoundError): version("example-addon")
""",
    )
    assert passed(project)["cases"]["passed"] == 2


def test_runtime_distribution_name_conflict_is_unsupported(project):
    configure_metadata(project)
    file = project / "packages/core/pyproject.toml"
    file.write_text(file.read_text().replace('name = "example-core"', 'name = "pytest"'))
    with pytest.raises(UnsupportedCapability) as raised:
        controller.run(project)
    assert raised.value.capability == "project-metadata:runtime-conflict"
    assert not list((project / ".pytest-telescope/cache").glob("*.json"))


def configure_suites(root):
    put(
        root,
        "pyproject.toml",
        """
[tool.pytest_telescope.suites.core]
root = "tests/core"
config = "tests/core/pytest.ini"
test_roots = ["tests/core"]
exclude = ["tests/core/test_excluded.py"]
[tool.pytest_telescope.suites.addon]
root = "tests/addon"
config = "tests/addon/setup.cfg"
test_roots = ["tests/addon"]
""",
    )
    for name, configuration in (("core", "pytest.ini"), ("addon", "setup.cfg")):
        section = "tool:pytest" if configuration == "setup.cfg" else "pytest"
        put(
            root,
            f"tests/{name}/{configuration}",
            f"[{section}]\nmarkers = selected: selection control\n",
        )
        put(
            root,
            f"tests/{name}/conftest.py",
            f'import pytest\n@pytest.fixture\ndef local_value(): return "{name}"\n',
        )
        put(
            root,
            f"tests/{name}/test_local.py",
            f'''
import pytest
from pathlib import Path
pytestmark = pytest.mark.telescope(inputs=["tests/{name}/conftest.py"])
@pytest.mark.selected
def test_local(local_value, pytestconfig):
    assert local_value == "{name}"
    assert Path.cwd() == Path("/project/tests/{name}")
    assert pytestconfig.rootpath == Path("/project/tests/{name}")
    assert pytestconfig.inipath == Path("/project/tests/{name}/{configuration}")
''',
        )
    put(
        root,
        "tests/core/test_excluded.py",
        'raise AssertionError("structural exclusion must precede import")\n',
    )


def test_suites_keep_roots_config_conftest_selection_and_membership_separate(project):
    configure_suites(project)
    cold = passed(project)
    assert cold["cases"]["passed"] == 2
    assert cold["suites"] == ["addon", "core"]
    assert passed(project)["counts"]["reused"] == 2
    assert passed(project, suites=["core"])["cases"]["passed"] == 1
    for selectors in (["-k", "local"], ["-m", "selected"]):
        assert passed(project, arguments=selectors)["cases"]["passed"] == 2
    selected = passed(project, arguments=["tests/core/test_local.py::test_local"])
    assert selected["counts"]["not-selected"] == 1
    assert selected["cases"]["passed"] == 1
    with pytest.raises(DeclarationError):
        controller.run(project, arguments=["tests/core/test_excluded.py"])
    put(
        project,
        "tests/core/test_added.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["tests/core/conftest.py"])\ndef test_added(): pass\n',
    )
    added = passed(project)
    assert added["collection"]["collected"] == 3
    assert added["counts"]["executed"] == 1
    assert added["counts"]["reused"] == 2
    (project / "tests/core/test_added.py").unlink()
    assert passed(project)["collection"]["collected"] == 2


def test_same_file_has_independent_suite_case_identities(project):
    put(
        project,
        "pyproject.toml",
        """
[tool.pytest_telescope.suites.first]
root = "."
config = "pyproject.toml"
test_roots = ["test_shared.py"]
[tool.pytest_telescope.suites.second]
root = "."
config = "pyproject.toml"
test_roots = ["test_shared.py"]
""",
    )
    put(project, "test_shared.py", "def test_shared(): pass\n")
    report = passed(project, budget=BUDGET)
    assert report["cases"]["passed"] == 2
    assert len({row["case_id"] for row in report["units"]}) == 2
    assert {row["file"] for row in report["units"]} == {"test_shared.py"}


def test_effective_addopts_override_removes_unsupported_scheduler(project):
    put(project, "pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n auto"\n')
    put(project, "test_one.py", "def test_one(): pass\n")
    with pytest.raises(UnsupportedCapability) as raised:
        controller.run(project)
    assert raised.value.capability == "pytest:distributed-scheduling"
    assert passed(project, arguments=["-o", "addopts="])["cases"]["passed"] == 1


def test_private_parameter_ids_stay_out_of_requests_cache_and_summaries(project, monkeypatch):
    sensitive = "synthetic-private-parameter-identity"
    put(project, "pyproject.toml", "[tool.pytest_telescope]\n")
    put(
        project,
        "test_parameters.py",
        f'import pytest\n@pytest.mark.parametrize("value", [pytest.param(1, id="{sensitive}")])\ndef test_value(value): assert value == 1\n',
    )
    original = runtime._owned_capture
    captured = []

    def capture(directory, data, args, timeout, **kwargs):
        request = Path(directory) / "control/request.json"
        if request.exists():
            captured.append(request.read_bytes())
        return original(directory, data, args, timeout, **kwargs)

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    selector = f"test_parameters.py::test_value[{sensitive}]"
    report = passed(project, arguments=[selector])
    expected = hashlib.sha256(
        json.dumps(["default", selector], separators=(",", ":")).encode()
    ).hexdigest()
    assert report["units"][0]["case_id"] == expected
    assert passed(project, arguments=[selector])["counts"]["reused"] == 1
    persisted = [
        path.read_bytes() for path in (project / ".pytest-telescope/cache").rglob("*.json")
    ]
    assert captured and persisted
    for data in [json.dumps(report).encode(), *captured, *persisted]:
        assert sensitive.encode() not in data


def test_real_parallel_workers_are_fresh_and_resource_bounded(project):
    put(project, "pyproject.toml", "[tool.pytest_telescope]\n")
    put(
        project,
        "test_parallel.py",
        """
import pytest
from pathlib import Path
@pytest.mark.parametrize("number", range(4))
def test_private_scratch(number):
    assert not Path("/tmp/unique").exists()
    Path("/tmp/unique").write_text(str(number))
    assert Path("/sys/fs/cgroup/memory.max").read_text().strip() == "536870912"
    assert Path("/sys/fs/cgroup/pids.max").read_text().strip() == "128"
    assert Path("/sys/fs/cgroup/cpu.max").read_text().split() == ["100000", "100000"]
""",
    )
    report = passed(project, budget=BUDGET)
    assert report["resources"]["concurrency"] == 2
    assert report["cases"]["passed"] == 4
    assert [row["case_id"] for row in report["units"]] == [
        hashlib.sha256(
            json.dumps(
                ["default", f"test_parallel.py::test_private_scratch[{number}]"],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for number in range(4)
    ]
    assert passed(project, budget=BUDGET)["counts"]["reused"] == 4
    with pytest.raises(DeclarationError, match="cannot fit"):
        controller.run(project, budget=BUDGET | {"memory_bytes": 1})


def test_explicit_async_plugin_and_independent_suite_configuration(tmp_path):
    put(tmp_path, "requirements.txt", "pytest-asyncio==1.3.0\ntyping_extensions==4.15.0\n")
    image = runtime.build_runtime(tmp_path, tmp_path / "requirements.txt")["image"]
    try:
        put(
            tmp_path,
            "pyproject.toml",
            """
[tool.pytest_telescope.suites.asyncio]
root = "tests/asyncio"
config = "tests/asyncio/pytest.ini"
test_roots = ["tests/asyncio"]
plugins = ["pytest_asyncio.plugin"]
""",
        )
        put(
            tmp_path,
            "tests/asyncio/pytest.ini",
            "[pytest]\nasyncio_mode = strict\nasyncio_default_fixture_loop_scope = function\n",
        )
        put(
            tmp_path,
            "tests/asyncio/test_async.py",
            """
import pytest
import pytest_asyncio
@pytest_asyncio.fixture
async def number(): return 7
@pytest.mark.asyncio
async def test_async(number): assert number == 7
""",
        )
        assert passed(tmp_path)["cases"]["passed"] == 1
        assert passed(tmp_path)["counts"]["reused"] == 1
    finally:
        runtime._image_info(image, runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", image])


def test_empty_structural_suite_never_falls_back_to_implicit_discovery(project):
    (project / "tests/empty").mkdir(parents=True)
    put(
        project,
        "pyproject.toml",
        """
[tool.pytest_telescope.defaults]
inputs = ["test_helper.py"]
[tool.pytest_telescope.suites.empty]
root = "."
config = "pyproject.toml"
test_roots = ["tests/empty"]
""",
    )
    put(project, "test_helper.py", 'raise AssertionError("outside structural inventory")\n')
    report = controller.run(project)
    assert report["status"] == "no_tests", report
    assert report["inventory_complete"] is True
    assert report["units"] == []
    assert report["collection"]["suites"][0]["files"] == []


def test_broad_metadata_and_explicit_empty_metadata_scope(project):
    configure_metadata(project)
    put(
        project,
        "tests/test_broad.py",
        """
def test_broad():
    from importlib.metadata import version
    assert version("example-core") == version("example-addon") == "1.0"
""",
    )
    put(
        project,
        "tests/test_empty.py",
        """
import pytest
pytestmark = pytest.mark.telescope(distributions=[])
def test_empty_metadata():
    from importlib.metadata import version, PackageNotFoundError
    for name in ("example-core", "example-addon"):
        with pytest.raises(PackageNotFoundError): version(name)
""",
    )
    report = passed(project)
    assert report["cases"]["passed"] == 4
