"""Behavioral controls for bounded workers and strict result records."""

import copy
import hashlib
import json
import sys

import pytest

from pytest_telescope import runtime
from pytest_telescope.model import InfrastructureError


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
        "cpu_seconds": 0.1,
        "failures": [],
    }


def test_command_stops_flood_before_later_side_effect(tmp_path):
    marker = tmp_path / "continued"
    program = (
        "import os,time; from pathlib import Path; "
        "os.write(1,b'x'*2097152); time.sleep(.3); "
        f"Path({str(marker)!r}).touch()"
    )
    with pytest.raises(InfrastructureError):
        runtime.command([sys.executable, "-c", program], timeout=5)
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
        ("cpu_seconds", float("inf")),
        ("worker_seconds", -1),
        ("cpu_seconds", True),
        ("case_ids", "b" * 64),
        ("case_ids", ["B" * 64]),
        ("complete", False),
        ("leaked_descendants", True),
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


def test_command_stderr_flood_and_deadline_are_bounded(tmp_path):
    import time

    for program, timeout in [
        ("import os,time; os.write(2,b'x'*2097152); time.sleep(5)", 3),
        ("import time; time.sleep(5)", 0.1),
    ]:
        started = time.monotonic()
        with pytest.raises(InfrastructureError):
            runtime.command([sys.executable, "-c", program], timeout=timeout)
        assert time.monotonic() - started < 2


def test_invocation_journal_exists_before_freeze_and_cleans_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "command", lambda *a, **k: pytest.fail("unused Docker called"))
    with runtime.invocation(tmp_path) as directory:
        journal = json.loads((directory / "journal.json").read_text())
        assert journal["owner_pid"] > 0
        assert journal["owner_start"]
        assert journal["snapshot"] == "scratch/" + directory.name + "/project"
        (directory / "project").mkdir()
        runtime.recover(tmp_path)  # Live owners are never removed.
        assert directory.exists()
    assert not directory.exists()


def test_recovery_never_removes_a_container_with_different_ownership(tmp_path, monkeypatch):
    original_stop = runtime._stop_container
    with pytest.raises(runtime.CleanupError):
        with runtime.invocation(tmp_path) as directory:
            journal = json.loads((directory / "journal.json").read_text())
            journal["cleaned"] = False
            runtime._durable(directory / "journal.json", journal)
            calls = []

            def fake_command(args, **kwargs):
                calls.append(args)
                if args[1] == "ps":
                    return ("a" * 64 + "\n").encode()
                if args[1] == "inspect":
                    return json.dumps(
                        [
                            {
                                "Id": "a" * 64,
                                "Name": "/" + journal["name"],
                                "Config": {"Labels": {runtime.LABEL + "project": "unrelated"}},
                            }
                        ]
                    ).encode()
                pytest.fail("attempted to remove an unrelated container")

            monkeypatch.setattr(runtime, "command", fake_command)
            original_stop(journal)
    assert (directory / "journal.json").exists()
    assert not any(call[1] == "rm" for call in calls)


@pytest.fixture(scope="module")
def docker_runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("runtime-contract")
    metadata = runtime.build_runtime(root)
    yield root, metadata["image"]
    # Remove only images produced and registered by this fixture.
    for image in json.loads((root / ".pytest-telescope/images.json").read_text()):
        runtime._image_info(image, runtime._project(root))
        runtime.command(["docker", "image", "rm", image])


