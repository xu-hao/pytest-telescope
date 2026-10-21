"""Public CLI behavior of real per-case marker collection and Bubblewrap execution.

Scoped cases launch bwrap; these controls are never skipped.
"""

import json
import os
import subprocess
import sys

import pytest


class Project:
    def __init__(self, root):
        self.root = root
        root.mkdir()

    def write(self, name, content):
        destination = self.root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    def cli(self, command, *arguments, expected=0):
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "pytest_telescope", command, *arguments],
            cwd=self.root,
            env={**os.environ, "PYTEST_ADDOPTS": ""},
            text=True,
            capture_output=True,
            timeout=120,
        )
        assert completed.returncode == expected, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    def run(self, *arguments, expected=0):
        return self.cli("run", *arguments, expected=expected)

    def prepare(self, files, configuration=""):
        self.write(
            "pyproject.toml",
            '[tool.pytest.ini_options]\npythonpath = ["."]\n'
            '[tool.pytest_telescope]\ntest_roots = ["tests"]\n' + configuration,
        )
        for name, content in files.items():
            self.write(name, content)
        return self


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path / "project")


def assert_outcomes(summary, *, executed=0, reused=0, failed=0, unselected=0):
    assert summary["counts"]["selected"] == executed + reused + failed
    assert summary["counts"]["unsupported"] == 0
    assert {
        key: value
        for key, value in summary["counts"].items()
        if key not in {"selected", "unsupported"}
    } == {
        "executed": executed,
        "reused": reused,
        "failed": failed,
        "not-selected": unselected,
        "infrastructure_error": 0,
    }
    assert len(summary["units"]) == executed + reused + failed + unselected
    assert summary["collection"]["collected"] == executed + reused + failed + unselected


