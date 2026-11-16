"""Per-file pytest configuration and body-free collection preflight."""

import json
import subprocess
import sys

import pytest

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError, UnsupportedCapability
from pytest_telescope.plan import make_plan


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def project(root, config="pyproject.toml", *, explicit=False):
    declaration = f'inputs=["{config}"]' if explicit else "inputs=[]"
    put(
        root,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.demo]\n"
        'root="."\ncwd="."\ntest_roots=["tests"]\n'
        f'config="{config}"\n'
        + (
            '[tool.pytest.ini_options]\nmarkers=["used: used marker"]\n'
            if config == "pyproject.toml"
            else ""
        ),
    )
    if config != "pyproject.toml":
        section = "tool:pytest" if config.endswith("setup.cfg") else "pytest"
        put(root, config, f"[{section}]\nmarkers = used: used marker\n")
    put(
        root,
        "tests/test_sample.py",
        "import pytest\n"
        f"@pytest.mark.telescope({declaration})\n"
        "def test_sample():\n"
        f"    text = open('/project/{config}').read()\n"
        + (
            "    assert 'pytest_telescope' in text\n"
            if explicit and config == "pyproject.toml"
            else "    assert 'pytest_telescope' not in text\n"
        ),
    )


@pytest.mark.parametrize(
    "config", ["pyproject.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"]
)
@pytest.mark.parametrize("mode", ["case", "batch"])
def test_non_pytest_edits_and_unused_markers_preserve_case_cache(tmp_path, config, mode):
    project(tmp_path, config)
    options = {"execution_mode": mode, "arguments": ["--strict-markers"]}
    assert controller.run(tmp_path, **options)["counts"]["executed"] == 1
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text() + "\n[tool.pytest_telescope.bundles.unused]\ninputs=[]\n")
    assert controller.run(tmp_path, **options)["counts"]["reused"] == 1
    path = tmp_path / config
    if config.endswith(".toml"):
        path.write_text(
            path.read_text().replace(
                '"used: used marker"', '"used: used marker", "unused: unused marker"'
            )
            + '\n[tool.ruff]\nline-length=99\n[project]\nname="sample"\nversion="1.0"\n# comment\n'
        )
    else:
        path.write_text(
            path.read_text().replace(
                "used: used marker", "used: used marker\n    unused: unused marker"
            )
            + "\n# comment\n"
        )
    report = controller.run(tmp_path, **options)
    assert report["status"] == "passed", report
    assert report["counts"]["reused"] == 1


@pytest.mark.parametrize("config", ["pyproject.toml", "pytest.ini"])
def test_bundle_edit_refreshes_only_consuming_file(tmp_path, config):
    put(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest_telescope.suites.alpha]\n"
        'root="."\ncwd="."\ntest_roots=["tests/alpha"]\n'
        f'config="{config}"\n'
        "[tool.pytest_telescope.suites.beta]\n"
        'root="."\ncwd="."\ntest_roots=["tests/beta"]\n'
        f'config="{config}"\n'
        '[tool.pytest_telescope.bundles.alpha]\ninputs=["data/alpha.txt"]\n'
        '[tool.pytest_telescope.bundles.beta]\ninputs=["data/beta.txt"]\n'
        + ("[tool.pytest.ini_options]\n" if config == "pyproject.toml" else ""),
    )
    if config == "pytest.ini":
        put(tmp_path, config, "[pytest]\n")
    for name in ("alpha", "beta"):
        put(tmp_path, f"data/{name}.txt", name)
        put(
            tmp_path,
            f"tests/{name}/test_{name}.py",
            "import pytest\n"
            f'pytestmark=pytest.mark.telescope(bundles=["{name}"])\n'
            f"def test_{name}():\n"
            f"    assert open('/project/data/{name}.txt').read() == '{name}'\n",
        )
    assert controller.run(tmp_path)["counts"]["executed"] == 2
    put(tmp_path, "data/added.txt", "added")
    path = tmp_path / "pyproject.toml"
    path.write_text(
        path.read_text().replace(
            'inputs=["data/beta.txt"]', 'inputs=["data/beta.txt", "data/added.txt"]'
        )
    )
    changed = controller.run(tmp_path)
    assert changed["status"] == "passed", changed
    assert {row["file"]: row["outcome"] for row in changed["units"]} == {
        "tests/alpha/test_alpha.py": "reused",
        "tests/beta/test_beta.py": "executed",
    }
    assert (
        next(row for row in changed["collection"]["suites"] if row["name"] == "alpha")["outcome"]
        == "reused"
    )


@pytest.mark.parametrize("config", ["pyproject.toml", "pytest.ini"])
def test_declared_config_exposes_raw_bytes_and_invalidates_consumer(tmp_path, config):
    project(tmp_path, config, explicit=True)
    if config != "pyproject.toml":
        path = tmp_path / "tests/test_sample.py"
        path.write_text(
            path.read_text().replace(
                "assert 'pytest_telescope' not in text", "assert '[pytest]' in text"
            )
        )
    assert controller.run(tmp_path)["counts"]["executed"] == 1
    path = tmp_path / config
    path.write_text(path.read_text() + "\n# raw input changed\n")
    report = controller.run(tmp_path)
    assert report["status"] == "passed", report
    assert report["counts"]["executed"] == 1


