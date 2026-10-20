"""A deterministic local Git round trip in private non-executable scratch."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.telescope(inputs=["data/message.txt"])
def test_local_origin(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = dict(
        os.environ,
        GIT_AUTHOR_DATE="2001-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2001-01-01T00:00:00+0000",
    )

    def git(*arguments, cwd=repository):
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Example Author")
    git("config", "user.email", "example@example.invalid")
    declared = Path(__file__).resolve().parents[1] / "data/message.txt"
    (repository / "message.txt").write_bytes(declared.read_bytes())
    git("add", "message.txt")
    git("commit", "-m", "initial")
    expected = git("rev-parse", "HEAD")
    origin = tmp_path / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(origin))
    git("remote", "add", "origin", origin.as_uri())
    git("push", "origin", "main")
    clone = tmp_path / "clone"
    git("clone", origin.as_uri(), str(clone))
    assert git("rev-parse", "HEAD", cwd=clone) == expected
    assert git("status", "--porcelain", cwd=clone) == ""
