"""Bounded synthetic mechanism proof for the proposed local Git profile.

This does not implement or qualify production runtime preparation/cache semantics.
Only the named proof container and its exact labelled image are removed.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

BASE = "python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
ARCHIVE_SHA256 = "3e35662fd5c46add561703e54031a1d8ad9df45811927689f0a51122b13be722"
WORKER = r"""
import hashlib, json, os, pathlib, shutil, socket, subprocess
root=pathlib.Path('/tmp/repository')
assert not root.exists()
root.mkdir()
env=dict(os.environ, GIT_AUTHOR_DATE='2001-01-01T00:00:00+0000', GIT_COMMITTER_DATE='2001-01-01T00:00:00+0000')
def git(*args, cwd=root, fails=False):
    result=subprocess.run(['git',*args],cwd=cwd,env=env,capture_output=True,text=True,timeout=10)
    assert (result.returncode != 0) if fails else (result.returncode == 0), (args,result.returncode,result.stderr)
    return result.stdout.strip()
assert git('--version')=='git version 2.47.3'
git('init','-b','main')
git('config','user.name','Example Author')
git('config','user.email','example@example.invalid')
assert git('config','user.name')=='Example Author'
(root/'input.txt').write_bytes(pathlib.Path('/project/input.txt').read_bytes())
git('add','input.txt');git('commit','-m','initial')
initial=git('rev-parse','HEAD')
git('branch','topic');git('checkout','topic')
(root/'input.txt').write_text('changed\n')
git('add','input.txt');git('commit','-m','topic')
topic=git('rev-parse','HEAD')
git('checkout','main');git('merge','--ff-only','topic')
git('merge-base','--is-ancestor',initial,'HEAD')
assert git('merge-base',initial,'HEAD')==initial
assert git('status','--porcelain')==''
origin=pathlib.Path('/tmp/origin.git')
git('init','--bare','--initial-branch=main',str(origin))
git('remote','add','origin',origin.as_uri())
git('push','origin','main')
assert git('--git-dir='+str(origin),'rev-parse','refs/heads/main')==topic
clone=pathlib.Path('/tmp/clone')
git('clone','--no-hardlinks',origin.as_uri(),str(clone))
assert git('rev-parse','HEAD',cwd=clone)==topic
assert git('status','--porcelain',cwd=clone)==''
git('fetch','origin')
worktree=pathlib.Path('/tmp/linked-worktree')
git('worktree','add','--detach',str(worktree),'HEAD')
assert git('rev-parse','HEAD',cwd=worktree)==topic
git('worktree','remove',str(worktree));assert not worktree.exists()
git('remote','set-url','origin','file:///tmp/absent-origin.git')
git('fetch','origin',fails=True)
git('remote','set-url','origin',origin.as_uri());git('fetch','origin')
lock=root/'.git/index.lock';lock.write_text('owned fault')
git('add','input.txt',fails=True);lock.unlink();git('add','input.txt')
lock=root/'.git/refs/heads/main.lock';lock.write_text('owned fault')
git('update-ref','refs/heads/main',initial,fails=True)
lock.unlink();git('update-ref','refs/heads/main',initial);git('update-ref','refs/heads/main',topic)
fetch=root/'.git/FETCH_HEAD';fetch.unlink();fetch.mkdir()
git('fetch','origin',fails=True);fetch.rmdir();git('fetch','origin')
try:pathlib.Path('/project/input.txt').write_text('forbidden')
except OSError:pass
else:raise AssertionError('project source became writable')
assert not pathlib.Path('/project/undeclared.txt').exists()
native=pathlib.Path('/tmp/generated-git');shutil.copyfile(shutil.which('git'),native);native.chmod(0o755)
try:subprocess.run([str(native),'--version'],check=True,timeout=10,capture_output=True)
except PermissionError:pass
else:raise AssertionError('scratch allowed native execution')
with socket.socket() as sock:
    sock.settimeout(1)
    assert sock.connect_ex(('192.0.2.1',9)) != 0
