"""Real scoped helper behavior: placement, membership, inherited grants and reuse."""

import hashlib
import json

import pytest
from test_helper_registry import consumer, no_new_records, project_at, put

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError


def passed(root, *, executed=None, reused=None, selected=None, **options):
    report = controller.run(root, workers=1, explain=True, **options)
    assert report["status"] == "passed", report
    assert report["inventory_complete"] is True
    assert report["grouping_complete"] is True
    assert report["counts"]["failed"] == report["counts"]["infrastructure_error"] == 0
    assert all(row["route"] == "scoped" for row in report["units"])
    for name, expected in (("executed", executed), ("reused", reused), ("selected", selected)):
        if expected is not None:
            assert report["counts"][name] == expected, report
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    return report


PLACEMENTS = {
    "module": 'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n',
    "class_scope": """
class Helper:
    pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])
""",
    "class_decorator": """
@pytest.mark.telescope(inputs=["helper-private.txt"])
class Helper:
    pass
""",
    "function": """
@pytest.mark.telescope(inputs=["helper-private.txt"])
def helper_operation():
    return 42
""",
    "parameter": """
@pytest.mark.parametrize("value", [
    pytest.param(42, marks=pytest.mark.telescope(inputs=["helper-private.txt"])),
])
def helper_operation(value):
    return value
""",
}


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_literal_helper_marker_placements_do_not_select_helper_cases(tmp_path, placement):
    root = project_at(tmp_path)
    put(
        root,
        "helpers/support.py",
        "from pathlib import Path\nimport pytest\n"
        + PLACEMENTS[placement]
        + '\ndef answer(): return Path("data/answer.txt").read_text().strip()\n'
        + 'def test_not_selected(): raise AssertionError("helper is not a test target")\n',
    )

    report = passed(root, executed=1, selected=1)

    assert [row["file"] for row in report["units"]] == ["tests/test_consumer.py"]
    assert report["collection"]["collected"] == 1


def test_marked_package_initializer_and_nested_helpers_are_explicit_inputs(tmp_path):
    root = project_at(tmp_path)
    put(
        root,
        "helpers/__init__.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'INITIAL = "ready"\n',
    )
    put(root, "helpers/nested/__init__.py", "from .value import VALUE\n")
    put(
        root,
        "helpers/nested/value.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'VALUE = "42"\n',
    )
    put(
        root,
        "helpers/support.py",
        'from . import INITIAL\nfrom .nested import VALUE\nimport pytest\n'
        'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'assert INITIAL == "ready"\ndef answer(): return VALUE\n',
    )
    consumer(
        root,
        inputs=[
            "helpers/__init__.py",
            "helpers/support.py",
            "helpers/nested/__init__.py",
            "helpers/nested/value.py",
        ],
    )

    passed(root, executed=1, selected=1)


@pytest.mark.parametrize("missing", ["initializer", "nested"])
def test_missing_required_helper_import_dependency_stays_absent(tmp_path, missing):
    root = project_at(tmp_path)
    put(root, "helpers/__init__.py", 'INITIAL = "ready"\n')
    put(root, "helpers/nested.py", 'VALUE = "42"\n')
    put(
        root,
        "helpers/support.py",
        'from . import INITIAL\nfrom .nested import VALUE\nimport pytest\n'
        'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'assert INITIAL == "ready"\ndef answer(): return VALUE\n',
    )
    inputs = ["helpers/support.py"]
    inputs.append("helpers/nested.py" if missing == "initializer" else "helpers/__init__.py")
    consumer(root, inputs=inputs)

    report = controller.run(root, workers=1)

    assert report["status"] == "failed", report
    assert report["sandbox"]["identity"] is not None
    assert report["collection"]["failures"]
    assert all(row["exception_type"] == "CollectionError" for row in report["collection"]["failures"])
    no_new_records(root)


def test_distinct_same_basename_helper_origins_keep_distinct_values(tmp_path):
    root = project_at(tmp_path)
    inputs = []
    for name, value in (("left", "41"), ("right", "43")):
        put(root, f"{name}/__init__.py", "")
        put(
            root,
            f"{name}/support.py",
            'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
            f'def answer(): return "{value}"\n',
        )
        inputs.extend([f"{name}/__init__.py", f"{name}/support.py"])
    put(
        root,
        "tests/test_consumer.py",
        f"import pytest\nfrom pathlib import Path\npytestmark = pytest.mark.telescope(inputs={inputs!r})\n"
        "from left.support import answer as left\nfrom right.support import answer as right\n"
        'def test_both():\n    assert (left(), right()) == ("41", "43")\n'
        '    assert not Path("helper-private.txt").exists()\n',
    )

    passed(root, executed=1, selected=1)


