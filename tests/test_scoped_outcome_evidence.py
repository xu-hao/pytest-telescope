"""Public outcome and environment contracts using owned real Bubblewrap projects."""

import hashlib
import json

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, fingerprint


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def configure(root, extra=""):
    put(root, "pyproject.toml", '[tool.pytest_telescope]\ntest_roots = ["tests"]\n' + extra)


def case_id(node):
    return hashlib.sha256(json.dumps(["default", node], separators=(",", ":")).encode()).hexdigest()


def unit(report, node):
    return next(row for row in report["units"] if row["case_id"] == case_id(node))


def digest(reason, kind="skip"):
    return {
        "profile": "pytest-reason-sha256-v1",
        "kind": kind,
        "sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }


def test_effective_environment_precisely_invalidates_consumers_and_preserves_privacy(
    tmp_path, monkeypatch, capsys
):
    configure(tmp_path, 'env = ["GLOBAL_OPTION"]\n')
    put(
        tmp_path,
        "tests/test_environment.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])

@pytest.mark.telescope(env=["FIRST_OPTION"])
def test_first():
    assert "SECOND_OPTION" not in os.environ

@pytest.mark.telescope(env=["SECOND_OPTION"])
def test_second():
    assert "FIRST_OPTION" not in os.environ
""",
    )
    for name in ("FIRST_OPTION", "SECOND_OPTION", "GLOBAL_OPTION"):
        monkeypatch.delenv(name, raising=False)
    requests = []
    original = sandbox.execute

    def execute(*args, request, **kwargs):
        requests.append(json.dumps(request))
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    cold = controller.run(tmp_path)
    assert cold["status"] == "passed" and cold["counts"]["executed"] == 2
    assert controller.run(tmp_path)["counts"]["reused"] == 2
    for value in ("", "synthetic-first-value", "synthetic-second-value"):
        monkeypatch.setenv("FIRST_OPTION", value)
        report = controller.run(tmp_path)
        assert report["status"] == "passed"
        assert unit(report, "tests/test_environment.py::test_first")["outcome"] == "executed"
        assert unit(report, "tests/test_environment.py::test_second")["outcome"] == "reused"
    monkeypatch.delenv("FIRST_OPTION")
    report = controller.run(tmp_path)
    assert report["counts"]["reused"] == 2
    monkeypatch.setenv("GLOBAL_OPTION", "synthetic-global-value")
    report = controller.run(tmp_path)
    assert report["counts"]["executed"] == 2
    content = (
        json.dumps(report).encode()
        + b"".join(path.read_bytes() for path in (tmp_path / ".pytest-telescope").rglob("*.json"))
        + capsys.readouterr().out.encode()
        + json.dumps(requests).encode()
    )
    for value in (b"synthetic-first-value", b"synthetic-second-value", b"synthetic-global-value"):
        assert value not in content


def test_environment_inheritance_and_empty_marker_routing(tmp_path, monkeypatch):
    configure(
        tmp_path,
        '[tool.pytest_telescope.defaults]\nenv = ["DEFAULT_OPTION"]\n'
        '[tool.pytest_telescope.bundles.shared]\nenv = ["BUNDLE_OPTION"]\n',
    )
    for name in (
        "DEFAULT_OPTION",
        "BUNDLE_OPTION",
        "MODULE_OPTION",
        "CLASS_OPTION",
        "FUNC_OPTION",
        "PARAM_OPTION",
    ):
        monkeypatch.setenv(name, "yes")
    put(
        tmp_path,
        "tests/test_inheritance.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(env=["MODULE_OPTION"], bundles=["shared"])
@pytest.mark.telescope(env=["CLASS_OPTION"])
class TestScopes:
    @pytest.mark.telescope(env=["FUNC_OPTION"])
    @pytest.mark.parametrize("value", [pytest.param(1, marks=pytest.mark.telescope(env=["PARAM_OPTION"]))])
    def test_item(self, value):
        for name in ("DEFAULT_OPTION", "BUNDLE_OPTION", "MODULE_OPTION", "CLASS_OPTION", "FUNC_OPTION", "PARAM_OPTION"):
            assert os.environ[name] == "yes"
""",
    )
    put(
        tmp_path,
        "tests/test_empty.py",
        "import pytest\n@pytest.mark.telescope(env=[])\ndef test_empty(): pass\n",
    )
    put(tmp_path, "tests/test_ordinary.py", "def test_ordinary(): pass\n")
    report = controller.run(tmp_path)
    assert report["status"] == "passed", report
    assert unit(report, "tests/test_empty.py::test_empty")["route"] == "scoped"
    assert unit(report, "tests/test_ordinary.py::test_ordinary")["route"] == "ordinary"
    warm = controller.run(tmp_path)
    assert unit(warm, "tests/test_empty.py::test_empty")["outcome"] == "reused"
    assert unit(warm, "tests/test_ordinary.py::test_ordinary")["outcome"] == "executed"


@pytest.mark.parametrize(
    "name",
    [
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "TELESCOPE_REQUEST",
    ],
)
def test_reserved_per_scope_environment_names_refuse(tmp_path, name):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_reserved.py",
        f"import pytest\n@pytest.mark.telescope(env=[{name!r}])\ndef test_item(): pass\n",
    )
    with pytest.raises(DeclarationError):
        controller.run(tmp_path)


