"""Behavioral qualification of exact frozen readable helper declaration registries.

Success and view controls use real Bubblewrap. Faults alter only owned fixture state.
"""

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def project_at(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    put(root, "helpers/__init__.py", "")
    put(root, "data/answer.txt", "42\n")
    put(root, "helper-private.txt", "the helper's own input\n")
    put(
        root,
        "helpers/support.py",
        "import pytest\nfrom pathlib import Path\n"
        'pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])\n'
        'def answer(): return Path("data/answer.txt").read_text().strip()\n',
    )
    consumer(root)
    return root


def consumer(root, *, inputs=None, extra="", body=None):
    if inputs is None:
        inputs = ["helpers/__init__.py", "helpers/support.py", "data/answer.txt"]
    if body is None:
        body = 'assert answer() == "42"\n    assert not Path("helper-private.txt").exists()'
    put(
        root,
        "tests/test_consumer.py",
        "from pathlib import Path\nimport pytest\n"
        f"pytestmark = pytest.mark.telescope(inputs={inputs!r})\n"
        "from helpers.support import answer\n"
        f"{extra}\ndef test_answer():\n    {body}\n",
    )


def no_new_records(root):
    cache = root / ".pytest-telescope/cache"
    assert not list(cache.glob("*.json"))
    assert not list((cache / "inventory").glob("*.json"))
    scratch = root / ".pytest-telescope/scratch"
    if scratch.exists():
        assert not list(scratch.iterdir())


def test_undeclared_helper_is_absent_from_scoped_import(tmp_path):
    root = project_at(tmp_path)
    consumer(root, inputs=["helpers/__init__.py", "data/answer.txt"])

    report = controller.run(root, workers=1)

    assert report["status"] == "failed", report
    assert report["sandbox"]["identity"] is not None
    assert report["inventory_complete"] is False
    assert report["units"] == []
    assert report["collection"]["outcome"] == "failed"
    assert report["collection"]["failures"]
    assert all(
        row["phase"] == "collect" and row["exception_type"] == "CollectionError"
        for row in report["collection"]["failures"]
    )
    assert [row["route"] for row in report["collection"]["suites"]] == ["scoped"]
    assert (root / "helpers/support.py").is_file()
    no_new_records(root)


def test_declared_helper_computed_marker_is_refused(tmp_path):
    root = project_at(tmp_path)
    put(
        root,
        "helpers/support.py",
        'import pytest\nkwargs = {"inputs": ["helper-private.txt"]}\n'
        'pytestmark = pytest.mark.telescope(**kwargs)\ndef answer(): return "42"\n',
    )

    with pytest.raises(DeclarationError, match="computed or duplicate telescope keyword"):
        controller.run(root, workers=1)

    no_new_records(root)


def test_declared_helper_hidden_dynamic_marker_is_refused(tmp_path):
    root = project_at(tmp_path)
    put(
        root,
        "helpers/support.py",
        'import pytest\npytestmark = getattr(pytest.mark, "telescope")('
        'inputs=["helper-private.txt"])\ndef answer(): return "42"\n',
    )

    with pytest.raises(DeclarationError, match="unsupported or changed declarations"):
        controller.run(root, workers=1)

    no_new_records(root)


@pytest.mark.parametrize("target", ["implementation.py", "implementation.source"])
def test_declared_helper_leaf_alias_preserves_import_origin(tmp_path, target):
    root = project_at(tmp_path)
    put(
        root,
        "helpers/" + target,
        f"""
import stat
from pathlib import Path
import pytest

MODULE_FILE = __file__
assert Path(__file__).name == "alias.py"
assert stat.S_ISLNK(Path(__file__).lstat().st_mode)
assert Path(__file__).readlink().as_posix() == {target!r}
pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"])

def answer():
    return Path("data/answer.txt").read_text().strip()
""",
    )
    (root / "helpers/alias.py").symlink_to(target)
    inputs = ["helpers/__init__.py", "helpers/alias.py", "helpers/" + target, "data/answer.txt"]
    put(
        root,
        "tests/test_consumer.py",
        f"""
import stat
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs={inputs!r})
from helpers.alias import MODULE_FILE, answer

def test_answer():
    assert answer() == "42"
    assert Path(MODULE_FILE).name == "alias.py"
    assert stat.S_ISLNK(Path(MODULE_FILE).lstat().st_mode)
    assert Path(MODULE_FILE).readlink().as_posix() == {target!r}
    assert not Path("helper-private.txt").exists()
    assert not Path("helpers/support.py").exists()
""",
    )

    report = controller.run(root, workers=1)

    assert report["status"] == "passed", report
    assert report["inventory_complete"] is True
    assert report["counts"]["selected"] == report["counts"]["executed"] == 1
    assert report["cases"]["passed"] == 1
    assert [row["file"] for row in report["units"]] == ["tests/test_consumer.py"]
    assert (root / "helpers/alias.py").is_symlink()
    assert (root / "helpers/alias.py").readlink().as_posix() == target
