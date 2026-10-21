"""One fresh ordinary pytest session per suite: no sandbox, snapshot or cache."""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from .model import InfrastructureError
from .result import validate_ordinary
from .sandbox import MAX_COLLECTION, WORKER, capture

# Load the worker by path so the session's import path gains no package directory.
BOOT = (
    "import importlib.util,sys\n"
    "spec=importlib.util.spec_from_file_location('_pytest_telescope_session',sys.argv[1])\n"
    "module=importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "raise SystemExit(module.ordinary_main(sys.argv[2]))\n"
)


def session(manifest, suite, files, options, selectors, selection_disabled, timeout):
    """Run the suite's ordinary cases with the invoking interpreter and environment.

    Every file is collected; scoped cases of mixed files are removed before fixture
    setup and returned only as inventory rows.
    """
    root = manifest.root
    nonce = uuid.uuid4().hex
    request = dict(
        mode="ordinary",
        nonce=nonce,
        project_root=str(root),
        suite=suite.name,
        suite_root=suite.root,
        registry=[row for row in manifest.registry if row["file"] in files],
        selectors=selectors,
        selection_disabled=selection_disabled,
        allow_unknown_selectors=True,
        metadata=False,
        args=[
            "-c",
            str(root / suite.config),
            "--rootdir=" + str(root / suite.root),
            "-o",
            "addopts=",
            *[part for plugin in suite.plugins for part in ("-p", plugin)],
            *options,
            *[str(root / name) for name in files],
        ],
    )
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_COLLECTION:
        raise InfrastructureError("ordinary session request exceeds 64 MiB")
    with tempfile.TemporaryDirectory(prefix="pytest-telescope-ordinary-") as directory:
        request_path = Path(directory) / "request.json"
        request_path.write_bytes(encoded)
        code, output = capture(
            [sys.executable, "-c", BOOT, str(WORKER), str(request_path)],
            timeout,
            env=dict(os.environ),
            cwd=root / suite.cwd,
            limit=MAX_COLLECTION,
        )
    try:
        frames = [
            line.removeprefix(b"TELESCOPE_RESULT ")
            for line in output.splitlines()
            if line.startswith(b"TELESCOPE_RESULT ")
        ]
        if len(frames) != 1:
            raise ValueError("ordinary result frame count")
        result = json.loads(frames[0])
        if not validate_ordinary(result, nonce) or result["exit_code"] != code:
            raise ValueError("ordinary result protocol")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InfrastructureError("ordinary pytest session returned no valid result") from exc
    return result