def test_same_file_literal_markers_are_independent_case_cache_units(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(inputs=["a.txt"])
def test_a():
    assert Path('a.txt').read_text() in ('old-a', 'new-a')
    assert not Path('b.txt').exists()
@pytest.mark.telescope(inputs=["b.txt"])
def test_b():
    assert Path('b.txt').read_text() == 'b'
    assert not Path('a.txt').exists()
""",
            "a.txt": "old-a",
            "b.txt": "b",
        }
    )
    cold = project.run()
    assert_outcomes(cold, executed=2)
    assert cold["collection"]["outcome"] == "executed"
    assert [group["attempts"] for group in cold["groups"]] == [1, 1]
    warm = project.run()
    assert_outcomes(warm, reused=2)
    # A warm inventory and result hit starts no sandbox at all.
    assert warm["collection"]["outcome"] == "reused"
    assert warm["collection"]["worker_seconds"] == 0
    assert [group["attempts"] for group in warm["groups"]] == [0, 0]
    project.write("a.txt", "new-a")
    assert_outcomes(project.run(), executed=1, reused=1)


@pytest.mark.parametrize(
    "marker",
    ["", "@pytest.mark.telescope(cache=False)", "@pytest.mark.telescope(absent=['absent.txt'])"],
)
def test_unmarked_and_policy_only_cases_run_ordinary_every_time(project, marker):
    project.prepare(
        {
            "tests/test_subject.py": f"""
import pytest
from pathlib import Path
{marker}
def test_ordinary():
    for name in ('default.txt', '.hidden', 'ignored.txt', 'build/data', 'dist/data', 'node_modules/data'):
        assert Path(name).read_text() in ('first', 'second')
    for name in ('.git', '.venv', 'venv', '.pytest_cache', '__pycache__'):
        assert Path(name).exists()
""",
            "default.txt": "first",
            ".hidden": "first",
            "ignored.txt": "first",
            ".gitignore": "ignored.txt\n",
            "build/data": "first",
            "dist/data": "first",
            "node_modules/data": "first",
            ".git/private": "excluded",
            ".venv/private": "excluded",
            "venv/private": "excluded",
            ".pytest_cache/private": "excluded",
            "__pycache__/private": "excluded",
        },
        '[tool.pytest_telescope.defaults]\ninputs = ["default.txt"]\n',
    )
    for _ in range(2):
        summary = project.run()
        assert_outcomes(summary, executed=1)
        assert summary["units"][0]["route"] == "ordinary"
        assert summary["sandbox"]["required"] is False
    assert not (project.root / ".pytest-telescope").exists()


@pytest.mark.parametrize("field", ["inputs", "bundles"])
def test_explicit_empty_scope_is_scoped_and_adds_defaults(project, field):
    project.prepare(
        {
            "tests/test_subject.py": f"""
import pytest
from pathlib import Path
@pytest.mark.telescope({field}=[])
def test_explicit():
    assert Path('default.txt').read_text() == 'default'
    assert Path('tests/test_subject.py').is_file()
    assert Path('pyproject.toml').is_file()
    assert not Path('unrelated.txt').exists()
""",
            "default.txt": "default",
            "unrelated.txt": "first",
        },
        '[tool.pytest_telescope.defaults]\ninputs = ["default.txt"]\n',
    )
    summary = project.run()
    assert_outcomes(summary, executed=1)
    assert summary["units"][0]["route"] == "scoped"
    project.write("unrelated.txt", "second")
    assert_outcomes(project.run(), reused=1)


def test_module_class_function_parameter_and_bundle_scopes_are_additive(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
pytestmark = pytest.mark.telescope(inputs=['module.txt'], bundles=['shared'])
class TestScopes:
    pytestmark = pytest.mark.telescope(inputs=['class.txt'])
    @pytest.mark.telescope(inputs=['function.txt'])
    @pytest.mark.parametrize('name', [
        pytest.param('a', marks=pytest.mark.telescope(inputs=['a.txt']), id='a'),
        pytest.param('b', marks=pytest.mark.telescope(inputs=['b.txt']), id='b'),
    ])
    def test_case(self, name):
        for path in ('default.txt', 'module.txt', 'class.txt', 'function.txt', 'bundle.txt', 'base.txt'):
            assert Path(path).read_text() in ('first', 'second')
        assert Path(name + '.txt').read_text() in ('first', 'second')
        assert not Path(('b' if name == 'a' else 'a') + '.txt').exists()
        assert not Path('unrelated.txt').exists()
""",
            **{
                name + ".txt": "first"
                for name in (
                    "default",
                    "module",
                    "class",
                    "function",
                    "bundle",
                    "base",
                    "a",
                    "b",
                    "unrelated",
                )
            },
        },
        """[tool.pytest_telescope.defaults]
inputs = ["default.txt"]
[tool.pytest_telescope.bundles.shared]
inputs = ["bundle.txt"]
bundles = ["base"]
[tool.pytest_telescope.bundles.base]
inputs = ["base.txt"]
""",
    )
    assert_outcomes(project.run(), executed=2)
    assert_outcomes(project.run(), reused=2)
    project.write("a.txt", "second")
    assert_outcomes(project.run(), executed=1, reused=1)
    project.write("unrelated.txt", "second")
    assert_outcomes(project.run(), reused=2)
    project.write("module.txt", "second")
    assert_outcomes(project.run(), executed=2)
    project.write("default.txt", "second")
    assert_outcomes(project.run(), executed=2)
    project.write("a.txt", "invalid")
    assert_outcomes(project.run(expected=1), failed=1, reused=1)