def test_real_setup_call_teardown_and_xfail_evidence_reused(tmp_path, capsys):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skips.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture
def skip_setup():
    pytest.skip("setup optional")
@pytest.fixture
def skip_teardown():
    yield
    pytest.skip("teardown optional")
def test_setup(skip_setup): pass
def test_call(): pytest.skip("call optional")
def test_teardown(skip_teardown): pass
@pytest.mark.skipif(True, reason="conditional option")
def test_skipif(): pass
@pytest.mark.xfail(reason="expected condition")
def test_xfail(): assert False
""",
    )
    cold = controller.run(tmp_path)
    assert cold["status"] == "passed", cold
    warm = controller.run(tmp_path)
    assert warm["counts"]["reused"] == 5
    expectations = {
        "setup": ("setup", digest("Skipped: setup optional")),
        "call": ("call", digest("Skipped: call optional")),
        "teardown": ("teardown", digest("Skipped: teardown optional")),
        "skipif": ("setup", digest("Skipped: conditional option")),
        "xfail": ("call", digest("expected condition", "xfail")),
    }
    for suffix, (phase, reason) in expectations.items():
        node = f"tests/test_skips.py::test_{suffix}"
        first, second = unit(cold, node), unit(warm, node)
        assert first["result_status"] == second["result_status"] == "skipped"
        assert first["complete"] and second["complete"]
        assert first["reports"] == second["reports"]
        assert next(row for row in first["reports"] if row["phase"] == phase) == {
            "phase": phase,
            "outcome": "skipped",
            "skip_reason": reason,
        }
    recorded = (
        json.dumps([cold, warm]).encode()
        + b"".join(path.read_bytes() for path in (tmp_path / ".pytest-telescope").rglob("*.json"))
        + capsys.readouterr().out.encode()
    )
    for raw in (
        b"setup optional",
        b"call optional",
        b"teardown optional",
        b"conditional option",
        b"expected condition",
    ):
        assert raw not in recorded


def test_skip_followed_by_teardown_failure_is_visible_but_not_reusable(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_teardown.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.fixture
def broken():
    yield
    raise RuntimeError("synthetic failure")
def test_item(broken): pytest.skip("optional before teardown")
""",
    )
    for _ in range(2):
        report = controller.run(tmp_path)
        row = unit(report, "tests/test_teardown.py::test_item")
        assert report["status"] == "failed" and row["result_status"] == "failed"
        assert row["reports"][1]["skip_reason"] == digest("Skipped: optional before teardown")
        assert row["reports"][2]["outcome"] == "failed"
        assert not report["groups"][0]["cache_eligible"]
        assert report["counts"]["reused"] == 0


