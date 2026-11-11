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
    assert output["schema"] == 4
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


def test_discovery_and_globs_skip_a_conda_environment(tmp_path):
    # A conda environment has no pyvenv.cfg; its conda-meta directory marks it.
    def put_file(name, text):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)

    put_file("envs/conda-meta/history", "")
    put_file("envs/lib/test_inside.py", "def test_inside(): pass\n")
    put_file(
        "test_public.py",
        "import os\nimport pytest\n"
        "pytestmark = pytest.mark.telescope(inputs=['**/*.py'])\n"
        "def test_public():\n"
        "    assert not os.path.lexists('/project/envs')\n",
    )
    put_file("pyproject.toml", '[tool.pytest_telescope]\ntest_roots=["."]\n')
    report = controller.run(tmp_path, explain=True, timeout=20)
    assert report["kind"] == "run", report
    assert sorted({unit["file"] for unit in report["units"]}) == ["test_public.py"], report
    assert report["counts"]["executed"] == 1 and report["counts"]["failed"] == 0, report


def test_an_input_below_an_unsearchable_directory_is_a_missing_input(tmp_path, monkeypatch, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "data.txt").write_text("hidden\n")
    (tmp_path / "test_public.py").write_text(
        "import pytest\npytestmark = pytest.mark.telescope(inputs=['locked/data.txt'])\n"
        "def test_public(): pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots=["test_public.py"]\n'
    )
    locked.chmod(0)
    try:
        monkeypatch.chdir(tmp_path)
        assert controller.main(["run"]) == 4
    finally:
        locked.chmod(0o755)
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "declaration_error", output
    assert output["error"] == "missing inputs; declare expected absence explicitly", output


@pytest.mark.parametrize("marker", ["pyvenv.cfg", "conda-meta"])
def test_many_names_below_one_environment_are_each_refused(tmp_path, monkeypatch, capsys, marker):
    # Names below one environment share their ancestors' checks; each still refuses.
    environment = tmp_path / "shared" / "env"
    environment.mkdir(parents=True)
    if marker == "pyvenv.cfg":
        (environment / "pyvenv.cfg").write_text("home = /synthetic/interpreter\n")
    else:
        (environment / "conda-meta").mkdir()
    names = [f"shared/data{index}.txt" for index in range(3)] + [
        f"shared/env/lib/module{index}.py" for index in range(3)
    ]
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    for declared in (names[:3] + names[3:4], names[3:]):
        (tmp_path / "test_public.py").write_text(
            f"import pytest\npytestmark = pytest.mark.telescope(inputs={declared!r})\n"
            "def test_public(): pass\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pytest_telescope]\ntest_roots=["test_public.py"]\n'
        )
        monkeypatch.chdir(tmp_path)
        assert controller.main(["run"]) == 4
        output = json.loads(capsys.readouterr().out)
        assert output["error"] == "metadata, cache and host environments are not project inputs"


@pytest.mark.parametrize(
    "arguments", [[], ["unknown-command"], ["run", "--timeout", "not-a-number"]]
)
def test_cli_argument_refusals_use_the_same_schema(capsys, arguments):
    assert controller.main(arguments) == 4
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == 4 and output["kind"] == "refusal"
    assert output["status"] == "declaration_error"
    assert output["inventory_complete"] is False
    assert output["inventory_digest"] is None


@pytest.mark.parametrize("field", ["root", "cwd"])
def test_suite_directories_reject_symlink_ancestors(tmp_path, field):
    (tmp_path / "real/sub").mkdir(parents=True)
    (tmp_path / "link").symlink_to("real", target_is_directory=True)
    (tmp_path / "test_public.py").write_text(
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_public(): pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest_telescope.suites.core]\nconfig="pyproject.toml"\n'
        'test_roots=["test_public.py"]\n' + f'{field}="link/sub"\n'
    )
    with pytest.raises(DeclarationError):
        load(tmp_path)


@pytest.mark.parametrize(
    "marker,declared",
    [
        ("pyvenv.cfg", "myenv"),
        ("pyvenv.cfg", "myenv/pyvenv.cfg"),
        ("conda-meta", "myenv"),
        ("conda-meta", "myenv/bin/python"),
    ],
)
@pytest.mark.parametrize("form", ["directory", "link"])
def test_a_declared_environment_is_named_as_not_a_project_input(
    tmp_path, monkeypatch, capsys, marker, declared, form
):
    project = tmp_path / "checkout"
    project.mkdir()
    environment = tmp_path / "shared-venv" if form == "link" else project / "myenv"
    environment.mkdir()
    if marker == "pyvenv.cfg":
        (environment / "pyvenv.cfg").write_text("home = /synthetic/interpreter\n")
    else:
        # A conda environment has no pyvenv.cfg; its install records mark it.
        (environment / "conda-meta").mkdir()
        (environment / "conda-meta" / "history").write_text("")
        (environment / "bin").mkdir()
        (environment / "bin" / "python").write_text("")
    if form == "link":
        (project / "myenv").symlink_to("../shared-venv")
    (project / "test_public.py").write_text(
        f"import pytest\npytestmark = pytest.mark.telescope(inputs=[{declared!r}])\n"
        "def test_public(): pass\n"
    )
    (project / "pyproject.toml").write_text(
        '[tool.pytest_telescope]\ntest_roots=["test_public.py"]\n'
    )
    monkeypatch.chdir(project)
    assert controller.main(["run"]) == 4
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "declaration_error", output
    assert output["error"] == "metadata, cache and host environments are not project inputs"
