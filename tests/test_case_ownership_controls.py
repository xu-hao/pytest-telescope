"""Ordinary owner boundaries exercised through the public API and real Bubblewrap."""

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError


def write(root, name, content):
    destination = root / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def project_at(tmp_path, source, configuration=""):
    root = tmp_path / "project"
    root.mkdir()
    write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n' + configuration,
    )
    write(root, "tests/test_cases.py", source)
    return root


def run(root, *, executed=0, reused=0, failed=0):
    report = controller.run(root, workers=1, timeout=20)
    assert report["status"] == ("failed" if failed else "passed"), report
    assert report["inventory_complete"] is True
    assert report["counts"]["executed"] == executed, report
    assert report["counts"]["reused"] == reused, report
    assert report["counts"]["failed"] == failed, report
    assert report["counts"]["infrastructure_error"] == 0, report
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    return report


def test_unchanged_sibling_class_mark_is_not_an_inherited_owner(tmp_path):
    root = project_at(
        tmp_path,
        """
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.telescope(inputs=["left.txt"])
class TestLeft:
    def test_left(self): pass

@pytest.mark.telescope(inputs=["right.txt"])
class TestRight:
    def test_right(self): pass

TestRight.pytestmark.extend(TestLeft.pytestmark)
""",
    )
    write(root, "left.txt", "left")
    write(root, "right.txt", "right")

    with pytest.raises(DeclarationError):
        controller.run(root, workers=1, timeout=20)


def test_unchanged_parameter_mark_cannot_move_to_an_equal_sibling_alternative(tmp_path):
    root = project_at(
        tmp_path,
        """
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.parametrize("value", [
    pytest.param(7, marks=[pytest.mark.telescope(inputs=["left.txt"])], id="same"),
    pytest.param(7, marks=[pytest.mark.telescope(inputs=["right.txt"])], id="same"),
])
def test_choice(value): pass

alternatives = test_choice.pytestmark[0].args[1]
alternatives[1].marks.extend(alternatives[0].marks)
""",
    )
    write(root, "left.txt", "left")
    write(root, "right.txt", "right")

    with pytest.raises(DeclarationError):
        controller.run(root, workers=1, timeout=20)


REMOVED_DECLARATIONS = {
    "module": """
pytestmark = pytest.mark.telescope(inputs=[])
def test_case(): pass
pytestmark = []
""",
    "class": """
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(cache=False)
class TestOwner:
    def test_case(self): pass
TestOwner.pytestmark.clear()
""",
    "function": """
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(cache=False)
def test_case(): pass
test_case.pytestmark.clear()
""",
    "parameter": """
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.parametrize("value", [
    pytest.param(7, marks=[pytest.mark.telescope(inputs=[], cache=False)]),
])
def test_case(value): pass
test_case.pytestmark[0].args[1][0].marks.clear()
""",
}


@pytest.mark.parametrize("placement", REMOVED_DECLARATIONS)
def test_removing_a_required_empty_scope_or_policy_marker_refuses(tmp_path, placement):
    root = project_at(tmp_path, "import pytest\n" + REMOVED_DECLARATIONS[placement])

    with pytest.raises(DeclarationError):
        controller.run(root, workers=1, timeout=20)


def test_empty_parameter_scope_and_cache_policy_stay_with_the_selected_alternative(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.parametrize("value", [
    pytest.param(7, marks=[pytest.mark.telescope(env=[], cache=False)], id="same"),
    pytest.param(7, id="same"),
])
def test_choice(value):
    assert not Path("unrelated.txt").exists()
""",
    )
    write(root, "unrelated.txt", "outside either alternative")

    run(root, executed=2)
    run(root, executed=1, reused=1)


def test_defaults_do_not_scope_ordinary_cases_or_transfer_a_sibling_scope(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest

@pytest.mark.telescope(cache=False)
def test_ordinary():
    assert Path("unrelated.txt").is_file()
    assert Path("default.txt").is_file()

@pytest.mark.telescope(inputs=[])
def test_scoped():
    assert Path("default.txt").is_file()
    assert not Path("unrelated.txt").exists()
""",
        '[tool.pytest_telescope.defaults]\ninputs = ["default.txt"]\n',
    )
    write(root, "default.txt", "shared only by scoped cases")
    write(root, "unrelated.txt", "ordinary visibility")

    cold = run(root, executed=2)
    warm = run(root, executed=1, reused=1)
    assert cold["route_counts"]["ordinary"]["executed"] == 1
    assert cold["route_counts"]["scoped"]["executed"] == 1
    assert warm["route_counts"]["ordinary"]["executed"] == 1
    assert warm["route_counts"]["scoped"]["reused"] == 1


