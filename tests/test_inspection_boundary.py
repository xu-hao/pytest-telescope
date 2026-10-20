"""Malformed dependency responses refuse with bounded typed infrastructure evidence."""

import json
import uuid

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


def test_real_foreign_image_is_refused_without_removal_or_state_writes(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    owner = uuid.uuid4().hex
    label = "org.pytest-telescope.fixture-owner"
    context = tmp_path / "foreign-image"
    context.mkdir()
    (context / "inert.txt").write_text("owned inspection fixture\n")
    (context / "Dockerfile").write_text(
        f'FROM scratch\nLABEL {label}="{owner}"\nCOPY inert.txt /inert.txt\n'
    )
    image_id = tmp_path / "foreign-image.id"
    prepared = None
    state = tmp_path / ".pytest-telescope/runtime.json"
    original = None
    try:
        runtime.command(["docker", "build", "--iidfile", str(image_id), str(context)], timeout=60)
        foreign = image_id.read_text().strip()
        before_image = json.loads(runtime.command(["docker", "image", "inspect", foreign]))
        assert before_image[0]["Id"] == foreign
        assert before_image[0]["Config"]["Labels"][label] == owner
        prepared = runtime.build_runtime(tmp_path)
        original = state.read_bytes()
        state.write_text(json.dumps({**prepared, "image": foreign}))
        before_state = state.read_bytes()
        with pytest.raises(InfrastructureError):
            runtime.inspect_runtime(tmp_path)
        assert state.read_bytes() == before_state
        after_image = json.loads(runtime.command(["docker", "image", "inspect", foreign]))
        assert after_image == before_image
    finally:
        try:
            if original is not None:
                state.write_bytes(original)
            if prepared is not None:
                runtime._image_info(prepared["image"], runtime._project(tmp_path))
                runtime.command(["docker", "image", "rm", prepared["image"]], timeout=30)
                remaining = runtime.command(["docker", "image", "ls", "--quiet", "--no-trunc"])
                assert prepared["image"] not in remaining.decode().splitlines()
        finally:
            if image_id.exists():
                foreign = image_id.read_text().strip()
                info = json.loads(runtime.command(["docker", "image", "inspect", foreign]))[0]
                assert info["Id"] == foreign
                assert info["Config"]["Labels"][label] == owner
                runtime.command(["docker", "image", "rm", foreign], timeout=30)
                assert not runtime.command(
                    ["docker", "image", "ls", "--quiet", "--filter", f"label={label}={owner}"]
                ).strip()
