"""Suite test_roots containment under the suite root and common-ancestor root guidance."""

import json

import pytest

from pytest_telescope import controller, ordinary, sandbox
from pytest_telescope.model import DeclarationError, load
from pytest_telescope.plan import make_plan


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def block_sessions(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("declaration validation must refuse before any session starts")

    monkeypatch.setattr(ordinary, "session", forbidden)
    monkeypatch.setattr(sandbox, "prepare", forbidden)


@pytest.mark.parametrize(
    "suite_root,test_roots,expected_root,offending",
    [
        ("subproject", ["shared/tests"], "subproject", "shared/tests"),
        (
            "subproject",
            ["shared/tests/test_example.py"],
            "subproject",
            "shared/tests/test_example.py",
        ),
        ("sub", ["sub2/tests"], "sub", "sub2/tests"),
        ("sub", ["sub_extra.py"], "sub", "sub_extra.py"),
        ("subproject", ["."], "subproject", "."),
        ("subproject", ["test_top.py"], "subproject", "test_top.py"),
        ("packages/core", ["packages"], "packages/core", "packages"),
        ("packages/core", ["packages/test_parent.py"], "packages/core", "packages/test_parent.py"),
        (
            "subproject",
            ["subproject/tests", "shared/tests"],
            "subproject",
            "shared/tests",
        ),
        ("./sub/", ["./sub2/tests/"], "sub", "sub2/tests"),
    ],
)
def test_outside_suite_root_test_roots_refuse_in_api_and_cli(
    tmp_path, monkeypatch, capsys, suite_root, test_roots, expected_root, offending
):
    for directory in (
        "subproject/tests",
        "shared/tests",
        "sub/tests",
        "sub2/tests",
        "packages/core",
    ):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for filename in (
        "subproject/tests/test_inside.py",
        "shared/tests/test_example.py",
        "sub/tests/test_sub.py",
        "sub2/tests/test_sub2.py",
        "sub_extra.py",
        "test_top.py",
        "packages/test_parent.py",
    ):
        put(tmp_path, filename, 'raise AssertionError("must not import")\n')
    put(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.example]\n"
        f"root = {json.dumps(suite_root)}\n"
        'config = "pyproject.toml"\n'
        f"test_roots = {json.dumps(test_roots)}\n",
    )
    block_sessions(monkeypatch)

    for call in (load, make_plan, controller.run):
        with pytest.raises(DeclarationError) as caught:
            call(tmp_path)
        message = str(caught.value)
        assert "example" in message
        assert expected_root in message
        assert offending in message
        assert caught.value.accounting is None

    monkeypatch.chdir(tmp_path)
    for command in (["plan"], ["run"]):
        assert controller.main(command) == 4
        output = json.loads(capsys.readouterr().out)
        assert output["schema"] == 4
        assert output["kind"] == "refusal"
        assert output["status"] == "declaration_error"
        assert output["inventory_complete"] is False
        assert output["inventory_digest"] is None
        assert "capability" not in output
        assert "example" in output["error"]
        assert expected_root in output["error"]
        assert offending in output["error"]


@pytest.mark.parametrize(
    "suite_root,test_roots",
    [
        ("subproject", ["subproject/../shared/tests"]),
        ("subproject", ["subproject/.."]),
        (".", ["../outside"]),
    ],
)
def test_traversal_in_suite_test_roots_refuses_with_declaration_error(
    tmp_path, monkeypatch, capsys, suite_root, test_roots
):
    project = tmp_path / "project"
    (project / "subproject/tests").mkdir(parents=True)
    (project / "shared/tests").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    put(
        project,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.example]\n"
        f"root = {json.dumps(suite_root)}\n"
        'config = "pyproject.toml"\n'
        f"test_roots = {json.dumps(test_roots)}\n",
    )
    block_sessions(monkeypatch)

    for call in (load, make_plan, controller.run):
        with pytest.raises(DeclarationError, match="without traversal"):
            call(project)

    monkeypatch.chdir(project)
    for command in (["plan"], ["run"]):
        assert controller.main(command) == 4
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "declaration_error"
        assert "without traversal" in output["error"]


@pytest.mark.parametrize(
    "entry",
    [
        "subproject/linked",
        "subproject/linked/tests",
        "subproject/linked/tests/test_example.py",
    ],
)
def test_symlink_and_ancestor_symlink_test_roots_refuse_with_declaration_error(
    tmp_path, monkeypatch, capsys, entry
):
    (tmp_path / "subproject").mkdir()
    (tmp_path / "shared/tests").mkdir(parents=True)
    put(tmp_path, "shared/tests/test_example.py", "def test_example():\n    pass\n")
    (tmp_path / "subproject/linked").symlink_to("../shared", target_is_directory=True)
    put(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.example]\n"
        'root = "subproject"\n'
        'config = "pyproject.toml"\n'
        f"test_roots = [{json.dumps(entry)}]\n",
    )
    block_sessions(monkeypatch)

    for call in (load, make_plan, controller.run):
        with pytest.raises(DeclarationError):
            call(tmp_path)

    monkeypatch.chdir(tmp_path)
    for command in (["plan"], ["run"]):
        assert controller.main(command) == 4
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "declaration_error", output