def test_inherited_cache_false_cannot_be_reenabled_by_function_marker(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
@pytest.mark.telescope(inputs=['class.txt'], cache=False)
class TestPolicy:
    @pytest.mark.telescope(inputs=['function.txt'], cache=True)
    def test_policy(self):
        assert Path('class.txt').read_text() == 'class'
        assert Path('function.txt').read_text() == 'function'
        assert not Path('other.txt').exists()
""",
            "class.txt": "class",
            "function.txt": "function",
            "other.txt": "unrelated",
        }
    )
    assert_outcomes(project.run(), executed=1)
    assert_outcomes(project.run(), executed=1)


def test_new_files_and_new_cases_refresh_inventory_without_invalidating_other_files(project):
    source = """import pytest
@pytest.mark.telescope(inputs=[])
def test_original(): pass
"""
    project.prepare({"tests/test_original.py": source})
    assert_outcomes(project.run(), executed=1)
    assert_outcomes(project.run(), reused=1)
    project.write(
        "tests/test_added.py",
        """import pytest
@pytest.mark.telescope(inputs=[])
def test_added(): pass
""",
    )
    assert_outcomes(project.run(), executed=1, reused=1)
    project.write(
        "tests/test_original.py",
        source
        + """
@pytest.mark.telescope(inputs=[])
def test_new_case(): pass
""",
    )
    assert_outcomes(project.run(), executed=2, reused=1)
    (project.root / "tests/test_added.py").unlink()
    assert_outcomes(project.run(), reused=2)


def test_real_dynamic_parametrization_refreshes_inventory_and_accounts_for_selection(project):
    project.prepare(
        {
            "tests/conftest.py": """
import json
from pathlib import Path
def pytest_generate_tests(metafunc):
    if 'value' in metafunc.fixturenames:
        values = json.loads(Path('cases.json').read_text())
        metafunc.parametrize('value', values, ids=['case-' + str(value) for value in values])
""",
            "tests/test_subject.py": """
import pytest
@pytest.mark.telescope(inputs=[])
def test_generated(value): assert value > 0
""",
            "cases.json": "[1, 2]",
        },
        '[tool.pytest_telescope.defaults]\ninputs = ["tests/conftest.py", "cases.json"]\n',
    )
    assert_outcomes(project.run(), executed=2)
    assert_outcomes(project.run(), reused=2)
    project.write("cases.json", "[1, 2, 3]")
    assert_outcomes(project.run(), executed=3)
    assert_outcomes(project.run("--", "-k", "case-3"), executed=1, unselected=2)
    assert_outcomes(project.run("--", "-k", "case-3"), reused=1, unselected=2)


@pytest.mark.parametrize(
    "selection",
    [
        ["-k", "alpha"],
        ["-m", "smoke"],
        ["tests/test_subject.py::test_beta[two]"],
    ],
)
def test_selectors_use_actual_cases_and_report_full_current_inventory(project, selection):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
@pytest.mark.telescope(inputs=[])
@pytest.mark.smoke
def test_alpha(): pass
@pytest.mark.telescope(inputs=[])
@pytest.mark.parametrize('value', [pytest.param(1, id='one'), pytest.param(2, id='two')])
def test_beta(value): assert value in (1, 2)
""",
        }
    )
    assert_outcomes(project.run("--", *selection), executed=1, unselected=2)
    assert_outcomes(project.run("--", *selection), reused=1, unselected=2)
    assert_outcomes(project.run("--", "-k", "does_not_exist", expected=5), unselected=3)


def test_each_case_gets_fresh_session_module_and_class_fixtures(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope='session')
def session_state(): return []
@pytest.fixture(scope='module')
def module_state(): return []
@pytest.fixture(scope='class')
def class_state(): return []
class TestFresh:
    @pytest.mark.parametrize('number', [1, 2])
    def test_fresh(self, number, session_state, module_state, class_state):
        for state in (session_state, module_state, class_state):
            assert state == []
            state.append(number)
def test_separate(session_state, module_state):
    assert session_state == []
    assert module_state == []
""",
        }
    )
    assert_outcomes(project.run(), executed=3)
    assert_outcomes(project.run(), reused=3)


MUTATED_MARKERS = {
    "module": """
pytestmark = pytest.mark.telescope(inputs=['a.txt'])
pytestmark.kwargs['inputs'].append('b.txt')
def test_case(): pass
""",
    "class": """
