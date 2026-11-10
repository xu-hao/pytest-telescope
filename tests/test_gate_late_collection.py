"""Actual late collection changes refuse before bodies; candidate provenance is doubled."""

import json

import pytest
from test_gate_profiles import gates as gates
from test_gate_profiles import profile_session as profile_session


@pytest.mark.parametrize("profile", ["core", "all"])
def test_complete_collection_profile_has_exact_receipt_and_runs_selected_bodies(
    profile_session, profile
):
    value = profile_session
    assert value.run(profile) == 0
    core, guarded = "tests/test_profile.py::test_core", "tests/test_profile.py::test_guard"
    assert json.loads(value.membership.read_text()) == {
        "schema": 1,
        "profile": profile,
        "pytest_version": "9.2.0",
        "selected": [core] if profile == "core" else [core, guarded],
        "deselected": [guarded] if profile == "core" else [],
    }
    assert (value.root / "core-ran").exists()
    assert (value.root / "guard-ran").exists() == (profile == "all")


@pytest.mark.parametrize("profile", ["core", "all"])
@pytest.mark.parametrize("phase", ["finish-last", "finish-wrapper", "modify-wrapper"])
def test_late_collection_mutation_refuses_before_every_body_and_membership_receipt(
    profile_session, profile, phase
):
    value = profile_session
    target = "test_core" if profile == "core" else "test_guard"
    changed = value.root / "late-change-observed"
    source = "from pathlib import Path\nimport pytest\n"
    if phase == "finish-last":
        source += (
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_collection_finish(session):\n"
            "    items, config = session.items, session.config\n"
        )
    elif phase == "finish-wrapper":
        source += (
            "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
            "def pytest_collection_finish(session):\n"
            "    yield\n"
            "    items, config = session.items, session.config\n"
        )
    else:
        source += (
            "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    yield\n"
        )
    source += (
        f"    removed = [item for item in items if item.name == {target!r}]\n"
        "    assert len(removed) == 1\n"
        "    items[:] = [item for item in items if item not in removed]\n"
        "    config.hook.pytest_deselected(items=removed)\n"
        f"    Path({str(changed)!r}).touch()\n"
    )
    (value.root / "tests/conftest.py").write_text(source)
    status = value.run(profile)
    observed = {
        "phase": phase,
        "profile": profile,
        "status": status,
        "mutation_ran": changed.exists(),
        "core_ran": (value.root / "core-ran").exists(),
        "guard_ran": (value.root / "guard-ran").exists(),
        "receipt_exists": value.membership.exists(),
    }
    print("LATE_COLLECTION_OBSERVATION " + json.dumps(observed, sort_keys=True))
    assert observed["mutation_ran"], observed
    assert status == pytest.ExitCode.USAGE_ERROR, observed
    assert not observed["core_ran"] and not observed["guard_ran"], observed
    assert not observed["receipt_exists"], observed
