"""Portable filesystem evidence for declared empty closed loader directories."""

import json
import os

from frozen_tool_configuration_fixtures import StaticConfiguration, direct_static_read

from pytest_telescope import sandbox


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


def test_private_mountpoints_preserve_owned_baseline_siblings_and_readonly_parent(
    tmp_path, monkeypatch
):
    configured = StaticConfiguration(tmp_path, monkeypatch)
    baseline = tmp_path / "owned-baseline"
    (baseline / "data").mkdir(parents=True)
    payload = baseline / "data/value.txt"
    payload.write_bytes(b"owned baseline sibling\n")
    alias = baseline / "literal-alias"
    alias.symlink_to("./data/value.txt")
    baseline.chmod(0o777)
    destination = baseline / "missing/loader"
    assert not destination.exists()
    # An explicitly owned system-root stand-in exercises the shared planner;
    # no host directory inventory, installed program or new runtime is selected.
    monkeypatch.setattr(sandbox, "SYSTEM_DIRECTORIES", (str(baseline), "/usr", "/etc"))
    configured.document["loader"] = {
        "profile": "linux-x86_64-glibc-v1",
        "directories": [str(destination)],
        "cache": None,
    }
    configured.save()
    with configured.capture() as capture:
        view = capture.view(["native"])
        assert str(destination) in view.closed
        for path in (payload, alias):
            assert direct_static_read(configured.box, view, str(path)) == (
                0,
                b"owned baseline sibling\n",
            )
        actual_capture = sandbox.capture
        attempted = baseline / "write-must-refuse"

        def write_mountpoint(argv, *args, **kwargs):
            # Bwrap's own bounded mkdir attempts a write after the readonly
            # scaffolding and closed binds, before the unchanged static reader.
            split = argv.index("--")
            return actual_capture(
                [*argv[:split], "--dir", str(attempted), *argv[split:]], *args, **kwargs
            )

        with monkeypatch.context() as write_attempt:
            write_attempt.setattr(sandbox, "capture", write_mountpoint)
            code, output = direct_static_read(configured.box, view, str(payload))
        assert code != 0 and b"Read-only file system" in output
        assert not attempted.exists()
    assert payload.read_bytes() == b"owned baseline sibling\n"
    assert os.readlink(alias) == "./data/value.txt"
    assert not destination.exists()
