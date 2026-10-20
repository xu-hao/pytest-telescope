"""Strict cleanup eligibility is separate from current runtime execution authority."""

import copy
import json

import pytest

from pytest_telescope import python_source, runtime
from pytest_telescope.model import InfrastructureError

IMAGE = "sha256:" + "1" * 64
PROJECT = "2" * 64
PREFIX = "org.pytest-telescope."
HISTORICAL = ("docker-nonroot-ro-none-v2", "docker-nonroot-ro-none-v3")
CURRENT = "docker-nonroot-ro-none-v4"


def image_info(policy, *, profile="python-v1", size=100):
    labels = {
        PREFIX + "project": PROJECT,
        PREFIX + "policy": policy,
        PREFIX + "recipe": "3" * 64,
    }
    if policy == CURRENT:
        labels.update(
            {
                PREFIX + "profile": profile,
                PREFIX + "provenance": "4" * 64,
                PREFIX + "source-slot": python_source.SLOT_ID,
            }
        )
    return {"Id": IMAGE, "Size": size, "Config": {"Labels": labels}}


def inspector(monkeypatch, response, *, failures=0, raw=False):
    calls = []

    def command(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments == ["docker", "image", "inspect", IMAGE]
        if len(calls) <= failures:
            raise InfrastructureError("synthetic transient inspection dependency failure")
        return response if raw else json.dumps(response).encode()

    monkeypatch.setattr(runtime, "command", command)
    return calls


@pytest.mark.parametrize("policy", HISTORICAL)
@pytest.mark.parametrize("size", [0, 100, 2_147_483_649])
def test_published_minimal_labels_allow_cleanup_inspection_only(monkeypatch, policy, size):
    info = image_info(policy, size=size)
    original = copy.deepcopy(info)
    calls = inspector(monkeypatch, [info])
    retained = runtime._retained_image_info(IMAGE, PROJECT)
    assert retained == original
    assert set(retained["Config"]["Labels"]) == {
        PREFIX + "project",
        PREFIX + "policy",
        PREFIX + "recipe",
    }
    assert len(calls) == 1
    with pytest.raises(InfrastructureError):
        runtime._image_info(IMAGE, PROJECT)
    assert len(calls) == 2


@pytest.mark.parametrize("profile", ["python-v1", "git-local-v1"])
def test_known_current_profile_labels_allow_retained_inspection(monkeypatch, profile):
    info = image_info(CURRENT, profile=profile)
    calls = inspector(monkeypatch, [info])
    assert runtime._retained_image_info(IMAGE, PROJECT) == info
    assert len(calls) == 1


@pytest.mark.parametrize("policy", [*HISTORICAL, CURRENT])
def test_unrelated_extra_image_labels_do_not_change_known_ownership(monkeypatch, policy):
    info = image_info(policy)
    info["Config"]["Labels"]["example.note"] = "synthetic image metadata"
    calls = inspector(monkeypatch, [info])
    assert runtime._retained_image_info(IMAGE, PROJECT) == info
    assert len(calls) == 1


@pytest.mark.parametrize(
    "image",
    [
        None,
        1,
        True,
        "",
        "latest",
        "sha256:" + "1" * 63,
        "sha256:" + "G" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "1" * 64 + "\n",
    ],
)
def test_nonimmutable_requested_ids_refuse_without_any_command(monkeypatch, image):
    calls = inspector(monkeypatch, [image_info(CURRENT)])
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(image, PROJECT)
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        [],
        [None],
        [1],
        [[], {}],
        [{"Id": IMAGE, "Size": 1}],
        [{"Id": IMAGE, "Size": 1, "Config": None}],
        [{"Id": IMAGE, "Size": 1, "Config": []}],
        [{"Id": IMAGE, "Size": 1, "Config": {}}],
        [{"Id": IMAGE, "Size": 1, "Config": {"Labels": None}}],
        [{"Id": IMAGE, "Size": 1, "Config": {"Labels": []}}],
    ],
)
def test_malformed_inspection_shapes_are_terminal(monkeypatch, response):
    calls = inspector(monkeypatch, response)
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize("response", [b"not-json", b"\xff", b"[]\n{}", b'{"partial":'])
def test_malformed_inspection_encoding_is_not_retried(monkeypatch, response):
    calls = inspector(monkeypatch, response, raw=True)
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", None),
        ("Id", "sha256:" + "9" * 64),
        ("Size", None),
        ("Size", -1),
        ("Size", True),
        ("Size", 1.0),
        ("Size", "100"),
    ],
)
def test_image_identity_and_size_mismatch_are_terminal(monkeypatch, field, value):
    info = image_info(CURRENT)
    info[field] = value
    calls = inspector(monkeypatch, [info])
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", None),
        ("project", "9" * 64),
        ("project", True),
        ("policy", None),
        ("policy", []),
        ("policy", {}),
        ("policy", "docker-nonroot-ro-none-v1"),
        ("policy", "docker-nonroot-ro-none-v5"),
        ("policy", "foreign-policy"),
    ],
)
def test_foreign_owner_and_unknown_policies_never_acquire_cleanup_authority(
    monkeypatch, field, value
):
    info = image_info(CURRENT)
    info["Config"]["Labels"][PREFIX + field] = value
    calls = inspector(monkeypatch, [info])
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize("policy", [*HISTORICAL, CURRENT])
@pytest.mark.parametrize("recipe", [None, "", "a" * 63, "G" * 64, True])
def test_every_known_policy_requires_exact_recipe_digest(monkeypatch, policy, recipe):
    info = image_info(policy)
    info["Config"]["Labels"][PREFIX + "recipe"] = recipe
    calls = inspector(monkeypatch, [info])
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", None),
        ("profile", []),
        ("profile", {}),
        ("profile", "python-v2"),
        ("profile", "git-local-v2"),
        ("provenance", None),
        ("provenance", "4" * 63),
        ("provenance", "G" * 64),
        ("source-slot", None),
        ("source-slot", "9" * 64),
    ],
)
def test_current_policy_requires_profile_provenance_and_exact_slot(monkeypatch, field, value):
    info = image_info(CURRENT)
    info["Config"]["Labels"][PREFIX + field] = value
    calls = inspector(monkeypatch, [info])
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize("policy", [*HISTORICAL, CURRENT])
def test_read_only_inspection_retries_one_dependency_failure(monkeypatch, policy):
    info = image_info(policy)
    calls = inspector(monkeypatch, [info], failures=1)
    assert runtime._retained_image_info(IMAGE, PROJECT) == info
    assert len(calls) == 2