def test_declared_root_pyproject_is_raw_input_for_non_pyproject_suite(tmp_path):
    project(tmp_path, "pytest.ini")
    path = tmp_path / "tests/test_sample.py"
    path.write_text(
        path.read_text()
        .replace("inputs=[]", 'inputs=["pyproject.toml"]')
        .replace(
            "text = open('/project/pytest.ini').read()",
            "text = open('/project/pyproject.toml').read()",
        )
        .replace("assert 'pytest_telescope' not in text", "assert 'pytest_telescope' in text")
    )
    assert controller.run(tmp_path)["counts"]["executed"] == 1
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text() + "\n# raw input changed\n")
    report = controller.run(tmp_path)
    assert report["status"] == "passed", report
    assert report["counts"]["executed"] == 1


@pytest.mark.parametrize(
    "filename", ["pyproject.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"]
)
def test_registered_markers_and_pythonpath_use_suite_config_parent(tmp_path, filename):
    config = f"sub/{filename}"
    put(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest_telescope.defaults]\ninputs=["sub/helpers/helper.py"]\n'
        "[tool.pytest_telescope.suites.demo]\n"
        'root="sub"\ncwd="."\ntest_roots=["sub/tests"]\n'
        f'config="{config}"\n',
    )
    if filename.endswith(".toml"):
        content = (
            '[tool.pytest.ini_options]\npythonpath=["helpers"]\nmarkers=["used: registered"]\n'
        )
    else:
        section = "tool:pytest" if filename == "setup.cfg" else "pytest"
        content = f"[{section}]\npythonpath = helpers\nmarkers = used: registered\n"
    put(tmp_path, config, content)
    put(tmp_path, "sub/helpers/helper.py", "VALUE = 7\n")
    for name, marker in (("marked", "@pytest.mark.used\n"), ("plain", "")):
        put(
            tmp_path,
            f"sub/tests/test_{name}.py",
            "import os\nimport stat\nimport pytest\nfrom helper import VALUE\n"
            "pytestmark=pytest.mark.telescope(inputs=[])\n"
            + marker
            + f"def test_{name}():\n    assert VALUE == 7\n"
            "    assert not os.path.exists('/project/pyproject.toml')\n"
            f"    config_stat = os.stat('/project/{config}')\n"
            "    assert stat.S_IMODE(config_stat.st_mode) == 0o444\n"
            "    assert config_stat.st_mtime_ns == 0\n",
        )
    options = {"arguments": ["--strict-markers"], "execution_mode": "batch"}
    first = controller.run(tmp_path, **options)
    assert first["status"] == "passed", first
    assert first["counts"]["executed"] == 2
    if filename.endswith(".toml"):
        put(
            tmp_path,
            config,
            content.replace('"used: registered"', '"used: registered", "unused: registered"'),
        )
    else:
        put(
            tmp_path,
            config,
            content.replace("used: registered", "used: registered\n    unused: registered"),
        )
    warm = controller.run(tmp_path, **options)
    assert warm["status"] == "passed", warm
    assert warm["counts"]["reused"] == 2
    changed_content = (
        (tmp_path / config).read_text().replace("used: registered", "used: changed", 1)
    )
    put(tmp_path, config, changed_content)
    changed = controller.run(tmp_path, **options)
    assert changed["status"] == "passed", changed
    assert {row["file"]: row["outcome"] for row in changed["units"]} == {
        "sub/tests/test_marked.py": "executed",
        "sub/tests/test_plain.py": "reused",
    }
    put(
        tmp_path,
        "sub/tests/test_unknown.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
        "@pytest.mark.unregistered\ndef test_unknown(): pass\n",
    )
    failed = controller.run(tmp_path, **options)
    assert failed["status"] == "failed", failed
    assert failed["collection"]["failures"]


def test_mixed_file_ordinary_collection_records_scoped_marker_names(tmp_path):
    project(tmp_path)
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\n"
        "@pytest.mark.used\n@pytest.mark.telescope(inputs=[])\n"
        "def test_scoped(): pass\n"
        "def test_ordinary(): pass\n",
    )
    options = {"arguments": ["--strict-markers"]}
    first = controller.run(tmp_path, **options)
    assert first["status"] == "passed", first
    path = tmp_path / "pyproject.toml"
    path.write_text(
        path.read_text().replace(
            '"used: used marker"', '"used: used marker", "unused: another marker"'
        )
    )
    warm = controller.run(tmp_path, **options)
    assert warm["status"] == "passed", warm
    assert {row["route"]: row["outcome"] for row in warm["units"]} == {
        "ordinary": "executed",
        "scoped": "reused",
    }