class TestMutation:
    pytestmark = pytest.mark.telescope(inputs=['a.txt'])
    pytestmark.kwargs['inputs'].append('b.txt')
    def test_case(self): pass
""",
    "function": """
@pytest.mark.telescope(inputs=['a.txt'])
def test_case(): pass
test_case.pytestmark[0].kwargs['inputs'].append('b.txt')
""",
    "parameter": """
@pytest.mark.parametrize('value', [
    pytest.param(1, marks=pytest.mark.telescope(inputs=['a.txt']), id='one'),
])
def test_case(value): pass
test_case.pytestmark[0].args[1][0].marks[0].kwargs['inputs'].append('b.txt')
""",
}


@pytest.mark.parametrize("level", MUTATED_MARKERS)
def test_marker_lists_cannot_be_mutated_after_supported_creation(project, level):
    project.prepare(
        {
            "tests/test_subject.py": "import pytest\n" + MUTATED_MARKERS[level],
            "a.txt": "a",
            "b.txt": "b",
        }
    )
    summary = project.run(expected=4)
    assert summary["status"] == "declaration_error"
    project.run(expected=4)  # A rejected declaration never becomes a successful hit.


DYNAMIC_MARKERS = {
    "alias": """
mark = pytest.mark.telescope
@mark(inputs=['a.txt'])
def test_case(): pass
""",
    "expression": """
@pytest.mark.telescope(inputs=['a.txt'] + [])
def test_case(): pass
""",
    "factory": """
def make_mark(): return pytest.mark.telescope(inputs=['a.txt'])
@make_mark()
def test_case(): pass
""",
    "hidden_getattr": """
@getattr(pytest.mark, 'telescope')(inputs=['a.txt'])
def test_case(): pass
""",
    "computed_kwargs": """
kwargs = {'inputs': ['a.txt']}
@pytest.mark.telescope(**kwargs)
def test_case(): pass
""",
}


@pytest.mark.parametrize("kind", DYNAMIC_MARKERS)
def test_unsupported_dynamic_declarations_are_rejected(project, kind):
    project.prepare(
        {
            "tests/test_subject.py": "import pytest\n" + DYNAMIC_MARKERS[kind],
            "a.txt": "a",
        }
    )
    assert project.run(expected=4)["status"] == "declaration_error"


def test_collection_hook_cannot_attach_an_unregistered_telescope_marker(project):
    project.prepare(
        {
            "tests/test_subject.py": "def test_case(): pass\n",
            "tests/conftest.py": """
import pytest
def pytest_collection_modifyitems(items):
    items[0].add_marker(getattr(pytest.mark, 'telescope')(inputs=['a.txt']))
""",
            "a.txt": "a",
        }
    )
    assert project.run(expected=4)["status"] == "declaration_error"


def test_in_project_selected_cache_directory_is_never_visible_to_scoped_cases(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
@pytest.mark.telescope(inputs=['visible.txt'])
def test_cache_is_not_an_input():
    assert Path('visible.txt').read_text() == 'visible'
    assert not Path('saved-results').exists()
""",
            "visible.txt": "visible",
            "saved-results/operator-note.txt": "not project input",
        }
    )
    args = ["--cache-dir", str(project.root / "saved-results")]
    assert_outcomes(project.run(*args), executed=1)
    assert_outcomes(project.run(*args), reused=1)


