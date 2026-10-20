"""Owned historical cache images can be replaced without qualifying them to execute."""

import hashlib
import json
import uuid

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import InfrastructureError


@pytest.fixture
def historical_registry(tmp_path):
    token = uuid.uuid4().hex
    fixture_label = "org.pytest-telescope.fixture-owner"
    project = runtime._project(tmp_path)
    images = []
    current = []

    def create(policy):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
        (tmp_path / "test_example.py").write_text("def test_example(): assert 2 + 2 == 4\n")
        state = tmp_path / ".pytest-telescope"
        state.mkdir()
        for index in range(4):
            context = tmp_path / f"historical-{index}"
            context.mkdir()
            recipe = hashlib.sha256(f"synthetic historical recipe {index}".encode()).hexdigest()
            (context / "Dockerfile").write_text(
                "FROM scratch\n"
                f'LABEL {fixture_label}="{token}" '
                f'{runtime.LABEL}project="{project}" {runtime.LABEL}policy="{policy}" '
                f'{runtime.LABEL}recipe="{recipe}"\n'
            )
            image = runtime.command(["docker", "build", "--quiet", str(context)], timeout=60)
            images.append(image.decode().strip())
        previous = {"image": images[0], "policy": policy, "worker": "previous worker"}
        runtime._durable(state / "images.json", images)
        runtime._durable(state / "runtime.json", previous)
        return tmp_path, state, list(images), (state / "runtime.json").read_bytes(), current

    yield create
    # Fixture labels independently authorize fixture cleanup, including intentionally
    # unrecognized policies. Never use the runtime validator to bless those fixtures.
    for image in images:
        code, output = runtime._capture(["docker", "image", "inspect", image], 30, merged=True)
        if code:
            assert b"No such image" in output
            continue
        info = json.loads(output)[0]
        assert info["Id"] == image and info["Config"]["Labels"][fixture_label] == token
        runtime.command(["docker", "image", "rm", image], timeout=30)
    for image in current:
        runtime._image_info(image, project)
        runtime.command(["docker", "image", "rm", image], timeout=30)
    assert not runtime.command(
        ["docker", "images", "-q", "--filter", f"label={fixture_label}={token}"]
    ).strip()
    assert not runtime.command(
        ["docker", "ps", "-aq", "--filter", f"label={runtime.LABEL}project={project}"]
    ).strip()


@pytest.mark.parametrize("policy", ["docker-nonroot-ro-none-v2", "docker-nonroot-ro-none-v3"])
def test_explicit_upgrade_evicts_historical_owned_image_and_preserves_selected(
    historical_registry, policy
):
    root, state, old, previous, current = historical_registry(policy)
    with pytest.raises(InfrastructureError):
        runtime.identity(root)
    with pytest.raises(InfrastructureError):
        runtime._image_info(old[0], runtime._project(root))
    prepared = runtime.build_runtime(root)
    current.append(prepared["image"])
    registered = json.loads((state / "images.json").read_bytes())
    assert registered == [old[0], old[2], old[3], prepared["image"]]
    assert len(registered) == runtime.MAX_IMAGES
    assert (state / "runtime.json").read_bytes() != previous
    assert runtime.identity(root)["image"] == prepared["image"]
    fresh = controller.run(root)
    warm = controller.run(root)
    assert fresh["status"] == warm["status"] == "passed"
    assert fresh["counts"]["executed"] == warm["counts"]["reused"] == 1


@pytest.mark.parametrize("fault", ["build", "removal", "unknown-policy"])
def test_upgrade_failure_preserves_prior_state_and_stops_before_unsafe_work(
    historical_registry, monkeypatch, fault
):
    policy = "unknown-policy" if fault == "unknown-policy" else "docker-nonroot-ro-none-v3"
    root, state, old, previous, current = historical_registry(policy)
    events = []
    command, capture = runtime.command, runtime._owned_capture

    def unavailable_command(args, **kwargs):
        if args[:3] == ["docker", "image", "rm"]:
            events.append("remove")
            if fault == "removal":
                raise InfrastructureError("controlled removal response unavailable")
        return command(args, **kwargs)

    def unavailable_build(directory, data, args, *rest, **kwargs):
        if args[:2] == ["docker", "build"]:
            events.append("build")
            if fault == "build":
                return 1, b"controlled dependency outage"
        return capture(directory, data, args, *rest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "command", unavailable_command)
        patch.setattr(runtime, "_owned_capture", unavailable_build)
        expected = runtime.CleanupError if fault == "removal" else InfrastructureError
        with pytest.raises(expected):
            runtime.build_runtime(root)
    assert (state / "runtime.json").read_bytes() == previous
    registered = json.loads((state / "images.json").read_bytes())
    assert old[0] in registered
    assert not list((state / "scratch").iterdir())
    if fault == "build":
        assert events == ["remove", "build"]
        assert registered == [old[0], old[2], old[3]]
        # A later explicit request after the dependency recovers can complete.
        prepared = runtime.build_runtime(root)
        current.append(prepared["image"])
        assert runtime.identity(root)["image"] == prepared["image"]
    else:
        assert events == (["remove"] if fault == "removal" else [])
        assert registered == old


@pytest.mark.parametrize(
    "registry",
    [
        ["sha256:" + "a" * 64] * 2,
        [{"image": "sha256:" + "a" * 64}],
        ["a mutable image tag"],
        ["sha256:" + str(index) * 64 for index in range(5)],
    ],
)
def test_invalid_retained_registry_refuses_before_backend_mutation(tmp_path, monkeypatch, registry):
    state = tmp_path / ".pytest-telescope"
    state.mkdir()
    runtime._durable(state / "images.json", registry)
    before = (state / "images.json").read_bytes()
    monkeypatch.setattr(runtime, "_local_endpoint", lambda: "unix:///synthetic.sock")
    monkeypatch.setattr(runtime, "recover", lambda root: None)

    def unexpected(*args, **kwargs):
        raise AssertionError("invalid registry reached a backend mutation or image inspection")

    monkeypatch.setattr(runtime, "command", unexpected)
    monkeypatch.setattr(runtime, "_owned_capture", unexpected)
    with pytest.raises(InfrastructureError, match="registry"):
        runtime.build_runtime(tmp_path)
    assert (state / "images.json").read_bytes() == before
    assert not (state / "runtime.json").exists()
