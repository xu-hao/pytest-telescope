"""Invocation-only file narrowing before source parsing and restricted collection."""

import os

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError
from pytest_telescope.plan import make_plan


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def module_source(suite, *, count=2):
    return (
        "import pytest\nfrom pathlib import Path\n"
        "pytestmark=pytest.mark.telescope(inputs=[])\n"
        + "\n".join(
            f"def test_{number}(pytestconfig):\n"
            f'    assert Path.cwd() == Path("/project")\n'
            f'    assert pytestconfig.rootpath == Path("/project/tests/{suite}")\n'
            '    assert not Path("private.txt").exists()\n'
            for number in range(count)
        )
    )


def configure(root, *, include="**"):
    put(
        root,
        "pyproject.toml",
        f'''
[tool.pytest_telescope.suites.core]
root = "tests/core"
cwd = "."
config = "pyproject.toml"
test_roots = ["tests/core"]
include = ["{include}"]
exclude = ["tests/core/test_excluded.py"]
[tool.pytest_telescope.suites.addon]
root = "tests/addon"
cwd = "."
config = "pyproject.toml"
test_roots = ["tests/addon"]
''',
    )
    for name, suite in (("test_alpha", "core"), ("test_beta", "core"), ("test_addon", "addon")):
        put(root, f"tests/{suite}/{name}.py", module_source(suite))
    put(root, "private.txt", "synthetic undeclared input")


def block_backend(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("static validation must not contact Docker or execute a worker")

    for name in ("identity", "recover", "execute", "command", "build_runtime"):
        monkeypatch.setattr(runtime, name, forbidden)


@pytest.fixture
def project(tmp_path):
    configure(tmp_path)
    image = runtime.build_runtime(tmp_path)["image"]
    yield tmp_path
    runtime._image_info(image, runtime._project(tmp_path))
    runtime.command(["docker", "image", "rm", image])


def run(root, *, status="passed", **kwargs):
    report = controller.run(root, **kwargs)
    assert report["status"] == status, report
    assert report["inventory_complete"] is True
    assert report["grouping_complete"] is True
    assert report["schema"] == 2
    return report


def suite_files(report):
    return {row["name"]: row["files"] for row in report["collection"]["suites"]}


def test_static_plan_narrows_before_ast_and_never_imports_or_contacts_backend(
    tmp_path, monkeypatch
):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/core/test_invalid.py",
        "import pytest\n@pytest.mark.telescope(inputs=unknown_factory())\ndef test_invalid(): pass\n",
    )
    put(
        tmp_path,
        "tests/core/test_unimportable.py",
        'from pathlib import Path\nPath(__file__).with_suffix(".imported").touch()\nraise RuntimeError("synthetic import refusal")\n',
    )
    block_backend(monkeypatch)
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, suites=["core"])
    selected = ["tests/core/test_beta.py", "tests/core/test_alpha.py"]
    result = make_plan(tmp_path, suites=["core"], structural_files={"core": selected})
    assert result["inventory"] == sorted(selected)
    assert result["suites"][0]["inventory"] == sorted(selected)
    assert result["case_inventory"] == "requires isolated pytest collection"
    assert not list(tmp_path.rglob("*.imported"))
    assert selected == ["tests/core/test_beta.py", "tests/core/test_alpha.py"]


