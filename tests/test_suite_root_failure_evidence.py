"""Failure evidence names project files relative to the project, whatever the suite root.

Every project here has one suite rooted at `subproject`, so pytest node IDs start at
`tests/...` while the project-relative name starts at `subproject/tests/...`. Scoped
controls use real Bubblewrap.
"""

import pytest

from pytest_telescope import controller

BROKEN_IMPORT = "import module_that_does_not_exist\n"
SCOPED = 'import pytest\npytestmark = pytest.mark.telescope(inputs=["subproject/data.txt"])\n'


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture(params=["suite-files-only", "same-name-at-project-root"])
def project(request, tmp_path):
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
    if request.param == "same-name-at-project-root":
        # Unrelated files that the suite-relative names would wrongly point at.
        for name in ("test_example.py", "test_other.py", "helper.py", "conftest.py"):
            put(root, f"tests/{name}", "def test_unrelated():\n    pass\n")
    return root


def collection_files(report):
    assert report["collection"]["failures"], report
    assert all(
        row["phase"] == "collect" and row["exception_type"] == "CollectionError"
        for row in report["collection"]["failures"]
    )
    return sorted(row["file"] for row in report["collection"]["failures"])


def test_ordinary_collection_failure_names_the_project_relative_file(project):
    put(project, "subproject/tests/test_other.py", BROKEN_IMPORT + "def test_other():\n    pass\n")

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert [row["route"] for row in report["collection"]["suites"]] == ["ordinary"]
    assert collection_files(report) == ["subproject/tests/test_other.py"]
    assert report["collection"]["suites"][0]["files"] == ["subproject/tests/test_other.py"]


def test_scoped_collection_failure_names_the_project_relative_file(project):
    put(
        project,
        "subproject/tests/test_example.py",
        SCOPED + BROKEN_IMPORT + "def test_example():\n    pass\n",
    )

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert report["sandbox"]["identity"] is not None
    assert [row["route"] for row in report["collection"]["suites"]] == ["scoped"]
    assert collection_files(report) == ["subproject/tests/test_example.py"]
    assert report["collection"]["suites"][0]["files"] == ["subproject/tests/test_example.py"]


def test_ordinary_session_with_a_scoped_case_names_each_project_relative_file(project):
    # One case declares a scope, so its module is collected by the suite's ordinary session.
    put(
        project,
        "subproject/tests/test_example.py",
        "import pytest\n"
        + BROKEN_IMPORT
        + '@pytest.mark.telescope(inputs=["subproject/data.txt"])\n'
        "def test_example():\n    pass\n",
    )
    put(project, "subproject/tests/test_other.py", BROKEN_IMPORT + "def test_other():\n    pass\n")

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert [row["route"] for row in report["collection"]["suites"]] == ["ordinary"]
    assert collection_files(report) == [
        "subproject/tests/test_example.py",
        "subproject/tests/test_other.py",
    ]


def test_execution_side_collection_failure_names_the_project_relative_file(project):
    # The module imports during the collection session and fails in the execution session.
    put(
        project,
        "subproject/tests/conftest.py",
        "import os\n"
        "def pytest_configure(config):\n"
        '    os.environ["SUBJECT_COLLECT_ONLY"] = "1" if config.option.collectonly else "0"\n',
    )
    put(
        project,
        "subproject/tests/test_example.py",
        "import os\nimport pytest\n"
        "pytestmark = pytest.mark.telescope("
        'inputs=["subproject/data.txt", "subproject/tests/conftest.py"])\n'
        'if os.environ["SUBJECT_COLLECT_ONLY"] == "0":\n'
        "    import module_that_does_not_exist\n"
        "def test_example():\n    pass\n",
    )

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert report["collection"]["outcome"] == "executed"
    assert report["collection"]["failures"] == []
    (group,) = report["groups"]
    assert group["route"] == "scoped" and group["stop_reason"] == "collection"
    assert group["failures"], report
    assert [(row["file"], row["phase"], row["exception_type"]) for row in group["failures"]] == [
        ("subproject/tests/test_example.py", "collect", "CollectionError")
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_phase_failure_names_the_project_relative_file(project, route):
    # A control: a failure with a traceback takes its file from the traceback's own path.
    put(project, "subproject/tests/helper.py", "def check():\n    raise LookupError\n")
    put(
        project,
        "subproject/tests/test_example.py",
        (
            "import pytest\npytestmark = pytest.mark.telescope("
            'inputs=["subproject/data.txt", "subproject/tests/helper.py"])\n'
            if route == "scoped"
            else ""
        )
        + "from helper import check\n"
        "def test_direct():\n    assert False\n"
        "def test_through_helper():\n    check()\n",
    )

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert {group["route"] for group in report["groups"]} == {route}
    rows = [row for group in report["groups"] for row in group["failures"]]
    assert sorted((row["file"], row["phase"], row["exception_type"]) for row in rows) == [
        ("subproject/tests/helper.py", "call", "LookupError"),
        ("subproject/tests/test_example.py", "call", "AssertionError"),
    ]


@pytest.mark.parametrize("route", ["ordinary", "scoped"])
def test_failure_decided_by_another_plugin_names_the_project_relative_file(project, route):
    # A project hook fails a passing report after every other hook has seen it pass.
    put(
        project,
        "subproject/tests/conftest.py",
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n"
        "    report = outcome.get_result()\n"
        '    if report.when == "call":\n'
        '        report.outcome = "failed"\n',
    )
    put(
        project,
        "subproject/tests/test_example.py",
        (
            "import pytest\npytestmark = pytest.mark.telescope("
            'inputs=["subproject/data.txt", "subproject/tests/conftest.py"])\n'
            if route == "scoped"
            else ""
        )
        + "def test_example():\n    pass\n",
    )

    report = controller.run(project, workers=1)

    assert report["status"] == "failed", report
    assert {group["route"] for group in report["groups"]} == {route}
    rows = [row for group in report["groups"] for row in group["failures"]]
    assert [(row["file"], row["phase"], row["exception_type"]) for row in rows] == [
        ("subproject/tests/test_example.py", "call", "UnexpectedPass")
    ]