@pytest.mark.parametrize("execution_mode", ["case", "batch"])
def test_a_narrow_case_cannot_borrow_another_cases_collection_input(project, execution_mode):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
values = [Path('collection.txt').read_text()]
@pytest.mark.telescope(inputs=['own.txt'])
@pytest.mark.parametrize('value', values)
def test_narrow(value): assert value == 'shared'
@pytest.mark.telescope(inputs=['collection.txt'])
def test_other(): pass
""",
            "own.txt": "own",
            "collection.txt": "shared",
        }
    )
    summary = project.run("--execution-mode", execution_mode, expected=1)
    if execution_mode == "case":
        assert_outcomes(summary, executed=1, failed=1)
        assert any(
            failure["phase"] == "collect"
            for unit in summary["units"]
            for failure in unit["failures"]
        )
    else:
        assert summary["counts"]["executed"] == 1
        assert summary["counts"]["infrastructure_error"] == 1
        assert summary["counts"]["selected"] == 2
    group = next(group for group in summary["groups"] if group["stop_reason"] == "collection")
    assert group["not_run"] == 1 and group["unresolved"] == 0
    assert group["passed"] == group["failed"] == group["skipped"] == 0
    assert not group["complete"] and not group["cache_eligible"]
    assert any(failure["phase"] == "collect" for failure in group["failures"])
    member = next(row for row in summary["units"] if row["group_id"] == group["group_id"])
    assert member["result_status"] == "not-run" and not member["complete"]
    assert member["passed"] == member["failed"] == member["skipped"] == 0
    for group in summary["groups"]:
        assert sum(
            group[name] for name in ("passed", "failed", "skipped", "not_run", "unresolved")
        ) == len(group["case_ids"])


def test_class_parametrization_carries_literal_parameter_declarations(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
@pytest.mark.parametrize('name', [
    pytest.param('a', marks=pytest.mark.telescope(inputs=['a.txt']), id='a'),
    pytest.param('b', marks=pytest.mark.telescope(inputs=['b.txt']), id='b'),
])
class TestValues:
    def test_value(self, name):
        assert Path(name + '.txt').read_text() in ('first', 'second')
        assert not Path(('b' if name == 'a' else 'a') + '.txt').exists()
""",
            "a.txt": "first",
            "b.txt": "first",
        }
    )
    assert_outcomes(project.run(), executed=2)
    project.write("a.txt", "second")
    assert_outcomes(project.run(), executed=1, reused=1)


def test_keyword_argvalues_accepts_literal_parameter_declarations(project):
    project.prepare(
        {
            "tests/test_subject.py": """
import pytest
from pathlib import Path
@pytest.mark.parametrize(argnames='name', argvalues=[
    pytest.param('a', marks=pytest.mark.telescope(inputs=['a.txt']), id='a'),
    pytest.param('b', marks=pytest.mark.telescope(inputs=['b.txt']), id='b'),
])
def test_value(name):
    assert Path(name + '.txt').read_text() == name
    assert not Path(('b' if name == 'a' else 'a') + '.txt').exists()
""",
            "a.txt": "a",
            "b.txt": "b",
        }
    )
    assert_outcomes(project.run(), executed=2)
    assert_outcomes(project.run(), reused=2)


def test_collection_finish_cannot_remove_items_after_validated_selection(project):
    project.prepare(
        {
            "tests/test_subject.py": "def test_one(): pass\ndef test_two(): pass\n",
            "tests/conftest.py": """
def pytest_collection_finish(session):
    session.items.pop()
""",
        }
    )
    assert project.run(expected=4)["status"] == "declaration_error"


@pytest.mark.parametrize("execution_mode", ["case", "batch"])
def test_nonzero_session_verdict_cannot_become_successful_member_evidence(project, execution_mode):
    project.prepare(
        {
            "conftest.py": "def pytest_sessionfinish(session):\n    if not session.config.option.collectonly: session.exitstatus = 1\n",
            "tests/test_subject.py": "import pytest\npytestmark=pytest.mark.telescope(inputs=['conftest.py'])\ndef test_one(): pass\ndef test_two(): pass\n",
        }
    )
    summary = project.run("--execution-mode", execution_mode, expected=1)
    assert summary["status"] == "failed"
    assert summary["cases"]["passed"] == 2
    assert summary["cases"]["failed"] == summary["cases"]["skipped"] == 0
    assert all(
        group["outcome"] == "failed" and group["complete"] and not group["cache_eligible"]
        for group in summary["groups"]
    )
    assert not list((project.root / ".pytest-telescope/cache").glob("*.json"))
