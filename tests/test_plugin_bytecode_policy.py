"""The real isolated pytest fixture must preserve policy in a later Python child."""

from test_plugin import invoke


def test_isolated_pytest_fixture_keeps_nested_python_bytecode_disabled(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
    (tmp_path / "owned_probe.py").write_text("VALUE = 37\n")
    (tmp_path / "test_child.py").write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "def test_child():\n"
        "    result = subprocess.run([sys.executable, '-c', "
        '"import json, sys, owned_probe; '
        "print(json.dumps({'disabled': sys.dont_write_bytecode, 'value': owned_probe.VALUE}))\"], "
        "capture_output=True, text=True, check=True, timeout=5)\n"
        "    assert json.loads(result.stdout) == {'disabled': True, 'value': 37}\n"
        "    assert not Path('__pycache__').exists()\n"
    )
    result = invoke(tmp_path, "-p", "no:cacheprovider")
    assert result.returncode == 0, result.stdout + result.stderr
