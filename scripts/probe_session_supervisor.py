"""Scratch mechanism proof; this supervisor is not the production worker."""

import argparse
import json
import threading
import time
import uuid
from pathlib import Path

from probe_session_children import TEST

from pytest_telescope import runtime

SUPERVISOR = r"""
import json,os,pathlib,resource,selectors,signal,sys,time
LIMIT=64*1024*1024
read,write=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
wake_read,wake_write=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
signal.signal(signal.SIGCHLD,lambda *_:None)
signal.set_wakeup_fd(wake_write)
worker=os.fork()
if worker==0:
    signal.set_wakeup_fd(-1)
    signal.signal(signal.SIGCHLD,signal.SIG_DFL)
    os.set_blocking(write,True)
    os.dup2(write,1);os.dup2(write,2)
    for fd in (read,write,wake_read,wake_write):os.close(fd)
    os.execv(sys.executable,[sys.executable,'-I','/project/runner.py'])
os.close(write)
selector=selectors.DefaultSelector();selector.register(read,selectors.EVENT_READ);selector.register(wake_read,selectors.EVENT_READ)
status=None;worker_cpu=0.;orphan_cpu=0.;reaped=0;wait_records=[];buffer=bytearray();exhausted=False

def drain_children():
    global status,worker_cpu,orphan_cpu,reaped
    deadline=time.monotonic()+.05
    for _ in range(4096):
        if time.monotonic()>=deadline:break
        try:pid,value,usage=os.wait4(-1,os.WNOHANG)
        except ChildProcessError:break
        if pid==0:break
        wait_records.append({"worker":pid==worker,"status":os.waitstatus_to_exitcode(value),"user_us":round(usage.ru_utime*1000000),"system_us":round(usage.ru_stime*1000000)})
        cpu=usage.ru_utime+usage.ru_stime
        if pid==worker:status=value;worker_cpu=cpu
        else:reaped+=1;orphan_cpu+=cpu

def read_pipe():
    global exhausted
    # Bound one read turn so continuous output cannot starve child reaping.
    for _ in range(16):
        try:chunk=os.read(read,min(65536,LIMIT+1-len(buffer)))
        except BlockingIOError:return
        if not chunk:return
        buffer.extend(chunk)
        if len(buffer)>LIMIT:
            exhausted=True;return

while status is None and not exhausted:
    drain_children()
    read_pipe()
    if status is not None or exhausted:break
    for key,_ in selector.select(.05):
        if key.fd==wake_read:
            try:os.read(wake_read,65536)
            except BlockingIOError:pass
        else:read_pipe()
if exhausted:
    print(json.dumps({'protocol_limit_refused':True,'received_bytes':len(buffer),'limit':LIMIT,'eligible':False}),flush=True)
    raise SystemExit(3)
# Worker is reaped. No waiting for any continuing descendant or pipe EOF.
drain_children();read_pipe()
remaining=[]
for path in pathlib.Path('/proc').iterdir():
    if path.name.isdecimal() and int(path.name)!=os.getpid():
        try:remaining.append((path/'stat').read_text().rsplit(')',1)[1].split()[0])
        except FileNotFoundError:pass
lines=bytes(buffer).splitlines()
assert len(lines)==1
prefix,payload=lines[0].split(b' ',1)
assert prefix in (b'TELESCOPE_RESULT',b'TELESCOPE_UNSUPPORTED')
child=json.loads(payload)
if prefix==b'TELESCOPE_UNSUPPORTED':
    assert child=={'capability':'project-metadata:runtime-conflict'}
    child['kind']='unsupported'
exit_status=os.waitstatus_to_exitcode(status)
usage=resource.getrusage(resource.RUSAGE_SELF);children=resource.getrusage(resource.RUSAGE_CHILDREN)
self_cpu=usage.ru_utime+usage.ru_stime;child_cpu=children.ru_utime+children.ru_stime
accounting_delta=child_cpu-worker_cpu-orphan_cpu
aggregate_us={'user_us':round(children.ru_utime*1000000),'system_us':round(children.ru_stime*1000000)}
rounding_us={key:aggregate_us[key]-sum(row[key] for row in wait_records) for key in aggregate_us}
# Each Linux timeval truncates nanoseconds once per user/system component.
# floor(sum(x))-sum(floor(x)) is in [0,N-1], not an arbitrary tolerance.
assert all(0<=value<=len(wait_records)-1 for value in rounding_us.values())
summary={key:value for key,value in child.items() if key!='padding'}
print(json.dumps({'child':summary,'worker_exit_status':exit_status,'reaped_orphans':reaped,
 'remaining':len(remaining),'remaining_zombies':sum(s=='Z' for s in remaining),
 'worker_accounted_cpu':worker_cpu,'adopted_accounted_cpu':orphan_cpu,'supervisor_cpu':self_cpu,
 'wait_records':wait_records,'aggregate_us':aggregate_us,'rounding_us':rounding_us,'accounting_delta':accounting_delta,'kernel_children_cpu':child_cpu,'total_cpu':self_cpu+child_cpu,'pipe_bytes':len(buffer),
 'eligible':exit_status==0 and child.get('pytest_exit')==0 and not remaining}),flush=True)
raise SystemExit(exit_status if exit_status>=0 else 128-exit_status)
"""

