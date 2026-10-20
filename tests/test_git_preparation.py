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
                    runtime.LABEL + "source-slot": runtime.python_source.SLOT_ID,
                    runtime.LABEL + "lifecycle": runtime.lifecycle_identity(),
                }
            },
        }

    monkeypatch.setattr(runtime, "_image_info", image_info)
    monkeypatch.setattr(runtime, "_retained_image_info", image_info)

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
        if len(calls) == 1:
            return 1, b"temporary failure"
        (args[0] / "build/image.id").write_text(new)
        return 0, b"image exported"

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
        (args[0] / "build/image.id").write_text("sha256:" + "b" * 64)
        return 0, b"image exported"

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
        if len(builds) == 1:
            return 1, b"temporary build outage"
        (directory / "build/image.id").write_text("sha256:" + "a" * 64)
        return 0, b"image exported"

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


def test_real_oldest_runtime_remains_usable_after_quota_preparation_failure(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    (tmp_path / "test_example.py").write_text("def test_example(): pass\n")
    state = tmp_path / ".pytest-telescope"
    source = runtime.worker_source()
    try:
        previous = runtime.build_runtime(tmp_path)
        assert controller.run(tmp_path)["status"] == "passed"
        original = (state / "runtime.json").read_bytes()
        expected = runtime.inspect_runtime(tmp_path)
        for revision in range(3):
            with monkeypatch.context() as scope:
                scope.setattr(
                    runtime,
                    "worker_source",
                    lambda: source + f"\n# synthetic revision {revision}\n".encode(),
                )
                runtime.build_runtime(tmp_path)
        (state / "runtime.json").write_bytes(original)
        assert json.loads((state / "images.json").read_text())[0] == previous["image"]
        with monkeypatch.context() as scope:
            scope.setattr(
                runtime, "_owned_capture", lambda *a, **k: (1, b"controlled build outage")
            )
            with pytest.raises(InfrastructureError):
                runtime.build_runtime(tmp_path)
        assert (state / "runtime.json").read_bytes() == original
        assert runtime.inspect_runtime(tmp_path) == expected
        assert controller.run(tmp_path)["counts"]["reused"] == 1
    finally:
        if (state / "images.json").exists():
            for image in json.loads((state / "images.json").read_text()):
                runtime._image_info(image, runtime._project(tmp_path))
                runtime.command(["docker", "image", "rm", image], timeout=30)


@pytest.mark.parametrize(
    "fault", ["malformed-archive", "missing-projection", "missing-native-file"]
)
def test_fixed_payload_integrity_failure_is_terminal_in_real_builder(tmp_path, monkeypatch, fault):
    import io
    import tarfile

    calls = []
    if fault != "missing-native-file":
        payload = b"inert malformed Debian archive"
        if fault == "missing-projection":
            # Valid inert Debian container with an empty data archive: no commands/hooks.
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz"):
                pass
            control = io.BytesIO()
            with tarfile.open(fileobj=control, mode="w:gz") as archive:
                content = b"Package: example-inert\nVersion: 1\nArchitecture: amd64\nMaintainer: Example <example@example.invalid>\nDescription: inert test\n"
                info = tarfile.TarInfo("./control")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

            def ar(name, contents):
                header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(contents):<10}`\n".encode()
                return header + contents + (b"\n" if len(contents) % 2 else b"")

            payload = (
                b"!<arch>\n"
                + ar("debian-binary", b"2.0\n")
                + ar("control.tar.gz", control.getvalue())
                + ar("data.tar.gz", data.getvalue())
            )

        def artifact(context, capture, **kwargs):
            (context / "git.deb").write_bytes(payload)
            return payload

        monkeypatch.setattr(profiles, "fetch_archive", artifact)
    else:
        monkeypatch.setitem(
            profiles.MANIFEST,
            "native_files_sha256",
            profiles.MANIFEST["native_files_sha256"]
            | {"/opt/telescope/git/missing-native-file": "0" * 64},
        )
    original = runtime._owned_capture

    def capture(directory, data, args, timeout, **kwargs):
        if args[:2] == ["docker", "build"]:
            calls.append(1)
        return original(directory, data, args, timeout, **kwargs)

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    with pytest.raises(InfrastructureError, match="integrity"):
        runtime.build_runtime(tmp_path, runtime_profile=profiles.GIT)
    assert len(calls) == 1
    assert not (tmp_path / ".pytest-telescope/runtime.json").exists()
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


@pytest.mark.parametrize(
    "output,expected",
    [
        (b"TELESCOPE_GIT_PROFILE_INTEGRITY\n", True),
        (b"#7 0.123 TELESCOPE_GIT_PROFILE_INTEGRITY\n", True),
        (b"RUN echo TELESCOPE_GIT_PROFILE_INTEGRITY; exit 72\n", False),
        (b"dependency outage\n", False),
    ],
)
def test_integrity_classifier_distinguishes_emitted_marker_from_command_rendering(output, expected):
    assert profiles.integrity_failed(output) is expected
