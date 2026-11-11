"""One static parse budget over test files and declared Python sources, in plan and run.

Accepted projects run real Bubblewrap. Refusal controls replace sandbox preparation with a
spy, so an over-bound project is observed to refuse before any sandbox exists.
"""

import ast

import pytest
from test_helper_registry import no_new_records, put
from test_helper_registry_contract import passed

from pytest_telescope import controller, markers, sandbox
from pytest_telescope.markers import discover
from pytest_telescope.model import DeclarationError
from pytest_telescope.plan import make_plan

HELPER = (
    'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\nVALUE = 42\n'
)


def nodes(source):
    return sum(1 for _ in ast.walk(ast.parse(source)))


def project(tmp_path, *, name="project", inputs=("pkg/__init__.py", "pkg/real.py"), module="real"):
    """A wholly scoped test that imports one declared helper module of package ``pkg``."""
    root = tmp_path / name
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    put(root, "helper-private.txt", "the helper's own input\n")
    put(root, "pkg/__init__.py", "")
    put(root, "pkg/real.py", HELPER)
    put(
        root,
        "tests/test_consumer.py",
        "import pytest\n"
        f"pytestmark = pytest.mark.telescope(inputs={list(inputs)!r})\n"
        f"from pkg.{module} import VALUE\n"
        "def test_value():\n    assert VALUE == 42\n",
    )
    return root


def alias_project(tmp_path):
    """The declared helper is imported through a leaf-link alias of the same file."""
    root = project(
        tmp_path, inputs=("pkg/__init__.py", "pkg/alias.py", "pkg/real.py"), module="alias"
    )
    (root / "pkg/alias.py").symlink_to("real.py")
    return root


def refuses_before_any_sandbox(root, monkeypatch, match):
    """Plan and run refuse with the same declaration error; no sandbox is prepared."""
    prepared = []

    def prepare(project):
        prepared.append(project)
        raise AssertionError("an over-bound project must refuse before sandbox preparation")

    monkeypatch.setattr(sandbox, "prepare", prepare)

    with pytest.raises(DeclarationError, match=match) as planned:
        make_plan(root)
    with pytest.raises(DeclarationError, match=match) as ran:
        controller.run(root, workers=1)

    assert str(ran.value) == str(planned.value)
    assert prepared == []
    assert not (root / ".pytest-telescope").exists()


def test_scoped_package_beyond_the_former_totals_plans_and_runs(tmp_path):
    # Each data module is within the per-file bounds; together they exceed the former
    # 2,000,000-node total that discovery applied when it parsed only test files.
    root = tmp_path / "project"
    line = "VALUE_{i} = (" + ", ".join(["1"] * 60) + ")\n"
    body = "".join(line.format(i=i) for i in range(2950))
    per_file = nodes(body)
    assert len(body.encode()) < markers.MAX_SOURCE
    assert per_file < markers.MAX_NODES
    assert 12 * per_file > 2_000_000
    for index in range(12):
        put(root, f"pkg/data_{index:02}.py", body)
    put(root, "pyproject.toml", '[tool.pytest.ini_options]\nmarkers = ["telescope"]\n')
    put(
        root,
        "tests/test_scoped.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["pkg"])\n'
        "def test_reads_declared_source():\n    assert True\n",
    )

    plan = make_plan(root)
    report = passed(root, executed=1, selected=1)

    assert {f"pkg/data_{index:02}.py" for index in range(12)} <= set(plan["inputs"])
    assert report["cases"]["passed"] == 1
    assert [row["file"] for row in report["units"]] == ["tests/test_scoped.py"]


@pytest.mark.parametrize(
    "total, match",
    [
        ("source", "Python source for static discovery exceeds the .* total budget"),
        ("nodes", "total budget of [0-9,]+ AST nodes"),
        ("markers", "total budget of [0-9,]+ literal telescope marker calls"),
    ],
)
def test_declared_source_over_a_total_refuses_in_plan_and_run_alike(
    tmp_path, monkeypatch, total, match
):
    root = project(tmp_path)
    test = (root / "tests/test_consumer.py").read_text()
    # The test file alone fits each total; adding its declared helper does not.
    if total == "source":
        monkeypatch.setattr(markers, "MAX_TOTAL_SOURCE", len(test) + len(HELPER) - 1)
    elif total == "nodes":
        distinct = nodes(test) + nodes("") + nodes(HELPER)
        monkeypatch.setattr(markers, "MAX_TOTAL_NODES", distinct - 1)
    else:
        monkeypatch.setattr(markers, "MAX_MARKERS", 1)

    refuses_before_any_sandbox(root, monkeypatch, match)


