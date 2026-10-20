"""Explicit per-view lookup paths for isolated CPython children; never installation."""

import hashlib
import json
import os
from pathlib import Path

from .model import (
    DeclarationError,
    InfrastructureError,
    UnsupportedCapability,
    excluded_namespace,
    mapping,
    relative,
    sequence,
)

DEFAULT = "default"
ISOLATED = "isolated-source-v1"
SLOT = "/usr/local/lib/python3.12/site-packages/zz_telescope_source_view.pth"
MAX_BYTES = 256 * 1024
SLOT_ID = hashlib.sha256(
    json.dumps(
        {"path": SLOT, "format": "data-only-utf8-v1", "empty": hashlib.sha256(b"").hexdigest()},
        sort_keys=True,
    ).encode()
).hexdigest()


def normalize(root, config, profile=None, roots=None, *, complete=False):
    config = mapping(config, "python", {"profile", "source_roots"})
    if complete and set(config) != {"profile", "source_roots"}:
        raise DeclarationError("suite python replacement requires profile and source_roots")
    profile = config.get("profile", DEFAULT) if profile is None else profile
    if type(profile) is not str or profile not in {DEFAULT, ISOLATED}:
        raise UnsupportedCapability("python:profile", "unsupported Python source profile")
    roots = sequence(config.get("source_roots", []) if roots is None else roots, "source_roots")
    if len(roots) > 64 or len(roots) != len(set(roots)):
        raise DeclarationError("source_roots requires at most 64 distinct ordered directories")
    if profile == DEFAULT and roots:
        raise DeclarationError("default Python profile cannot declare source_roots")
    for name in roots:
        if (
            name != relative(name)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or excluded_namespace(root, name)
        ):
            raise DeclarationError("source roots require canonical project directory paths")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in Path(name).parts:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
        except OSError as exc:
            raise DeclarationError(
                "source roots require ordinary directories without symlinks"
            ) from exc
        finally:
            os.close(descriptor)
    result = {"profile": profile, "source_roots": list(roots)}
    projection(result, True)
    return result


def validate_view(settings, files):
    if settings["profile"] == DEFAULT:
        return
    prefixes = ["" if root == "." else root + "/" for root in settings["source_roots"]]
    for name in files:
        for prefix in prefixes:
            if not name.startswith(prefix):
                continue
            entry = name[len(prefix) :].split("/", 1)[0]
            if entry.split(".")[0] in {"sitecustomize", "usercustomize"} or entry.endswith(
                (".dist-info", ".egg-info", ".egg-link")
            ):
                raise UnsupportedCapability(
                    "python:source-layout",
                    "source roots cannot expose startup hooks or installed metadata",
                )


def projection(settings, metadata):
    if settings["profile"] == DEFAULT:
        return b""
    paths = ["/project" if name == "." else "/project/" + name for name in settings["source_roots"]]
    if metadata:
        paths.append("/metadata")
    data = "".join(path + "\n" for path in paths).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise DeclarationError("Python source projection exceeds 256 KiB")
    return data


def key(settings, metadata):
    return {
        "slot": SLOT_ID,
        "settings": settings,
        "projection": hashlib.sha256(projection(settings, metadata)).hexdigest(),
    }


def envelope(suites, *, validated=False):
    rows = []
    for suite in suites:
        setting = suite.python
        value = {"slot": SLOT_ID, "settings": setting}
        identity = (
            hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if validated
            else None
        )
        rows.append({"suite": suite.name, **setting, "identity": identity})
    return {"suites": rows}


def materialize(directory, settings, metadata):
    if settings["profile"] == DEFAULT:
        return None
    path = directory / "source-view.pth"
    data = projection(settings, metadata)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        path.chmod(0o444)
        if path.read_bytes() != data:
            raise InfrastructureError("Python source projection changed before mounting")
        os.utime(path, ns=(0, 0))
    except OSError as exc:
        raise InfrastructureError("owned Python source projection storage failed") from exc
    return path


def dockerfile():
    # The immutable image verifies the interpreter location and empty slot during build.
    return (
        "RUN python -c 'import os,pathlib,sysconfig; "
        'assert sysconfig.get_path("purelib")=="/usr/local/lib/python3.12/site-packages"; '
        f'p=pathlib.Path("{SLOT}"); assert not p.exists(); '
        'p.write_bytes(b""); p.chmod(0o444); '
        'assert p.read_bytes()==b"" and p.stat().st_size==0; os.utime(p,ns=(0,0))' + "'\n"
    )
