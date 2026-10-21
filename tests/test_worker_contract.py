"""Bounded sessions, owned scratch and strict worker result records.

The real-worker controls launch bwrap and are never skipped.
"""

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pytest_telescope import sandbox
from pytest_telescope.model import InfrastructureError

ENVIRONMENT = {"PATH": os.defpath}


def good_result():
    return {
        "protocol": 1,
        "nonce": "a" * 32,
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "case_ids": ["b" * 64],
        "worker_seconds": 0.2,
        "failures": [],
    }


def test_capture_stops_flood_before_later_side_effect(tmp_path):
    marker = tmp_path / "continued"
    program = (
        "import os,time; from pathlib import Path; "
        "os.write(1,b'x'*2097152); time.sleep(.3); "
        f"Path({str(marker)!r}).touch()"
    )
    with pytest.raises(InfrastructureError, match="output"):
        sandbox.capture([sys.executable, "-c", program], 5, env=ENVIRONMENT)
    assert not marker.exists()


def test_capture_bounds_stderr_flood_and_deadline():
    for program, timeout in [
        ("import os,time; os.write(2,b'x'*2097152); time.sleep(5)", 3),
        ("import time; time.sleep(5)", 0.1),
    ]:
        started = time.monotonic()
        with pytest.raises(InfrastructureError):
            sandbox.capture([sys.executable, "-c", program], timeout, env=ENVIRONMENT)
        assert time.monotonic() - started < 2


def test_deadline_kills_descendants_even_after_the_session_exits(tmp_path):
    marker = tmp_path / "descendant-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(.4); " + f"Path({str(marker)!r}).touch()"
    )
    program = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
    with pytest.raises(InfrastructureError, match="deadline"):
        sandbox.capture([sys.executable, "-c", program], 0.1, env=ENVIRONMENT)
    time.sleep(0.5)
    assert not marker.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", True),
        ("exit_code", False),
        ("exit_code", 3),
        ("complete", 1),
        ("leaked_descendants", 0),
        ("collected", True),
        ("passed", -1),
        ("failed", 1),
        ("skipped", "0"),
        ("worker_seconds", float("nan")),
        ("worker_seconds", -1),
        ("worker_seconds", True),
        ("case_ids", "b" * 64),
        ("case_ids", ["B" * 64]),
        ("complete", False),
        ("leaked_descendants", True),
        ("cpu_seconds", 0.1),
    ],
)
def test_invalid_result_is_never_accepted(field, value):
    from pytest_telescope.result import validate_result

    record = good_result()
    record[field] = value
    # Exercise the actual JSON-decoded values, not a source-text assertion.
    assert not validate_result(json.loads(json.dumps(record)), successful_only=True)


def test_duplicate_case_ids_and_unknown_fields_are_rejected():
    from pytest_telescope.result import validate_result

    record = good_result()
    record.update(collected=2, passed=2, case_ids=["b" * 64, "b" * 64])
    assert not validate_result(record)
    record = good_result()
    record["surprise"] = 1
    assert not validate_result(record)


def test_valid_outcome_modes_and_nonce():
    from pytest_telescope.result import validate_result

    record = good_result()
    assert validate_result(record, nonce="a" * 32, successful_only=True)
    assert not validate_result(record, nonce="c" * 32)
    failure = copy.deepcopy(record)
    failure.update(exit_code=1, passed=0, failed=1)
    assert validate_result(failure)
    assert not validate_result(failure, successful_only=True)
    empty = copy.deepcopy(record)
    empty.update(exit_code=5, collected=0, passed=0, case_ids=[])
    assert validate_result(empty)
    assert not validate_result(empty, successful_only=True)


def test_execution_success_cannot_claim_zero_cases():
    from pytest_telescope.result import validate_result

    empty = good_result()
    empty.update(collected=0, passed=0, case_ids=[])
    assert not validate_result(empty, successful_only=True)


def test_execution_case_binding_accepts_only_the_expected_case():
    from pytest_telescope.result import validate_result

    record = good_result()
    assert validate_result(record, successful_only=True, case_id="b" * 64)
    assert not validate_result(record, successful_only=True, case_id="c" * 64)


def collection_record():
    return {
        "protocol": 4,
        "kind": "collection",
        "nonce": "a" * 32,
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "items": [
            {
                "case_id": "c" * 64,
                "file": "test_subject.py",
                "origins": [],
                "aliases": sorted(["c" * 64, hashlib.sha256(b"test_subject.py").hexdigest()]),
                "selected": True,
                "order": 0,
            }
        ],
        "worker_seconds": 0.1,
        "failures": [],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", True),
        ("kind", "execution"),
        ("complete", 1),
        ("items", None),
        ("exit_code", False),
        ("worker_seconds", float("nan")),
    ],
)
def test_collection_protocol_rejects_malformed_records(field, value):
    from pytest_telescope.result import validate_collection

    result = collection_record()
    result[field] = value
    assert not validate_collection(result)


