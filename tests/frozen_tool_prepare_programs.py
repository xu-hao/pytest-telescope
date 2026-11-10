"""Finite named-input preparation, invoked explicitly before program qualification.

This inventories declared prerequisites and copies tiny owned configuration/package
trees. It never launches a tool, imports the owned plugin copy, installs, compiles,
constructs a Capture, or starts a controller. Its caller supplies claimed scratch.
This is explicit compatibility-benchmark preparation; each invocation selects its
named profiles. The default functional gate does not invoke this helper.
"""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from frozen_tool_installed_fixture import InstalledConfiguration
from frozen_tool_program_fixture import (
    PROFILE_DOCUMENTS,
    ProgramConfiguration,
    prerequisite_identity,
    read_prerequisite_receipt,
)
from frozen_tool_worker_fixture import WorkerConfiguration

PROFILES = ("node", "installed", "browser")


def validate_receipt(path, expected, directory, *, profiles):
    """Validate only explicitly selected benchmark profiles, without running a program."""
    if (
        type(profiles) not in (tuple, list)
        or not profiles
        or any(type(name) is not str or name not in PROFILE_DOCUMENTS for name in profiles)
        or len(set(profiles)) != len(profiles)
    ):
        raise ValueError("select distinct supported preparation profiles")
    receipt = read_prerequisite_receipt(path, expected)
    if set(receipt["profiles"]) != set(profiles):
        raise ValueError("prerequisite receipt profiles differ from explicit selection")
    from pytest_telescope.tool_context import FORMAT, parse

    for profile in receipt["profiles"].values():
        documents = profile["documents"]
        with tempfile.TemporaryDirectory(prefix="receipt-validation-", dir=directory) as owned:
            project = Path(owned)
            for index, document in enumerate(documents.values()):
                filename = f"declaration-{index}.json"
                content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError("prerequisite declaration exceeds4MiB")
                with (project / filename).open("xb") as stream:
                    stream.write(content)
                parsed = parse(project, "benchmark", {"format": FORMAT, "manifest": filename})
                if not parsed.document["roots"]:
                    raise ValueError("prerequisite profile has an empty declaration")
        if prerequisite_identity(documents) != profile["identity"]:
            raise ValueError("prerequisite declaration identity disagrees")
    return receipt


def prepare(directory, destination, *, profiles=("node", "installed")):
    if (
        not profiles
        or len(profiles) != len(set(profiles))
        or any(name not in PROFILES for name in profiles)
    ):
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
                fixture = ProgramConfiguration(
                    worker, browser=name == "browser", require_receipt=False
                )
                documents = {"worker": worker.document, "frontend": fixture.document}
            prepared[name] = {"identity": fixture.identity, "documents": documents}
    content = json.dumps(
        {"schema": 1, "profiles": prepared}, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("complete prerequisite receipt exceeds4MiB")
    with Path(destination).open("xb") as stream:
        stream.write(content)
    validate_receipt(destination, hashlib.sha256(content).hexdigest(), directory, profiles=profiles)
    print(
        json.dumps(
            {
                "prepared": {name: profile["identity"] for name, profile in prepared.items()},
                "receipt_bytes": len(content),
            }
        ),
        flush=True,
    )


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