def test_collection_skip_is_not_a_manufactured_case(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_collection.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
pytest.skip("collection optional", allow_module_level=True)
""",
    )
    report = controller.run(tmp_path)
    assert report["status"] == "no_tests" and report["inventory_complete"]
    assert report["units"] == [] and report["counts"]["selected"] == 0
    assert report["collection"]["skips"] == [
        {
            "suite": "default",
            "route": "scoped",
            "file": "tests/test_collection.py",
            "phase": "collect",
            "reason": digest("Skipped: collection optional"),
        }
    ]


def test_corrupt_reason_with_valid_checksum_cannot_be_reused(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skip.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): pytest.skip("optional reason")
""",
    )
    assert controller.run(tmp_path)["counts"]["executed"] == 1
    record_path = next((tmp_path / ".pytest-telescope/cache").glob("*.json"))
    record = json.loads(record_path.read_text())
    record["result"]["case_results"][0]["reports"][1]["skip_reason"]["sha256"] = "F" * 64
    record["checksum"] = fingerprint(record["result"])
    record_path.write_text(json.dumps(record))
    report = controller.run(tmp_path)
    assert report["counts"]["reused"] == 0 and report["counts"]["executed"] == 1


def test_batch_groups_require_equal_effective_environment(tmp_path, monkeypatch):
    configure(tmp_path, '[tool.pytest_telescope.execution]\nmode = "batch"\n')
    monkeypatch.setenv("FIRST_OPTION", "first")
    monkeypatch.setenv("SECOND_OPTION", "second")
    put(
        tmp_path,
        "tests/test_groups.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(env=["FIRST_OPTION"])
def test_a():
    assert os.environ["FIRST_OPTION"] in ("first", "changed")
    assert "SECOND_OPTION" not in os.environ
@pytest.mark.telescope(env=["FIRST_OPTION"])
def test_b():
    assert os.environ["FIRST_OPTION"] in ("first", "changed")
    assert "SECOND_OPTION" not in os.environ
@pytest.mark.telescope(env=["SECOND_OPTION"])
def test_c():
    assert os.environ["SECOND_OPTION"] == "second"
    assert "FIRST_OPTION" not in os.environ
""",
    )
    cold = controller.run(tmp_path)
    assert cold["status"] == "passed", cold
    assert sorted(len(group["case_ids"]) for group in cold["groups"]) == [1, 2]
    assert controller.run(tmp_path)["counts"]["reused"] == 3
    monkeypatch.setenv("FIRST_OPTION", "changed")
    # A changed value changes the group's identity and reruns both members together.
    # Use a fixture which accepts both values without changing the project source.
    report = controller.run(tmp_path)
    assert unit(report, "tests/test_groups.py::test_c")["outcome"] == "reused"
    assert (
        unit(report, "tests/test_groups.py::test_a")["group_id"]
        == unit(report, "tests/test_groups.py::test_b")["group_id"]
    )
    assert report["counts"]["reused"] == 1


def test_oversized_reason_refuses_without_exposing_reason(tmp_path, capsys):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_oversized.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): pytest.skip("synthetic-oversized-" + "r" * 4097)
""",
    )
    report = controller.run(tmp_path)
    assert report["status"] == "infrastructure_error"
    assert not any(group["cache_eligible"] for group in report["groups"])
    assert all(group["attempts"] <= 2 for group in report["groups"])
    captured = capsys.readouterr()
    payload = (
        json.dumps(report).encode()
        + b"".join(path.read_bytes() for path in (tmp_path / ".pytest-telescope").rglob("*.json"))
        + captured.out.encode()
        + captured.err.encode()
    )
    assert b"synthetic-oversized-" not in payload


