"""Malformed dependency responses refuse with bounded typed infrastructure evidence."""

import json

import pytest

from pytest_telescope import runtime
from pytest_telescope.model import InfrastructureError


@pytest.mark.parametrize("labels", [None, [], "invalid"])
def test_nullable_or_non_table_image_labels_are_typed_failures(tmp_path, monkeypatch, labels):
    image = "sha256:" + "a" * 64
    response = [{"Id": image, "Config": {"Labels": labels}, "Size": 1}]
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args == ["docker", "image", "inspect", image]
        return json.dumps(response).encode()

    monkeypatch.setattr(runtime, "command", command)
    with pytest.raises(InfrastructureError):
        runtime._image_info(image, runtime._project(tmp_path))
    assert len(calls) == 1


def test_non_utf8_platform_response_is_typed_and_bounded(tmp_path, monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args == ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]
        return b"\xff"

    monkeypatch.setattr(runtime, "command", command)
    with pytest.raises(InfrastructureError):
        runtime.inspect_runtime(tmp_path, runtime_profile="git-local-v1")
    assert len(calls) == 2
    assert not (tmp_path / ".pytest-telescope").exists()


def test_real_foreign_base_image_is_refused_without_removal_or_state_writes(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    prepared = runtime.build_runtime(tmp_path)
    state = tmp_path / ".pytest-telescope/runtime.json"
    original = state.read_bytes()
    base = json.loads(runtime.command(["docker", "image", "inspect", runtime.BASE]))[0]
    altered = {**prepared, "image": base["Id"]}
    state.write_text(json.dumps(altered))
    before = state.read_bytes()
    try:
        with pytest.raises(InfrastructureError):
            runtime.inspect_runtime(tmp_path)
        assert state.read_bytes() == before
        still_present = json.loads(runtime.command(["docker", "image", "inspect", base["Id"]]))[0]
        assert still_present["Id"] == base["Id"]
        assert still_present["RootFS"] == base["RootFS"]
    finally:
        state.write_bytes(original)
        runtime._image_info(prepared["image"], runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", prepared["image"]], timeout=30)