def test_collection_protocol_separates_inventory_from_executed_success():
    from pytest_telescope.result import validate_collection

    result = collection_record()
    assert validate_collection(result, nonce="a" * 32, successful_only=True)
    result["passed"] = 1
    assert not validate_collection(result)
    result.pop("passed")
    result["items"].append(copy.deepcopy(result["items"][0]))
    assert not validate_collection(result)


def test_collection_node_identity_must_belong_to_reported_file():
    from pytest_telescope.result import validate_collection

    result = collection_record()
    result["items"][0]["file"] = "other_subject.py"
    assert not validate_collection(result)


@pytest.mark.parametrize("order", [True, -1, 0.5, "0", None])
def test_collection_selected_order_must_be_an_explicit_position(order):
    from pytest_telescope.result import validate_collection

    result = collection_record()
    result["items"][0]["order"] = order
    assert not validate_collection(result)


def test_unselected_collection_item_has_no_execution_position():
    from pytest_telescope.result import validate_collection

    result = collection_record()
    item = result["items"][0]
    item["selected"] = False
    assert not validate_collection(result)
    item["order"] = None
    result["exit_code"] = 5
    assert validate_collection(result)


def ordinary_record():
    return {
        **good_result(),
        "kind": "ordinary",
        "inventory_complete": True,
        "dispatch_started": True,
        "declaration_error": False,
        "items": collection_record()["items"],
        "case_ids": ["c" * 64],
        "case_results": [{"case_id": "c" * 64, "outcome": "passed", "complete": True}],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "collection"},
        {"dispatch_started": False},
        {"case_ids": ["d" * 64]},
        {"inventory_complete": False},
        {"declaration_error": True},
        {"passed": 0},
        {"cpu_seconds": None},
    ],
)
def test_ordinary_session_record_is_strict(change):
    from pytest_telescope.result import validate_ordinary

    assert validate_ordinary(ordinary_record(), nonce="a" * 32)
    record = {**ordinary_record(), **change}
    assert not validate_ordinary(record)


def test_frozen_copies_are_owned_while_alive_and_removed_afterwards(tmp_path):
    with sandbox.invocation(tmp_path) as directory:
        (directory / "project").mkdir()
        (directory / "project" / "frozen.txt").write_text("frozen")
        (directory / "project" / "frozen.txt").chmod(0o444)
        sandbox.recover(tmp_path)  # Live owners are never removed.
        assert directory.exists()
    assert not directory.exists()


def test_recovery_removes_copies_whose_owner_exited(tmp_path):
    program = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(Path(sandbox.__file__).parents[1])!r})\n"
        "from pytest_telescope import sandbox\n"
        "context = sandbox.invocation(sys.argv[1])\n"
        "directory = context.__enter__()\n"
        "(directory / 'project').mkdir()\n"
        "print(directory, flush=True)\n"
        "os._exit(0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    abandoned = Path(completed.stdout.strip())
    assert abandoned.exists()
    sandbox.recover(tmp_path)
    assert not abandoned.exists()


def test_recovery_refuses_unrecognized_scratch_entries(tmp_path):
    with sandbox.invocation(tmp_path):
        pass
    (tmp_path / ".pytest-telescope/scratch/unrelated").mkdir()
    with pytest.raises(sandbox.CleanupError):
        sandbox.recover(tmp_path)
    assert (tmp_path / ".pytest-telescope/scratch/unrelated").exists()


def test_recovery_waits_for_an_invocation_being_registered(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    opening = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    finish = threading.Event()
    original_open = os.open

    def delayed_open(path, flags, *args, **kwargs):
        if Path(path).name == "owner.lock" and flags & os.O_CREAT:
            opening.set()
            assert release.wait(2)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sandbox.os, "open", delayed_open)

    def allocate():
        with sandbox.invocation(tmp_path):
            entered.set()
            assert finish.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(allocate)
        assert opening.wait(2)
        timer = threading.Timer(0.1, release.set)
        timer.start()
        try:
            sandbox.recover(tmp_path)
            assert entered.wait(2)
        finally:
            release.set()
            finish.set()
            timer.join()
            worker.result(timeout=2)


