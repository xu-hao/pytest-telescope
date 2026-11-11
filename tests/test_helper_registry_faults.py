"""Bounded frozen-source, record, link, cache and lifecycle failure controls."""

import ast
import contextlib
import json

import pytest
from test_helper_registry import consumer, no_new_records, project_at, put
from test_helper_registry_contract import passed
from test_static_parse_bounds import refuses_before_any_sandbox

from pytest_telescope import controller, frozen_registry, markers, sandbox
from pytest_telescope.model import DeclarationError, InfrastructureError, fingerprint
from pytest_telescope.result import validate_result


@pytest.mark.parametrize(
    "fault",
    [
        "changed_bytes",
        "missing_file",
        "missing_hash",
        "wrong_hash",
        "unsupported_kind",
        "escaping_path",
        "extra_field",
        "duplicate_record",
        "missing_record",
        "nonrecord",
        "invalid_mode",
    ],
)
def test_frozen_helper_source_and_record_faults_refuse_without_success(
    tmp_path, monkeypatch, fault
):
    root = project_at(tmp_path)
    snapshot = controller.snapshot

    def faulty_snapshot(manifest, unit, destination):
        records = snapshot(manifest, unit, destination)
        record = next(row for row in records if row["path"] == "helpers/support.py")
        target = destination / record["path"]
        if fault == "changed_bytes":
            target.chmod(0o644)
            target.write_bytes(target.read_bytes() + b"\n# changed frozen bytes\n")
            target.chmod(0o444)
        elif fault == "missing_file":
            target.unlink()
        elif fault == "missing_hash":
            del record["sha256"]
        elif fault == "wrong_hash":
            record["sha256"] = "0" * 64
        elif fault == "unsupported_kind":
            record["kind"] = "device"
        elif fault == "escaping_path":
            record["path"] = "../support.py"
        elif fault == "extra_field":
            record["extra"] = True
        elif fault == "duplicate_record":
            records.append(dict(record))
        elif fault == "missing_record":
            records.remove(record)
        elif fault == "nonrecord":
            records.append("not a frozen file record")
        elif fault == "invalid_mode":
            record["mode"] = True
        return records

    monkeypatch.setattr(controller, "snapshot", faulty_snapshot)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    assert report["collection"]["outcome"] == "infrastructure_error"
    assert report["collection"]["reason"]
    assert report["counts"]["executed"] == report["counts"]["reused"] == 0
    no_new_records(root)


def test_unreadable_frozen_helper_has_a_bounded_infrastructure_outcome(tmp_path, monkeypatch):
    root = project_at(tmp_path)
    regular_bytes = frozen_registry.regular_bytes
    denied = []

    def read_frozen(directory, name, limit):
        if name == "helpers/support.py":
            denied.append(name)
            raise PermissionError("owned inaccessible frozen helper")
        return regular_bytes(directory, name, limit)

    monkeypatch.setattr(frozen_registry, "regular_bytes", read_frozen)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    assert report["collection"]["reason"] == "cannot read frozen declaration source"
    assert denied == ["helpers/support.py"]
    no_new_records(root)


@pytest.mark.parametrize("phase", ["collection", "case"])
def test_changed_helper_after_parsing_is_reverified_before_each_fresh_worker(
    tmp_path, monkeypatch, phase
):
    root = project_at(tmp_path)
    materialize = controller.materialize
    calls = []
    changed = False

    def changed_materialize(pool, unit, destination, records):
        nonlocal changed
        materialize(pool, unit, destination, records)
        calls.append(destination)
        if not changed and len(calls) == (1 if phase == "collection" else 2):
            helper = pool / "helpers/support.py"
            helper.chmod(0o644)
            helper.write_bytes(helper.read_bytes() + b"\n# deterministic post-parse change\n")
            helper.chmod(0o444)
            changed = True

    monkeypatch.setattr(controller, "materialize", changed_materialize)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    assert len(calls) == (2 if phase == "collection" else 3)
    assert len(set(calls)) == len(calls)
    assert all(not path.exists() for path in calls)
    assert report["counts"]["executed"] == report["counts"]["reused"] == 0
    assert not list((root / ".pytest-telescope/cache").glob("*.json"))
    assert not list((root / ".pytest-telescope/scratch").iterdir())