@pytest.mark.parametrize("corruption", ["phase", "context", "count", "old-schema"])
def test_checksums_cannot_hide_corrupt_lifecycle_or_old_cache(tmp_path, corruption):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skip.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_item(): pytest.skip("optional reason")
""",
    )
    assert controller.run(tmp_path)["counts"]["executed"] == 1
    path = next((tmp_path / ".pytest-telescope/cache").glob("*.json"))
    record = json.loads(path.read_text())
    result = record["result"]
    if corruption == "phase":
        result["case_results"][0]["reports"][1]["phase"] = "setup"
    elif corruption == "context":
        result["context_id"] = "f" * 64
    elif corruption == "count":
        result["skipped"] = 0
    else:
        record["schema"] = 5
    record["checksum"] = fingerprint(result)
    path.write_text(json.dumps(record))
    report = controller.run(tmp_path)
    assert report["counts"]["executed"] == 1 and report["counts"]["reused"] == 0


@pytest.mark.parametrize(
    "declaration",
    [
        "pytest.mark.telescope(env=names)",
        'pytest.mark.telescope(env=["OPTION"], unknown=[])',
        'pytest.mark.telescope(env=[""])',
        'pytest.mark.telescope(env=["NOT-AN-IDENTIFIER"])',
    ],
)
def test_nonliteral_unknown_or_invalid_environment_declarations_refuse(tmp_path, declaration):
    from pytest_telescope.plan import make_plan

    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_invalid.py",
        f"""import pytest
names = ["OPTION"]
@pytest.mark.telescope(inputs=[])
@{declaration}
def test_item(): pass
""",
    )
    with pytest.raises(DeclarationError):
        make_plan(tmp_path)


def test_mutated_environment_declaration_never_widens_scope(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_mutated.py",
        """import pytest
pytestmark = pytest.mark.telescope(env=["SYNTHETIC_OPTION"])
pytestmark.kwargs["env"].append("OTHER_OPTION")
def test_item(): pass
""",
    )
    with pytest.raises(DeclarationError):
        controller.run(tmp_path)


def test_opt_in_skipif_transitions_and_unrelated_case_remains_reusable(tmp_path, monkeypatch):
    configure(tmp_path)
    monkeypatch.delenv("SYNTHETIC_OPT_IN", raising=False)
    put(
        tmp_path,
        "tests/test_opt_in.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
@pytest.mark.telescope(env=["SYNTHETIC_OPT_IN"])
@pytest.mark.skipif(os.environ.get("SYNTHETIC_OPT_IN") != "1", reason="opt in required")
def test_opt_in():
    assert os.environ["SYNTHETIC_OPT_IN"] == "1"
def test_unrelated():
    assert "SYNTHETIC_OPT_IN" not in os.environ
""",
    )
    first = controller.run(tmp_path)
    selected = "tests/test_opt_in.py::test_opt_in"
    other = "tests/test_opt_in.py::test_unrelated"
    assert first["status"] == "passed" and unit(first, selected)["result_status"] == "skipped"
    assert unit(first, selected)["reports"][0]["skip_reason"] == digest("Skipped: opt in required")
    assert controller.run(tmp_path)["counts"]["reused"] == 2
    monkeypatch.setenv("SYNTHETIC_OPT_IN", "")
    empty = controller.run(tmp_path)
    assert unit(empty, selected)["outcome"] == "executed"
    assert unit(empty, selected)["result_status"] == "skipped"
    assert unit(empty, other)["outcome"] == "reused"
    monkeypatch.setenv("SYNTHETIC_OPT_IN", "1")
    enabled = controller.run(tmp_path)
    assert enabled["status"] == "passed"
    assert unit(enabled, selected)["outcome"] == "executed"
    assert unit(enabled, selected)["result_status"] == "passed"
    assert unit(enabled, other)["outcome"] == "reused"
    assert controller.run(tmp_path)["counts"]["reused"] == 2