def test_fresh_execution_result_must_match_requested_case(tmp_path, monkeypatch):
    """A stand-in session answers for a different case; the result is refused."""

    def wrong_case(argv, timeout, **kwargs):
        request = Path(argv[argv.index(sandbox.REQUEST) - 1])
        result = good_result()
        result.update(nonce=json.loads(request.read_text())["nonce"], case_ids=["c" * 64])
        return 0, b"TELESCOPE_RESULT " + json.dumps(result).encode() + b"\n"

    monkeypatch.setattr(sandbox, "capture", wrong_case)
    box = sandbox.Sandbox("/stand-in/bwrap", sys.executable, (), "/usr/bin", "a" * 64, {})
    with sandbox.invocation(tmp_path) as directory:
        project = directory / "project"
        project.mkdir()
        with pytest.raises(InfrastructureError, match="identity"):
            sandbox.execute(
                box,
                project,
                ["test_subject.py"],
                {},
                10,
                root=tmp_path,
                invocation_directory=directory,
                request={
                    "mode": "execution",
                    "registry": [],
                    "case_id": "d" * 64,
                    "expected_digest": "a" * 64,
                },
            )


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    return sandbox.prepare(tmp_path_factory.mktemp("worker-contract"))


def case_identity(nodeid):
    return hashlib.sha256(
        json.dumps(["default", nodeid], separators=(",", ":")).encode()
    ).hexdigest()


