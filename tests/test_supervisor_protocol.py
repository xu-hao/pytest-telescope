"""Bounded lifecycle IPC and final accounting, using the shipped supervisor."""

import json
import uuid

import pytest

from pytest_telescope import runtime, supervisor


def provisional(**changes):
    return {
        "nonce": "owned-nonce",
        "exit_code": 0,
        "complete": True,
        "leaked_descendants": False,
        "worker_seconds": 9876,
        "cpu_seconds": 9876,
        **changes,
    }


def test_final_accounting_replaces_provisional_cpu_without_adding_it():
    output = b"TELESCOPE_RESULT " + json.dumps(provisional()).encode()
    line = supervisor.final_record(0, output, 2.5, 1.25, {"nonce": "owned-nonce"})
    result = json.loads(line.split(" ", 1)[1])
    assert result["cpu_seconds"] == 1.25
    assert result["worker_seconds"] == 2.5


@pytest.mark.parametrize(
    "output,status",
    [
        (b"", 0),
        (b"TELESCOPE_RESULT {", 0),
        (b"TELESCOPE_RESULT []", 0),
        (b"TELESCOPE_UNSUPPORTED {}", 4),
        (b'TELESCOPE_UNSUPPORTED {"capability":"project-metadata:runtime-conflict"}', 0),
        (b"TELESCOPE_RESULT " + json.dumps(provisional(nonce="foreign")).encode(), 0),
        (b"TELESCOPE_RESULT " + json.dumps(provisional(exit_code=True)).encode(), 1),
        (b"TELESCOPE_RESULT " + json.dumps(provisional()).encode(), -15),
        (b"TELESCOPE_RESULT " + json.dumps(provisional(complete=1)).encode(), 0),
        (b"TELESCOPE_RESULT " + json.dumps(provisional(leaked_descendants=True)).encode(), 0),
    ],
)
def test_invalid_provisional_records_never_become_final(output, status):
    with pytest.raises((ValueError, TypeError)):
        supervisor.final_record(status, output, 1, 1, {"nonce": "owned-nonce"})


@pytest.mark.parametrize("bound", ["iterations", "deadline", "live", "none"])
def test_supervisor_reap_turn_is_nonblocking_and_bounded(monkeypatch, bound):
    calls = []
    clock = [0]

    def monotonic():
        clock[0] += 0.02 if bound == "deadline" else 0
        return clock[0]

    def wait4(pid, flags):
        assert pid == -1 and flags == supervisor.os.WNOHANG
        calls.append(1)
        if bound == "none":
            raise ChildProcessError
        return (0 if bound == "live" else 42), 0, None

    monkeypatch.setattr(supervisor.os, "wait4", wait4)
    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    supervisor.Reaper(42).drain()
    assert len(calls) == {"iterations": 4096, "deadline": 2, "live": 1, "none": 1}[bound]


CHILD = r"""
import json,os,signal,sys,time
mode=os.environ['PROBE_MODE']
record={'nonce':'owned-nonce','exit_code':0,'complete':True,'leaked_descendants':False,'cpu_seconds':9999,'worker_seconds':9999}
if mode=='missing':raise SystemExit(0)
if mode=='malformed':print('TELESCOPE_RESULT {');raise SystemExit(0)
if mode=='signal':os.kill(os.getpid(),signal.SIGTERM)
if mode=='exit':os._exit(17)
if mode in ('live','closed-live','unsupported-live'):
    child=os.fork()
    if child==0:
        if mode=='closed-live':os.close(1);os.close(2)
        time.sleep(30);os._exit(0)
if mode.startswith('unsupported'):
    print('TELESCOPE_UNSUPPORTED '+json.dumps({'capability':'project-metadata:runtime-conflict'}),flush=True)
    raise SystemExit(4)
if mode=='assertion':record['exit_code']=1
if mode=='large':record['padding']='x'*(2*1024*1024)
if mode=='oversize':record['padding']='x'*(1024*1024)
line='TELESCOPE_RESULT '+json.dumps(record)
print(line,flush=True)
if mode=='duplicate':print(line,flush=True)
raise SystemExit(record['exit_code'])
"""

WRAPPER = r"""
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location('supervisor','/opt/telescope/supervisor.py')
supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
try:
    limit=supervisor.MAX_INVENTORY if os.environ['PROBE_MODE']=='large' else supervisor.MAX_CASE
    status,output,seconds,cpu=supervisor.supervise([sys.executable,'-I','/project/child.py'],limit)
    result=supervisor.final_record(status,output,seconds,cpu,{'nonce':'owned-nonce'})
    prefix,body=result.split(' ',1)
    record=json.loads(body)
    print(json.dumps({'accepted':True,'status':status,'prefix':prefix,'bytes':len(output),'cpu':record.get('cpu_seconds')}),flush=True)
except (OSError,ValueError,TypeError,KeyError):
    print(json.dumps({'accepted':False}),flush=True)
"""


