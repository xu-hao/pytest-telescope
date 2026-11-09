"""Actual worker boundaries after a collection union and at final mount dispatch."""

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from pytest_telescope import sandbox

from frozen_tool_fixtures import ToolProject, assert_counts, cache_bytes, put
from frozen_tool_program_fixture import capture_evidence
from frozen_tool_worker_fixture import WorkerConfiguration


@pytest.fixture(autouse=True)
def evidence(tmp_path, monkeypatch):
    with capture_evidence(monkeypatch, tmp_path):
        yield


def test_two_native_consumers_do_not_inherit_discovery_union_configuration(tmp_path, monkeypatch):
    worker = WorkerConfiguration(tmp_path, monkeypatch)
    project = worker.project
    for name in ("first", "second"):
        put(worker.config, name + ".txt", name + " selected\n", 0o640)
        document = copy.deepcopy(worker.document)
        config = next(root for root in document["roots"] if root["id"] == "config")
        config["entries"] = [row for row in config["entries"] if row["path"] != "selected.txt"]
        config["entries"].append(worker.row(worker.config / (name + ".txt"), name + ".txt"))
        project.documents[name] = document
        project.definitions[name] = {"format": "frozen-tool-v1", "manifest": "tool-locks/" + name + ".json"}
        project.save(name)
    project.configure()
    project.module("test_union.py", """from pathlib import Path
import pytest
@pytest.mark.telescope(tools=['first'])
def test_first():
    assert Path('/etc/first.txt').read_bytes() == b'first selected\\n'
    assert not Path('/etc/second.txt').exists()
    assert not Path('/tools/second').exists()
@pytest.mark.telescope(tools=['second'])
def test_second():
    assert Path('/etc/second.txt').read_bytes() == b'second selected\\n'
    assert not Path('/etc/first.txt').exists()
    assert not Path('/tools/first').exists()
""")
    assert_counts(project.run(timeout=120), executed=2)
    assert_counts(project.run(timeout=120), reused=2)


def test_final_environment_bind_mutation_refuses_before_child_launch(tmp_path, monkeypatch):
    worker = WorkerConfiguration(tmp_path, monkeypatch)
    worker.project.module("test_control.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['worker'])\ndef test_control(): pass\n")
    assert_counts(worker.project.run(timeout=60), executed=1)
    previous = cache_bytes(worker.project.root)
    worker.project.module("test_new.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['worker'])\ndef test_new(): pass\n")
    source = tmp_path / "owned-late-overlay"
    source.mkdir()
    put(source, "value", "unselected late input")
    execute, capture = sandbox.execute, sandbox.capture
    attempted, children = [], []

    def changed(box, *args, **kwargs):
        if kwargs.get("tool_view") is not None:
            attempted.append(kwargs["request"]["mode"])
            box = replace(box, binds=(*box.binds, (str(source), "/etc/late-overlay")))
        return execute(box, *args, **kwargs)

    def observed(argv, *args, **kwargs):
        if argv and Path(argv[0]).name == "bwrap" and argv[-1] == sandbox.WORKER_PATH:
            children.append(tuple(argv))
        return capture(argv, *args, **kwargs)

    monkeypatch.setattr(sandbox, "execute", changed)
    monkeypatch.setattr(sandbox, "capture", observed)
    report = worker.project.run(timeout=60)
    assert report["status"] == "infrastructure_error", report
    assert attempted and not children
    assert cache_bytes(worker.project.root) == previous


def test_data_only_consumer_retains_owned_baseline_environment_bind(tmp_path, monkeypatch):
    project = ToolProject(tmp_path / "project", tmp_path / "installed")
    project.add_tree("data")
    baseline = tmp_path / "owned-baseline"
    baseline.mkdir()
    put(baseline, "value.txt", "baseline unchanged\n")
    prepare = sandbox.prepare

    def with_owned_baseline(*args, **kwargs):
        box = prepare(*args, **kwargs)
        return replace(box, binds=(*box.binds, (str(baseline), "/owned-baseline")))

    monkeypatch.setattr(sandbox, "prepare", with_owned_baseline)
    project.module("test_data.py", """from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(tools=['data'])
def test_data():
    assert Path('/owned-baseline/value.txt').read_bytes() == b'baseline unchanged\\n'
    assert Path('/tools/data/install/value.txt').read_bytes() == b'data'
""")
    assert_counts(project.run(), executed=1)
    assert_counts(project.run(), reused=1)


def test_ordinary_native_default_stays_fresh_without_tool_capture(tmp_path, monkeypatch):
    worker = WorkerConfiguration(tmp_path, monkeypatch)
    baseline = tmp_path / "owned-ordinary-value"
    baseline.write_text("first")
    worker.project.configure('[tool.pytest_telescope.defaults]\ntools = ["worker"]\n')
    worker.project.module("test_ordinary.py", f"""from pathlib import Path
def test_ordinary():
    assert Path({str(baseline)!r}).read_text() in ('first', 'second')
    assert Path.cwd() == Path({str(worker.project.root)!r})
""")

    def no_sandbox(*args, **kwargs):
        raise AssertionError("an ordinary-only native default must not prepare a sandbox")

    monkeypatch.setattr(sandbox, "prepare", no_sandbox)
    for value in ("first", "second"):
        baseline.write_text(value)
        report = worker.project.run()
        assert_counts(report, executed=1)
        assert report["sandbox"]["required"] is False
        assert report["units"][0]["route"] == "ordinary"
