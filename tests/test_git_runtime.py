"""Public profile preflight and required local-Git worker behavior."""

import json
import re

import pytest

from pytest_telescope import controller, runtime
from pytest_telescope.model import DeclarationError, InfrastructureError, UnsupportedCapability
from pytest_telescope.plan import make_plan


def put(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def configure(root, *, profile=None, requires=None):
    text = '[tool.pytest_telescope]\ntest_roots=["tests"]\n'
    if profile is not None or requires is not None:
        text += "[tool.pytest_telescope.runtime]\n"
        if profile is not None:
            text += f"profile={json.dumps(profile)}\n"
        if requires is not None:
            text += f"requires={json.dumps(requires)}\n"
    put(root, "pyproject.toml", text)
    put(root, "tests/test_example.py", "def test_example(): pass\n")


def forbid_backend_and_cache(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("profile preflight reached backend or cache")

    for name in ("command", "identity", "recover", "execute"):
        monkeypatch.setattr(runtime, name, forbidden)
    monkeypatch.setattr(controller, "CacheStore", forbidden)


def test_static_profile_defaults_and_normalized_union_do_not_contact_backend(tmp_path, monkeypatch):
    configure(tmp_path)
    forbid_backend_and_cache(monkeypatch)
    assert make_plan(tmp_path)["runtime"] == {
        "profile": "python-v1",
        "required_capabilities": [],
        "identity": None,
    }
    configure(tmp_path, profile="git-local-v1", requires=["git-local", "git-local"])
    original = (tmp_path / "pyproject.toml").read_bytes()
    for required in (None, [], ["git-local"], ["git-local", "git-local"]):
        result = make_plan(tmp_path, required_capabilities=required)
        assert result["runtime"] == {
            "profile": "git-local-v1",
            "required_capabilities": ["git-local"],
            "identity": None,
        }
        assert result["execution"]["profile"] == "case-v1"
    assert (tmp_path / "pyproject.toml").read_bytes() == original


@pytest.mark.parametrize(
    "profile,requires,capability",
    [
        ("python-v2", [], "runtime:profile"),
        ("git-local-v2", [], "runtime:profile"),
        ("python-v1", ["git-local"], "runtime:capability"),
        ("git-local-v1", ["git-remote"], "runtime:capability"),
    ],
)
def test_incompatible_profiles_refuse_before_backend_cache_or_import(
    tmp_path, monkeypatch, profile, requires, capability
):
    configure(tmp_path, profile=profile, requires=requires)
    put(tmp_path, "tests/test_example.py", 'raise RuntimeError("must not import")\n')
    forbid_backend_and_cache(monkeypatch)
    for invoke in (make_plan, controller.run, runtime.build_runtime):
        with pytest.raises(UnsupportedCapability) as failure:
            invoke(tmp_path)
        assert failure.value.capability == capability


def test_caller_cannot_remove_configured_requirement(tmp_path, monkeypatch):
    configure(tmp_path, profile="git-local-v1", requires=["git-local"])
    forbid_backend_and_cache(monkeypatch)
    for invoke in (make_plan, controller.run):
        with pytest.raises(UnsupportedCapability) as failure:
            invoke(tmp_path, runtime_profile="python-v1", required_capabilities=[])
        assert failure.value.capability == "runtime:capability"


@pytest.mark.parametrize("requirements", ["git-local", [1], ["git-local"] * 17, ("git-local",)])
def test_invalid_requirement_lists_refuse_without_backend(tmp_path, monkeypatch, requirements):
    configure(tmp_path)
    forbid_backend_and_cache(monkeypatch)
    for invoke in (make_plan, controller.run):
        with pytest.raises(DeclarationError):
            invoke(tmp_path, runtime_profile="git-local-v1", required_capabilities=requirements)


@pytest.mark.parametrize("command", ["plan", "run"])
def test_cli_profile_refusal_uses_schema_two(tmp_path, monkeypatch, capsys, command):
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    forbid_backend_and_cache(monkeypatch)
    assert (
        controller.main(
            [command, "--runtime-profile", "python-v1", "--require-capability", "git-local"]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == 2 and report["kind"] == "refusal"
    assert report["status"] == "unsupported" and report["capability"] == "runtime:capability"
    assert report.get("runtime", {}).get("identity") is None


def test_cli_static_profile_override_is_invocation_only(tmp_path, monkeypatch, capsys):
    configure(tmp_path)
    original = (tmp_path / "pyproject.toml").read_bytes()
    monkeypatch.chdir(tmp_path)
    forbid_backend_and_cache(monkeypatch)
    assert (
        controller.main(
            [
                "plan",
                "--runtime-profile",
                "git-local-v1",
                "--require-capability",
                "git-local",
                "--require-capability",
                "git-local",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["runtime"] == {
        "profile": "git-local-v1",
        "required_capabilities": ["git-local"],
        "identity": None,
    }
    assert (tmp_path / "pyproject.toml").read_bytes() == original


GIT_CASES = r"""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

pytestmark = pytest.mark.telescope(inputs=["input.txt"])

@pytest.mark.parametrize("variant", [0, 1])
def test_local_git(tmp_path, variant):
    repository = tmp_path / "repository"
    repository.mkdir()
    env = dict(os.environ, GIT_AUTHOR_DATE="2001-01-01T00:00:00+0000",
               GIT_COMMITTER_DATE="2001-01-01T00:00:00+0000")
    def git(*args, cwd=repository, fails=False):
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
        assert (result.returncode != 0) if fails else (result.returncode == 0)
        return result.stdout.strip()
    assert git("--version") == "git version 2.47.3"
    git("init", "-b", "main")
    git("config", "user.name", "Example Author")
    git("config", "user.email", "example@example.invalid")
    assert git("config", "user.name") == "Example Author"
    (repository / "input.txt").write_bytes(Path("/project/input.txt").read_bytes())
    git("add", "input.txt")
    git("commit", "-m", "initial")
    initial = git("rev-parse", "HEAD")
    git("branch", "topic")
    git("checkout", "topic")
    (repository / "input.txt").write_text("changed\n")
    git("add", "input.txt")
    git("commit", "-m", "topic")
    topic = git("rev-parse", "HEAD")
    git("checkout", "main")
    git("merge", "--ff-only", "topic")
    git("merge-base", "--is-ancestor", initial, "HEAD")
    assert git("merge-base", initial, "HEAD") == initial
    assert git("status", "--porcelain") == ""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(origin))
    git("remote", "add", "origin", origin.as_uri())
    git("push", "origin", "main")
    assert git("--git-dir=" + str(origin), "rev-parse", "refs/heads/main") == topic
    clone = tmp_path / "clone"
    git("clone", "--no-hardlinks", origin.as_uri(), str(clone))
    assert git("rev-parse", "HEAD", cwd=clone) == topic
    assert git("status", "--porcelain", cwd=clone) == ""
    git("fetch", "origin")
    worktree = tmp_path / "linked-worktree"
    git("worktree", "add", "--detach", str(worktree), "HEAD")
    assert git("rev-parse", "HEAD", cwd=worktree) == topic
    git("worktree", "remove", str(worktree))
    assert not worktree.exists()
    git("remote", "set-url", "origin", (tmp_path / "missing.git").as_uri())
    git("fetch", "origin", fails=True)
    git("remote", "set-url", "origin", origin.as_uri())
    git("fetch", "origin")
    lock = repository / ".git/index.lock"
    lock.write_text("owned obstruction")
    git("add", "input.txt", fails=True)
    lock.unlink()
    git("add", "input.txt")
    lock = repository / ".git/refs/heads/main.lock"
    lock.write_text("owned obstruction")
    git("update-ref", "refs/heads/main", initial, fails=True)
    lock.unlink()
    git("update-ref", "refs/heads/main", initial)
    git("update-ref", "refs/heads/main", topic)
    fetch = repository / ".git/FETCH_HEAD"
    fetch.unlink()
    fetch.mkdir()
    git("fetch", "origin", fails=True)
    fetch.rmdir()
    git("fetch", "origin")
    with pytest.raises(OSError):
        Path("/project/input.txt").write_text("forbidden")
    assert not Path("/project/undeclared.txt").exists()
    assert not Path("/project/.git").exists()
    native = tmp_path / "generated-git"
    shutil.copyfile(shutil.which("git"), native)
    native.chmod(0o755)
    with pytest.raises(PermissionError):
        subprocess.run([str(native), "--version"], check=True, timeout=10, capture_output=True)
    assert os.getuid() != 0
"""


@pytest.fixture
def prepared(tmp_path):
    images = set()

    def prepare(profile):
        result = runtime.build_runtime(tmp_path, runtime_profile=profile)
        images.add(result["image"])
        return result

    yield tmp_path, prepare
    for image in images:
        runtime._image_info(image, runtime._project(tmp_path))
        runtime.command(["docker", "image", "rm", image])


def passed(root, **kwargs):
    result = controller.run(root, **kwargs)
    assert result["schema"] == 2 and result["kind"] == "run"
    assert result["status"] == "passed", result
    assert result["inventory_complete"] and result["grouping_complete"]
    assert re.fullmatch("[0-9a-f]{64}", result["runtime"]["identity"])
    return result


@pytest.mark.parametrize("mode", ["case", "batch"])
def test_local_git_operations_fault_repair_isolation_and_warm_reuse(prepared, monkeypatch, mode):
    root, prepare = prepared
    configure(root, profile="git-local-v1", requires=["git-local"])
    put(root, "tests/test_example.py", GIT_CASES)
    put(root, "input.txt", "declared input\n")
    put(root, "undeclared.txt", "synthetic excluded input\n")
    prepare("git-local-v1")
    cold = passed(root, execution_mode=mode)
    assert cold["counts"]["executed"] == cold["cases"]["passed"] == 2
    assert len(cold["groups"]) == (2 if mode == "case" else 1)
    assert cold["runtime"]["profile"] == "git-local-v1"
    assert cold["runtime"]["required_capabilities"] == ["git-local"]
    assert cold["execution"]["profile"] == mode + "-v1"

    def no_fresh_work(*args, **kwargs):
        pytest.fail("warm reuse launched or prepared a worker")

    monkeypatch.setattr(runtime, "execute", no_fresh_work)
    monkeypatch.setattr(runtime, "build_runtime", no_fresh_work)
    warm = passed(root, execution_mode=mode, required_capabilities=["git-local", "git-local"])
    assert warm["counts"]["reused"] == 2 and warm["counts"]["executed"] == 0
    assert warm["collection"]["outcome"] == "reused"
    assert warm["runtime"] == cold["runtime"]
    with monkeypatch.context() as preflight:
        forbid_backend_and_cache(preflight)
        with pytest.raises(UnsupportedCapability) as failure:
            controller.run(root, runtime_profile="python-v1", required_capabilities=[])
        assert failure.value.capability == "runtime:capability"


def test_profile_requirements_and_execution_modes_bind_reuse(prepared):
    root, prepare = prepared
    configure(root)
    put(
        root,
        "tests/test_example.py",
        "import pytest\n@pytest.mark.telescope(inputs=[])\ndef test_example(): pass\n",
    )
    original = (root / "pyproject.toml").read_bytes()
    prepare("python-v1")
    default = passed(root)
    assert default["runtime"]["profile"] == "python-v1"
    assert passed(root)["counts"]["reused"] == 1
    prepare("git-local-v1")
    git = passed(root, runtime_profile="git-local-v1")
    assert git["counts"]["executed"] == 1
    assert git["runtime"]["identity"] != default["runtime"]["identity"]
    assert passed(root, runtime_profile="git-local-v1")["counts"]["reused"] == 1
    required = passed(root, runtime_profile="git-local-v1", required_capabilities=["git-local"])
    assert required["counts"]["executed"] == 1
    assert (
        passed(
            root, runtime_profile="git-local-v1", required_capabilities=["git-local", "git-local"]
        )["counts"]["reused"]
        == 1
    )
    batch = passed(
        root,
        runtime_profile="git-local-v1",
        required_capabilities=["git-local"],
        execution_mode="batch",
    )
    assert batch["counts"]["executed"] == 1
    assert batch["groups"][0]["group_id"] != required["groups"][0]["group_id"]
    assert batch["runtime"] == required["runtime"]
    with pytest.raises(InfrastructureError):
        controller.run(root, runtime_profile="python-v1")
    assert (root / "pyproject.toml").read_bytes() == original


@pytest.mark.parametrize("daemon_platform", [b"linux/arm64\n", b"windows/amd64\n"])
def test_git_profile_refuses_unsupported_daemon_before_build_worker_or_cache(
    tmp_path, monkeypatch, daemon_platform
):
    configure(tmp_path, profile="git-local-v1", requires=["git-local"])
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args == ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]
        return daemon_platform

    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(
        controller, "CacheStore", lambda *a, **kw: pytest.fail("platform refusal reached cache")
    )
    for invoke in (runtime.build_runtime, controller.run):
        with pytest.raises(UnsupportedCapability) as failure:
            invoke(tmp_path)
        assert failure.value.capability == "runtime:platform"
    assert len(calls) == 2


def test_cli_preparation_profile_override_reaches_public_builder(tmp_path, monkeypatch, capsys):
    configure(tmp_path)
    monkeypatch.chdir(tmp_path)
    received = []

    def prepare(root, requirements=None, *, runtime_profile=None):
        received.append((root, requirements, runtime_profile))
        return {"profile": runtime_profile}

    monkeypatch.setattr(runtime, "build_runtime", prepare)
    assert controller.main(["runtime", "--runtime-profile", "git-local-v1"]) == 0
    assert received == [(tmp_path, None, "git-local-v1")]
    assert json.loads(capsys.readouterr().out) == {"profile": "git-local-v1"}
