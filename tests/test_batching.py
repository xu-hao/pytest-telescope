"""Observable real-Bubblewrap controls for the optional identical-scope batch profile.

These controls launch bwrap and are never skipped.
"""

import hashlib
import json
from dataclasses import replace

import pytest

from pytest_telescope import controller, sandbox
from pytest_telescope.model import DeclarationError, fingerprint
from pytest_telescope.plan import make_plan


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def configure(root, *, mode="batch", extra=""):
    put(
        root,
        "pyproject.toml",
        f'''
[tool.pytest.ini_options]
pythonpath = ["."]
[tool.pytest_telescope]
test_roots = ["tests"]
[tool.pytest_telescope.execution]
mode = "{mode}"
{extra}
''',
    )


def case_id(node, suite="default"):
    return hashlib.sha256(json.dumps([suite, node], separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def project(tmp_path):
    return tmp_path


def run(root, *, status="passed", **options):
    report = controller.run(root, explain=True, **options)
    assert report["status"] == status, report
    assert report["schema"] == 3 and report["kind"] == "run"
    assert report["inventory_complete"] is True
    assert report["grouping_complete"] is True
    selected = {row["case_id"]: row for row in report["units"] if row["selected"]}
    grouped = [member for group in report["groups"] for member in group["case_ids"]]
    assert len(grouped) == len(set(grouped)) == len(selected)
    assert set(grouped) == set(selected)
    for group in report["groups"]:
        assert len(group["group_id"]) == 64
        assert group["unresolved"] == 0
        assert sum(
            group[field] for field in ("passed", "failed", "skipped", "not_run", "unresolved")
        ) == len(group["case_ids"])
        for member in group["case_ids"]:
            assert selected[member]["group_id"] == group["group_id"]
            assert selected[member]["attempts"] == group["attempts"]
    assert all(row["group_id"] is None for row in report["units"] if not row["selected"])
    if report["execution"]["mode"] == "batch":
        assert report["execution"]["profile"] == "batch-v1"
        assert all(row["worker_seconds"] is None for row in report["units"])
    assert all("cpu_seconds" not in group for group in report["groups"])
    return report


def simple_module(root, filename="tests/test_simple.py", count=3):
    put(
        root,
        filename,
        "import pytest\npytestmark = pytest.mark.telescope(inputs=[])\n"
        + "\n".join(f"def test_{number}(): assert True" for number in range(count))
        + "\n",
    )


def test_batch_shares_fixture_lifetimes_and_scratch_without_widening_scope(project):
    configure(project)
    put(project, "undeclared.txt", "synthetic undeclared resource")
    put(
        project,
        "tests/test_shared.py",
        """
from pathlib import Path
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
created = {"session": 0, "module": 0, "class": 0}
@pytest.fixture(scope="session")
def session_state(tmp_path_factory):
    created["session"] += 1
    value = {"indices": [], "paths": [], "shared": tmp_path_factory.mktemp("shared")}
    yield value
    assert value["indices"] == [0, 1, 2]
    assert len(set(value["paths"])) == 3
@pytest.fixture(scope="module")
def module_state():
    created["module"] += 1
    value = []
    yield value
    assert value == [0, 1, 2]
@pytest.fixture(scope="class")
def class_state():
    created["class"] += 1
    value = []
    yield value
    assert value == [0, 1, 2]
class TestShared:
    @pytest.mark.parametrize("number", [0, 1, 2])
    def test_lifetime(self, number, session_state, module_state, class_state, tmp_path):
        assert created == {"session": 1, "module": 1, "class": 1}
        assert session_state["indices"] == module_state == class_state == list(range(number))
        assert not Path("undeclared.txt").exists()
        with pytest.raises(FileNotFoundError): Path("undeclared.txt").read_text()
        scratch = Path("/tmp/batch-state")
        if number == 0:
            assert not scratch.exists()
            scratch.write_text("shared")
        else:
            assert scratch.read_text() == "shared"
        assert session_state["shared"].is_dir()
        assert str(tmp_path) not in session_state["paths"]
        session_state["paths"].append(str(tmp_path))
        for values in (session_state["indices"], module_state, class_state): values.append(number)
""",
    )
    cold = run(project)
    (group,) = cold["groups"]
    assert group["outcome"] == "executed" and group["complete"] and group["cache_eligible"]
    assert (group["passed"], group["failed"], group["skipped"], group["not_run"]) == (3, 0, 0, 0)
    assert group["case_ids"] == [
        case_id(f"tests/test_shared.py::TestShared::test_lifetime[{number}]") for number in range(3)
    ]
    warm = run(project)
    assert warm["counts"]["reused"] == 3
    assert warm["groups"][0]["outcome"] == "reused"
    assert warm["groups"][0]["attempts"] == 0
    assert warm["groups"][0]["worker_seconds"] == 0


def test_default_case_mode_and_singleton_batch_profiles_are_mutually_ineligible(project):
    configure(project, mode="case")
    simple_module(project, count=1)
    assert make_plan(project)["execution"] == {"mode": "case", "profile": "case-v1", "workers": 1}
    first = run(project)
    assert first["execution"] == {"mode": "case", "profile": "case-v1", "workers": 1}
    assert len(first["groups"]) == 1
    batch = run(project, execution_mode="batch")
    assert batch["counts"]["executed"] == 1 and batch["counts"]["reused"] == 0
    assert first["groups"][0]["group_id"] != batch["groups"][0]["group_id"]
    assert run(project, execution_mode="batch")["counts"]["reused"] == 1
    assert run(project)["counts"]["reused"] == 1
    assert 'mode = "case"' in (project / "pyproject.toml").read_text()


def test_unequal_scopes_remain_separate_and_equivalent_expansions_share(project):
    configure(project, extra='[tool.pytest_telescope.bundles.left]\ninputs=["left.txt"]\n')
    put(project, "left.txt", "left")
    put(project, "right.txt", "right")
    put(
        project,
        "tests/test_scopes.py",
        """
from pathlib import Path
import pytest
@pytest.mark.telescope(inputs=["left.txt"])
def test_left():
    assert Path("left.txt").read_text().startswith("left")
    assert not Path("right.txt").exists()
@pytest.mark.telescope(bundles=["left"])
def test_left_again():
    assert Path("left.txt").exists()
    assert not Path("right.txt").exists()
@pytest.mark.telescope(inputs=["right.txt"])
def test_right():
    assert Path("right.txt").read_text() == "right"
    assert not Path("left.txt").exists()
""",
    )
    cold = run(project)
    assert sorted(len(group["case_ids"]) for group in cold["groups"]) == [1, 2]
    assert run(project)["counts"]["reused"] == 3
    put(project, "left.txt", "left changed")
    changed = run(project)
    assert changed["counts"]["executed"] == 2 and changed["counts"]["reused"] == 1
    source = project / "tests/test_scopes.py"
    source.write_text(source.read_text() + "# changed source identity\n")
    assert run(project)["counts"]["executed"] == 3


def test_groups_never_cross_module_or_suite_boundaries(project):
    put(
        project,
        "pyproject.toml",
        """
[tool.pytest_telescope.execution]
mode = "batch"
[tool.pytest_telescope.suites.first]
root = "."
config = "pyproject.toml"
test_roots = ["tests"]
[tool.pytest_telescope.suites.second]
root = "."
config = "pyproject.toml"
test_roots = ["tests"]
""",
    )
    put(
        project,
        "conftest.py",
        'import pytest\n@pytest.fixture(scope="session")\ndef shared(): return []\ndef pytest_collection_modifyitems(items):\n    items.sort(key=lambda item: item.path.name, reverse=True)\n',
    )
    for filename in ("tests/test_one.py", "tests/test_two.py"):
        put(
            project,
            filename,
            "import pytest\npytestmark = pytest.mark.telescope(inputs=['conftest.py'])\n"
            "def test_first(shared):\n    assert shared == []\n    shared.append(1)\ndef test_second(shared):\n    assert shared == [1]\n",
        )
    report = run(project)
    assert len(report["groups"]) == 4
    assert all(len(group["files"]) == 1 for group in report["groups"])
    assert [(group["suite"], group["files"][0]) for group in report["groups"]] == sorted(
        (group["suite"], group["files"][0]) for group in report["groups"]
    )
    assert {(group["suite"], group["files"][0]) for group in report["groups"]} == {
        (suite, filename)
        for suite in ("first", "second")
        for filename in ("tests/test_one.py", "tests/test_two.py")
    }
    assert all(len(group["case_ids"]) == 2 for group in report["groups"])
    assert report["counts"]["executed"] == 8


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_preserves_actual_pytest_order_including_plugin_reordering(project, reverse):
    configure(project, extra='[tool.pytest_telescope.defaults]\ninputs=["tests/conftest.py"]\n')
    put(
        project,
        "tests/conftest.py",
        "def pytest_collection_modifyitems(items):\n    "
        + ("items.reverse()" if reverse else "pass")
        + "\n",
    )
    names = ["zeta", "alpha", "middle"]
    expected = list(reversed(names)) if reverse else names
    source = "import pytest\npytestmark=pytest.mark.telescope(inputs=[])\nseen=[]\n"
    for name in names:
        before = expected[: expected.index(name)]
        source += f'def test_{name}():\n    assert seen == {before!r}\n    seen.append("{name}")\n'
    put(project, "tests/test_order.py", source)
    report = run(project)
    assert report["groups"][0]["case_ids"] == [
        case_id(f"tests/test_order.py::test_{name}") for name in expected
    ]
    assert run(project)["counts"]["reused"] == 3


def test_selection_and_membership_are_atomic_group_identity(project):
    configure(project)
    simple_module(project)
    all_cases = run(project)
    selected = run(project, arguments=["-k", "test_0 or test_2"])
    assert selected["counts"]["executed"] == 2 and selected["counts"]["not-selected"] == 1
    assert all_cases["groups"][0]["group_id"] != selected["groups"][0]["group_id"]
    assert selected["groups"][0]["case_ids"] == [
        case_id(f"tests/test_simple.py::test_{number}") for number in (0, 2)
    ]
    (unselected,) = [row for row in selected["units"] if not row["selected"]]
    assert unselected["group_id"] is None and unselected["result_status"] is None
    assert run(project, arguments=["-k", "test_0 or test_2"])["counts"]["reused"] == 2
    assert run(project)["counts"]["reused"] == 3
    simple_module(project, count=4)
    assert run(project)["counts"]["executed"] == 4


@pytest.mark.parametrize("corruption", ["missing", "malformed", "truncated", "missing-member"])
def test_missing_or_corrupt_group_evidence_reruns_every_member(project, corruption):
    configure(project)
    simple_module(project)
    assert run(project)["counts"]["executed"] == 3
    records = list((project / ".pytest-telescope/cache").glob("*.json"))
    assert len(records) == 1
    if corruption == "missing":
        records[0].unlink()
    elif corruption == "malformed":
        records[0].write_text('{"incomplete":')
    elif corruption == "missing-member":
        record = json.loads(records[0].read_text())
        record["result"]["case_results"].pop()
        record["checksum"] = fingerprint(record["result"])
        records[0].write_text(json.dumps(record))
    else:
        content = records[0].read_bytes()
        records[0].write_bytes(content[: len(content) // 2])
    report = run(project)
    assert report["counts"]["executed"] == 3 and report["counts"]["reused"] == 0
    assert len(report["groups"]) == 1


def test_noncacheable_policy_separates_groups_and_no_cache_disables_all_reuse(project):
    configure(project)
    put(
        project,
        "tests/test_policy.py",
        """
import pytest
pytestmark=pytest.mark.telescope(inputs=[])
def test_first(): pass
@pytest.mark.telescope(cache=False)
def test_noncacheable(): pass
def test_last(): pass
""",
    )
    cold = run(project)
    assert sorted(len(group["case_ids"]) for group in cold["groups"]) == [1, 2]
    assert sorted(group["cache_eligible"] for group in cold["groups"]) == [False, True]
    warm = run(project)
    assert warm["counts"]["executed"] == 1 and warm["counts"]["reused"] == 2
    disabled = run(project, no_cache=True)
    assert disabled["counts"]["executed"] == 3 and disabled["counts"]["reused"] == 0
    assert not any(group["cache_eligible"] for group in disabled["groups"])
    assert run(project, force=True)["counts"]["executed"] == 3


def test_metadata_and_sandbox_identity_invalidate_whole_batch(project, monkeypatch):
    configure(
        project,
        extra="""
[tool.pytest_telescope.distributions.example]
format="pep621-static-v1"
metadata="package/pyproject.toml"
""",
    )
    put(project, "package/pyproject.toml", '[project]\nname="example-batch"\nversion="1.0"\n')
    put(
        project,
        "tests/test_metadata.py",
        """
from importlib.metadata import version
import pytest
pytestmark=pytest.mark.telescope(distributions=["example"])
def test_first(): assert version("example-batch") in ("1.0", "2.0")
def test_second(): assert version("example-batch") in ("1.0", "2.0")
""",
    )
    assert run(project)["counts"]["executed"] == 2
    assert run(project)["counts"]["reused"] == 2
    put(project, "package/pyproject.toml", '[project]\nname="example-batch"\nversion="2.0"\n')
    assert run(project)["counts"]["executed"] == 2
    original = sandbox.prepare
    # A different interpreter or set of installed distributions changes this identity.
    monkeypatch.setattr(sandbox, "prepare", lambda root: replace(original(root), identity="0" * 64))
    assert run(project)["counts"]["executed"] == 2


@pytest.mark.parametrize("arguments", [["-x"], ["--maxfail=1"]])
def test_pytest_stop_preserves_observed_results_and_accounts_unrun_members(project, arguments):
    configure(project)
    put(
        project,
        "tests/test_stop.py",
        """
import pytest
pytestmark=pytest.mark.telescope(inputs=[])
def test_first(): pass
def test_second(): assert False
def test_third(): pass
""",
    )
    report = run(project, status="failed", arguments=arguments)
    (group,) = report["groups"]
    assert group["outcome"] == "incomplete" and group["stop_reason"] == "pytest-stop"
    assert not group["complete"] and not group["cache_eligible"]
    assert (group["passed"], group["failed"], group["skipped"], group["not_run"]) == (1, 1, 0, 1)
    assert {row["case_id"]: row["result_status"] for row in report["units"]} == {
        case_id("tests/test_stop.py::test_first"): "passed",
        case_id("tests/test_stop.py::test_second"): "failed",
        case_id("tests/test_stop.py::test_third"): "not-run",
    }
    assert report["counts"]["executed"] == 1 and report["counts"]["failed"] == 1
    assert report["counts"]["infrastructure_error"] == 1
    assert not list((project / ".pytest-telescope/cache").glob("*.json"))


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_shared_fixture_failure_invalidates_whole_batch(project, phase):
    configure(project)
    fixture = (
        '    raise RuntimeError("synthetic shared setup failure")\n'
        if phase == "setup"
        else '    yield\n    raise RuntimeError("synthetic shared teardown failure")\n'
    )
    put(
        project,
        "tests/test_fixture.py",
        """
import pytest
pytestmark=pytest.mark.telescope(inputs=[])
@pytest.fixture(scope="module")
def shared():
"""
        + fixture
        + """
def test_first(shared): pass
def test_second(shared): pass
""",
    )
    report = run(project, status="failed")
    (group,) = report["groups"]
    assert group["complete"] and not group["cache_eligible"] and group["outcome"] == "failed"
    assert group["failed"] == (2 if phase == "setup" else 1)
    assert group["passed"] == (0 if phase == "setup" else 1)
    assert not list((project / ".pytest-telescope/cache").glob("*.json"))
    assert run(project, status="failed")["counts"]["reused"] == 0


def test_skipped_members_are_counted_without_reason_diagnostics(project):
    configure(project)
    private_reason = "synthetic-private-skip-reason"
    put(
        project,
        "tests/test_skips.py",
        f'''
import pytest
pytestmark=pytest.mark.telescope(inputs=[])
def test_first(): pass
def test_second(): pytest.skip("{private_reason}")
''',
    )
    cold = run(project)
    assert cold["groups"][0]["passed"] == cold["groups"][0]["skipped"] == 1
    assert cold["groups"][0]["cache_eligible"]
    warm = run(project)
    assert warm["counts"]["reused"] == 2
    assert warm["case_outcomes"]["cached_passed"] == warm["case_outcomes"]["cached_skipped"] == 1
    for content in [
        json.dumps(cold).encode(),
        json.dumps(warm).encode(),
        *[path.read_bytes() for path in (project / ".pytest-telescope/cache").rglob("*.json")],
    ]:
        assert private_reason.encode() not in content


def test_cli_execution_mode_and_api_validation(project, monkeypatch, capsys):
    configure(project, mode="case")
    simple_module(project, count=2)
    monkeypatch.chdir(project)
    assert controller.main(["run", "--execution-mode", "batch"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == 3
    assert report["execution"] == {"mode": "batch", "profile": "batch-v1", "workers": 1}
    assert len(report["groups"]) == 1 and report["counts"]["executed"] == 2
    assert controller.main(["run", "--execution-mode", "invalid"]) == 4
    refused = json.loads(capsys.readouterr().out)
    assert refused["schema"] == 3 and refused["status"] == "declaration_error"
    with pytest.raises(DeclarationError):
        controller.run(project, execution_mode="invalid")


def test_oversized_group_refuses_entire_plan_before_any_fresh_execution(project, monkeypatch):
    from pytest_telescope.model import UnsupportedCapability

    configure(project)
    simple_module(project, "tests/test_a_small.py", count=1)
    put(
        project,
        "tests/test_z_large.py",
        """
import pytest
pytestmark=pytest.mark.telescope(inputs=[])
@pytest.mark.parametrize("number", range(4097))
def test_large(number): pass
""",
    )
    original = sandbox.execute
    collections = []

    def collection_only(*args, request, **kwargs):
        assert request["mode"] == "collection", (
            "an oversized planned batch must refuse before any fresh group"
        )
        collections.append(1)
        return original(*args, request=request, **kwargs)

    monkeypatch.setattr(sandbox, "execute", collection_only)
    with pytest.raises(UnsupportedCapability) as raised:
        controller.run(project)
    assert raised.value.capability == "execution:batch-size"
    assert collections
    assert not list((project / ".pytest-telescope/cache").glob("*.json"))