def run_worker(root, box, source, *, arguments=(), timeout=10, registry=None):
    registry = [] if registry is None else registry
    with sandbox.invocation(root) as pool:
        project = pool / "project"
        project.mkdir(mode=0o755)
        (project / "test_subject.py").write_text(source)
        (project / "test_subject.py").chmod(0o444)
        args = ["-p", "no:cacheprovider", *arguments, "test_subject.py"]
        with sandbox.invocation(root) as directory:
            inventory = sandbox.execute(
                box,
                project,
                args,
                {},
                max(timeout, 10),
                root=root,
                invocation_directory=directory,
                request={"mode": "collection", "registry": registry, "selectors": []},
            )
        digest = hashlib.sha256(
            json.dumps(
                sorted(
                    (
                        {"case_id": row["case_id"], "origins": row["origins"]}
                        for row in inventory["items"]
                    ),
                    key=lambda row: row["case_id"],
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        results = []
        if inventory["exit_code"] == 0:
            for item in inventory["items"]:
                if not item["selected"]:
                    continue
                with sandbox.invocation(root) as directory:
                    results.append(
                        sandbox.execute(
                            box,
                            project,
                            args,
                            {},
                            timeout,
                            root=root,
                            invocation_directory=directory,
                            request={
                                "mode": "execution",
                                "registry": registry,
                                "case_id": item["case_id"],
                                "expected_digest": digest,
                            },
                        )
                    )
        return {
            "inventory": inventory,
            "exit_code": max(
                (result["exit_code"] for result in results), default=inventory["exit_code"]
            ),
            "complete": all(result["complete"] for result in results) and inventory["complete"],
            "collected": sum(result["collected"] for result in results),
            "passed": sum(result["passed"] for result in results),
            "failed": sum(result["failed"] for result in results),
            "skipped": sum(result["skipped"] for result in results),
            "failures": inventory["failures"]
            + [failure for result in results for failure in result["failures"]],
        }


def test_real_worker_outcomes_and_safe_failure_evidence(tmp_path, box):
    result = run_worker(
        tmp_path,
        box,
        """
import pytest
@pytest.mark.xfail
def test_xfail(): assert False
@pytest.mark.xfail
def test_xpass(): pass
@pytest.mark.xfail(strict=True)
def test_strict_xpass(): pass
@pytest.mark.skip
def test_skip(): pass
@pytest.fixture
def setup_failure(): raise ValueError("private exception body")
def test_setup(setup_failure): pass
@pytest.fixture
def teardown_failure():
    yield
    raise RuntimeError("private exception body")
def test_teardown(teardown_failure): pass
def test_pass(): pass
""",
    )
    assert result["exit_code"] == 1
    assert result["complete"] is True
    assert (result["collected"], result["passed"], result["failed"], result["skipped"]) == (
        7,
        2,
        3,
        2,
    )
    assert {(item["phase"], item["exception_type"]) for item in result["failures"]} == {
        ("call", "UnexpectedPass"),
        ("setup", "ValueError"),
        ("teardown", "RuntimeError"),
    }
    assert "private exception body" not in json.dumps(result)
    assert all(item["file"] == "test_subject.py" for item in result["failures"])


def test_real_worker_selection_can_be_empty(tmp_path, box):
    empty = run_worker(tmp_path, box, "def test_unselected(): pass", arguments=("-k", "absent"))
    assert empty["exit_code"] == 5
    assert empty["complete"] is True
    assert empty["collected"] == 0


@pytest.mark.parametrize(
    "source,timeout,match",
    [
        ("def test_deadline():\n import time; time.sleep(20)\n", 0.5, "deadline"),
        (
            "def test_leak():\n import subprocess; subprocess.Popen(['sleep','20'])\n",
            10,
            "descendant",
        ),
        ("def test_flood():\n import os; os.write(1,b'x'*2097152)\n", 10, "output"),
    ],
)
def test_real_worker_incidents_clean_owned_resources(tmp_path, box, source, timeout, match):
    with pytest.raises(InfrastructureError, match=match):
        run_worker(tmp_path, box, source, timeout=timeout)
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


def test_real_worker_startup_failure_is_bounded(tmp_path, box):
    from dataclasses import replace

    broken = replace(box, executable="/nonexistent/python")
    with pytest.raises(InfrastructureError, match="sandbox startup failed|complete result"):
        run_worker(tmp_path, broken, "def test_pass(): pass", timeout=5)
    assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())


def held(name):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect("\0" + name)
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.mark.parametrize("signum", [15, 9])
def test_real_controller_interruption_stops_the_sandbox_and_recovers(tmp_path, box, signum):
    name = "telescope-" + uuid.uuid4().hex
    source_root = str(Path(sandbox.__file__).resolve().parents[1])
    program = f"""
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pytest_telescope import sandbox
root = Path(sys.argv[2])
box = sandbox.prepare(root)
with sandbox.invocation(root) as directory:
    project = directory / 'project'
    project.mkdir(mode=0o755)
    (project / 'test_subject.py').write_text(
        "import socket, time\\n"
        "def test_wait():\\n"
        "    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\\n"
        "    held.bind('\\\\0{name}')\\n"
        "    held.listen()\\n"
        "    time.sleep(30)\\n"
    )
    (project / 'test_subject.py').chmod(0o444)
    nodeid = 'test_subject.py::test_wait'
    case_id = hashlib.sha256(json.dumps(['default', nodeid], separators=(',', ':')).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps([{{'case_id': case_id, 'origins': []}}],
                                       sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with sandbox.invocation(root) as worker:
        sandbox.execute(box, project, ['test_subject.py'], {{}}, 40, root=root,
                        invocation_directory=worker,
                        request={{'mode': 'execution', 'registry': [],
                                 'case_id': case_id, 'expected_digest': digest}})
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, source_root, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not held(name):
            assert process.poll() is None, process.communicate()
            time.sleep(0.05)
        assert held(name)
        os.kill(process.pid, signum)
        _, stderr = process.communicate(timeout=20)
        assert process.returncode != 0
        if signum == 15:
            assert b"InvocationInterrupted" in stderr
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and held(name):
            time.sleep(0.05)
        assert not held(name)
        sandbox.recover(tmp_path)
        assert not list((tmp_path / ".pytest-telescope/scratch").iterdir())
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        sandbox.recover(tmp_path)


def test_real_marker_inheritance_and_immutable_arguments(tmp_path, box):
    from pytest_telescope.markers import discover

    source = """
import pytest
pytestmark = pytest.mark.telescope(inputs=[])
class TestOrigins:
    pytestmark = pytest.mark.telescope(inputs=[])
    @pytest.mark.telescope(inputs=[])
    @pytest.mark.parametrize('value', [
        pytest.param(1, marks=pytest.mark.telescope(inputs=[]), id='one'),
        pytest.param(2, marks=pytest.mark.telescope(inputs=[]), id='two'),
    ])
    def test_origin(self, value):
        assert value in (1, 2)
"""
    registry, _ = discover(source.encode(), "test_subject.py")
    result = run_worker(tmp_path, box, source, registry=registry)
    assert result["exit_code"] == 0
    assert result["passed"] == 2
    assert all(len(row["origins"]) == 4 for row in result["inventory"]["items"])
    mutation = (
        "\nTestOrigins.test_origin.pytestmark[-1].kwargs['inputs'].append('undeclared.txt')\n"
    )
    registry, _ = discover((source + mutation).encode(), "test_subject.py")
    rejected = run_worker(tmp_path, box, source + mutation, registry=registry)
    assert rejected["exit_code"] == 4
    assert rejected["passed"] == 0


def test_real_equivalent_checkouts_share_a_cache_directory(tmp_path):
    from pytest_telescope.controller import run

    roots = [tmp_path / "checkout-a", tmp_path / "checkout-b"]
    for root in roots:
        root.mkdir()
        (root / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots = ["."]\n')
        (root / "test_subject.py").write_text(
            "import pytest\n"
            "@pytest.mark.telescope(inputs=[])\n"
            "def test_value(): assert 2 + 2 == 4\n"
        )
    first = run(roots[0], cache_dir=tmp_path / "shared-cache")
    second = run(roots[1], cache_dir=tmp_path / "shared-cache")
    assert first["counts"]["executed"] == 1
    assert second["counts"]["reused"] == 1
    assert second["counts"]["executed"] == 0