@pytest.mark.parametrize("boundary", ["suite_exclude", "structural_files"])
def test_structurally_omitted_helper_is_readable_without_collecting_its_tests(tmp_path, boundary):
    root = project_at(tmp_path)
    if boundary == "suite_exclude":
        put(
            root,
            "pyproject.toml",
            '[tool.pytest.ini_options]\npythonpath = ["."]\n'
            '[tool.pytest_telescope.suites.default]\nroot = "."\nconfig = "pyproject.toml"\n'
            'test_roots = ["tests"]\nexclude = ["tests/test_support.py"]\n',
        )
    put(root, "tests/__init__.py", "")
    put(
        root,
        "tests/test_support.py",
        'import pytest\npytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'def answer(): return "42"\n'
        'def test_not_selected(): raise AssertionError("helper must not be collected")\n',
    )
    put(
        root,
        "tests/test_consumer.py",
        'import pytest\nfrom pathlib import Path\n'
        'pytestmark = pytest.mark.telescope(inputs=["tests/__init__.py", "tests/test_support.py"])\n'
        'from tests.test_support import answer\ndef test_answer():\n'
        '    assert answer() == "42"\n    assert not Path("helper-private.txt").exists()\n',
    )
    options = (
        {"structural_files": {"default": ["tests/test_consumer.py"]}}
        if boundary == "structural_files"
        else {}
    )

    report = passed(root, executed=1, selected=1, **options)

    assert [row["file"] for row in report["units"]] == ["tests/test_consumer.py"]
    assert report["counts"]["not-selected"] == 0
    assert report["collection"]["suites"][0]["files"] == ["tests/test_consumer.py"]


def test_changed_helper_invalidates_only_its_consumers_and_not_helper_cache_policy(tmp_path):
    root = project_at(tmp_path)
    (root / "tests/test_consumer.py").unlink()
    for name, value in (("left", "41"), ("right", "43")):
        put(
            root,
            f"helpers/{name}.py",
            "import pytest\n"
            'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"], cache=False)\n'
            f'def answer(): return "{value}"\n',
        )
        put(
            root,
            f"tests/test_{name}.py",
            'import pytest\nfrom pathlib import Path\n'
            f'pytestmark = pytest.mark.telescope(inputs=["helpers/__init__.py", "helpers/{name}.py"])\n'
            f"from helpers.{name} import answer\ndef test_answer():\n"
            f'    assert answer() == "{value}"\n    assert not Path("helper-private.txt").exists()\n',
        )

    cold = passed(root, executed=2)
    warm = passed(root, reused=2, executed=0)
    assert cold["collection"]["outcome"] == "executed"
    assert warm["collection"]["outcome"] == "reused"
    helper = root / "helpers/left.py"
    helper.write_text(helper.read_text() + "\n# a different frozen helper identity\n")
    changed = passed(root, executed=1, reused=1)

    assert changed["collection"]["outcome"] == "executed"
    assert {row["file"]: row["outcome"] for row in changed["units"]} == {
        "tests/test_left.py": "executed",
        "tests/test_right.py": "reused",
    }
    passed(root, executed=0, reused=2)


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_helper_scopes_keep_batch_lifecycle_order_selection_and_reuse_precise(tmp_path, mode):
    root = project_at(tmp_path)
    put(root, "extra.txt", "extra\n")
    put(
        root,
        "tests/test_consumer.py",
        """
import pytest
from pathlib import Path
pytestmark = pytest.mark.telescope(inputs=["helpers/__init__.py", "helpers/support.py", "data/answer.txt"])
from helpers.support import answer

@pytest.fixture(scope="session")
def state(tmp_path_factory, request):
    path = tmp_path_factory.mktemp("shared") / "seen"
    path.write_text("")
    return path, [item.name for item in request.session.items]

def check(state, name, *, extra):
    path, expected = state
    assert path.read_text().splitlines() == expected[:expected.index(name)]
    path.write_text(path.read_text() + name + "\\n")
    assert answer() == "42"
    assert Path("extra.txt").exists() is extra
    assert not Path("helper-private.txt").exists()

def test_first(state):
    check(state, "test_first", extra=False)

@pytest.mark.telescope(inputs=[])
def test_second(state):
    check(state, "test_second", extra=False)

@pytest.mark.telescope(inputs=["extra.txt"])
def test_third(state):
    check(state, "test_third", extra=True)
""",
    )
    options = {"execution_mode": mode}
    cold = passed(root, executed=3, selected=3, **options)
    identities = {}
    for name in ("test_first", "test_second", "test_third"):
        encoded = json.dumps(["default", f"tests/test_consumer.py::{name}"], separators=(",", ":"))
        identities[name] = hashlib.sha256(encoded.encode()).hexdigest()
    expected_groups = (
        [[identities["test_first"], identities["test_second"]], [identities["test_third"]]]
        if mode == "batch"
        else [[identities[name]] for name in identities]
    )
    assert [row["case_ids"] for row in cold["groups"]] == expected_groups
    passed(root, executed=0, reused=3, **options)
    selected = passed(
        root, executed=2, selected=2, arguments=["-k", "test_second or test_third"], **options
    )
    assert selected["counts"]["not-selected"] == 1
    assert [row["case_ids"] for row in selected["groups"]] == [
        [identities["test_second"]],
        [identities["test_third"]],
    ]
    put(root, "extra.txt", "changed extra\n")
    changed = passed(root, executed=1, reused=2, **options)
    assert {row["case_id"]: row["outcome"] for row in changed["units"]} == {
        identities["test_first"]: "reused",
        identities["test_second"]: "reused",
        identities["test_third"]: "executed",
    }