@pytest.mark.parametrize(
    "suite_root,test_roots,expected_files",
    [
        ("subproject", None, ["subproject/test_direct.py", "subproject/tests/test_nested.py"]),
        (
            "subproject",
            ["subproject"],
            ["subproject/test_direct.py", "subproject/tests/test_nested.py"],
        ),
        (
            "./subproject/",
            ["./subproject/tests/", "./subproject/test_direct.py"],
            ["subproject/test_direct.py", "subproject/tests/test_nested.py"],
        ),
    ],
)
def test_inside_suite_root_test_roots_are_accepted(
    tmp_path, monkeypatch, suite_root, test_roots, expected_files
):
    put(tmp_path, "subproject/test_direct.py", "def test_direct():\n    pass\n")
    put(tmp_path, "subproject/tests/test_nested.py", "def test_nested():\n    pass\n")
    put(tmp_path, "shared/tests/test_outside.py", "def test_outside():\n    pass\n")
    put(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.example]\n"
        f"root = {json.dumps(suite_root)}\n"
        'config = "pyproject.toml"\n'
        + (f"test_roots = {json.dumps(test_roots)}\n" if test_roots is not None else ""),
    )
    block_sessions(monkeypatch)

    preview = make_plan(tmp_path)
    assert preview["suites"][0]["root"] == "subproject"
    assert preview["suites"][0]["inventory"] == expected_files


def test_common_ancestor_root_with_independent_cwd_runs_and_reports_sibling_test_roots(tmp_path):
    project = tmp_path / "project"
    put(
        project,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.combined]\n"
        'root = "."\n'
        'cwd = "subproject"\n'
        'config = "subproject/pytest.ini"\n'
        'test_roots = ["subproject/tests", "shared/tests"]\n',
    )
    put(project, "subproject/pytest.ini", "[pytest]\n")
    put(project, "subproject/local.txt", "subproject-value\n")
    put(project, "shared/common.txt", "shared-value\n")
    put(
        project,
        "subproject/tests/test_local.py",
        "from pathlib import Path\nimport pytest\n"
        'pytestmark = pytest.mark.telescope(inputs=["subproject/local.txt"])\n'
        "def test_same_name(pytestconfig):\n"
        '    assert Path("local.txt").read_text() == "subproject-value\\n"\n'
        '    assert pytestconfig.rootpath == Path("/project")\n'
        '    assert Path.cwd() == Path("/project/subproject")\n',
    )
    put(
        project,
        "shared/tests/test_shared.py",
        "from pathlib import Path\nimport pytest\n"
        '@pytest.mark.telescope(inputs=["shared/common.txt"])\n'
        "def test_same_name(pytestconfig):\n"
        '    assert (pytestconfig.rootpath / "shared/common.txt").read_text() == "shared-value\\n"\n'
        '    assert Path.cwd() == Path("/project/subproject")\n'
        "def test_ordinary_cwd(pytestconfig):\n"
        "    assert Path.cwd() == pytestconfig.rootpath / 'subproject'\n",
    )

    cold = controller.run(project, workers=1)
    assert cold["status"] == "passed", cold
    assert len({row["case_id"] for row in cold["units"]}) == 3
    assert sorted((row["file"], row["route"], row["outcome"]) for row in cold["units"]) == [
        ("shared/tests/test_shared.py", "ordinary", "executed"),
        ("shared/tests/test_shared.py", "scoped", "executed"),
        ("subproject/tests/test_local.py", "scoped", "executed"),
    ]

    warm = controller.run(project, workers=1)
    assert warm["status"] == "passed", warm
    assert sorted((row["file"], row["route"], row["outcome"]) for row in warm["units"]) == [
        ("shared/tests/test_shared.py", "ordinary", "executed"),
        ("shared/tests/test_shared.py", "scoped", "reused"),
        ("subproject/tests/test_local.py", "scoped", "reused"),
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_common_ancestor_root_names_sibling_files_on_collection_failure_and_skip(tmp_path, route):
    project = tmp_path / "project"
    put(
        project,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.combined]\n"
        'root = "."\n'
        'cwd = "subproject"\n'
        'config = "subproject/pytest.ini"\n'
        'test_roots = ["subproject/tests", "shared/tests"]\n',
    )
    put(project, "subproject/pytest.ini", "[pytest]\n")
    prefix = (
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"
        if route == "scoped"
        else "import pytest\n"
    )
    put(project, "subproject/tests/test_ok.py", prefix + "def test_ok():\n    pass\n")
    put(
        project,
        "shared/tests/test_skipped.py",
        prefix + 'pytest.skip("sibling skip", allow_module_level=True)\n',
    )

    skipped_report = controller.run(project, workers=1)
    assert skipped_report["status"] == "passed", skipped_report
    assert [(row["file"], row["route"]) for row in skipped_report["collection"]["skips"]] == [
        ("shared/tests/test_skipped.py", route)
    ]

    put(
        project,
        "shared/tests/test_broken.py",
        prefix + "import missing_sibling_dependency\n",
    )
    failed_report = controller.run(project, workers=1)
    assert failed_report["status"] == "failed", failed_report
    assert [row["file"] for row in failed_report["collection"]["failures"]] == [
        "shared/tests/test_broken.py"
    ]