@pytest.fixture(scope="module")
def ipc_runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("supervisor-ipc")
    (root / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    view = root / "view"
    view.mkdir(mode=0o755)
    view.chmod(0o755)
    for name, content in (("child.py", CHILD), ("wrapper.py", WRAPPER)):
        (view / name).write_text(content)
        (view / name).chmod(0o444)
    prepared = runtime.build_runtime(root)
    try:
        yield root, view, prepared
    finally:
        runtime.recover(root)
        for image in json.loads((root / ".pytest-telescope/images.json").read_text()):
            runtime._image_info(image, runtime._project(root))
            runtime.command(["docker", "image", "rm", image], timeout=30)


@pytest.mark.parametrize(
    "mode,accepted",
    [("normal", True), ("assertion", True), ("unsupported", True), ("large", True)]
    + [
        (mode, False)
        for mode in (
            "missing",
            "malformed",
            "signal",
            "exit",
            "live",
            "closed-live",
            "unsupported-live",
            "oversize",
            "duplicate",
        )
    ],
)
def test_shipped_supervisor_ipc_and_lifecycle(ipc_runtime, mode, accepted):
    _, view, prepared = ipc_runtime
    token = uuid.uuid4().hex
    name = "telescope-supervisor-ipc-" + token
    label = "org.pytest-telescope.fixture-owner"
    try:
        code, output = runtime._capture(
            [
                "docker",
                "run",
                "--name",
                name,
                "--label",
                label + "=" + token,
                "--read-only",
                "--network=none",
                "--ipc=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=536870912",
                "--memory-swap=536870912",
                "--cpus=1",
                "--ulimit",
                "nofile=256:256",
                "--user=65534:65534",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=67108864",
                "--mount",
                f"type=bind,src={view},dst=/project,readonly",
                "--env",
                "PROBE_MODE=" + mode,
                "--entrypoint=python",
                prepared["image"],
                "-I",
                "/project/wrapper.py",
            ],
            30,
            merged=True,
        )
        assert code == 0, output
        result = json.loads(output)
        assert result["accepted"] is accepted
        if accepted:
            assert result["status"] == (
                1 if mode == "assertion" else 4 if mode == "unsupported" else 0
            )
            if mode == "unsupported":
                assert result["prefix"] == "TELESCOPE_UNSUPPORTED"
            else:
                assert 0 < result["cpu"] < 9999
            if mode == "large":
                assert result["bytes"] > 2 * 1024 * 1024
    finally:
        info = json.loads(runtime.command(["docker", "inspect", name]))[0]
        assert info["Config"]["Labels"][label] == token
        runtime.command(["docker", "rm", "-f", name])
        assert not runtime.command(
            ["docker", "ps", "-aq", "--filter", "label=" + label + "=" + token]
        ).strip()


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("descendant", "descendant"),
        ("output", "output"),
        ("protocol", "protocol"),
        ("unhashable", "identity mismatch"),
        ("unknown", "identity mismatch"),
        ("foreign", "identity mismatch"),
        ("duplicate", "identity mismatch"),
        ("conflict", "identity mismatch"),
        ("exit", "identity mismatch"),
        ("malformed", "validation failed"),
    ],
)
def test_controller_fault_frames_are_bounded_typed_and_owned(
    tmp_path, monkeypatch, variant, expected
):
    from pytest_telescope.model import InfrastructureError

    cleaned = []
    monkeypatch.setattr(
        runtime, "_stop_container", lambda data, **kwargs: cleaned.append(data["token"])
    )

    def capture(directory, data, command, timeout, **kwargs):
        kind = variant if variant in {"descendant", "output", "protocol"} else "protocol"
        record = {"kind": kind, "nonce": data["token"]}
        if variant == "unhashable":
            record["kind"] = []
        if variant == "unknown":
            record["kind"] = "other"
        if variant == "foreign":
            record["nonce"] = "f" * 32
        line = b"TELESCOPE_INFRASTRUCTURE " + json.dumps(record).encode() + b"\n"
        if variant == "duplicate":
            line += line
        if variant == "conflict":
            line += b"TELESCOPE_RESULT {}\n"
        if variant == "malformed":
            line = b"TELESCOPE_INFRASTRUCTURE {\n"
        return (0 if variant == "exit" else 3), line

    monkeypatch.setattr(runtime, "_owned_capture", capture)
    with runtime.invocation(tmp_path) as pool:
        snapshot = pool / "project"
        snapshot.mkdir()
        with runtime.invocation(tmp_path) as invocation:
            token = json.loads((invocation / "journal.json").read_text())["token"]
            with pytest.raises(InfrastructureError, match=expected):
                runtime.execute(
                    "sha256:" + "a" * 64,
                    snapshot,
                    [],
                    {},
                    10,
                    root=tmp_path,
                    invocation_directory=invocation,
                    request={"mode": "collection", "registry": [], "selectors": []},
                )
            assert token in cleaned
            assert json.loads((invocation / "journal.json").read_text())["cleaned"] is True


def test_lifecycle_bytes_invalidate_prepared_identity_and_cache(tmp_path, monkeypatch):
    from pytest_telescope import controller
    from pytest_telescope.model import InfrastructureError

    (tmp_path / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    (tmp_path / "test_example.py").write_text("def test_example(): assert 2 + 2 == 4\n")
    try:
        runtime.build_runtime(tmp_path)
        initial = controller.run(tmp_path)
        assert initial["status"] == "passed" and initial["counts"]["executed"] == 1
        warm = controller.run(tmp_path)
        assert warm["counts"]["reused"] == 1
        state = tmp_path / ".pytest-telescope/runtime.json"
        original = state.read_bytes()
        source = runtime.supervisor_source()
        monkeypatch.setattr(
            runtime, "supervisor_source", lambda: source + b"\n# synthetic lifecycle revision\n"
        )
        with pytest.raises(InfrastructureError):
            runtime.inspect_runtime(tmp_path)
        assert state.read_bytes() == original
        runtime.build_runtime(tmp_path)
        changed = controller.run(tmp_path)
        assert changed["status"] == "passed" and changed["counts"]["executed"] == 1
        assert changed["runtime"]["identity"] != initial["runtime"]["identity"]
        assert controller.run(tmp_path)["counts"]["reused"] == 1
    finally:
        runtime.recover(tmp_path)
        registry = tmp_path / ".pytest-telescope/images.json"
        if registry.exists():
            for image in json.loads(registry.read_text()):
                runtime._retained_image_info(image, runtime._project(tmp_path))
                runtime.command(["docker", "image", "rm", image], timeout=30)
