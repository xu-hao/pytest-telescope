"""Reserved declarations and real fixed worker settings are independent contracts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError
from pytest_telescope.plan import make_plan

RESERVED = ("PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE")
ROUTES = ("global", "defaults", "bundle", "unused_bundle", "module", "class", "function", "parameter")


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def project(root, route, name):
    config = '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
    mark = f"pytest.mark.telescope(env=[{name!r}], inputs=['tests/conftest.py'])"
    source = "import pytest\n"
    body = "def test_item(): pass\n"
    if route == "global":
        config += f"env = [{json.dumps(name)}]\n"
    elif route == "defaults":
        config += f"[tool.pytest_telescope.defaults]\nenv = [{json.dumps(name)}]\n"
    elif route in ("bundle", "unused_bundle"):
        config += '[tool.pytest_telescope.bundles.outer]\nbundles = ["inner"]\n'
        config += f"[tool.pytest_telescope.bundles.inner]\nenv = [{json.dumps(name)}]\n"
        if route == "bundle":
            source += 'pytestmark = pytest.mark.telescope(bundles=["outer"], inputs=["tests/conftest.py"])\n'
    elif route == "module":
        source += f"pytestmark = {mark}\n"
    elif route == "class":
        body = f"@{mark}\nclass TestItems:\n    def test_item(self): pass\n"
    elif route == "function":
        body = f"@{mark}\n" + body
    elif route == "parameter":
        body = f'@pytest.mark.parametrize("value", [pytest.param(1, marks={mark})])\ndef test_item(value): pass\n'
    put(root, "pyproject.toml", config)
    put(root, "tests/test_item.py", source + body)
    # An ordinary import witness detects project execution independently of error text.
    put(root, "tests/test_witness.py", "from pathlib import Path\nPath('imported').touch()\ndef test_witness(): Path('body').touch()\n")
    put(root, "tests/conftest.py", "from pathlib import Path\ndef pytest_sessionstart(session): Path('started').touch()\n")


def absent_witnesses(root):
    assert not any((root / name).exists() for name in ("started", "imported", "body"))


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("value", [None, "", "synthetic-reserved-value"])
@pytest.mark.parametrize("route", ROUTES)
def test_plan_rejects_reserved_names_in_every_state(tmp_path, monkeypatch, route, value, name):
    project(tmp_path, route, name)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(DeclarationError) as error:
        make_plan(tmp_path)
    assert type(error.value) is DeclarationError
    absent_witnesses(tmp_path)


@pytest.mark.parametrize("route", ROUTES)
def test_run_rejects_before_project_session_or_import(tmp_path, monkeypatch, route):
    project(tmp_path, route, "HOME")
    monkeypatch.setenv("HOME", "synthetic-home")
    try:
        with pytest.raises(DeclarationError) as error:
            controller.run(tmp_path, workers=1)
        assert type(error.value) is DeclarationError
        assert controller.error_result(error.value)["status"] == "declaration_error"
    finally:
        absent_witnesses(tmp_path)


@pytest.mark.parametrize("command", ["plan", "run"])
def test_cli_classifies_reserved_declaration_without_project_execution(tmp_path, command):
    project(tmp_path, "global", "HOME")
    result = subprocess.run(
        [sys.executable, "-m", "pytest_telescope", command],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 4, result.stdout
    result = json.loads(result.stdout)
    assert result["kind"] == "refusal" and result["status"] == "declaration_error"
    assert "capability" not in result
    absent_witnesses(tmp_path)


def passed(report):
    assert report["status"] == "passed", report
    assert report["sandbox"]["backend"] == "bubblewrap"
    assert len(report["sandbox"]["identity"]) == 64
    assert report["inventory_complete"]
    assert all(row["complete"] for row in report["groups"])
    for row in report["units"]:
        assert row["reports"] == [
            {"phase": phase, "outcome": "passed", "skip_reason": None}
            for phase in ("setup", "call", "teardown")
        ]


def test_real_worker_fixed_environment_and_bytecode_behavior(tmp_path):
    put(tmp_path, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n')
    expected_path = ":".join(dict.fromkeys([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]))
    put(tmp_path, "tests/test_worker.py", f'''import importlib.util
import os
import sys
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(env=[])
def test_worker(tmp_path):
    assert os.getcwd() == "/project"
    assert os.environ["PATH"] == {expected_path!r}
    assert {{name: os.environ[name] for name in ("HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")}} == {{"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}}
    source = tmp_path / "plain_module.py"
    source.write_text("value = 37\\n")
    spec = importlib.util.spec_from_file_location("plain_module", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.value == 37
    assert not (tmp_path / "__pycache__").exists()
''')
    passed(controller.run(tmp_path, workers=1))


@pytest.mark.parametrize("value", [None, "", "synthetic-first", "synthetic-second"])
@pytest.mark.parametrize("route", ["global", "module"])
def test_real_legitimate_forwarding_exact_state(tmp_path, monkeypatch, route, value):
    name = "SYNTHETIC_OPTION"
    project(tmp_path, route, name)
    # This fixture is wholly scoped and needs no ordinary witness module or conftest.
    (tmp_path / "tests/test_witness.py").unlink()
    (tmp_path / "tests/conftest.py").unlink()
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    declaration = f'env=["{name}"]' if route == "module" else "env=[]"
    put(tmp_path, "tests/test_item.py", f'''import os
import pytest
pytestmark = pytest.mark.telescope({declaration})
def test_item():
    assert ("{name}" in os.environ, os.environ.get("{name}")) == {(value is not None, value)!r}
''')
    passed(controller.run(tmp_path, workers=1))
    warm = controller.run(tmp_path, workers=1)
    passed(warm)
    assert warm["counts"]["reused"] == 1
