"""Synthetic Docker mechanism proof before production batch implementation.

Uses an installed distribution to prepare the existing restricted runtime. The small
observer records pytest's real fixture/report lifecycle; it is not the batch scheduler.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from pytest_telescope import runtime

OBSERVER = r"""
import hashlib, json, sys, pytest
class Observer:
    def __init__(self):
        self.cases = {}
        self.finished = set()
        self.members = []
    def pytest_collection_finish(self, session):
        self.members = [self.identity(item.nodeid) for item in session.items]
    def identity(self, value):
        return hashlib.sha256(value.encode()).hexdigest()
    def pytest_runtest_logreport(self, report):
        key = self.identity(report.nodeid)
        if report.failed:
            self.cases[key] = 'failed'
        elif self.cases.get(key) != 'failed':
            if report.skipped:
                self.cases[key] = 'skipped'
            elif report.when == 'call':
                self.cases[key] = 'passed'
        if report.when == 'teardown':
            self.finished.add(key)
observer = Observer()
with open('/dev/null', 'w') as sink:
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = sink
    try:
        code = int(pytest.main(ARGS, plugins=[observer]))
    finally:
        sys.stdout, sys.stderr = saved
complete = set(observer.members) == observer.finished
print(json.dumps(dict(exit_code=code, complete=complete,
    cache_eligible=complete and code == 0,
    members=[dict(case_id=key, outcome=observer.cases.get(key, 'not-run'),
                  complete=key in observer.finished) for key in observer.members])))
sys.exit(code)
"""

SHARED = """
from pathlib import Path
import pytest
calls = dict(session=0, module=0, cls=0)
paths = []
@pytest.fixture(scope="session")
def shared_session(tmp_path_factory):
    calls["session"] += 1
    value = tmp_path_factory.mktemp("shared")
    yield value
    assert calls["session"] == 1
    assert (value / "counter").read_text() == "2"
@pytest.fixture(scope="module")
def shared_module():
    calls["module"] += 1
    yield
    assert calls["module"] == 1
@pytest.fixture(scope="class")
def shared_class():
    calls["cls"] += 1
    yield
    assert calls["cls"] == 1
class TestShared:
    @pytest.mark.parametrize("index", [1, 2])
    def test_state(self, index, shared_session, shared_module, shared_class, tmp_path):
        assert calls == dict(session=1, module=1, cls=1)
        counter = shared_session / "counter"
        previous = int(counter.read_text()) if counter.exists() else 0
        assert previous + 1 == index
        counter.write_text(str(index))
        assert str(tmp_path) not in paths
        paths.append(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            Path("undeclared.txt").read_text()
"""

FAILURES = """
import pytest
@pytest.fixture(scope="module")
def shared():
    yield
    FAILURE

def test_first(shared):
    FIRST

def test_second(shared):
    assert True
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True)
    root.chmod(0o755)
    prepared = runtime.build_runtime(root)
    scenarios = [
        ("shared-lifetimes", SHARED, [], ["passed", "passed"], True),
        (
            "shared-teardown-failure",
            FAILURES.replace("FAILURE", "assert False").replace("FIRST", "assert True"),
            [],
            ["passed", "failed"],
            True,
        ),
        (
            "shared-setup-failure",
            FAILURES.replace("yield", "assert False\n    yield")
            .replace("FAILURE", "pass")
            .replace("FIRST", "assert True"),
            [],
            ["failed", "failed"],
            True,
        ),
        (
            "stop-first",
            FAILURES.replace("FAILURE", "pass").replace("FIRST", "assert False"),
            ["-x"],
            ["failed", "not-run"],
            False,
        ),
        (
            "maxfail-one",
            FAILURES.replace("FAILURE", "pass").replace("FIRST", "assert False"),
            ["--maxfail=1"],
            ["failed", "not-run"],
            False,
        ),
    ]
    for visible, hidden in (("left", "right"), ("right", "left")):
        source = (
            "from pathlib import Path\nimport pytest\n"
            "def test_scope():\n"
            f"    assert Path('{visible}.txt').read_text() == '{visible}'\n"
            f"    with pytest.raises(FileNotFoundError): Path('{hidden}.txt').read_text()\n"
        )
        scenarios.append((f"unequal-{visible}", source, [], ["passed"], True))
    receipts = []
    try:
        for name, source, options, expected, complete in scenarios:
            snapshot = root / name
            snapshot.mkdir(mode=0o755)
            snapshot.chmod(0o755)
            (snapshot / "test_group.py").write_text(source)
            (snapshot / "pytest.ini").write_text("[pytest]\n")
            if name.startswith("unequal-"):
                visible = name.removeprefix("unequal-")
                (snapshot / (visible + ".txt")).write_text(visible)
            for path in snapshot.iterdir():
                path.chmod(0o444)
                os.utime(path, ns=(0, 0))
            os.utime(snapshot, ns=(0, 0))
            token = (
                "pytest-telescope-batch-proof-"
                + hashlib.sha256(str(snapshot).encode()).hexdigest()[:20]
            )
            arguments = ["-q", "-p", "no:cacheprovider", *options, "test_group.py"]
            code = "ARGS = " + repr(arguments) + "\n" + OBSERVER
            command = [
                "docker",
                "run",
                "--name",
                token,
                "--rm",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65534:65534",
                "--cpus=1",
                "--memory=512m",
                "--memory-swap=512m",
                "--pids-limit=128",
                "--ipc=none",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--workdir=/project",
                "--mount",
                f"type=bind,src={snapshot},dst=/project,readonly",
                "--entrypoint=python",
                prepared["image"],
                "-I",
                "-c",
                code,
            ]
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
                (root / (name + ".log")).write_text(completed.stdout + completed.stderr)
                report = json.loads(completed.stdout)
                assert completed.returncode == report["exit_code"]
                assert [row["outcome"] for row in report["members"]] == expected, name
                assert report["complete"] is complete, name
                assert report["cache_eligible"] is (complete and "failed" not in expected), name
                receipts.append(dict(scenario=name, **report))
            finally:
                subprocess.run(["docker", "rm", "-f", token], capture_output=True, timeout=30)
        result = {"scenarios": receipts, "passed": len(receipts)}
        (root / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        runtime.command(["docker", "image", "rm", prepared["image"]])


if __name__ == "__main__":
    main()