def test_read_only_inspection_exhausts_after_two_attempts(monkeypatch):
    calls = inspector(monkeypatch, [image_info(CURRENT)], failures=2)
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 2


def test_malformed_retry_response_is_terminal_without_third_attempt(monkeypatch):
    calls = inspector(monkeypatch, b"malformed-after-transient", failures=1, raw=True)
    with pytest.raises(InfrastructureError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert len(calls) == 2


def test_cleanup_failure_is_never_retried_as_an_inspection_outage(monkeypatch):
    calls = []

    def failed_cleanup(arguments, **kwargs):
        calls.append(arguments)
        raise runtime.CleanupError("synthetic unconfirmed dependency cleanup")

    monkeypatch.setattr(runtime, "command", failed_cleanup)
    with pytest.raises(runtime.CleanupError):
        runtime._retained_image_info(IMAGE, PROJECT)
    assert calls == [["docker", "image", "inspect", IMAGE]]


def test_current_execution_inspection_does_not_add_a_nested_retry(monkeypatch):
    calls = inspector(monkeypatch, [image_info(CURRENT)], failures=1)
    with pytest.raises(InfrastructureError):
        runtime._image_info(IMAGE, PROJECT)
    assert len(calls) == 1


@pytest.mark.parametrize("lifecycle", [None, "", "0" * 64, [], 1])
def test_pre_lifecycle_or_mismatched_v4_is_cleanup_only(monkeypatch, lifecycle):
    info = image_info(CURRENT)
    if lifecycle is not None:
        info["Config"]["Labels"][PREFIX + "lifecycle"] = lifecycle
    calls = inspector(monkeypatch, [info])
    assert runtime._retained_image_info(IMAGE, PROJECT) == info
    with pytest.raises(InfrastructureError):
        runtime._image_info(IMAGE, PROJECT)
    assert len(calls) == 2


def test_current_execution_requires_exact_shipped_lifecycle(monkeypatch):
    info = image_info(CURRENT)
    info["Config"]["Labels"][PREFIX + "lifecycle"] = runtime.lifecycle_identity()
    inspector(monkeypatch, [info])
    assert runtime._image_info(IMAGE, PROJECT) == info
    source = runtime.supervisor_source()
    monkeypatch.setattr(runtime, "supervisor_source", lambda: source + b"\n# changed lifecycle\n")
    with pytest.raises(InfrastructureError):
        runtime._image_info(IMAGE, PROJECT)
    assert runtime._retained_image_info(IMAGE, PROJECT) == info
