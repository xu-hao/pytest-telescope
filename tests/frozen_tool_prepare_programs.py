"""Finite named-input preparation, invoked explicitly before program qualification.

This inventories declared prerequisites and copies tiny owned configuration/package
trees. It never launches a tool, imports the owned plugin copy, installs, compiles,
constructs a Capture, or starts a controller. Its caller supplies claimed scratch.
"""

import json
from pathlib import Path

import pytest

from frozen_tool_installed_fixture import InstalledConfiguration
from frozen_tool_program_fixture import ProgramConfiguration
from frozen_tool_worker_fixture import WorkerConfiguration


def prepare(directory, destination):
    directory = Path(directory)
    profiles = {}
    for name in ("node", "installed", "browser"):
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
            profiles[name] = {"identity": fixture.identity, "documents": documents}
    content = json.dumps({"schema": 1, "profiles": profiles}, sort_keys=True, separators=(",", ":")).encode()
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("complete prerequisite receipt exceeds4MiB")
    with Path(destination).open("xb") as stream:
        stream.write(content)
    print(json.dumps({"prepared": {name: profile["identity"] for name, profile in profiles.items()},
                      "receipt_bytes": len(content)}), flush=True)
