"""Build and verify the exact installed distribution in an active virtual environment."""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from gate import Stages, candidate_binding, source_identity, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--functional-profile", choices=("core", "all"), default="all")
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error("activate a dedicated virtual environment before running checks")
    claimed = os.environ.get("TMPDIR")
    if not claimed or not Path(claimed).is_absolute() or not Path(claimed).is_dir():
        parser.error("provide an existing absolute caller-owned TMPDIR")
    root = Path(__file__).resolve().parent.parent
    source = source_identity(root)
    output = args.artifacts.resolve()
    output.mkdir(parents=True, exist_ok=False)
    stages = Stages(output, root)
    error = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="telescope-functional-gate-", dir=claimed
        ) as scratch:
            scratch = Path(scratch)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(scratch))
            for name in (
                "TELESCOPE_FIXTURE_NODE",
                "TELESCOPE_FIXTURE_ADMISSION",
                "TELESCOPE_FIXTURE_ADMISSION_SHA256",
                "TELESCOPE_FIXTURE_CHROMIUM",
            ):
                env.pop(name, None)
            stages.run("lint", "-m", "ruff", "check", ".", env=env)
            stages.run("format", "-m", "ruff", "format", "--check", ".", env=env)
            stages.run("build", "-m", "build", "--outdir", str(output / "dist"), env=env)
            distributions = sorted((output / "dist").iterdir())
            stages.run(
                "metadata",
                "-m",
                "twine",
                "check",
                "--strict",
                *(str(path) for path in distributions),
                env=env,
            )
            stages.run(
                "docs",
                "-m",
                "sphinx",
                "-n",
                "-W",
                "--keep-going",
                "-b",
                "dirhtml",
                "docs",
                str(output / "site"),
                env=env,
            )
            wheels = list((output / "dist").glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("expected exactly one built wheel")
            binding = candidate_binding(root, wheels[0])
            if binding["source"] != source:
                raise RuntimeError("candidate changed during build")
            stages.run(
                "install",
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                "--no-compile",
                str(wheels[0]),
                env=env,
            )
            binding_file = output / "candidate-binding.json"
            binding_hash = write_json(binding_file, binding)
            helper = str(root / "scripts/functional_gate.py")
            arguments = [
                "--binding",
                str(binding_file),
                "--binding-sha256",
                binding_hash,
                "--scratch",
                str(scratch),
                "--functional-profile",
                args.functional_profile,
            ]
            stages.run(
                "functional",
                "-I",
                "-B",
                helper,
                "tests",
                *arguments,
                "--membership",
                str(output / "functional-membership.json"),
                timeout=1200,
                env=env,
            )
            stages.run("harness", "-I", "-B", helper, "harness", *arguments, env=env)
        if source_identity(root) != source:
            raise RuntimeError("candidate changed during qualification")
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)[:1024]
        raise
    finally:
        write_json(
            output / "gate-result.json",
            {
                "source": source,
                "error": error,
                "status": "passed" if error is None else "failed",
                "functional_profile": args.functional_profile,
                "verdict_kind": "full" if args.functional_profile == "all" else "compatibility",
            },
        )


if __name__ == "__main__":
    main()
