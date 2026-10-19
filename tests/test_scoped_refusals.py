"""Public refusal envelopes and host-environment namespace boundaries."""

import json

import pytest

from pytest_telescope import controller
from pytest_telescope.model import (
    DeclarationError,
    InfrastructureError,
    UnsupportedCapability,
    load,
)


@pytest.mark.parametrize(
    "error,exit_code,status",
    [
        (UnsupportedCapability("scratch:exec", "unsupported profile"), 4, "unsupported"),
        (DeclarationError("invalid input"), 4, "declaration_error"),
        (InfrastructureError("backend unavailable"), 3, "infrastructure_error"),
        (KeyboardInterrupt(), 3, "infrastructure_error"),
        (OSError("unavailable storage"), 3, "infrastructure_error"),
        (RecursionError(), 3, "infrastructure_error"),
        (MemoryError(), 3, "infrastructure_error"),
    ],
)
def test_cli_refusals_have_versioned_incomplete_inventory(
    monkeypatch, capsys, error, exit_code, status
):
    def refused(*args, **kwargs):
        raise error

    monkeypatch.setattr(controller, "run", refused)
    assert controller.main(["run"]) == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == 1
    assert output["kind"] == "refusal"
    assert output["status"] == status
    assert output["inventory_complete"] is False
    assert output["inventory_digest"] is None
    if isinstance(error, UnsupportedCapability):
        assert output["capability"] == "scratch:exec"
    else:
        assert "capability" not in output


@pytest.mark.parametrize("environment", [".venv", "custom-environment"])
@pytest.mark.parametrize(
    "input_kind", ["suite-root", "suite-cwd", "suite-config", "test-root-file", "distribution"]
)
def test_automatic_inputs_cannot_include_host_environments(tmp_path, environment, input_kind):
    directory = tmp_path / environment
    directory.mkdir()
    if environment == "custom-environment":
        (directory / "pyvenv.cfg").write_text("home = /synthetic/interpreter\n")
    (directory / "test_private.py").write_text("def test_private(): pass\n")
    (directory / "pytest.ini").write_text("[pytest]\n")
    (directory / "pyproject.toml").write_text('[project]\nname="example-private"\nversion="1"\n')
    (tmp_path / "test_public.py").write_text("def test_public(): pass\n")
    configuration = "[tool.pytest_telescope]\n"
    if input_kind == "suite-root":
        configuration += f'[tool.pytest_telescope.suites.private]\nroot="{environment}"\nconfig="pyproject.toml"\ntest_roots=["test_public.py"]\n'
    elif input_kind == "suite-cwd":
        configuration += f'[tool.pytest_telescope.suites.private]\nroot="."\ncwd="{environment}"\nconfig="pyproject.toml"\ntest_roots=["test_public.py"]\n'
    elif input_kind == "suite-config":
        configuration += f'[tool.pytest_telescope.suites.private]\nroot="."\nconfig="{environment}/pytest.ini"\ntest_roots=["test_public.py"]\n'
    elif input_kind == "test-root-file":
        configuration += f'test_roots=["{environment}/test_private.py"]\n'
    else:
        configuration += f'[tool.pytest_telescope.distributions.private]\nformat="pep621-static-v1"\nmetadata="{environment}/pyproject.toml"\n'
    (tmp_path / "pyproject.toml").write_text(configuration)
    with pytest.raises(DeclarationError):
        load(tmp_path)


@pytest.mark.parametrize(
    "arguments", [[], ["unknown-command"], ["run", "--timeout", "not-a-number"]]
)
def test_cli_argument_refusals_use_the_same_schema(capsys, arguments):
    assert controller.main(arguments) == 4
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == 1 and output["kind"] == "refusal"
    assert output["status"] == "declaration_error"
    assert output["inventory_complete"] is False
    assert output["inventory_digest"] is None