@pytest.mark.parametrize("offender", ["invalid-marker", "unimportable"])
def test_selectors_do_not_replace_structural_narrowing(project, offender):
    source = (
        "import pytest\n@pytest.mark.telescope(inputs=unknown_factory())\ndef test_bad(): pass\n"
        if offender == "invalid-marker"
        else 'raise RuntimeError("synthetic collection import refusal")\ndef test_bad(): pass\n'
    )
    put(project, "tests/core/test_bad.py", source)
    arguments = ["tests/core/test_alpha.py::test_0"]
    if offender == "invalid-marker":
        with pytest.raises(DeclarationError):
            controller.run(project, suites=["core"], arguments=arguments)
    else:
        baseline = controller.run(project, suites=["core"], arguments=arguments)
        assert baseline["status"] == "failed"
        assert baseline["collection"]["outcome"] == "failed"
    report = run(
        project,
        suites=["core"],
        arguments=arguments,
        structural_files={"core": ["tests/core/test_alpha.py"]},
    )
    assert suite_files(report) == {"core": ["tests/core/test_alpha.py"]}
    assert report["counts"]["selected"] == report["counts"]["executed"] == 1
    assert report["counts"]["not-selected"] == 1
    assert {row["file"] for row in report["units"]} == {"tests/core/test_alpha.py"}
    assert report["collection"]["collected"] == 2
    with pytest.raises(DeclarationError):
        controller.run(
            project,
            suites=["core"],
            arguments=["tests/core/test_beta.py"],
            structural_files={"core": ["tests/core/test_alpha.py"]},
        )


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_exact_lists_are_normalized_keyed_and_confined_to_the_affected_suite(project, mode):
    options = {"execution_mode": mode}
    original = (project / "pyproject.toml").read_bytes()
    assert run(project, **options)["counts"]["executed"] == 6
    assert run(project, **options)["counts"]["reused"] == 6
    assert run(project, structural_files={}, **options)["counts"]["reused"] == 6
    complete = {"core": ["tests/core/test_alpha.py", "tests/core/test_beta.py"]}
    explicit = run(project, structural_files=complete, **options)
    assert explicit["counts"]["executed"] == 4 and explicit["counts"]["reused"] == 2
    normalized = run(
        project, structural_files={"core": list(reversed(complete["core"]))}, **options
    )
    assert normalized["counts"]["reused"] == 6
    subset = {"core": ["tests/core/test_alpha.py"]}
    narrowed = run(project, structural_files=subset, **options)
    assert narrowed["counts"]["executed"] == narrowed["counts"]["reused"] == 2
    assert narrowed["counts"]["not-selected"] == 0
    assert suite_files(narrowed) == {
        "core": ["tests/core/test_alpha.py"],
        "addon": ["tests/addon/test_addon.py"],
    }
    assert len(narrowed["units"]) == 4
    assert all(row["file"] != "tests/core/test_beta.py" for row in narrowed["units"])
    assert len(narrowed["groups"]) == (4 if mode == "case" else 2)
    put(project, "tests/core/test_new.py", module_source("core"))
    unchanged = run(project, structural_files=subset, **options)
    assert unchanged["counts"]["reused"] == 4
    assert unchanged["collection"]["outcome"] == "reused"
    expanded = run(
        project,
        structural_files={"core": ["tests/core/test_alpha.py", "tests/core/test_new.py"]},
        **options,
    )
    assert expanded["counts"]["executed"] == 4 and expanded["counts"]["reused"] == 2
    assert suite_files(expanded)["core"] == ["tests/core/test_alpha.py", "tests/core/test_new.py"]
    assert (
        next(row for row in expanded["collection"]["suites"] if row["name"] == "addon")["outcome"]
        == "reused"
    )
    automatic = run(project, **options)
    assert automatic["collection"]["collected"] == 8
    assert "tests/core/test_new.py" in suite_files(automatic)["core"]
    assert (project / "pyproject.toml").read_bytes() == original


def test_empty_override_has_no_fallback_inventory_and_unspecified_suites_are_unchanged(project):
    put(
        project,
        "tests/core/test_bad.py",
        "import pytest\n@pytest.mark.telescope(inputs=unknown_factory())\ndef test_bad(): pass\n",
    )
    result = run(project, structural_files={"core": []})
    assert suite_files(result) == {"core": [], "addon": ["tests/addon/test_addon.py"]}
    assert result["collection"]["collected"] == result["counts"]["executed"] == 2
    assert {row["suite"] for row in result["units"]} == {"addon"}
    empty = run(project, status="no_tests", suites=["core"], structural_files={"core": []})
    assert empty["units"] == empty["groups"] == []
    assert suite_files(empty) == {"core": []}