def test_parameter_and_environment_sensitive_reason_is_digest_only(tmp_path, monkeypatch, capsys):
    configure(tmp_path)
    monkeypatch.setenv("REASON_OPTION", "synthetic-env-reason")
    put(
        tmp_path,
        "tests/test_private.py",
        """import os
import pytest
pytestmark = pytest.mark.telescope(env=["REASON_OPTION"])
@pytest.mark.parametrize("value", [pytest.param("synthetic-param-value", id="synthetic-param-id")])
def test_item(value):
    pytest.skip(value + ":" + os.environ["REASON_OPTION"])
""",
    )
    requests = []
    original = sandbox.execute

    def execute(*args, request, **kwargs):
        requests.append(json.dumps(request))
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)
    cold = controller.run(tmp_path)
    warm = controller.run(tmp_path)
    assert cold["status"] == warm["status"] == "passed"
    assert cold["counts"]["executed"] == warm["counts"]["reused"] == 1
    first, second = cold["units"][0], warm["units"][0]
    assert first["reports"] == second["reports"]
    assert first["reports"][1]["skip_reason"] == digest(
        "Skipped: synthetic-param-value:synthetic-env-reason"
    )
    captured = capsys.readouterr()
    evidence = (
        json.dumps([cold, warm, requests]).encode()
        + b"".join(path.read_bytes() for path in (tmp_path / ".pytest-telescope").rglob("*.json"))
        + captured.out.encode()
        + captured.err.encode()
    )
    for raw in (b"synthetic-param-id", b"synthetic-param-value", b"synthetic-env-reason"):
        assert raw not in evidence


def test_skipped_file_selector_is_covered_without_a_fabricated_case(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skipped.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
pytest.skip("file unavailable", allow_module_level=True)
""",
    )
    selected = controller.run(tmp_path, arguments=["tests/test_skipped.py"])
    assert selected["status"] == "no_tests" and selected["inventory_complete"]
    assert selected["units"] == [] and selected["counts"]["selected"] == 0
    assert selected["collection"]["skips"] == [
        {
            "suite": "default",
            "route": "scoped",
            "file": "tests/test_skipped.py",
            "phase": "collect",
            "reason": digest("Skipped: file unavailable"),
        }
    ]


@pytest.mark.parametrize("file", ["tests/test_skipped.py", "tests/test_passing.py"])
def test_missing_explicit_node_is_not_satisfied_by_file_coverage(tmp_path, file):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skipped.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
pytest.skip("file unavailable", allow_module_level=True)
""",
    )
    put(
        tmp_path,
        "tests/test_passing.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_real(): pass
""",
    )
    with pytest.raises(DeclarationError):
        controller.run(tmp_path, arguments=[file + "::test_nonexistent"])


@pytest.mark.parametrize("skipped", [False, True])
def test_ordinary_file_without_collected_cases_has_honest_file_coverage(tmp_path, skipped):
    configure(tmp_path)
    source = (
        'import pytest\npytest.skip("ordinary unavailable", allow_module_level=True)\n'
        if skipped
        else "def helper(): pass\n"
    )
    put(tmp_path, "tests/test_ordinary_empty.py", source)
    report = controller.run(tmp_path, arguments=["tests/test_ordinary_empty.py"])
    assert report["status"] == "no_tests" and report["inventory_complete"]
    assert report["units"] == [] and report["counts"]["selected"] == 0
    expected = (
        [
            {
                "suite": "default",
                "route": "ordinary",
                "file": "tests/test_ordinary_empty.py",
                "phase": "collect",
                "reason": digest("Skipped: ordinary unavailable"),
            }
        ]
        if skipped
        else []
    )
    assert report["collection"]["skips"] == expected


def test_mixed_collection_skips_survive_inventory_reuse_without_fake_members(tmp_path):
    configure(tmp_path)
    put(
        tmp_path,
        "tests/test_skipped.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
pytest.skip("file unavailable", allow_module_level=True)
""",
    )
    put(
        tmp_path,
        "tests/test_passing.py",
        """import pytest
pytestmark = pytest.mark.telescope(inputs=[])
def test_real(): pass
""",
    )
    cold = controller.run(tmp_path)
    warm = controller.run(tmp_path)
    assert cold["status"] == warm["status"] == "passed"
    assert len(cold["units"]) == len(warm["units"]) == 1
    assert cold["counts"]["executed"] == warm["counts"]["reused"] == 1
    assert warm["collection"]["outcome"] == "reused"
    assert (
        cold["collection"]["skips"]
        == warm["collection"]["skips"]
        == [
            {
                "suite": "default",
                "route": "scoped",
                "file": "tests/test_skipped.py",
                "phase": "collect",
                "reason": digest("Skipped: file unavailable"),
            }
        ]
    )