RUNNER = r"""
import contextlib,importlib.util,json,os,pathlib,resource,signal,sys
import pytest
mode=os.environ['PROBE_MODE']
def emit(value):print('TELESCOPE_RESULT '+json.dumps(value),flush=True)
if mode=='exit':
    emit({'kind':'early_exit'});os._exit(17)
if mode=='signal':
    emit({'kind':'signal_exit'});os.kill(os.getpid(),signal.SIGTERM)
if mode=='unsupported':
    print('TELESCOPE_UNSUPPORTED '+json.dumps({'capability':'project-metadata:runtime-conflict'}),flush=True);raise SystemExit(4)
if mode in ('large','oversize'):
    emit({'pytest_exit':0,'padding':'x'*((2 if mode=='large' else 65)*1024*1024)});raise SystemExit(0)
with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
    result=int(pytest.main(['-q','/project/test_session.py']+(['--collect-only'] if mode=='collection' else [])))
report=json.loads(pathlib.Path('/tmp/session-report.json').read_text())
report['pytest_exit']=result
# The production worker currently performs this bounded final sweep. The prototype
# retains it only after all test/fixture ownership has ended; live processes remain.
spec=importlib.util.spec_from_file_location('qualified_worker','/opt/telescope/worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
worker.reap_completed_children()
usage=resource.getrusage(resource.RUSAGE_SELF);children=resource.getrusage(resource.RUSAGE_CHILDREN)
report['worker_report_cpu']=usage.ru_utime+usage.ru_stime+children.ru_utime+children.ru_stime
emit(report)
"""