def run_worker(root, image, source, *, arguments=(), timeout=10, conftest=None, registry=None):
    import hashlib

    registry = [] if registry is None else registry
    with runtime.invocation(root) as pool:
        project = pool / "project"
        project.mkdir(mode=0o755)
        project.chmod(0o755)
        (project / "test_subject.py").write_text(source)
        (project / "test_subject.py").chmod(0o444)
        if conftest:
            (project / "conftest.py").write_text(conftest)
            (project / "conftest.py").chmod(0o444)
        args = ["-p", "no:cacheprovider", "-s", *arguments, "test_subject.py"]
        with runtime.invocation(root) as directory:
            inventory = runtime.execute(
                image,
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
                with runtime.invocation(root) as directory:
                    results.append(
                        runtime.execute(
                            image,
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


def test_real_worker_outcomes_and_safe_failure_evidence(docker_runtime):
    root, image = docker_runtime
    result = run_worker(
        root,
        image,
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


def test_real_worker_static_hosts_and_selection(docker_runtime):
    root, image = docker_runtime
    result = run_worker(
        root,
        image,
        """
import socket
from pathlib import Path
def test_policy():
    assert socket.gethostname() == 'telescope'
    assert Path('/etc/hostname').read_text() == 'telescope\\n'
    assert Path('/etc/resolv.conf').read_text() == '# No network resolver\\n'
    assert Path('/etc/hosts').read_text() == '127.0.0.1 localhost telescope\\n::1 localhost\\n'
""",
    )
    assert result["exit_code"] == 0
    empty = run_worker(root, image, "def test_unselected(): pass", arguments=("-k", "absent"))
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
def test_real_worker_incidents_clean_owned_resources(docker_runtime, source, timeout, match):
    root, image = docker_runtime
    with pytest.raises(InfrastructureError, match=match):
        run_worker(root, image, source, timeout=timeout)
    assert not list((root / ".pytest-telescope/scratch").iterdir())
    remaining = runtime.command(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=" + runtime.LABEL + "project=" + runtime._project(root),
        ]
    )
    assert remaining == b""


def test_real_worker_startup_failure_is_bounded(docker_runtime):
    root, _ = docker_runtime
    with pytest.raises(InfrastructureError, match="complete result"):
        run_worker(root, "sha256:" + "0" * 64, "def test_pass(): pass", timeout=5)
    assert not list((root / ".pytest-telescope/scratch").iterdir())


@pytest.mark.parametrize("signum", [15, 9])
def test_real_controller_interruption_and_recovery(docker_runtime, signum):
    import os
    import subprocess
    import time
    from pathlib import Path

    root, image = docker_runtime
    source_root = str(Path(runtime.__file__).resolve().parents[1])
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from pytest_telescope import runtime
from pathlib import Path
root=Path(sys.argv[2])
with runtime.invocation(root) as directory:
    project=directory/'project'
    project.mkdir(mode=0o755)
    project.chmod(0o755)
    (project/'test_subject.py').write_text(
        "def test_wait():\\n import time; from pathlib import Path; "
        "Path('/tmp/ready').touch(); time.sleep(30)\\n"
    )
    (project/'test_subject.py').chmod(0o444)
    import hashlib, json
    nodeid = 'test_subject.py::test_wait'
    case_id = hashlib.sha256(json.dumps(['default', nodeid], separators=(',',':')).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps([{'case_id': case_id, 'origins': []}],
                                      sort_keys=True,separators=(',',':')).encode()).hexdigest()
    runtime.execute(sys.argv[3],project,['test_subject.py'],{},40,root=root,
                    invocation_directory=directory,
                    request={'mode':'execution','registry':[],
                             'case_id':case_id,'expected_digest':digest})
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, source_root, str(root), image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        container_name = None
        while time.monotonic() < deadline:
            journals = list((root / ".pytest-telescope/scratch").glob("invocation-*/journal.json"))
            if journals:
                journal = json.loads(journals[0].read_text())
                running = (
                    runtime.command(
                        [
                            "docker",
                            "ps",
                            "--filter",
                            "name=^/" + journal["name"] + "$",
                            "--format",
                            "{{.Names}}",
                        ]
                    )
                    .decode()
                    .strip()
                )
                if (
                    running
                    and runtime._capture(
                        ["docker", "exec", running, "test", "-f", "/tmp/ready"], 5
                    )[0]
                    == 0
                ):
                    container_name = running
                    break
            time.sleep(0.05)
        assert container_name is not None
        os.kill(process.pid, signum)
        _, stderr = process.communicate(timeout=20)
        assert process.returncode != 0
        if signum == 15:
            assert b"InvocationInterrupted" in stderr
        runtime.recover(root)
        assert not list((root / ".pytest-telescope/scratch").iterdir())
        assert (
            runtime.command(["docker", "ps", "-aq", "--filter", "name=^/" + container_name + "$"])
            == b""
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        runtime.recover(root)


def test_deadline_kills_client_descendants_even_after_client_exits(tmp_path):
    import time

    marker = tmp_path / "descendant-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(.4); " + f"Path({str(marker)!r}).touch()"
    )
    program = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
    with pytest.raises(InfrastructureError, match="deadline"):
        runtime.command([sys.executable, "-c", program], timeout=0.1)
    time.sleep(0.5)
    assert not marker.exists()


def test_recovery_waits_for_an_invocation_being_registered(tmp_path, monkeypatch):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

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

    monkeypatch.setattr(runtime.os, "open", delayed_open)

    def allocate():
        with runtime.invocation(tmp_path):
            entered.set()
            assert finish.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(allocate)
        assert opening.wait(2)
        timer = threading.Timer(0.1, release.set)
        timer.start()
        try:
            runtime.recover(tmp_path)
            assert entered.wait(2)
        finally:
            release.set()
            finish.set()
            timer.join()
            worker.result(timeout=2)


def test_identity_tracks_filesystem_configuration_and_daemon_policy(tmp_path, monkeypatch):
    import hashlib

    state = tmp_path / ".pytest-telescope"
    state.mkdir()
    metadata = {
        "image": "sha256:" + "a" * 64,
        "recipe": "b" * 64,
        "worker": hashlib.sha256(runtime.worker_source()).hexdigest(),
        "policy": runtime.POLICY,
        "profile": "python-v1",
        "provenance": runtime.runtime_profiles.provenance("python-v1"),
    }
    runtime._durable(state / "runtime.json", metadata)
    inspected = {
        "Size": 100,
        "Os": "linux",
        "Architecture": "amd64",
        "RootFS": {"Type": "layers", "Layers": ["sha256:" + "c" * 64]},
        "Config": {
            "Labels": {
                runtime.LABEL + "project": runtime._project(tmp_path),
                runtime.LABEL + "profile": metadata["profile"],
                runtime.LABEL + "provenance": metadata["provenance"],
            },
            "Env": ["FIXED=1"],
        },
    }
    daemon = {
        "KernelVersion": "daemon-kernel-identity",
        "ServerVersion": "docker-test",
        "ID": "owned-daemon",
        "SecurityOptions": ["no-new-privileges"],
        "DefaultRuntime": "runc",
        "Runtimes": {"runc": {"path": "runc"}},
    }
    monkeypatch.setattr(runtime, "_local_endpoint", lambda: "unix:///owned-docker.sock")
    monkeypatch.setattr(runtime, "_image_info", lambda *args: copy.deepcopy(inspected))
    monkeypatch.setattr(runtime, "command", lambda *args, **kwargs: json.dumps(daemon).encode())
    identity = runtime.identity(tmp_path)
    assert identity["kernel"] == "daemon-kernel-identity"
    original = identity["execution"]
    inspected["Config"]["Labels"][runtime.LABEL + "project"] = "other-owned-project"
    assert runtime.identity(tmp_path)["execution"] == original
    inspected["Config"]["Env"] = ["FIXED=2"]
    assert runtime.identity(tmp_path)["execution"] != original
    inspected["Config"]["Env"] = ["FIXED=1"]
    inspected["RootFS"]["Layers"] = ["sha256:" + "d" * 64]
    assert runtime.identity(tmp_path)["execution"] != original
    inspected["RootFS"]["Layers"] = ["sha256:" + "c" * 64]
    daemon["SecurityOptions"] = ["changed-policy"]
    assert runtime.identity(tmp_path)["execution"] != original
    daemon["SecurityOptions"] = ["no-new-privileges"]
    daemon["ID"] = "different-daemon"
    assert runtime.identity(tmp_path)["execution"] != original


def test_remote_endpoint_is_rejected_before_any_worker_mount(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example.invalid:2376")
    with pytest.raises(InfrastructureError, match="local Unix"):
        runtime._local_endpoint()


def test_oversized_requirements_and_nonregular_state_are_rejected(tmp_path, monkeypatch):
    import os

    from pytest_telescope.model import DeclarationError

    monkeypatch.setattr(runtime, "_local_endpoint", lambda: "unix:///unused.sock")
    requirements = tmp_path / "requirements.txt"
    with requirements.open("wb") as stream:
        stream.truncate(65537)
    with pytest.raises(DeclarationError, match="64 KiB"):
        runtime.build_runtime(tmp_path, requirements)
    fifo = tmp_path / "state.fifo"
    os.mkfifo(fifo)
    with pytest.raises(InfrastructureError, match="regular file"):
        runtime._read(fifo, 128)


def test_real_equivalent_checkouts_reuse_across_owned_runtime_images(tmp_path, docker_runtime):
    from pytest_telescope.controller import run

    roots = [tmp_path / "checkout-a", tmp_path / "checkout-b"]
    images = []
    try:
        for root in roots:
            root.mkdir()
        (roots[0] / "pyproject.toml").write_text('[tool.pytest_telescope]\ntest_roots = ["."]\n')
        (roots[0] / "test_subject.py").write_text("def test_value(): assert 2 + 2 == 4\n")
        for name in ("pyproject.toml", "test_subject.py"):
            (roots[1] / name).write_bytes((roots[0] / name).read_bytes())
        for root in roots:
            images.append((root, runtime.build_runtime(root)["image"]))
        assert images[0][1] != images[1][1]  # Ownership still differs safely.
        first = run(roots[0], cache_dir=tmp_path / "shared-cache")
        second = run(roots[1], cache_dir=tmp_path / "shared-cache")
        assert first["counts"]["executed"] == 1
        assert second["counts"]["reused"] == 1
        assert second["counts"]["executed"] == 0
    finally:
        for root, image in images:
            runtime._image_info(image, runtime._project(root))
            runtime.command(["docker", "image", "rm", image])


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
        "cpu_seconds": 0.1,
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
        ("cpu_seconds", float("nan")),
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


@pytest.mark.parametrize("pytest_version", ["8.3.5", "9.1.1"])
def test_real_marker_inheritance_and_immutable_arguments_on_supported_pytest_versions(
    tmp_path, monkeypatch, pytest_version
):
    from pytest_telescope.markers import discover

    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(
        runtime,
        "DEFAULT_REQUIREMENTS",
        runtime.DEFAULT_REQUIREMENTS.replace("pytest==9.1.1", "pytest==" + pytest_version),
    )
    image = runtime.build_runtime(root)["image"]
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
    try:
        registry, _ = discover(source.encode(), "test_subject.py")
        result = run_worker(root, image, source, registry=registry)
        assert result["exit_code"] == 0
        assert result["passed"] == 2
        assert all(len(row["origins"]) == 4 for row in result["inventory"]["items"])
        mutation = (
            "\nTestOrigins.test_origin.pytestmark[-1].kwargs['inputs'].append('undeclared.txt')\n"
        )
        registry, _ = discover((source + mutation).encode(), "test_subject.py")
        rejected = run_worker(root, image, source + mutation, registry=registry)
        assert rejected["exit_code"] == 4
        assert rejected["passed"] == 0
    finally:
        runtime._image_info(image, runtime._project(root))
        runtime.command(["docker", "image", "rm", image])


def test_execution_success_cannot_claim_zero_cases():
    from pytest_telescope.result import validate_result

    empty = good_result()
    empty.update(collected=0, passed=0, case_ids=[])
    assert not validate_result(empty, successful_only=True)


def test_fresh_execution_result_must_match_requested_case(tmp_path, monkeypatch):
    from pytest_telescope.model import InfrastructureError

    def wrong_case(directory, data, args, timeout, **kwargs):
        result = good_result()
        result.update(nonce=data["token"], case_ids=["c" * 64])
        return 0, b"TELESCOPE_RESULT " + json.dumps(result).encode() + b"\n"

    monkeypatch.setattr(runtime, "_owned_capture", wrong_case)
    monkeypatch.setattr(runtime, "_stop_container", lambda data: None)
    with runtime.invocation(tmp_path) as directory:
        project = directory / "project"
        project.mkdir()
        with pytest.raises(InfrastructureError, match="identity"):
            runtime.execute(
                "sha256:" + "a" * 64,
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


def test_execution_case_binding_accepts_only_the_expected_case():
    from pytest_telescope.result import validate_result

    record = good_result()
    assert validate_result(record, successful_only=True, case_id="b" * 64)
    assert not validate_result(record, successful_only=True, case_id="c" * 64)


def test_collection_node_identity_must_belong_to_reported_file():
    from pytest_telescope.result import validate_collection

    result = collection_record()
    item = result["items"][0]
    item["file"] = "other_subject.py"
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