PER_FILE_FAULTS = {
    "syntax": (lambda: "def broken(:\n", "cannot parse"),
    "nodes": (lambda: "VALUES = [" + "a," * (markers.MAX_NODES // 2) + "]\n", "AST nodes per file"),
    "depth": (lambda: "DEEP = " + "[" * 130 + "]" * 130 + "\n", "depth limit"),
    "size": (lambda: "#" + "x" * markers.MAX_SOURCE + "\n", "per-file static discovery limit"),
}


@pytest.mark.parametrize("fault", PER_FILE_FAULTS)
def test_declared_non_test_source_per_file_faults_refuse_in_plan_and_run_alike(
    tmp_path, monkeypatch, fault
):
    # The consumer never imports the broken module; declaring it is enough.
    root = project(tmp_path, inputs=("pkg/__init__.py", "pkg/broken.py", "pkg/real.py"))
    source, match = PER_FILE_FAULTS[fault]
    put(root, "pkg/broken.py", source())

    refuses_before_any_sandbox(root, monkeypatch, match + ".*: pkg/broken.py$")


def test_alias_records_are_labeled_per_import_origin(tmp_path, monkeypatch):
    root = alias_project(tmp_path)
    execute = sandbox.execute
    requests = []

    def recording(*args, request, **options):
        requests.append(request)
        return execute(*args, request=request, **options)

    monkeypatch.setattr(sandbox, "execute", recording)

    passed(root, executed=1, selected=1)

    test = (root / "tests/test_consumer.py").read_bytes()
    expected = [
        *discover(HELPER.encode(), "pkg/alias.py")[0],
        *discover(HELPER.encode(), "pkg/real.py")[0],
        *discover(test, "tests/test_consumer.py")[0],
    ]
    assert [row["file"] for row in expected] == [
        "pkg/alias.py",
        "pkg/real.py",
        "tests/test_consumer.py",
    ]
    assert len({row["id"] for row in expected}) == 3
    assert [request["mode"] for request in requests] == ["collection", "execution"]
    assert all(request["registry"] == expected for request in requests)


@pytest.mark.parametrize("total", ["source", "nodes"])
def test_alias_counts_its_file_source_and_nodes_once(tmp_path, monkeypatch, total):
    root = alias_project(tmp_path)
    test = (root / "tests/test_consumer.py").read_text()
    # Between one and two copies of the aliased file: it is parsed once, not per origin.
    if total == "source":
        monkeypatch.setattr(markers, "MAX_TOTAL_SOURCE", len(test) + 2 * len(HELPER) - 1)
    else:
        distinct = nodes(test) + nodes("") + nodes(HELPER)
        monkeypatch.setattr(markers, "MAX_TOTAL_NODES", distinct + nodes(HELPER) - 1)

    make_plan(root)
    passed(root, executed=1, selected=1)


def test_alias_counts_its_literal_markers_per_import_origin(tmp_path, monkeypatch):
    # One test marker and one helper marker fit; the alias adds its own helper record.
    monkeypatch.setattr(markers, "MAX_MARKERS", 2)
    make_plan(project(tmp_path, name="without-alias"))
    root = alias_project(tmp_path)

    refuses_before_any_sandbox(
        root, monkeypatch, "total budget of 2 literal telescope marker calls"
    )


def test_declared_source_changed_before_freezing_refuses(tmp_path, monkeypatch):
    root = project(tmp_path)
    snapshot = controller.snapshot

    def changed_snapshot(manifest, unit, destination):
        put(root, "pkg/real.py", HELPER + "CHANGED = True\n")
        return snapshot(manifest, unit, destination)

    monkeypatch.setattr(controller, "snapshot", changed_snapshot)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    assert "changed while freezing" in report["collection"]["reason"]
    assert report["counts"]["executed"] == report["counts"]["reused"] == 0
    no_new_records(root)