closure={}
output=subprocess.check_output(['ldd',shutil.which('git')],text=True)
for line in output.splitlines():
    words=line.split()
    paths=[word for word in words if word.startswith('/')]
    for name in paths:
        path=pathlib.Path(name).resolve()
        closure[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
closure[shutil.which('git')]=hashlib.sha256(pathlib.Path(shutil.which('git')).read_bytes()).hexdigest()
print(json.dumps({'local_operations':'passed','owned_faults_and_recovery':4,'source_readonly':True,'undeclared_absent':True,'scratch_noexec':True,'external_network_denied':True,'initial_commit':initial,'topic_commit':topic,'closure':closure},sort_keys=True))
"""


def command(args, timeout=120, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"proof command failed ({result.returncode}): {result.stderr[:4096]}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    options = parser.parse_args()
    directory = options.directory.resolve()
    archive = directory / "git.deb"
    assert archive.stat().st_size == 8861572
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA256
    view = directory / "snapshot"
    view.mkdir()
    view.chmod(0o755)
    (view / "input.txt").write_text("declared input\n")
    (view / "input.txt").chmod(0o444)
    (directory / "undeclared.txt").write_text("not part of the snapshot\n")
    (directory / "worker.py").write_text(WORKER)
    (directory / "Dockerfile").write_text(
        f"FROM {BASE}\n"
        "COPY git.deb /tmp/git.deb\n"
        "RUN dpkg-deb --extract /tmp/git.deb /tmp/package && "
        "mkdir -p /opt/telescope/git/bin /opt/telescope/git/templates && "
        "cp /tmp/package/usr/bin/git /opt/telescope/git/bin/git && "
        "cp /tmp/package/usr/share/doc/git/copyright /opt/telescope/git/copyright && "
        "ln /opt/telescope/git/bin/git /opt/telescope/git/bin/git-upload-pack && "
        "ln /opt/telescope/git/bin/git /opt/telescope/git/bin/git-receive-pack && "
        "ln /opt/telescope/git/bin/git /opt/telescope/git/bin/git-upload-archive && "
        "rm -rf /tmp/package /tmp/git.deb\n"
        "COPY worker.py /opt/proof.py\n"
        "RUN chmod 0444 /opt/proof.py\n"
        'LABEL org.pytest-telescope.proof="issue-29"\n'
        'ENV PATH="/opt/telescope/git/bin:${PATH}" GIT_EXEC_PATH=/opt/telescope/git/bin '
        "GIT_TEMPLATE_DIR=/opt/telescope/git/templates GIT_CONFIG_NOSYSTEM=1 "
        "GIT_ALLOW_PROTOCOL=file GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
        "GIT_CONFIG_VALUE_0=/dev/null HOME=/tmp TMPDIR=/tmp\n"
        'USER 65534:65534\nENTRYPOINT ["python","/opt/proof.py"]\n'
    )
    image = None
    name = "telescope-issue29-git-proof"
    try:
        built = command(["docker", "build", "--quiet", "--network=none", str(directory)])
        image = built.stdout.strip().splitlines()[-1]
        assert image.startswith("sha256:")
        info = json.loads(command(["docker", "image", "inspect", image]).stdout)[0]
        assert info["Config"]["Labels"]["org.pytest-telescope.proof"] == "issue-29"
        rows = []
        for _ in range(2):
            completed = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--pull=never",
                    "--read-only",
                    "--network=none",
                    "--ipc=none",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--user=65534:65534",
                    "--cpus=1",
                    "--memory=536870912",
                    "--memory-swap=536870912",
                    "--pids-limit=128",
                    "--ulimit",
                    "nofile=256:256",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,size=67108864",
                    "--mount",
                    f"type=bind,src={view},dst=/project,readonly",
                    image,
                ],
                timeout=90,
            )
            rows.append(json.loads(completed.stdout))
        assert rows[0] == rows[1]
        default = command(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--cpus=1",
                "--memory=268435456",
                "--memory-swap=268435456",
                "--pids-limit=32",
                "--entrypoint=python",
                BASE,
                "-c",
                "import shutil;assert shutil.which('git') is None;print('default Git absent')",
            ]
        )
        receipt = dict(
            image=image, image_bytes=info["Size"], repeats=rows, default=default.stdout.strip()
        )
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
    finally:
        existing = command(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"], check=False)
        if existing.stdout.strip():
            command(["docker", "rm", "-f", *existing.stdout.split()])
        if image:
            command(["docker", "image", "rm", image])
        assert not command(
            ["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"]
        ).stdout.strip()
        if image:
            assert command(["docker", "image", "inspect", image], check=False).returncode != 0
        print("Exact owned proof container/image cleanup verified.")


if __name__ == "__main__":
    main()
