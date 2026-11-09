"""Real imported-helper integration with scoped environment and skip evidence."""

import hashlib
import json

import pytest
from test_helper_registry import project_at, put
from test_helper_registry_contract import passed

def helper_with_environment(root):
    put(
        root,
        "helpers/support.py",
        """
import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.telescope(inputs=["helper-private.txt"], env=["HELPER_OPTION"])

def answer():
    return Path("data/answer.txt").read_text().strip()

def variable(name):
    return os.environ.get(name)

def skip_here(phase):
    pytest.skip("owned helper " + phase + " optional")
""",
    )


def test_helper_environment_is_not_a_grant_or_an_unrelated_cache_dependency(tmp_path, monkeypatch):
    root = project_at(tmp_path)
    helper_with_environment(root)
    (root / "tests/test_consumer.py").unlink()
    for name in ("HELPER_OPTION", "CONSUMER_OPTION"):
        monkeypatch.delenv(name, raising=False)
    for name, environment in (("active", ["CONSUMER_OPTION"]), ("unrelated", [])):
        expected = '(None, "", "owned-first-option", "owned-second-option")' if environment else "(None,)"
        put(
            root,
            f"tests/test_{name}.py",
            f"""
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(
    inputs=["helpers/__init__.py", "helpers/support.py", "data/answer.txt"],
    env={environment!r},
)
from helpers.support import answer, variable

def test_answer():
    assert answer() == "42"
    assert variable("HELPER_OPTION") is None
    assert variable("CONSUMER_OPTION") in {expected}
    assert not Path("helper-private.txt").exists()
""",
        )

    reports = [passed(root, executed=2), passed(root, executed=0, reused=2)]
    monkeypatch.setenv("HELPER_OPTION", "owned-helper-only-option")
    unchanged = passed(root, executed=0, reused=2)
    assert unchanged["collection"]["outcome"] == "reused"
    reports.append(unchanged)
    for value in ("", "owned-first-option", "owned-second-option"):
        monkeypatch.setenv("CONSUMER_OPTION", value)
        report = passed(root, executed=1, reused=1)
        assert {row["file"]: row["outcome"] for row in report["units"]} == {
            "tests/test_active.py": "executed",
            "tests/test_unrelated.py": "reused",
        }
        reports.append(report)
    monkeypatch.delenv("CONSUMER_OPTION")
    reports.append(passed(root, executed=0, reused=2))
    recorded = json.dumps(reports).encode() + b"".join(
        path.read_bytes() for path in (root / ".pytest-telescope/cache").rglob("*.json")
    )
    for value in (b"owned-helper-only-option", b"owned-first-option", b"owned-second-option"):
        assert value not in recorded


def reason(phase):
    return {
        "profile": "pytest-reason-sha256-v1",
        "kind": "skip",
        "sha256": hashlib.sha256(f"Skipped: owned helper {phase} optional".encode()).hexdigest(),
    }


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_imported_helper_skip_phases_and_reason_evidence_survive_reuse(tmp_path, monkeypatch, mode):
    root = project_at(tmp_path)
    helper_with_environment(root)
    monkeypatch.setenv("HELPER_OPTION", "owned-helper-only-option")
    monkeypatch.setenv("ACTIVE_OPTION", "enabled")
    put(
        root,
        "tests/test_consumer.py",
        """
import pytest
from pathlib import Path
pytestmark = pytest.mark.telescope(
    inputs=["helpers/__init__.py", "helpers/support.py", "data/answer.txt"],
    env=["ACTIVE_OPTION"],
)
from helpers.support import answer, skip_here, variable

def check_helper():
    assert answer() == "42"
    assert variable("ACTIVE_OPTION") in ("enabled", "changed")
    assert variable("HELPER_OPTION") is None
    assert not Path("helper-private.txt").exists()

@pytest.fixture
def optional_setup():
    check_helper()
    skip_here("setup")

@pytest.fixture
def optional_teardown():
    yield
    check_helper()
    skip_here("teardown")

def test_setup(optional_setup):
    check_helper()

def test_call():
    check_helper()
    skip_here("call")

def test_teardown(optional_teardown):
    check_helper()
""",
    )
    cold = passed(root, executed=3, selected=3, execution_mode=mode)
    warm = passed(root, executed=0, reused=3, execution_mode=mode)
    assert cold["execution"] == {"mode": mode, "profile": f"{mode}-v2", "workers": 1}
    assert len(cold["groups"]) == (1 if mode == "batch" else 3)
    assert all(group["cache_eligible"] for group in cold["groups"])
    assert cold["case_outcomes"]["fresh_skipped"] == 3
    assert warm["case_outcomes"]["cached_skipped"] == 3
    assert cold["cases"]["skipped"] == warm["cases"]["skipped"] == 3
    for phase in ("setup", "call", "teardown"):
        encoded = json.dumps(["default", f"tests/test_consumer.py::test_{phase}"], separators=(",", ":"))
        case_id = hashlib.sha256(encoded.encode()).hexdigest()
        first = next(row for row in cold["units"] if row["case_id"] == case_id)
        second = next(row for row in warm["units"] if row["case_id"] == case_id)
        assert first["outcome"] == "executed" and second["outcome"] == "reused"
        assert first["result_status"] == second["result_status"] == "skipped"
        assert first["complete"] is True and second["complete"] is True
        expected = [{"phase": "setup", "outcome": "passed", "skip_reason": None}]
        if phase != "setup":
            expected.append({"phase": "call", "outcome": "passed", "skip_reason": None})
        expected.append({"phase": "teardown", "outcome": "passed", "skip_reason": None})
        for report in expected:
            if report["phase"] == phase:
                report.update(outcome="skipped", skip_reason=reason(phase))
        assert first["reports"] == second["reports"] == expected
    monkeypatch.setenv("ACTIVE_OPTION", "changed")
    changed = passed(root, executed=3, reused=0, execution_mode=mode)
    assert changed["cases"]["skipped"] == 3
    recorded = json.dumps([cold, warm, changed]).encode() + b"".join(
        path.read_bytes() for path in (root / ".pytest-telescope/cache").rglob("*.json")
    )
    for raw in (b"owned helper setup optional", b"owned helper call optional", b"owned helper teardown optional", b"owned-helper-only-option"):
        assert raw not in recorded
