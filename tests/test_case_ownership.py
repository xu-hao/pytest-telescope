"""Case-owned declarations under ordinary Pytest collection and real Bubblewrap."""

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError


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
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n',
    )
    write(root, "tests/test_cases.py", source)
    return root


def test_unchanged_sibling_function_mark_is_refused_before_granting_inputs(tmp_path):
    root = project_at(
        tmp_path,
        """
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.telescope(inputs=["left.txt"])
def test_left():
    assert Path("left.txt").read_text() == "left"
    assert not Path("right.txt").exists()

@pytest.mark.telescope(inputs=["right.txt"])
def test_right():
    assert Path("right.txt").read_text() == "right"
    assert Path("left.txt").read_text() == "left"

# Reuse the existing ordinary Mark unchanged; both literal calls keep their real origin.
test_right.pytestmark.extend(test_left.pytestmark)
""",
    )
    write(root, "left.txt", "left")
    write(root, "right.txt", "right")

    with pytest.raises(DeclarationError):
        report = controller.run(root, workers=1, timeout=20)
        print({"observed_status": report["status"], "actual_counts": report["counts"]})

    assert not list((root / ".pytest-telescope/cache").glob("**/*.json"))
