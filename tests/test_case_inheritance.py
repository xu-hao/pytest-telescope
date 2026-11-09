"""Case-owned declarations under ordinary Pytest collection and real Bubblewrap."""

from pytest_telescope import controller


def write(root, filename, content):
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def project_at(tmp_path, source):
    root = tmp_path / "project"
    root.mkdir()
    write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        'markers = ["expect_pair: companion oracle", "expect_upper: companion oracle", '
        '"expect_lower: companion oracle", "expected_paths: companion oracle"]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    write(root, "tests/test_cases.py", source)
    return root


def passed(root, *, executed, reused=0):
    report = controller.run(root, workers=1, timeout=20)
    assert report["status"] == "passed", report
    assert report["inventory_complete"] is True
    assert report["counts"]["executed"] == executed
    assert report["counts"]["reused"] == reused
    assert report["counts"]["failed"] == report["counts"]["infrastructure_error"] == 0
    assert all(unit["route"] == "scoped" for unit in report["units"])
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    return report


def test_real_inherited_method_and_class_mro_keep_additive_scope_and_policy(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=["module.txt"])

@pytest.mark.telescope(inputs=["base.txt"])
class ScopeBase:
    @pytest.mark.telescope(inputs=["method.txt"])
    def test_inherited(self):
        for name in ("module", "base", "mixin", "derived", "method"):
            assert Path(name + ".txt").read_text() == "present"
        assert not Path("unrelated.txt").exists()

@pytest.mark.telescope(inputs=["mixin.txt"], cache=False)
class ScopeMixin: pass

@pytest.mark.telescope(inputs=["derived.txt"], cache=True)
class TestDerived(ScopeBase, ScopeMixin): pass
""",
    )
    for name in ("module", "base", "mixin", "derived", "method", "unrelated"):
        write(root, name + ".txt", "present")

    passed(root, executed=1)
    passed(root, executed=1)


PARAMETER_BODY = """
        expected = set(request.node.get_closest_marker("expected_paths").args[0])
        for name in ("pair", "upper", "lower", "first", "second"):
            assert Path(name + ".txt").exists() == (name in expected)
        assert not Path("unrelated.txt").exists()
"""


def test_real_parameter_dimensions_keep_equal_values_ids_and_alternatives_independent(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.parametrize("left,right", [
    pytest.param(7, 7, marks=[pytest.mark.telescope(inputs=["pair.txt"]), pytest.mark.expect_pair], id="same"),
    pytest.param(7, 7, id="same"),
])
class TestClassDimension:
    @pytest.mark.parametrize("upper", [
        pytest.param(7, marks=[pytest.mark.telescope(inputs=["upper.txt"]), pytest.mark.expect_upper], id="named"),
        pytest.param(7, id="named"),
    ])
    @pytest.mark.parametrize("lower", [
        pytest.param(7, marks=[pytest.mark.telescope(inputs=["lower.txt"], cache=False), pytest.mark.expect_lower], id="named"),
        pytest.param(7, id="named"),
    ])
    def test_product(self, left, right, upper, lower, request):
        # Ordinary companion marks are an independent oracle for this synthetic project.
        expected = {
            name for name in ("pair", "upper", "lower")
            if request.node.get_closest_marker("expect_" + name) is not None
        }
        for name in ("pair", "upper", "lower"):
            assert Path(name + ".txt").exists() == (name in expected)
        assert not Path("first.txt").exists()
        assert not Path("second.txt").exists()

@pytest.mark.parametrize("value", [
    pytest.param(7, marks=[pytest.mark.telescope(inputs=["first.txt"]), pytest.mark.expected_paths(["first"])], id="same"),
    pytest.param(7, marks=[pytest.mark.telescope(inputs=["second.txt"]), pytest.mark.expected_paths(["second"])], id="same"),
    pytest.param(7, marks=pytest.mark.expected_paths([]), id="same"),
])
def test_equal(value, request):
"""
        + PARAMETER_BODY,
    )
    for name in ("pair", "upper", "lower", "first", "second", "unrelated"):
        write(root, name + ".txt", "present")

    passed(root, executed=11)
    passed(root, executed=4, reused=7)
    write(root, "pair.txt", "changed")
    passed(root, executed=6, reused=5)
    write(root, "unrelated.txt", "changed")
    passed(root, executed=4, reused=7)
