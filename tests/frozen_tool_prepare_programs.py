"""Finite named-input preparation, invoked explicitly before program qualification.

This inventories declared prerequisites and copies tiny owned configuration/package
trees. It never launches a tool, imports the owned plugin copy, installs, compiles,
constructs a Capture, or starts a controller. Its caller supplies claimed scratch.
Functional preparation excludes browsers; browser benchmarks select that profile
explicitly.
"""

import argparse
import json
from pathlib import Path

import pytest
from frozen_tool_installed_fixture import InstalledConfiguration
from frozen_tool_program_fixture import ProgramConfiguration
from frozen_tool_worker_fixture import WorkerConfiguration

PROFILES = ("node", "installed", "browser")


def prepare(directory, destination, *, profiles=("node", "installed")):
    if not profiles or len(profiles) != len(set(profiles)) or any(name not in PROFILES for name in profiles):
        raise ValueError("select distinct supported preparation profiles")
    directory = Path(directory)
    prepared = {}
    for name in profiles:
        owned = directory / name
        owned.mkdir()
        with pytest.MonkeyPatch.context() as monkeypatch:
            if name == "installed":
                fixture = InstalledConfiguration(owned, monkeypatch, require_receipt=False)
                documents = fixture.admission_documents
            else:
                worker = WorkerConfiguration(owned, monkeypatch)
                fixture = ProgramConfiguration(worker, browser=name == "browser", require_receipt=False)
                documents = {"worker": worker.document, "frontend": fixture.document}
            prepared[name] = {"identity": fixture.identity, "documents": documents}
    content = json.dumps({"schema": 1, "profiles": prepared}, sort_keys=True, separators=(",", ":")).encode()
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("complete prerequisite receipt exceeds4MiB")
    with Path(destination).open("xb") as stream:
        stream.write(content)
    print(json.dumps({"prepared": {name: profile["identity"] for name, profile in prepared.items()},
                      "receipt_bytes": len(content)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--profile", action="append", choices=PROFILES)
    args = parser.parse_args()
    profiles = ("node", "installed") if args.profile is None else tuple(args.profile)
    args.directory.mkdir(parents=True, exist_ok=False)
    prepare(args.directory, args.output, profiles=profiles)


if __name__ == "__main__":
    main()
