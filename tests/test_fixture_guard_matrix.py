"""Additional guard controls against real owned Bubblewrap executions."""

import hashlib
import json
from pathlib import Path

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, UnsupportedCapability, fingerprint


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def configure(root):
    put(
        root,
        "pyproject.toml",
        '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        '[tool.pytest_telescope.execution]\nmode = "batch"\nworkers = 1\n',
    )


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def test_parameterized_class_and_autouse_fixture_lifecycle_is_shared(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_class.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module", autouse=True)
def module_lifetime():
    state = []
    yield state
    assert state == [0, 1]
class TestCases:
    @pytest.fixture(scope="class")
    def class_lifetime(self, module_lifetime):
        yield module_lifetime
        assert module_lifetime == [0, 1]
    @pytest.mark.parametrize("value", [0, 1])
    def test_case(self, value, class_lifetime):
        assert class_lifetime == list(range(value))
        class_lifetime.append(value)
""",
    )
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["fixture_guard"]["state"] == "admitted"
    assert report["counts"]["executed"] == 2 and len(report["groups"]) == 1
    assert all(row["result_status"] == "passed" for row in report["units"])
    assert all(
        row["reports"][-1]["phase"] == "teardown" and row["reports"][-1]["outcome"] == "passed"
        for row in report["units"]
    )
    assert controller.run(tmp_path, fixture_guard="available-v1")["counts"]["reused"] == 2


def test_dynamic_available_but_unrequested_module_fixture_refuses_split(tmp_path, monkeypatch):
    configure(tmp_path)
    put(tmp_path, "a.txt", "a")
    put(tmp_path, "b.txt", "b")
    put(
        tmp_path,
        "tests/test_dynamic.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def dynamic_target(): return object()
@pytest.mark.telescope(inputs=["a.txt"])
def test_a(request): assert request.getfixturevalue("dynamic_target") is not None
@pytest.mark.telescope(inputs=["b.txt"])
def test_b(): pass
""",
    )
    modes = []
    real = sandbox.execute

    def observe(*args, request, **kwargs):
        modes.append(request["mode"])
        return real(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    with pytest.raises(UnsupportedCapability) as exc:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert exc.value.capability == "execution:fixture-sharing"
    assert "guard-inspection" in modes and "guard-batch" not in modes


def test_session_fixture_available_across_files_refuses_split(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/conftest.py",
        """import pytest
@pytest.fixture(scope="session")
def shared(): return object()
""",
    )
    for suffix in ("a", "b"):
        put(
            tmp_path,
            f"tests/test_{suffix}.py",
            """import pytest
pytestmark = pytest.mark.telescope(inputs=["tests/conftest.py"])
def test_item(shared): assert shared is not None
""",
        )
    with pytest.raises(UnsupportedCapability) as exc:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert exc.value.capability == "execution:fixture-sharing"


def test_package_fixture_owner_is_conservatively_refused(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_package.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="package")
def package_owner(): return object()
def test_item(package_owner): assert package_owner is not None
""",
    )
    with pytest.raises(UnsupportedCapability) as exc:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert exc.value.capability == "execution:fixture-metadata"


def test_same_name_fixture_override_is_user_owned(tmp_path):
    configure(tmp_path)
    put(tmp_path, "a.txt", "a")
    put(tmp_path, "b.txt", "b")
    put(
        tmp_path,
        "tests/test_override.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def doctest_namespace(): return {}
@pytest.mark.telescope(inputs=["a.txt"])
def test_a(doctest_namespace): pass
@pytest.mark.telescope(inputs=["b.txt"])
def test_b(doctest_namespace): pass
""",
    )
    with pytest.raises(UnsupportedCapability) as exc:
        controller.run(tmp_path, fixture_guard="available-v1")
    assert exc.value.capability == "execution:fixture-sharing"


