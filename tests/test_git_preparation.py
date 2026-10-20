"""Bounded preparation and immutable profile evidence, without live dependencies."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope import runtime_profiles as profiles
from pytest_telescope.model import InfrastructureError

FETCH_ARCHIVE = profiles.fetch_archive


def test_archive_verified_before_extraction_and_network_retried_once(tmp_path, monkeypatch):
    payload = b"synthetic inert artifact"
    monkeypatch.setitem(
        profiles.MANIFEST,
        "archive",
        {
            "url": "https://example.invalid/fixed.deb",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    attempts = []

    def capture(args, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            return 71, b""
        (tmp_path / "git.deb").write_bytes(payload)
        return 0, b""

    profiles.fetch_archive(tmp_path, capture)
    assert attempts == [30, 30]
    assert (tmp_path / "git.deb").read_bytes() == payload


@pytest.mark.parametrize("failure", ["network", "integrity", "false-success", "cleanup"])
def test_archive_failure_is_bounded_and_never_accepted(tmp_path, failure):
    calls = []

    def capture(args, timeout):
        calls.append(timeout)
        if failure == "cleanup":
            raise runtime.CleanupError("owned cleanup unconfirmed")
        if failure == "false-success":
            (tmp_path / "git.deb").write_bytes(b"wrong artifact")
            return 0, b""
        return (71 if failure == "network" else 72), b""

    with pytest.raises(InfrastructureError):
        profiles.fetch_archive(tmp_path, capture, terminal=runtime.CleanupError)
    assert len(calls) == (2 if failure == "network" else 1)


def test_installed_manifest_binds_profile_and_runner_identity(monkeypatch, tmp_path):
    installed = Path(profiles.__file__).with_name("git_local_v1.json")
    assert json.loads(installed.read_bytes()) == profiles.MANIFEST
    before = profiles.provenance(profiles.GIT)
    monkeypatch.setitem(profiles.MANIFEST, "projection", "synthetic revision")
    assert profiles.provenance(profiles.GIT) != before
    # Exercise the recorder on an isolated installed-package-shaped directory.
    (tmp_path / "controller.py").write_text("# synthetic implementation\n")
    manifest = tmp_path / "git_local_v1.json"
    manifest.write_text('{"revision":1}')
    monkeypatch.setattr(controller, "__file__", str(tmp_path / "controller.py"))
    first = controller.runner_identity()
    manifest.write_text('{"revision":2}')
    assert controller.runner_identity() != first


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    state = tmp_path / ".pytest-telescope"
    state.mkdir()
    old = ["sha256:" + str(i) * 64 for i in range(1, 5)]
    previous = {"image": old[0], "profile": "python-v1", "opaque": "preserve exact previous state"}
    runtime._durable(state / "runtime.json", previous)
    runtime._durable(state / "images.json", old)
    events = []
    monkeypatch.setattr(runtime, "recover", lambda root: None)
    monkeypatch.setattr(runtime, "_local_endpoint", lambda: "unix:///synthetic.sock")
    monkeypatch.setattr(profiles, "fetch_archive", lambda *a, **k: b"verified inert archive")
    monkeypatch.setattr(profiles, "dockerfile", lambda *a: (f"FROM {runtime.BASE}\n", ""))
    monkeypatch.setattr(runtime, "_journal", lambda *a: {})

    def command(args, **kwargs):
        if args[1] == "version":
            return b"linux/amd64\n"
        assert args[:3] == ["docker", "image", "rm"]
        events.append(("remove", args[3]))
        return b""

    monkeypatch.setattr(runtime, "command", command)

    def image_info(image, project, recipe=None):
        return {
            "Size": 100,
            "Config": {
                "Labels": {
                    runtime.LABEL + "profile": "git-local-v1",
                    runtime.LABEL + "provenance": profiles.provenance(profiles.GIT),
                }
            },
        }

    monkeypatch.setattr(runtime, "_image_info", image_info)

    @contextmanager
    def invocation(root):
        directory = tmp_path / f"attempt-{sum(event[0] == 'enter' for event in events)}"
        directory.mkdir()
        events.append(("enter", str(directory)))
        try:
            yield directory
        finally:
            events.append(("cleanup", str(directory)))

    monkeypatch.setattr(runtime, "invocation", invocation)
    return tmp_path, state, old, previous, events


def test_failed_preparation_at_quota_preserves_oldest_selected_state(preparation, monkeypatch):
    root, state, old, previous, events = preparation

    def capture(*args, **kwargs):
        events.append(("build", None))
        return 1, b"synthetic dependency outage"

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    with pytest.raises(InfrastructureError, match="exhausted"):
        runtime.build_runtime(root, runtime_profile=profiles.GIT)
    assert json.loads((state / "runtime.json").read_text()) == previous
    assert json.loads((state / "images.json").read_text()) == [old[0], old[2], old[3]]
    assert events[0] == ("remove", old[1])
    assert [event[0] for event in events[1:]] == [
        "enter",
        "build",
        "cleanup",
        "enter",
        "build",
        "cleanup",
    ]


def test_build_retry_commits_only_after_cleanup(preparation, monkeypatch):
    root, state, old, previous, events = preparation
    new = "sha256:" + "a" * 64
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        assert json.loads((state / "runtime.json").read_text()) == previous
        return (1, b"temporary failure") if len(calls) == 1 else (0, new.encode() + b"\n")

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    result = runtime.build_runtime(root, runtime_profile=profiles.GIT)
    assert len(calls) == 2 and events[-1][0] == "cleanup"
    assert result["image"] == new and result["profile"] == profiles.GIT
    assert json.loads((state / "runtime.json").read_text()) == result
    assert old[0] in json.loads((state / "images.json").read_text())


@pytest.mark.parametrize("failure", ["integrity", "cleanup", "inspection"])
def test_terminal_preparation_never_replaces_prior_state(preparation, monkeypatch, failure):
    root, state, old, previous, events = preparation
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        if failure == "cleanup":
            raise runtime.CleanupError("cannot confirm cleanup")
        if failure == "integrity":
            return 1, profiles.INTEGRITY.encode()
        return 0, ("sha256:" + "b" * 64).encode()

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    if failure == "inspection":
        original = runtime._image_info

        def inspect(image, *args):
            if image not in old:
                raise InfrastructureError("inspection unavailable")
            return original(image, *args)

        monkeypatch.setattr(runtime, "_image_info", inspect)
    with pytest.raises(InfrastructureError):
        runtime.build_runtime(root, runtime_profile=profiles.GIT)
    assert len(calls) == 1
    assert json.loads((state / "runtime.json").read_text()) == previous


def test_total_download_budget_survives_build_retry(preparation, monkeypatch):
    root, state, old, previous, events = preparation
    payload = b"verified synthetic native archive"
    monkeypatch.setitem(
        profiles.MANIFEST,
        "archive",
        {
            "url": "https://example.invalid/pinned.deb",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    # Restore the real bounded retrieval adapter after fixture isolation.
    monkeypatch.setattr(profiles, "fetch_archive", FETCH_ARCHIVE)
    monkeypatch.setattr(profiles, "dockerfile", lambda *a: (f"FROM {runtime.BASE}\n", ""))
    downloads, builds = [], []

    def capture(directory, data, args, timeout, **kwargs):
        if args[0] != "docker":
            downloads.append(1)
            if len(downloads) == 1:
                return 71, b""
            (directory / "build/git.deb").write_bytes(payload)
            return 0, b""
        builds.append(1)
        assert (directory / "build/git.deb").read_bytes() == payload
        return (
            (1, b"temporary build outage")
            if len(builds) == 1
            else (0, ("sha256:" + "a" * 64).encode())
        )

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    runtime.build_runtime(root, runtime_profile=profiles.GIT)
    assert len(downloads) == len(builds) == 2


def test_readonly_inspection_retries_once_without_preparation_or_state_mutation(
    tmp_path, monkeypatch
):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope.runtime]\nprofile="git-local-v1"\nrequires=["git-local"]\n'
    )
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    calls = []

    def identity(root, *, runtime_profile=None):
        calls.append(runtime_profile)
        if len(calls) == 1:
            raise InfrastructureError("transient backend inspection failure")
        return {"execution": {"immutable": "synthetic identity"}}

    def forbidden(*a, **k):
        pytest.fail("read-only inspection reached a mutating or execution operation")

    monkeypatch.setattr(runtime, "identity", identity)
    for name in ("build_runtime", "recover", "execute", "_state", "_durable"):
        monkeypatch.setattr(runtime, name, forbidden)
    result = runtime.inspect_runtime(tmp_path, required_capabilities=["git-local", "git-local"])
    assert result == {
        "profile": "git-local-v1",
        "required_capabilities": ["git-local"],
        "identity": controller.fingerprint({"immutable": "synthetic identity"}),
    }
    assert calls == ["git-local-v1"] * 2
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_inspection_exhaustion_and_typed_mismatch_do_not_multiply_retries(tmp_path, monkeypatch):
    calls = []

    def failing(*a, **k):
        calls.append(1)
        raise InfrastructureError("dependency unavailable")

    monkeypatch.setattr(runtime, "identity", failing)
    with pytest.raises(InfrastructureError):
        runtime.inspect_runtime(tmp_path)
    assert len(calls) == 2
    from pytest_telescope.model import UnsupportedCapability

    with pytest.raises(UnsupportedCapability):
        runtime.inspect_runtime(tmp_path, required_capabilities=["git-local"])
    assert len(calls) == 2