@pytest.mark.parametrize(
    "entry",
    [
        "tests/core/test_missing.py",
        "tests/core",
        "tests/core/test_alpha.txt",
        "tests/addon/test_addon.py",
        "tests/core/test_excluded.py",
        "tests/core/helper.py",
        "tests/core/nested/test_nested.py",
        "./tests/core/test_alpha.py",
        "tests//core/test_alpha.py",
        "tests/core/../core/test_alpha.py",
        "tests\\core\\test_alpha.py",
        "tests/core/test_alpha.py/",
    ],
)
def test_invalid_paths_refuse_in_static_plan_and_before_backend(tmp_path, monkeypatch, entry):
    configure(tmp_path, include="tests/core/test_*.py")
    for path in (
        "tests/core/test_alpha.txt",
        "tests/core/test_excluded.py",
        "tests/core/helper.py",
        "tests/core/nested/test_nested.py",
    ):
        put(tmp_path, path, "def test_example(): pass\n")
    block_backend(monkeypatch)
    override = {"core": [entry]}
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, structural_files=override)
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, structural_files=override)


@pytest.mark.parametrize(
    "override",
    [
        {"unknown": []},
        {"core": "tests/core/test_alpha.py"},
        {"core": ["tests/core/test_alpha.py", "tests/core/test_alpha.py"]},
        {"core": [1]},
        {"core": None},
        {"core": ("tests/core/test_alpha.py",)},
        [],
    ],
)
def test_invalid_override_shapes_and_duplicates_refuse_without_backend(
    tmp_path, monkeypatch, override
):
    configure(tmp_path)
    block_backend(monkeypatch)
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, structural_files=override)
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, structural_files=override)


def test_unselected_suite_key_refuses_without_backend(tmp_path, monkeypatch):
    configure(tmp_path)
    block_backend(monkeypatch)
    override = {"addon": ["tests/addon/test_addon.py"]}
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, suites=["core"], structural_files=override)
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, suites=["core"], structural_files=override)


@pytest.mark.parametrize("kind", ["absolute", "leaf-symlink", "ancestor-symlink", "fifo"])
def test_paths_must_be_canonical_regular_files_without_links(tmp_path, monkeypatch, kind):
    configure(tmp_path)
    entry = "tests/core/test_alias.py"
    if kind == "absolute":
        entry = str(tmp_path / "tests/core/test_alpha.py")
    elif kind == "leaf-symlink":
        (tmp_path / entry).symlink_to("test_alpha.py")
    elif kind == "ancestor-symlink":
        (tmp_path / "tests/core/alias").symlink_to(
            tmp_path / "tests/core", target_is_directory=True
        )
        entry = "tests/core/alias/test_alpha.py"
    else:
        os.mkfifo(tmp_path / entry)
    block_backend(monkeypatch)
    with pytest.raises(DeclarationError):
        make_plan(tmp_path, structural_files={"core": [entry]})
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, structural_files={"core": [entry]})


def test_configured_python_file_patterns_still_constrain_overrides(tmp_path, monkeypatch):
    configure(tmp_path)
    configuration = tmp_path / "pyproject.toml"
    configuration.write_text(
        '[tool.pytest.ini_options]\npython_files=["check_*.py"]\n' + configuration.read_text()
    )
    put(tmp_path, "tests/core/check_small.py", module_source("core"))
    block_backend(monkeypatch)
    result = make_plan(
        tmp_path, suites=["core"], structural_files={"core": ["tests/core/check_small.py"]}
    )
    assert result["inventory"] == ["tests/core/check_small.py"]
    with pytest.raises(DeclarationError):
        make_plan(
            tmp_path, suites=["core"], structural_files={"core": ["tests/core/test_alpha.py"]}
        )