@pytest.mark.parametrize(
    "fault", ["target_text", "regular_replacement", "cycle", "escape", "target_bytes"]
)
def test_frozen_helper_link_mutation_cannot_change_its_authenticated_origin(
    tmp_path, monkeypatch, fault
):
    root = project_at(tmp_path)
    (root / "helpers/alias.py").symlink_to("support.py")
    consumer(
        root,
        inputs=["helpers/__init__.py", "helpers/alias.py", "helpers/support.py", "data/answer.txt"],
    )
    source = root / "tests/test_consumer.py"
    source.write_text(source.read_text().replace("from helpers.support", "from helpers.alias"))
    snapshot = controller.snapshot

    def faulty_snapshot(manifest, unit, destination):
        records = snapshot(manifest, unit, destination)
        record = next(row for row in records if row["path"] == "helpers/alias.py")
        alias = destination / record["path"]
        if fault == "target_bytes":
            target = destination / "helpers/support.py"
            target.chmod(0o644)
            target.write_bytes(target.read_bytes() + b"\n# changed alias target\n")
            target.chmod(0o444)
        else:
            alias.unlink()
            if fault == "regular_replacement":
                alias.write_text("# not a link anymore\n")
            else:
                target = {
                    "target_text": "other.py",
                    "cycle": "alias.py",
                    "escape": "../../outside.py",
                }[fault]
                alias.symlink_to(target)
                if fault != "target_text":
                    record["target"] = target
        return records

    monkeypatch.setattr(controller, "snapshot", faulty_snapshot)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    no_new_records(root)


@pytest.mark.parametrize("target", ["support.py", "../../outside.py"])
def test_helper_alias_omitted_or_escaping_target_is_refused(tmp_path, target):
    root = project_at(tmp_path)
    (root.parent / "outside.py").write_text("# owned outside-project target\n")
    (root / "helpers/alias.py").symlink_to(target)
    consumer(root, inputs=["helpers/__init__.py", "helpers/alias.py"])

    with pytest.raises(DeclarationError, match="target|escapes"):
        controller.run(root, workers=1)

    no_new_records(root)


def test_declared_dangling_python_leaf_link_and_absence_remain_exact(tmp_path):
    root = project_at(tmp_path)
    (root / "helpers/alias.py").symlink_to("missing.source")
    put(
        root,
        "tests/test_consumer.py",
        "import pytest\nfrom pathlib import Path\n"
        'pytestmark = pytest.mark.telescope(inputs=["helpers/alias.py"], absent=["helpers/missing.source"])\n'
        'def test_absence():\n    alias = Path("helpers/alias.py")\n'
        "    assert alias.is_symlink()\n    assert not alias.exists()\n"
        '    assert alias.readlink().as_posix() == "missing.source"\n',
    )

    passed(root, executed=1)
    passed(root, reused=1, executed=0)
    put(root, "helpers/missing.source", "now present\n")

    with pytest.raises(DeclarationError, match="expected-absent"):
        controller.run(root, workers=1)


def depth(tree):
    return max((depth(child) + 1 for child in ast.iter_child_nodes(tree)), default=0)