def test_file_marker_union_keeps_batch_members_together(tmp_path):
    project(tmp_path)
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
        "@pytest.mark.used\ndef test_marked(): pass\n"
        "def test_plain(): pass\n",
    )
    options = {"execution_mode": "batch", "arguments": ["--strict-markers"]}
    first = controller.run(tmp_path, **options)
    assert first["status"] == "passed", first
    assert len(first["groups"]) == 1
    assert first["groups"][0]["case_ids"] and len(first["groups"][0]["case_ids"]) == 2
    path = tmp_path / "pyproject.toml"
    path.write_text(
        path.read_text().replace('"used: used marker"', '"used: used marker", "unused: unused"')
    )
    warm = controller.run(tmp_path, **options)
    assert warm["status"] == "passed", warm
    assert len(warm["groups"]) == 1 and warm["groups"][0]["outcome"] == "reused"
    assert warm["counts"]["reused"] == 2


def test_collect_only_primes_scoped_inventory_without_running_bodies(tmp_path):
    project(tmp_path)
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\ndef test_scoped():\n    assert False\n",
    )
    put(
        tmp_path,
        "tests/test_ordinary.py",
        "from pathlib import Path\ndef test_ordinary():\n    Path('ordinary_ran').write_text('yes')\n",
    )
    first = controller.run(tmp_path, collect_only=True)
    assert first["status"] == "passed", first
    assert first["groups"] == []
    assert first["counts"]["selected"] == 2
    assert first["counts"]["not-run"] == 2
    assert not (tmp_path / "ordinary_ran").exists()
    second = controller.run(tmp_path)
    assert second["collection"]["suites"][0]["outcome"] == "reused"
    assert (tmp_path / "ordinary_ran").exists()


def test_cli_collect_only_has_body_free_accounting(tmp_path):
    project(tmp_path)
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\ndef test_scoped():\n    assert False\n",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest_telescope.controller", "run", "--collect-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["counts"]["not-run"] == 1
    assert report["groups"] == []


def test_collect_only_empty_inventory_and_unselected_inventory(tmp_path):
    project(tmp_path)
    (tmp_path / "tests/test_sample.py").unlink()
    empty = controller.run(tmp_path, collect_only=True)
    assert empty["status"] == "no_tests", empty
    assert empty["collection"]["collected"] == 0
    assert empty["groups"] == []
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
        "def test_sample(): assert False\n",
    )
    unselected = controller.run(tmp_path, collect_only=True, arguments=["-k", "no_match"])
    assert unselected["status"] == "passed", unselected
    assert unselected["collection"]["collected"] == 1
    assert unselected["counts"]["not-selected"] == 1
    with pytest.raises(DeclarationError, match="selector"):
        controller.run(
            tmp_path,
            collect_only=True,
            arguments=["tests/test_sample.py::missing"],
        )


@pytest.mark.parametrize("route", ["scoped", "ordinary"])
def test_collect_only_reports_collection_import_failures(tmp_path, route):
    project(tmp_path)
    source = "import missing_test_helper\n"
    if route == "scoped":
        source = "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n" + source
    put(tmp_path, "tests/test_sample.py", source + "def test_sample(): pass\n")
    report = controller.run(tmp_path, collect_only=True)
    assert report["status"] == "failed", report
    assert report["collection"]["failures"]


@pytest.mark.parametrize("value", [0, None, "yes"])
def test_collect_only_requires_literal_boolean(tmp_path, value):
    project(tmp_path)
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, collect_only=value)


@pytest.mark.requires_fixture_guard
def test_collect_only_runs_fixture_guard_admission_without_bodies(tmp_path, monkeypatch):
    project(tmp_path)
    put(tmp_path, "a.txt", "a")
    put(tmp_path, "b.txt", "b")
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
        "@pytest.fixture(scope='module')\ndef shared(): return object()\n"
        "@pytest.mark.telescope(inputs=['a.txt'])\n"
        "def test_a(shared): assert False\n"
        "@pytest.mark.telescope(inputs=['b.txt'])\n"
        "def test_b(shared): assert False\n",
    )
    modes = []
    original = controller.sandbox.execute

    def observe(*args, request, **kwargs):
        modes.append(request["mode"])
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(controller.sandbox, "execute", observe)
    with pytest.raises(UnsupportedCapability) as error:
        controller.run(
            tmp_path, collect_only=True, execution_mode="batch", fixture_guard="available-v1"
        )
    assert error.value.capability == "execution:fixture-sharing"
    assert "guard-capture" in modes and "guard-inspection" in modes
    assert "guard-batch" not in modes


@pytest.mark.requires_fixture_guard
def test_collect_only_successfully_admits_fixture_guard_without_body(tmp_path):
    project(tmp_path)
    put(
        tmp_path,
        "tests/test_sample.py",
        "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\n"
        "def test_scoped(): assert False\n",
    )
    report = controller.run(
        tmp_path, collect_only=True, execution_mode="batch", fixture_guard="available-v1"
    )
    assert report["status"] == "passed", report
    assert report["fixture_guard"]["state"] == "admitted"
    assert report["groups"] == []
    assert report["counts"]["not-run"] == 1


@pytest.mark.parametrize("invalid", ['nested={value="bad"}', 'nested=[1, "bad"]'])
def test_nested_pytest_toml_options_refuse_explicitly(tmp_path, invalid):
    project(tmp_path)
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text() + invalid + "\n")
    with pytest.raises(DeclarationError, match="pytest"):
        make_plan(tmp_path)