@pytest.mark.parametrize("origin", ["missing", "none", "integer", "empty", "outside", "foreign"])
def test_helper_current_module_origin_must_authenticate_its_own_source(tmp_path, origin):
    root = project_at(tmp_path)
    outside = root.parent / "outside.py"
    outside.write_text("# owned source outside the project view\n")
    put(root, "decoys/support.py", "# readable, but has no Telescope declaration\n")
    assignment = {
        "missing": "del __file__",
        "none": "__file__ = None",
        "integer": "__file__ = 42",
        "empty": "__file__ = ''",
        "outside": f"__file__ = {str(outside)!r}",
        "foreign": "__file__ = 'decoys/support.py'",
    }[origin]
    put(
        root,
        "helpers/support.py",
        "import pytest\n" + assignment + "\n"
        'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'def answer(): return "42"\n',
    )
    consumer(root, inputs=["helpers/__init__.py", "helpers/support.py", "decoys/support.py"])

    with pytest.raises(DeclarationError, match="unsupported or changed declarations"):
        controller.run(root, workers=1)

    no_new_records(root)


@pytest.mark.parametrize("level", ["module", "parameter"])
def test_authenticated_helper_marker_cannot_be_inherited_by_a_foreign_case(tmp_path, level):
    root = project_at(tmp_path)
    prefix = (
        'import pytest\nfrom helpers.support import pytestmark as foreign\n'
        'pytestmark = [pytest.mark.telescope(inputs=["helpers/__init__.py", "helpers/support.py"]), foreign]\n'
        if level == "module"
        else 'import pytest\nfrom helpers.support import pytestmark as foreign\n'
        'pytestmark = pytest.mark.telescope(inputs=["helpers/__init__.py", "helpers/support.py"])\n'
        '@pytest.mark.parametrize("value", [pytest.param(42, marks=foreign)])\n'
    )
    function = "(): pass\n" if level == "module" else "(value): assert value == 42\n"
    put(root, "tests/test_consumer.py", prefix + "def test_case" + function)

    with pytest.raises(DeclarationError, match="unsupported or changed declarations"):
        controller.run(root, workers=1)

    no_new_records(root)


def test_consumer_inherited_marker_list_mutation_cannot_add_a_helper_grant(tmp_path):
    root = project_at(tmp_path)
    consumer(root, extra='pytestmark.kwargs["inputs"].append("helper-private.txt")')

    with pytest.raises(DeclarationError, match="unsupported or changed declarations"):
        controller.run(root, workers=1)

    no_new_records(root)
