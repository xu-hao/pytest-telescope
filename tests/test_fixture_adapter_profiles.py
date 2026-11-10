"""Core controls for exact installed adapter admission and unsupported-version refusal."""

from importlib.metadata import version

import pytest
from test_fixture_guard_phases import distinct_groups

from pytest_telescope import controller, sandbox
from pytest_telescope.fixture_guard import framework_definition_ids
from pytest_telescope.model import UnsupportedCapability


def test_exact_invoking_pytest_adapter_admission_or_refusal():
    actual = version("pytest")
    if actual in {"9.1.1", "9.2.0"}:
        identities = framework_definition_ids(actual)
        assert isinstance(identities, frozenset) and len(identities) == 5
        assert all(len(identity) == 64 for identity in identities)
    else:
        with pytest.raises(UnsupportedCapability) as refusal:
            framework_definition_ids(actual)
        assert refusal.value.capability == "execution:fixture-version"


def test_unsupported_adapter_refuses_before_preparation(tmp_path, monkeypatch):
    distinct_groups(tmp_path)
    actual_version = controller.version

    def metadata_double(name):
        return "8.3.5" if name == "pytest" else actual_version(name)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported adapter must refuse before sandbox preparation or dispatch")

    monkeypatch.setattr(controller, "version", metadata_double)
    monkeypatch.setattr(sandbox, "prepare", forbidden)
    monkeypatch.setattr(sandbox, "execute", forbidden)
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-version"
    assert not (tmp_path / ".pytest-telescope/cache").exists()
