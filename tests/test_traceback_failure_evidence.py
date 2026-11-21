"""Failure evidence reports `<runtime>` when the traceback name is not a project file.

Ordinary and scoped (real Bubblewrap) controls verify generated code (`<string>`),
relative code filenames (`compile` and relative-`co_filename` imports in suites rooted
at `subproject`), outside-project frames, and legitimate relative `sys.path` project
imports.
"""

import zipfile

import pytest

from pytest_telescope import controller


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def put_zip(root, name, entries):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in entries.items():
            archive.writestr(member, content)


def scope_header(route, *inputs):
    if route != "scoped":
        return ""
    return f"import pytest\npytestmark = pytest.mark.telescope(inputs={list(inputs)!r})\n"


def failure_rows(report, route):
    assert report["status"] == "failed", report
    if route == "scoped":
        assert report["sandbox"]["identity"] is not None
    assert {group["route"] for group in report["groups"]} == {route}
    group_rows = [row for group in report["groups"] for row in group["failures"]]
    unit_rows = [row for unit in report["units"] for row in unit["failures"]]
    assert unit_rows == group_rows
    return group_rows


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
@pytest.mark.parametrize(
    ("statement", "expected_type"),
    [
        ("exec(\"raise LookupError('boom')\")", "LookupError"),
        ('eval("1 / 0")', "ZeroDivisionError"),
    ],
)
def test_generated_code_failure_reports_runtime(tmp_path, route, statement, expected_type):
    project = tmp_path / "project"
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(project, "data.txt", "ok\n")
    # Even if a project file literally named `<string>` exists in cwd, generated code
    # must still report `<runtime>`.
    put(project, "<string>", "# decoy\n")
    put(
        project,
        "tests/test_generated.py",
        scope_header(route, "data.txt", "<string>") + f"def test_generated():\n    {statement}\n",
    )

    report = controller.run(project, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 1, "call", expected_type)
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_generated_fixture_setup_failure_reports_runtime(tmp_path, route):
    project = tmp_path / "project"
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(project, "data.txt", "ok\n")
    put(
        project,
        "tests/test_fixture_exec.py",
        "import pytest\n"
        + ('pytestmark = pytest.mark.telescope(inputs=["data.txt"])\n' if route == "scoped" else "")
        + "@pytest.fixture\n"
        + "def broken():\n"
        + "    exec(\"raise ValueError('boom')\")\n"
        + "def test_uses_broken(broken):\n"
        + "    pass\n",
    )

    report = controller.run(project, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 1, "setup", "ValueError")
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_compile_with_relative_filename_reports_runtime(tmp_path, route):
    project = tmp_path / "project"
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(project, "helpers/tool.py", "def ok():\n    return 1\n")
    put(
        project,
        "tests/test_compiled.py",
        scope_header(route, "helpers/tool.py")
        + "def test_compiled():\n"
        + '    code = compile("\\nraise KeyError(\'boom\')\\n", "helpers/tool.py", "exec")\n'
        + "    exec(code)\n",
    )

    report = controller.run(project, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 2, "call", "KeyError")
    ]