@pytest.mark.parametrize(
    "budget",
    [
        "source_file",
        "source_total",
        "nodes_file",
        "nodes_total",
        "depth",
        "markers_file",
        "markers_total",
        "aliases",
    ],
)
def test_helper_source_parser_and_alias_budgets_are_bounded(tmp_path, monkeypatch, budget):
    root = project_at(tmp_path)
    helper = root / "helpers/support.py"
    helper.write_text(
        helper.read_text()
        + "\n"
        + "\n".join(f"VALUE_{number} = {number}" for number in range(40))
        + "\n"
    )
    test_source = (root / "tests/test_consumer.py").read_bytes()
    helper_source = helper.read_bytes()
    test_nodes = len(list(ast.walk(ast.parse(test_source))))
    helper_nodes = len(list(ast.walk(ast.parse(helper_source))))
    if budget == "source_file":
        monkeypatch.setattr(markers, "MAX_SOURCE", len(test_source) + 1)
    elif budget == "source_total":
        monkeypatch.setattr(markers, "MAX_TOTAL_SOURCE", len(test_source) + len(helper_source) - 1)
    elif budget == "nodes_file":
        monkeypatch.setattr(markers, "MAX_NODES", test_nodes + 1)
    elif budget == "nodes_total":
        monkeypatch.setattr(markers, "MAX_TOTAL_NODES", test_nodes + helper_nodes - 1)
    elif budget == "depth":
        helper.write_text(helper.read_text() + "DEEP = " + "[" * 24 + "0" + "]" * 24 + "\n")
        monkeypatch.setattr(markers, "MAX_DEPTH", depth(ast.parse(test_source)) + 1)
    elif budget == "markers_file":
        helper.write_text(
            helper.read_text()
            + "\nclass Extra:\n    pytestmark = pytest.mark.telescope(inputs=[])\n"
        )
        monkeypatch.setattr(markers, "MAX_MARKERS", 1)
    elif budget == "markers_total":
        monkeypatch.setattr(markers, "MAX_MARKERS", 1)
    elif budget == "aliases":
        # An alias is parsed once but authenticates its own records: one test marker
        # and one helper marker fit, three more alias origins of the helper do not.
        inputs = ["helpers/__init__.py", "helpers/support.py", "data/answer.txt"]
        for number in range(3):
            name = f"helpers/alias_{number}.py"
            (root / name).symlink_to("support.py")
            inputs.append(name)
        consumer(root, inputs=inputs)
        monkeypatch.setattr(markers, "MAX_MARKERS", 2)

    refuses_before_any_sandbox(root, monkeypatch, "limit|budget|node|marker|syntax")

    no_new_records(root)


def test_repeated_views_share_one_bounded_parse_per_frozen_origin(tmp_path, monkeypatch):
    root = project_at(tmp_path)
    put(root, "tests/test_second.py", (root / "tests/test_consumer.py").read_text())
    total = sum(
        (root / name).stat().st_size
        for name in (
            "tests/test_consumer.py",
            "tests/test_second.py",
            "helpers/support.py",
            "helpers/__init__.py",
        )
    )
    monkeypatch.setattr(markers, "MAX_TOTAL_SOURCE", total)

    passed(root, executed=2, selected=2)


@pytest.mark.parametrize(
    "corruption",
    [
        "inventory_malformed",
        "inventory_foreign_origin",
        "case_malformed",
        "case_wrong_identity",
        "batch_missing_member",
    ],
)
def test_helper_cache_records_must_revalidate_exact_inventory_and_membership(tmp_path, corruption):
    root = project_at(tmp_path)
    mode = "batch" if corruption == "batch_missing_member" else "case"
    count = 2 if mode == "batch" else 1
    if mode == "batch":
        consumer(root, extra='def test_second(): assert answer() == "42"')
    passed(root, executed=count, execution_mode=mode)
    passed(root, reused=count, executed=0, execution_mode=mode)
    cache = root / ".pytest-telescope/cache"
    path = (
        next((cache / "inventory").glob("*.json"))
        if corruption.startswith("inventory")
        else next(cache.glob("*.json"))
    )
    if corruption.endswith("malformed"):
        path.write_text('{"incomplete":')
    else:
        record = json.loads(path.read_text())
        if corruption == "inventory_foreign_origin":
            found, _ = markers.discover(
                (root / "helpers/support.py").read_bytes(), "helpers/support.py"
            )
            item = record["result"]["items"][0]
            item["origins"] = sorted([*item["origins"], found[0]["id"]])
        elif corruption == "case_wrong_identity":
            expected = record["result"]["case_ids"][0]
            context = record["result"]["context_id"]
            assert validate_result(
                record["result"],
                successful_only=True,
                case_id=expected,
                context_id=context,
            )
            assert expected != "0" * 64
            record["result"]["case_ids"] = ["0" * 64]
            record["result"]["case_results"][0]["case_id"] = "0" * 64
            assert validate_result(record["result"], successful_only=True, context_id=context)
            assert not validate_result(
                record["result"],
                successful_only=True,
                case_id=expected,
                context_id=context,
            )
        elif corruption == "batch_missing_member":
            record["result"]["case_results"].pop()
        record["checksum"] = fingerprint(record["result"])
        path.write_text(json.dumps(record))

    report = passed(root, execution_mode=mode)

    if corruption.startswith("inventory"):
        assert report["collection"]["outcome"] == "executed"
        assert report["counts"]["reused"] == count
    else:
        assert report["counts"]["executed"] == count
        assert report["counts"]["reused"] == 0


