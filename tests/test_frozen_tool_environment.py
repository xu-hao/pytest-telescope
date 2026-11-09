"""Tool-local environment conflicts preserve ordinary and nonconsumer contracts."""

import json

import pytest
from frozen_tool_fixtures import ToolProject, assert_counts

from pytest_telescope import controller, sandbox


@pytest.fixture
def project(tmp_path):
    return ToolProject(tmp_path / "project", tmp_path / "installed")


@pytest.mark.parametrize("name", ["PATH", "HOME", "TMPDIR", "LD_LIBRARY_PATH", "PYTHONHASHSEED"])
@pytest.mark.parametrize("value", [None, ""])
def test_selected_tool_refuses_declared_runtime_controls_even_absent_or_empty(
    project, monkeypatch, name, value
):
    project.add_tree("first")
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    project.module("test_conflict.py", f"""import pytest
pytestmark = pytest.mark.telescope(tools=['first'], env=[{name!r}])
def test_conflict(): pass
""")
    report = project.run()
    assert report["status"] == "declaration_error", report


def test_omitted_tools_preserve_existing_environment_forwarding(project, monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/owned-environment-home")
    project.module("test_environment.py", """import os
import pytest
pytestmark = pytest.mark.telescope(env=['HOME'])
def test_environment():
    assert os.environ['HOME'] == '/tmp/owned-environment-home'
""")
    assert_counts(project.run(), executed=1)
    assert_counts(project.run(), reused=1)


def test_tool_and_user_environment_values_remain_exact_and_consumer_local(project, monkeypatch):
    project.add_tree("first")
    monkeypatch.setenv("OWNED_TOOL_MODE", "1")
    project.module("test_environment.py", """import os
import pytest
pytestmark = pytest.mark.telescope(tools=[])
@pytest.mark.telescope(tools=['first'], env=['OWNED_TOOL_MODE'])
def test_consumer():
    assert int(os.environ['OWNED_TOOL_MODE']) > 0
def test_unrelated():
    assert 'OWNED_TOOL_MODE' not in os.environ
""")
    assert_counts(project.run(), executed=2)
    assert_counts(project.run(), reused=2)
    monkeypatch.setenv("OWNED_TOOL_MODE", "2")
    changed = project.run()
    assert_counts(changed, executed=1, reused=1)
    assert "OWNED_TOOL_MODE" not in json.dumps(changed)


def test_default_only_tool_runs_ordinary_without_preparing_a_sandbox(project, monkeypatch):
    project.definitions["unused"] = {
        "format": "frozen-tool-v1", "manifest": "tool-locks/not-present.json",
    }
    project.configure('[tool.pytest_telescope.defaults]\ntools = ["unused"]\n')
    project.module("test_ordinary.py", f"""from pathlib import Path
def test_ordinary():
    assert Path.cwd() == Path({str(project.root)!r})
""")

    def unexpected_prepare(*args, **kwargs):
        raise AssertionError("ordinary-only run must not prepare a sandbox")

    monkeypatch.setattr(sandbox, "prepare", unexpected_prepare)
    for _ in range(2):
        report = project.run()
        assert_counts(report, executed=1)
        assert report["sandbox"]["required"] is False
        assert report["units"][0]["route"] == "ordinary"


def test_cli_tool_refusal_has_exact_public_envelope_without_raw_paths(project, monkeypatch, capsys):
    source = project.add_tree("first")
    project.documents["first"]["schema"] = 999
    project.save("first")
    project.module("test_invalid.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['first'])\ndef test_invalid(): pass\n")
    monkeypatch.chdir(project.root)
    assert controller.main(["run"]) == 4
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["schema"] == 4
    assert report["kind"] == "refusal"
    assert report["status"] == "unsupported"
    assert report["capability"] == "tool:frozen-v1"
    assert report["inventory_complete"] is False
    assert str(source) not in output.out + output.err


def test_no_cache_tool_execution_does_not_publish_new_records(project):
    project.add_tree("first")
    project.module("test_value.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['first'])
def test_value():
    assert Path('/tools/first/install/value.txt').read_text() == 'first'
""")
    for _ in range(2):
        report = project.run(no_cache=True)
        assert_counts(report, executed=1)
        assert not report["groups"][0]["cache_eligible"]
    assert_counts(project.run(), executed=1)
    assert_counts(project.run(), reused=1)