@pytest.fixture(params=["suite-files-only", "same-name-at-project-root"])
def subproject(request, tmp_path):
    root = tmp_path / "project"
    put(
        root,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.subproject]\n"
        'root = "subproject"\nconfig = "subproject/pytest.ini"\n'
        'test_roots = ["subproject/tests"]\n',
    )
    put(root, "subproject/pytest.ini", "[pytest]\n")
    put(root, "subproject/data.txt", "red\n")
    put(root, "subproject/helpers/tool.py", "def boom():\n    raise LookupError('boom')\n")
    put_zip(
        root,
        "subproject/helpers.zip",
        {"ztool.py": "def boom():\n    raise ValueError('zip-boom')\n"},
    )
    if request.param == "same-name-at-project-root":
        put(root, "helpers/tool.py", "def unrelated():\n    pass\n")
        put_zip(root, "helpers.zip", {"ztool.py": "def unrelated():\n    pass\n"})
    return root


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_relative_sys_path_directory_import_in_subproject_names_project_file(subproject, route):
    put(
        subproject,
        "subproject/tests/test_dir_import.py",
        scope_header(route, "subproject/data.txt", "subproject/helpers/tool.py")
        + "import sys\n"
        + 'sys.path.insert(0, "helpers")\n'
        + "import tool\n"
        + "def test_helper_failure():\n"
        + "    tool.boom()\n",
    )

    report = controller.run(subproject, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("subproject/helpers/tool.py", 2, "call", "LookupError")
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_relative_sys_path_zipimport_in_subproject_reports_runtime(subproject, route):
    put(
        subproject,
        "subproject/tests/test_zip_import.py",
        scope_header(route, "subproject/data.txt", "subproject/helpers.zip")
        + "import sys\n"
        + 'sys.path.insert(0, "helpers.zip")\n'
        + "import ztool\n"
        + "def test_zip_helper_failure():\n"
        + "    ztool.boom()\n",
    )

    report = controller.run(subproject, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 2, "call", "ValueError")
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
@pytest.mark.parametrize("chdir_before_call", [False, True])
def test_relative_co_filename_import_in_subproject_reports_runtime(
    subproject, route, chdir_before_call
):
    put(
        subproject,
        "subproject/tests/test_rel_co_filename.py",
        scope_header(route, "subproject/data.txt", "subproject/helpers/tool.py")
        + "import importlib.machinery\n"
        + "import importlib.util\n"
        + "import sys\n"
        + "\n"
        + "class RelativeFinder:\n"
        + "    def find_spec(self, fullname, path, target=None):\n"
        + '        if fullname == "rel_tool":\n'
        + "            return importlib.util.spec_from_loader(\n"
        + '                fullname, importlib.machinery.SourceFileLoader(fullname, "helpers/tool.py")\n'
        + "            )\n"
        + "        return None\n"
        + "\n"
        + "def rel_hook(entry):\n"
        + '    if entry == "rel-helpers":\n'
        + "        return RelativeFinder()\n"
        + "    raise ImportError\n"
        + "\n"
        + "def test_relative_loader(monkeypatch, tmp_path):\n"
        + "    monkeypatch.setattr(sys, 'path_hooks', [rel_hook, *sys.path_hooks])\n"
        + "    monkeypatch.setattr(sys, 'path', ['rel-helpers', *sys.path])\n"
        + "    sys.path_importer_cache.pop('rel-helpers', None)\n"
        + "    monkeypatch.delitem(sys.modules, 'rel_tool', raising=False)\n"
        + "    import rel_tool\n"
        + ("    monkeypatch.chdir(tmp_path)\n" if chdir_before_call else "")
        + "    rel_tool.boom()\n",
    )

    report = controller.run(subproject, workers=1)

    rows = failure_rows(report, route)
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 2, "call", "LookupError")
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_stdlib_traceback_reports_runtime(tmp_path, route):
    project = tmp_path / "project"
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(project, "data.txt", "ok\n")
    put(
        project,
        "tests/test_stdlib.py",
        scope_header(route, "data.txt")
        + "import json\n"
        + "def test_stdlib():\n"
        + '    json.loads("{")\n',
    )

    report = controller.run(project, workers=1)

    rows = failure_rows(report, route)
    assert len(rows) == 1
    assert rows[0]["file"] == "<runtime>"
    assert rows[0]["phase"] == "call"
    assert rows[0]["exception_type"] == "JSONDecodeError"
    assert rows[0]["line"] > 0


def test_outside_project_module_traceback_reports_runtime(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    put(external, "outside.py", "def boom():\n    raise RuntimeError('outside')\n")
    put(project, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    put(
        project,
        "tests/test_external.py",
        "import sys\n"
        + f"sys.path.insert(0, {str(external)!r})\n"
        + "import outside\n"
        + "def test_outside():\n"
        + "    outside.boom()\n",
    )

    report = controller.run(project, workers=1)

    rows = failure_rows(report, "ordinary")
    assert [(row["file"], row["line"], row["phase"], row["exception_type"]) for row in rows] == [
        ("<runtime>", 2, "call", "RuntimeError")
    ]