@pytest.mark.parametrize("phase", ["collection", "execution", "batch"])
@pytest.mark.parametrize("exhaust", [False, True])
def test_helper_infrastructure_retry_is_bounded_with_fresh_scratch(
    tmp_path, monkeypatch, phase, exhaust
):
    root = project_at(tmp_path)
    execute = sandbox.execute
    attempts = []

    def unavailable(*args, request, invocation_directory, **options):
        if request["mode"] == phase:
            attempts.append(invocation_directory)
            if exhaust or len(attempts) == 1:
                if phase != "collection":
                    # Explicit launched-worker fault double: failed sessions count
                    # only after the production start callback has been signalled.
                    options["on_session_start"]()
                raise InfrastructureError("owned deterministic unavailable worker")
        return execute(*args, request=request, invocation_directory=invocation_directory, **options)

    monkeypatch.setattr(sandbox, "execute", unavailable)
    mode = "batch" if phase == "batch" else "case"

    report = controller.run(root, workers=1, execution_mode=mode)

    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert all(not directory.exists() for directory in attempts)
    assert not any(report["route_counts"]["ordinary"].values())
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    if exhaust:
        assert report["status"] == "infrastructure_error", report
        assert report["counts"]["executed"] == report["counts"]["reused"] == 0
        assert not list((root / ".pytest-telescope/cache").glob("*.json"))
        if phase != "collection":
            assert report["groups"][0]["attempts"] == 2
            assert report["groups"][0]["cache_eligible"] is False
    else:
        assert report["status"] == "passed", report
        assert report["counts"]["executed"] == 1
        if phase != "collection":
            assert report["groups"][0]["attempts"] == 2
            assert report["groups"][0]["timing_complete"] is False
        passed(root, reused=1, executed=0, execution_mode=mode)
        assert len(attempts) == 2


def test_helper_cleanup_failure_preserves_prior_hits_and_publishes_no_new_records(
    tmp_path, monkeypatch
):
    root = project_at(tmp_path)
    passed(root, executed=1)
    cache = root / ".pytest-telescope/cache"
    previous = {path: path.read_bytes() for path in cache.rglob("*.json")}
    put(root, "data/answer.txt", "42\n\n")
    invocation = sandbox.invocation
    allocations = 0

    @contextlib.contextmanager
    def failed_outer_cleanup(project):
        nonlocal allocations
        allocations += 1
        ordinal = allocations
        with invocation(project) as directory:
            yield directory
        if ordinal == 1:
            raise sandbox.CleanupError("owned outer cleanup receipt failed")

    monkeypatch.setattr(sandbox, "invocation", failed_outer_cleanup)

    report = controller.run(root, workers=1)

    assert report["status"] == "infrastructure_error", report
    assert report["counts"]["executed"] == 1
    assert all(not group["cache_eligible"] for group in report["groups"])
    assert {path: path.read_bytes() for path in cache.rglob("*.json")} == previous
    assert not list(cache.rglob("*.pending"))
    assert not list((root / ".pytest-telescope/scratch").iterdir())


def test_ordinary_only_execution_stays_fresh_without_snapshot_or_helper_discovery(
    tmp_path, monkeypatch
):
    root = project_at(tmp_path)
    put(
        root,
        "tests/test_consumer.py",
        'from pathlib import Path\ndef test_ordinary(): assert Path("helper-private.txt").is_file()\n',
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary-only work must not enter frozen scoped discovery")

    monkeypatch.setattr(controller, "snapshot", forbidden)
    monkeypatch.setattr(controller, "FrozenRegistry", forbidden)
    monkeypatch.setattr(sandbox, "prepare", forbidden)

    for _ in range(2):
        report = controller.run(root, workers=1)
        assert report["status"] == "passed", report
        assert report["counts"]["executed"] == 1
        assert report["units"][0]["route"] == "ordinary"
        assert report["sandbox"]["required"] is False
        assert report["counts"]["reused"] == 0
    assert not (root / ".pytest-telescope").exists()
