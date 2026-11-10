"""Gate profile behavior with explicit provenance/process doubles and tiny pytest cases."""

import importlib.metadata
import json
import sys
import types
from pathlib import Path

import pytest
from test_functional_gate import gates as gates

import pytest_telescope
from pytest_telescope.model import UnsupportedCapability


@pytest.mark.parametrize("selection", [None, "all", "core"])
def test_gate_records_explicit_full_or_compatibility_dispatch(
    gates, tmp_path, monkeypatch, selection
):
    scratch, output = tmp_path / "scratch", tmp_path / "artifacts"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "environment-double"))
    identity = {"head": "a" * 40, "tree": "b" * 40}
    monkeypatch.setattr(gates.check, "source_identity", lambda root: dict(identity))
    stages = []

    class StageDouble:
        def __init__(self, directory, root):
            assert directory == output

        def run(self, label, *arguments, **kwargs):
            stages.append((label, arguments))
            if label == "build":
                destination = Path(arguments[arguments.index("--outdir") + 1])
                destination.mkdir()
                (destination / "candidate.whl").write_bytes(b"owned wheel double")

    def binding(root, wheel):
        return {
            "schema": 2,
            "root": str(root),
            "source": dict(identity),
            "wheel_path": str(wheel),
            "wheel": {"sha256": "c" * 64},
            "interpreter": {"unit_double": "not executed"},
        }

    def forbidden(*args, **kwargs):
        pytest.fail("orchestration unit controls must not build, install or launch a process")

    monkeypatch.setattr(gates.check, "Stages", StageDouble)
    monkeypatch.setattr(gates.check, "candidate_binding", binding)
    monkeypatch.setattr(gates.gate.subprocess, "Popen", forbidden)
    monkeypatch.setattr(gates.gate.subprocess, "run", forbidden)
    arguments = ["check.py", "--artifacts", str(output)]
    if selection is not None:
        arguments += ["--functional-profile", selection]
    monkeypatch.setattr(sys, "argv", arguments)
    gates.check.main()
    expected = selection or "all"
    result = json.loads((output / "gate-result.json").read_text())
    assert result["status"] == "passed"
    assert result["functional_profile"] == expected
    assert result["verdict_kind"] == ("full" if expected == "all" else "compatibility")
    assert [name for name, _ in stages] == [
        "lint",
        "format",
        "build",
        "metadata",
        "docs",
        "install",
        "functional",
        "harness",
    ]
    for label, argv in stages:
        if label in {"functional", "harness"}:
            assert argv[argv.index("--functional-profile") + 1] == expected
            if label == "functional":
                assert Path(argv[argv.index("--membership") + 1]).is_relative_to(output)
            else:
                assert "--membership" not in argv
    assert set(json.loads((output / "candidate-binding.json").read_text())) == {
        "schema",
        "root",
        "source",
        "wheel_path",
        "wheel",
        "interpreter",
    }


@pytest.fixture
def profile_session(gates, tmp_path, monkeypatch):
    """Actual tiny collection; wheel/origin/interpreter qualification is explicitly doubled."""
    root, scratch = tmp_path / "toy", tmp_path / "scratch"
    (root / "tests").mkdir(parents=True)
    scratch.mkdir()
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts=""\n'
        'markers=["requires_fixture_guard: owned positive profile case"]\n'
    )
    (root / "tests/test_profile.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        "def test_core(): Path('core-ran').touch()\n"
        "@pytest.mark.requires_fixture_guard\n"
        "def test_guard(): Path('guard-ran').touch()\n"
    )
    binding = {
        "schema": 2,
        "root": str(root),
        "source": {"head": "a" * 40, "tree": "b" * 40},
        "wheel_path": str(tmp_path / "wheel-double.whl"),
        "wheel": {"sha256": "c" * 64},
        "interpreter": {"unit_double": "not qualified"},
    }
    binding_path = tmp_path / "binding.json"
    digest = gates.gate.write_json(binding_path, binding)
    origin, verified = str(Path(pytest_telescope.__file__)), []

    def verify(value, *, first_import=False):
        assert value == binding
        verified.append(first_import)
        return origin

    monkeypatch.setattr(gates.functional, "verify_candidate", verify)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.chdir(root)
    membership = tmp_path / "membership.json"

    def run(profile, version="9.2.0"):
        actual_version = importlib.metadata.version
        monkeypatch.setattr(
            importlib.metadata,
            "version",
            lambda name: version if name == "pytest" else actual_version(name),
        )
        monkeypatch.setattr(pytest, "__version__", version)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "functional_gate.py",
                "tests",
                "--functional-profile",
                profile,
                "--membership",
                str(membership),
                "--binding",
                str(binding_path),
                "--binding-sha256",
                digest,
                "--scratch",
                str(scratch),
            ],
        )
        return gates.functional.main()

    return types.SimpleNamespace(root=root, membership=membership, run=run, verified=verified)


@pytest.mark.parametrize(("profile", "version"), [("all", "9.2.0"), ("core", "8.3.5")])
def test_actual_collection_records_complete_profile_membership(profile_session, profile, version):
    value = profile_session
    assert value.run(profile, version) == 0
    core = "tests/test_profile.py::test_core"
    positive = "tests/test_profile.py::test_guard"
    assert json.loads(value.membership.read_text()) == {
        "schema": 1,
        "profile": profile,
        "pytest_version": version,
        "selected": [core, positive] if profile == "all" else [core],
        "deselected": [] if profile == "all" else [positive],
    }
    assert (value.root / "core-ran").exists()
    assert (value.root / "guard-ran").exists() == (profile == "all")
    assert value.verified == [True, False]


def test_all_profile_refuses_unsupported_adapter_before_bodies(profile_session):
    value = profile_session
    with pytest.raises(UnsupportedCapability) as refusal:
        value.run("all", "8.3.5")
    assert refusal.value.capability == "execution:fixture-version"
    assert not (value.root / "core-ran").exists()
    assert not (value.root / "guard-ran").exists()
    assert not value.membership.exists()


@pytest.mark.parametrize("profile", ["core", "all"])
def test_external_deselection_refuses_before_bodies(profile_session, profile):
    value = profile_session
    (value.root / "tests/conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    removed = [item for item in items if item.name == 'test_core']\n"
        "    items[:] = [item for item in items if item not in removed]\n"
        "    config.hook.pytest_deselected(items=removed)\n"
    )
    assert value.run(profile) != 0
    assert not (value.root / "core-ran").exists()
    assert not (value.root / "guard-ran").exists()