def test_guarded_cache_corruption_is_not_reused(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_cache.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): pass
""",
    )
    cold = controller.run(tmp_path, fixture_guard="available-v1")
    assert cold["status"] == "passed" and cold["counts"]["executed"] == 1
    assert controller.run(tmp_path, fixture_guard="available-v1")["counts"]["reused"] == 1
    paths = list((tmp_path / ".pytest-telescope/cache").glob("*.json"))
    guarded = [path for path in paths if json.loads(path.read_text())["schema"] == 7]
    assert len(guarded) == 1
    record = json.loads(guarded[0].read_text())
    record["result"]["graph_id"] = "0" * 64
    record["checksum"] = fingerprint(record["result"])
    guarded[0].write_text(json.dumps(record))
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert report["status"] == "passed" and report["counts"]["reused"] == 0
    assert report["counts"]["executed"] == 1
    assert controller.run(tmp_path)["counts"]["reused"] == 0


def test_guard_conditions_reject_invalid_shapes_and_keep_value_private(
    tmp_path, monkeypatch, capsys
):
    configure(tmp_path)
    secret = "synthetic-Guard-7f26a05bc4e14e9db27a960e"
    monkeypatch.setenv("GUARD_SYNTHETIC", secret)
    put(
        tmp_path,
        "tests/test_private.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(env=["GUARD_SYNTHETIC"])
def test_item(): assert os.environ["GUARD_SYNTHETIC"]
""",
    )
    case = case_id("tests/test_private.py::test_item")
    row = {"case_id": case, "name": "GUARD_SYNTHETIC", "op": "equals", "value": secret}
    requests = []
    original = sandbox.execute

    def observe(*args, request, **kwargs):
        requests.append(json.dumps(request))
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", observe)
    cold = controller.run(tmp_path, fixture_guard="available-v1", condition_checks=[row])
    warm = controller.run(tmp_path, fixture_guard="available-v1", condition_checks=[row])
    assert cold["status"] == warm["status"] == "passed" and warm["counts"]["reused"] == 1
    for invalid in (
        [row, row],
        [{**row, "extra": 1}],
        [{**row, "value": "x" * 4097}],
        [{**row, "value": chr(0xD800)}],
        [{**row, "name": "PYTHONPATH"}],
    ):
        with pytest.raises(DeclarationError):
            controller.run(tmp_path, fixture_guard="available-v1", condition_checks=invalid)
    with pytest.raises(UnsupportedCapability) as mismatch:
        controller.run(
            tmp_path,
            fixture_guard="available-v1",
            condition_checks=[{**row, "value": secret + "x"}],
        )
    assert mismatch.value.capability == "execution:condition-mismatch"
    captured = capsys.readouterr()
    evidence = (
        json.dumps([cold, warm, requests]).encode()
        + str(mismatch.value).encode()
        + captured.out.encode()
        + captured.err.encode()
    )
    for path in (tmp_path / ".pytest-telescope").rglob("*.json"):
        evidence += path.read_bytes()
    assert secret.encode() not in evidence
    assert hashlib.sha256(secret.encode()).hexdigest().encode() not in evidence


@pytest.mark.parametrize(
    "corruption",
    [
        "nonce",
        "missing",
        "duplicate",
        "truncated",
        "context_id",
        "group_id",
        "registry_id",
        "graph_id",
        "unexpected",
    ],
)
def test_real_inspection_transport_corruption_fails_closed(tmp_path, monkeypatch, corruption):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_wire.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): pass
""",
    )
    original = sandbox.capture
    mutations = []

    def corrupt(argv, timeout, **kwargs):
        code, output = original(argv, timeout, **kwargs)
        if sandbox.REQUEST not in argv:
            return code, output
        request_path = Path(argv[argv.index(sandbox.REQUEST) - 1])
        request = json.loads(request_path.read_text())
        if request["mode"] != "guard-inspection":
            return code, output
        mutations.append(corruption)
        prefix = b"TELESCOPE_RESULT "
        lines = output.splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        assert len(indices) == 1
        index = indices[0]
        if corruption == "missing":
            del lines[index]
        elif corruption == "duplicate":
            lines.append(lines[index])
        elif corruption == "truncated":
            lines[index] = lines[index][:-4]
        else:
            data = json.loads(lines[index][len(prefix) :])
            if corruption == "unexpected":
                data["unexpected"] = True
            else:
                data[corruption] = ("1" if data[corruption][0] == "0" else "0") + data[corruption][
                    1:
                ]
            lines[index] = prefix + json.dumps(data).encode()
        return code, b"\n".join(lines) + b"\n"

    monkeypatch.setattr(sandbox, "capture", corrupt)
    report = controller.run(tmp_path, fixture_guard="available-v1")
    assert (
        report["status"] == "infrastructure_error"
        and report["fixture_guard"]["state"] == "unavailable"
    )
    assert len(mutations) == 2
    assert not any(group["cache_eligible"] for group in report["groups"])


def test_unguarded_record_is_not_a_guarded_hit(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_cache.py",
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\ndef test_item(): pass\n",
    )
    ordinary = controller.run(tmp_path)
    assert ordinary["schema"] == 4 and ordinary["counts"]["executed"] == 1
    guarded = controller.run(tmp_path, fixture_guard="available-v1")
    assert guarded["schema"] == 5 and guarded["status"] == "passed"
    assert guarded["counts"]["executed"] == 1 and guarded["counts"]["reused"] == 0
    assert controller.run(tmp_path, fixture_guard="available-v1")["counts"]["reused"] == 1
    assert controller.run(tmp_path)["counts"]["reused"] == 1
