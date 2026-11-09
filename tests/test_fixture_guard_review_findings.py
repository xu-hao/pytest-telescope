"""Focused causal controls for review findings; owned synthetic projects only."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, InfrastructureError, UnsupportedCapability


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n')


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def narrow_project(tmp_path):
    configure(tmp_path)
    put(tmp_path, "flag.txt", "synthetic")
    put(tmp_path, "tests/test_inventory.py", '''from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(inputs=["flag.txt"])
def test_flagged(): pass
def test_unflagged(): pass
if Path("flag.txt").exists():
    def test_generated(): pass
''')


def test_unguarded_narrow_inventory_mismatch_is_declaration_error(tmp_path):
    narrow_project(tmp_path)
    with pytest.raises(DeclarationError) as plain:
        controller.run(tmp_path)
    assert type(plain.value) is DeclarationError


def test_narrow_inventory_mismatch_is_declaration_error_before_body(tmp_path, monkeypatch):
    narrow_project(tmp_path)
    modes = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        modes.append(request["mode"])
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    with pytest.raises(DeclarationError) as guarded:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert type(guarded.value) is DeclarationError
    assert "guard-capture" in modes and "guard-batch" not in modes


def test_unrepresentable_package_fixture_stays_fixture_metadata_refusal(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/test_package.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="package")
def package_fixture(): return object()
def test_item(package_fixture): assert package_fixture is not None
''')
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-metadata"
    assert controller.run(tmp_path)["status"] == "passed"


@pytest.mark.parametrize("action", ["exit", "dynamic_marker"])
def test_post_body_exit_code_four_does_not_claim_pre_dispatch(tmp_path, monkeypatch, action):
    configure(tmp_path)
    body = ('pytest.exit("synthetic exit", returncode=4)' if action == "exit"
            else 'pytest.mark.telescope(inputs=[])')
    put(tmp_path, "tests/test_late.py", '''import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item():
    os.write(1, b"F3_ACTUAL_BODY\\n")
    ''' + body + '\n')
    original = sandbox.capture
    events = []

    def observe(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST in argv:
            request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
            if request["mode"] == "guard-batch":
                events.append((code, b"F3_ACTUAL_BODY\n" in output))
        return code, output

    monkeypatch.setattr(sandbox, "capture", observe)
    report = controller.run(tmp_path, fixture_guard="available-v1", arguments=["-s"])
    assert events == [(4, True)]
    assert report["schema"] == 5 and report["kind"] == "run"
    assert report["fixture_guard"]["state"] == "admitted"
    assert report["groups"][0]["attempts"] == 1
    assert not report["groups"][0]["cache_eligible"]
    assert all("before dispatch" not in row["reason"].lower() for row in report["units"])


def test_class_fixture_visible_to_function_is_plain_pytest_compatible_but_guard_refuses(tmp_path):
    configure(tmp_path)
    put(tmp_path, "tests/conftest.py", '''import pytest
@pytest.fixture(scope="class")
def class_fixture():
    state = []
    yield state
    assert state == ["used"]
''')
    put(tmp_path, "tests/test_function.py", '''import pytest
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])
def test_item(class_fixture): class_fixture.append("used")
''')
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    plain = subprocess.run([sys.executable, "-I", "-B", "-m", "pytest", "-p", "pytest_telescope.plugin", "-p", "no:cacheprovider", "-q", "tests/test_function.py"],
                           cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=30, check=False)
    assert plain.returncode == 0, plain.stdout.decode("utf-8", "replace")
    with pytest.raises(UnsupportedCapability) as refusal:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert refusal.value.capability == "execution:fixture-metadata"


def test_invalid_unicode_expectation_is_not_retained_by_public_exception_chain(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.setenv("SYNTHETIC_F6_OPTION", "valid")
    put(tmp_path, "tests/test_value.py", '''import pytest
pytestmark = pytest.mark.telescope(env=["SYNTHETIC_F6_OPTION"])
def test_item(): pass
''')
    case = case_id("tests/test_value.py::test_item")
    base = {"case_id": case, "name": "SYNTHETIC_F6_OPTION", "op": "equals", "value": "valid"}
    assert controller.run(tmp_path, fixture_guard="available-v1", condition_checks=[base])["status"] == "passed"
    secret = "F6_private_value_31c5" + chr(0xD800) + "_tail"
    with pytest.raises(DeclarationError) as failure:
        controller.run(tmp_path, fixture_guard="available-v1", condition_checks=[{**base, "value": secret}])
    pending = [failure.value]
    seen = set()
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        if (getattr(error, "object", None) == secret or secret in error.args
                or repr(secret) in repr(error)):
            pytest.fail("synthetic condition value retained by exception chain", pytrace=False)
        pending.extend([error.__context__, error.__cause__])


def test_unguarded_post_body_exit_four_reports_declaration_error(tmp_path, monkeypatch):
    configure(tmp_path)
    put(tmp_path, "tests/test_late.py", '''import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item():
    os.write(1, b"F3_PLAIN_BODY\\n")
    pytest.exit("synthetic exit", returncode=4)
''')
    original = sandbox.capture
    events = []

    def observe(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST in argv:
            request = json.loads(Path(argv[argv.index(sandbox.REQUEST) - 1]).read_text())
            if request["mode"] == "batch":
                events.append((code, b"F3_PLAIN_BODY\n" in output))
        return code, output

    monkeypatch.setattr(sandbox, "capture", observe)
    with pytest.raises(DeclarationError) as error:
        controller.run(tmp_path, arguments=["-s"])
    assert type(error.value) is DeclarationError
    assert events == [(4, True)]