OWNERSHIP = r"""
import pytest,signal,time

@pytest.fixture(scope='session',autouse=True)
def fixture_lifetime():
    REPORT['fixture_setup']=REPORT.get('fixture_setup',0)+1
    yield
    REPORT['fixture_teardown']=REPORT.get('fixture_teardown',0)+1
    pathlib.Path('/tmp/session-report.json').write_text(json.dumps(REPORT))

def test_owned_child_statuses():
    delayed=subprocess.Popen([sys.executable,'-I','-c','import os,time; time.sleep(.03); os._exit(37)'])
    assert delayed.poll() is None
    time.sleep(.05)
    assert delayed.poll()==37 and delayed.wait()==37
    signaled=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)'])
    signaled.terminate();assert signaled.wait()==-signal.SIGTERM
    child=os.fork()
    if child==0:os._exit(23)
    waited,status=os.waitpid(child,0)
    assert waited==child and os.waitstatus_to_exitcode(status)==23
    cpu=subprocess.Popen([sys.executable,'-I','-c','import time; s=time.process_time(); exec("while time.process_time()-s<.03: pass")'])
    assert cpu.wait()==0
    REPORT['owned_statuses']=[37,-signal.SIGTERM,23,0]
    # Completed adopted CPU is measured separately by PID1, not added to the
    # worker report a second time.
    intermediate=os.fork()
    if intermediate==0:
        orphan=os.fork()
        if orphan==0:
            start=time.process_time()
            while time.process_time()-start<.03:pass
            os._exit(0)
        os._exit(0)
    os.waitpid(intermediate,0)
    time.sleep(.05)
    if os.environ['PROBE_MODE'] in ('live','cancel'):
        intermediate=os.fork()
        if intermediate==0:
            if os.fork()==0:time.sleep(30);os._exit(0)
            os._exit(0)
        os.waitpid(intermediate,0)
        if os.environ['PROBE_MODE']=='cancel':time.sleep(30)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope.runtime]\nprofile="git-local-v1"\n'
    )
    view = root / "view"
    view.mkdir()
    test = TEST.replace(
        "import json, os, pathlib, subprocess, sys",
        "import json, os, pathlib, subprocess, sys, pytest",
    )
    test = test.replace(
        "def test_long_session(tmp_path):",
        "@pytest.mark.parametrize('partition',range(int(os.environ['PROBE_CASES'])))\ndef test_long_session(tmp_path,partition):",
    )
    test = test.replace("if index%16==0", "if REPORT['completed']%16==1")
    for name, source in (
        (
            "test_session.py",
            test
            + OWNERSHIP
            + "\nif os.environ['PROBE_MODE']=='collection':\n    for _ in range(160):orphan(); REPORT['completed']+=1\n    REPORT['collection_processes']=processes()\n    pathlib.Path('/tmp/session-report.json').write_text(json.dumps(REPORT))\n",
        ),
        ("runner.py", RUNNER),
        ("supervisor.py", SUPERVISOR),
    ):
        compile(source, name, "exec")
        (view / name).write_text(source)
        (view / name).chmod(0o444)
    view.chmod(0o755)
    prepared = runtime.build_runtime(root)
    records = []
    try:
        for mode, count, cases in (
            ("collection", 1, 1),
            ("case", 160, 1),
            ("batch", 1, 160),
            ("live", 1, 1),
            ("exit", 1, 1),
            ("signal", 1, 1),
            ("unsupported", 1, 1),
            ("large", 1, 1),
            ("oversize", 1, 1),
            ("cancel", 1, 1),
        ):
            token = uuid.uuid4().hex
            name = "telescope-init-proof-" + token
            owner = "org.pytest-telescope.proof-owner"
            cancelled = threading.Event()
            watcher = None
            watch_errors = []

            def cancel_running():
                try:
                    for _ in range(100):
                        code, out = runtime._capture(["docker", "top", name, "-eo", "pid"], 5)
                        if code == 0 and len(out.splitlines()) >= 4:
                            cancelled.set()
                            return
                        time.sleep(0.05)
                    watch_errors.append("owned live processes not observed")
                except Exception as exc:
                    watch_errors.append(type(exc).__name__)

            try:
                cmd = [
                    "docker",
                    "run",
                    "--name",
                    name,
                    "--label",
                    owner + "=" + token,
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
                    "--workdir=/project",
                    "--mount",
                    f"type=bind,src={view},dst=/project,readonly",
                    "--env",
                    f"PROBE_COUNT={count}",
                    "--env",
                    "PROBE_ORPHANS=1",
                    "--env",
                    f"PROBE_CASES={cases}",
                    "--env",
                    f"PROBE_MODE={mode}",
                    "--entrypoint=python",
                    prepared["image"],
                    "-I",
                    "/project/supervisor.py",
                ]
                if mode == "cancel":
                    watcher = threading.Thread(target=cancel_running)
                    watcher.start()
                try:
                    code, output = runtime._capture(
                        cmd, 120, merged=True, cancellation=cancelled if mode == "cancel" else None
                    )
                except runtime.InvocationInterrupted:
                    assert mode == "cancel"
                    records.append({"scenario": mode, "cancelled": True, "eligible": False})
                else:
                    try:
                        report = json.loads(output)
                    except json.JSONDecodeError:
                        records.append(
                            {
                                "scenario": mode,
                                "container_exit": code,
                                "protocol_error": output[:16384].decode("utf-8", "replace"),
                            }
                        )
                        raise
                    report["scenario"] = mode
                    report["container_exit"] = code
                    records.append(report)
                    if mode == "collection":
                        assert (
                            code == 0 and report["eligible"] and report["child"]["completed"] == 160
                        )
                    elif mode in ("case", "batch"):
                        assert (
                            code == 0 and report["eligible"] and report["child"]["completed"] == 160
                        )
                        assert report["child"]["owned_statuses"] == [37, -15, 23, 0]
                        assert (
                            report["child"]["fixture_setup"]
                            == report["child"]["fixture_teardown"]
                            == 1
                        )
                        assert report["adopted_accounted_cpu"] > 0
                        assert (
                            report["worker_accounted_cpu"] >= report["child"]["worker_report_cpu"]
                        )
                    elif mode == "live":
                        assert not report["eligible"] and report["remaining"] >= 1
                    elif mode == "exit":
                        assert code == 17 and report["worker_exit_status"] == 17
                    elif mode == "signal":
                        assert code == 143 and report["worker_exit_status"] == -15
                    elif mode == "unsupported":
                        assert code == 4 and report["child"]["kind"] == "unsupported"
                    elif mode == "large":
                        assert (
                            code == 0
                            and report["eligible"]
                            and report["pipe_bytes"] > 2 * 1024 * 1024
                        )
                    elif mode == "oversize":
                        assert (
                            code == 3
                            and report["protocol_limit_refused"]
                            and not report["eligible"]
                        )
                    else:
                        raise AssertionError("cancellation did not interrupt")
                info = json.loads(runtime.command(["docker", "inspect", name]))[0]
                assert (
                    info["Config"]["Labels"][owner] == token
                    and info["HostConfig"]["PidsLimit"] == 128
                )
                assert (
                    info["HostConfig"]["NetworkMode"] == "none"
                    and info["HostConfig"]["ReadonlyRootfs"]
                )
            finally:
                if watcher is not None:
                    watcher.join(timeout=10)
                info = json.loads(runtime.command(["docker", "inspect", name]))[0]
                assert info["Config"]["Labels"][owner] == token
                runtime.command(["docker", "rm", "-f", name])
                assert not runtime.command(
                    ["docker", "ps", "-aq", "--filter", "label=" + owner + "=" + token]
                ).strip()
                if watcher is not None:
                    assert not watcher.is_alive() and not watch_errors
    finally:
        runtime._image_info(prepared["image"], runtime._project(root))
        runtime.command(["docker", "image", "rm", prepared["image"]], timeout=30)
        args.receipt.write_text(
            json.dumps({"scenarios": records, "owned_cleanup": True}, indent=2) + "\n"
        )
    print(json.dumps({"scenarios": records, "owned_cleanup": True}))


if __name__ == "__main__":
    main()
