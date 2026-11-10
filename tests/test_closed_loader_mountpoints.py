"""Portable filesystem evidence for declared empty closed loader directories."""

import json
import os

from frozen_tool_configuration_fixtures import StaticConfiguration, direct_static_read


def test_absent_closed_loader_directory_under_readonly_system_parent(tmp_path, monkeypatch):
    configured = StaticConfiguration(tmp_path, monkeypatch)
    destination = "/usr/local/lib/pytest-telescope-owned-absent-mount"
    assert not os.path.lexists(destination)
    with configured.capture() as capture:
        baseline = direct_static_read(configured.box, capture.view(["native"]), "/etc/selected.txt")
        assert baseline == (0, b"declared configuration\n")
    configured.document["loader"] = {
        "profile": "linux-x86_64-glibc-v1",
        "directories": [destination],
        "cache": None,
    }
    configured.save()
    with configured.capture() as capture:
        view = capture.view(["native"])
        assert destination in view.closed
        projected = next(source for source, target in view.bindings if target == destination)
        assert projected.is_dir() and list(projected.iterdir()) == []
        result = direct_static_read(configured.box, view, "/etc/selected.txt")
        print(
            json.dumps({"baseline": baseline[0], "result": result[0], "output": result[1].decode()})
        )
        assert result == baseline
    assert not os.path.lexists(destination)
