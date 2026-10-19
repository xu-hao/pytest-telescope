"""Build and verify the installed distribution in an active virtual environment."""

import argparse
import subprocess
import sys
from pathlib import Path


def run(*args: str) -> None:
    subprocess.run([sys.executable, *args], check=True, timeout=300)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error("activate a dedicated virtual environment before running checks")
    output = args.artifacts.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run("-m", "ruff", "check", ".")
    run("-m", "ruff", "format", "--check", ".")
    run("-m", "build", "--outdir", str(output / "dist"))
    distributions = sorted((output / "dist").iterdir())
    run("-m", "twine", "check", "--strict", *(str(path) for path in distributions))
    run("-m", "sphinx", "-n", "-W", "--keep-going", "-b", "dirhtml", "docs", str(output / "site"))
    wheels = list((output / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("expected exactly one built wheel")
    run("-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheels[0]))
    run("-m", "pytest", "-q", "--basetemp", str(output / "tests"))
    run("-m", "unittest", "discover", "-s", "benchmarks", "-p", "test_*.py")


if __name__ == "__main__":
    main()