def test_overridden_method_does_not_inherit_the_replaced_base_function_declaration(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(env=[])

class ScopeBase:
    pytestmark = [
        pytest.mark.telescope(inputs=["base.txt"]),
        pytest.mark.telescope(env=[], cache=False),
    ]
    @pytest.mark.telescope(inputs=["replaced.txt"])
    def test_case(self):
        raise AssertionError("the replaced implementation must not execute")

@pytest.mark.telescope(inputs=["derived.txt"])
class TestDerived(ScopeBase):
    @pytest.mark.telescope(inputs=["replacement.txt"], cache=True)
    def test_case(self):
        for name in ("base", "derived", "replacement"):
            assert Path(name + ".txt").is_file()
        assert not Path("replaced.txt").exists()
""",
    )
    for name in ("base", "derived", "replaced", "replacement"):
        write(root, name + ".txt", "present")

    run(root, executed=1)
    run(root, executed=1)


def test_diamond_class_ancestry_keeps_each_actual_ancestor_scope(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.telescope(inputs=["base.txt"])
class ScopeBase:
    @pytest.mark.telescope(inputs=["method.txt"])
    def test_case(self):
        for name in ("base", "left", "right", "method"):
            assert Path(name + ".txt").is_file()
        assert not Path("unrelated.txt").exists()

@pytest.mark.telescope(inputs=["left.txt"])
class ScopeLeft(ScopeBase): pass

@pytest.mark.telescope(inputs=["right.txt"])
class ScopeRight(ScopeBase): pass

@pytest.mark.telescope(inputs=[])
class TestDiamond(ScopeLeft, ScopeRight): pass
""",
    )
    for name in ("base", "left", "right", "method", "unrelated"):
        write(root, name + ".txt", "present")

    run(root, executed=1)
    run(root, reused=1)


def test_authenticated_helper_inputs_metadata_environment_absence_and_policy_do_not_transfer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OWNERSHIP_HELPER_OPTION", "synthetic helper setting")
    root = project_at(
        tmp_path,
        """
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import os
import pytest
pytestmark = pytest.mark.telescope(inputs=["support.py"])
from support import answer

def test_consumer():
    assert answer() == 42
    assert not Path("helper-only.txt").exists()
    assert not Path("helper-absence.txt").exists()
    assert "OWNERSHIP_HELPER_OPTION" not in os.environ
    with pytest.raises(PackageNotFoundError):
        version("ownership-only-helper-fixture")
""",
        "[tool.pytest_telescope.distributions.helper]\n"
        'metadata = "metadata/pyproject.toml"\nformat = "pep621-static-v1"\n',
    )
    write(
        root,
        "support.py",
        """
import pytest
pytestmark = pytest.mark.telescope(
    inputs=["helper-only.txt"], distributions=["helper"],
    env=["OWNERSHIP_HELPER_OPTION"], absent=["helper-absence.txt"], cache=False,
)
def answer(): return 42
""",
    )
    write(root, "helper-only.txt", "helper declaration only")
    write(root, "helper-absence.txt", "helper assertion must not apply to consumer")
    write(
        root,
        "metadata/pyproject.toml",
        '[project]\nname = "ownership-only-helper-fixture"\nversion = "1.0"\n',
    )

    run(root, executed=1)
    run(root, reused=1)


def test_environment_changes_rerun_only_the_owner_with_exact_present_empty_absent_values(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OWNERSHIP_CASE_OPTION", raising=False)
    root = project_at(tmp_path, "")
    (root / "tests/test_cases.py").unlink()
    write(
        root,
        "tests/test_left.py",
        """
from pathlib import Path
import os
import pytest
pytestmark = pytest.mark.telescope(inputs=["expected.txt"], env=["OWNERSHIP_CASE_OPTION"])
def test_left():
    assert os.environ.get("OWNERSHIP_CASE_OPTION", "<absent>") == Path("expected.txt").read_text()
""",
    )
    write(
        root,
        "tests/test_right.py",
        """
import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_right():
    assert "OWNERSHIP_CASE_OPTION" not in os.environ
""",
    )
    write(root, "expected.txt", "<absent>")
    run(root, executed=2)
    run(root, reused=2)

    # Keep the oracle file unchanged for the first run of each environment transition.
    # The real failure proves invalidation and that the old value was not reused.
    for value in ("synthetic owner setting", "", None):
        if value is None:
            monkeypatch.delenv("OWNERSHIP_CASE_OPTION")
        else:
            monkeypatch.setenv("OWNERSHIP_CASE_OPTION", value)
        run(root, reused=1, failed=1)
        write(root, "expected.txt", "<absent>" if value is None else value)
        report = run(root, executed=1, reused=1)
        assert {row["file"]: row["outcome"] for row in report["units"]} == {
            "tests/test_left.py": "executed",
            "tests/test_right.py": "reused",
        }
