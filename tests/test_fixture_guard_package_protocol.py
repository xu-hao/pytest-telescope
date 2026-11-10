"""Strict package graph controls using actual Bubblewrap worker records."""

import json
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import fingerprint


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def different(value):
    return ("1" if value[0] == "0" else "0") + value[1:]


def project(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["tests"]\n'
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )
    put(
        root,
        "tests/pkg/__init__.py",
        """state = []
def setup_module(): state.append("started")
def teardown_module(): assert state == ["started"]
""",
    )
    put(
        root,
        "tests/pkg/test_item.py",
        """import pytest
from pkg import state
pytestmark = pytest.mark.telescope(inputs=["tests/pkg/__init__.py"])
def test_item(): assert state == ["started"]
""",
    )


def recommit(data, mode):
    if mode == "guard-capture":
        data["content_id"] = fingerprint(
            {
                "protocol": "fixture-content-v1",
                "registry_id": data["registry_id"],
                "case_ids": data["case_ids"],
                "items": data["items"],
            }
        )
    else:
        data["graph_id"] = fingerprint(
            {
                "protocol": data["protocol"],
                "context_id": data["context_id"],
                "group_id": data["group_id"],
                "case_ids": data["case_ids"],
                "registry_id": data["registry_id"],
                "items": data["items"],
            }
        )


@pytest.mark.parametrize(
    "mode,fault",
    [
        ("guard-capture", "missing"),
        ("guard-capture", "lifecycle-recommitted"),
        ("guard-inspection", "missing"),
        ("guard-inspection", "extra-field"),
        ("guard-inspection", "malformed-lifecycle"),
        ("guard-inspection", "malformed-owner"),
        ("guard-inspection", "duplicate-owner"),
        ("guard-inspection", "owner-recommitted"),
        ("guard-inspection", "lifecycle-recommitted"),
        ("guard-inspection", "graph-mismatch"),
    ],
)
def test_package_graph_corruption_fails_closed_before_body(tmp_path, monkeypatch, mode, fault):
    project(tmp_path)
    original = sandbox.capture
    events = []

    def alter(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request_path = Path(argv[argv.index(sandbox.REQUEST) - 1])
        request = json.loads(request_path.read_text())
        events.append(request["mode"])
        if request["mode"] != mode:
            return code, output
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines()
        indices = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        assert len(indices) == 1
        index = indices[0]
        data = json.loads(lines[index][len(prefix) :])
        item = data["items"][0]
        assert len(item["packages"]) == 1
        package = item["packages"][0]
        assert package["lifecycle"][0] is not None and package["lifecycle"][1] is not None
        if fault == "missing":
            del item["packages"]
        elif fault == "extra-field":
            package["unexpected"] = True
        elif fault == "malformed-lifecycle":
            package["lifecycle"] = [package["lifecycle"][0]]
        elif fault == "malformed-owner":
            package["owner_id"] = "invalid"
        elif fault == "duplicate-owner":
            item["packages"].append(dict(package))
        elif fault == "owner-recommitted":
            package["owner_id"] = different(package["owner_id"])
            recommit(data, mode)
        elif fault == "lifecycle-recommitted":
            package["lifecycle"][0] = different(package["lifecycle"][0])
            recommit(data, mode)
        else:
            data["graph_id"] = different(data["graph_id"])
        lines[index] = prefix + json.dumps(data).encode()
        return code, b"\n".join(lines) + b"\n"

    monkeypatch.setattr(sandbox, "capture", alter)
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["schema"] == 5 and report["status"] == "infrastructure_error"
    assert report["fixture_guard"] == {
        "profile": "available-v1",
        "state": "unavailable",
        "conditions": [],
    }
    assert mode in events and "guard-batch" not in events
    assert not any(group["cache_eligible"] for group in report["groups"])
    for path in (tmp_path / ".pytest-telescope/cache").glob("*.json"):
        assert json.loads(path.read_text())["schema"] != 8
