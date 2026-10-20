"""Bounded synthetic evidence for completed descendants in long restricted sessions."""

import argparse
import json
import uuid
from pathlib import Path

from pytest_telescope import runtime

TEST = r"""
import json, os, pathlib, subprocess, sys

REPORT = {"completed": 0, "samples": [], "failure_errno": None, "git_returncode": None}

def processes():
    rows=[]
    for path in pathlib.Path('/proc').iterdir():
        if path.name.isdecimal():
            try:
                fields=(path/'stat').read_text().rsplit(')',1)[1].split()
                rows.append((int(path.name),fields[0],int(fields[1])))
            except FileNotFoundError:
                pass
    return {"processes":len(rows),"zombies":sum(state=='Z' for _,state,_ in rows),
            "zombies_parent_init":sum(state=='Z' and parent==1 for _,state,parent in rows),
            "direct_children":sum(parent==os.getpid() for _,_,parent in rows),
            "fds":len(list(pathlib.Path('/proc/self/fd').iterdir()))}

def orphan():
    read,write=os.pipe()
    child=os.fork()
    if child==0:
        os.close(read)
        try:
            adopted=os.fork()
        except OSError as exc:
            os.write(write,str(exc.errno).encode())
            os._exit(1)
        if adopted==0:
            os.write(write,b'x')
            os._exit(0)
        os._exit(0)
    os.close(write)
    status=os.waitpid(child,0)[1]
    token=os.read(read,16)
    os.close(read)
    if status:raise OSError(int(token), 'controlled descendant creation failure')
    assert token==b'x'

def test_long_session(tmp_path):
    repository=tmp_path/'repository';repository.mkdir()
    subprocess.run(['git','init','-b','main',str(repository)],check=True,capture_output=True)
    try:
        for index in range(int(os.environ['PROBE_COUNT'])):
            if os.environ['PROBE_ORPHANS']=='1':orphan()
            operation=subprocess.run(['git','status','--porcelain'],cwd=repository,capture_output=True)
            if operation.returncode:
                REPORT['git_returncode']=operation.returncode
            assert operation.returncode==0
            REPORT['completed']+=1
            if index%16==0:REPORT['samples'].append(processes())
    except OSError as exc:
        REPORT['failure_errno']=exc.errno
        raise
    finally:
        REPORT['before_final_reap']=processes()
        pathlib.Path('/tmp/session-report.json').write_text(json.dumps(REPORT))
"""

RUNNER = r"""
import contextlib,importlib.util,json,os,pathlib,resource,time
import pytest
with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
    result=int(pytest.main(['-q','/project/test_session.py']))
report=json.loads(pathlib.Path('/tmp/session-report.json').read_text())
report['pytest_exit']=result
spec=importlib.util.spec_from_file_location('qualified_worker','/opt/telescope/worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
worker.reap_completed_children()
remaining=[]
for path in pathlib.Path('/proc').iterdir():
    if path.name.isdecimal() and int(path.name)!=os.getpid():
        try:
            state=(path/'stat').read_text().rsplit(')',1)[1].split()
            remaining.append(state[0])
        except FileNotFoundError:pass
report['after_final_reap']={'remaining':len(remaining),'zombies':sum(s=='Z' for s in remaining)}
print(json.dumps(report),flush=True)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "pyproject.toml").write_text(
        '[tool.pytest_telescope.runtime]\nprofile="git-local-v1"\n'
    )
    view = root / "view"
    view.mkdir()
    (view / "test_session.py").write_text(TEST)
    (view / "runner.py").write_text(RUNNER)
    view.chmod(0o755)
    for source in view.iterdir():
        source.chmod(0o444)
    prepared = runtime.build_runtime(root)
    records = []
    try:
        for label, count, orphans in (("short", 1, 1), ("git-only", 160, 0), ("long", 160, 1)):
            token = uuid.uuid4().hex
            name = "telescope-child-proof-" + token
            owner = "org.pytest-telescope.proof-owner"
            try:
                code, output = runtime._capture(
                    [
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
                        f"PROBE_ORPHANS={orphans}",
                        "--entrypoint=python",
                        prepared["image"],
                        "-I",
                        "/project/runner.py",
                    ],
                    120,
                    merged=True,
                )
                assert code == 0, output
                info = json.loads(runtime.command(["docker", "inspect", name]))[0]
                assert info["Config"]["Labels"][owner] == token
                assert info["HostConfig"]["PidsLimit"] == 128
                assert info["HostConfig"]["NetworkMode"] == "none"
                records.append({"scenario": label, "pids_limit": 128, **json.loads(output)})
            finally:
                info = json.loads(runtime.command(["docker", "inspect", name]))[0]
                assert info["Config"]["Labels"][owner] == token
                runtime.command(["docker", "rm", "-f", name])
                assert not runtime.command(
                    ["docker", "ps", "-aq", "--filter", "label=" + owner + "=" + token]
                ).strip()
    finally:
        runtime._image_info(prepared["image"], runtime._project(root))
        runtime.command(["docker", "image", "rm", prepared["image"]], timeout=30)
        args.receipt.write_text(
            json.dumps({"scenarios": records, "owned_cleanup": True}, indent=2) + "\n"
        )
    print(json.dumps({"scenarios": records, "owned_cleanup": True}))
    # Failing-first expectation: a long session must not exhaust the budget merely
    # because completed adopted descendants have not yet reached final teardown.
    assert all(row["pytest_exit"] == 0 for row in records)


if __name__ == "__main__":
    main()
