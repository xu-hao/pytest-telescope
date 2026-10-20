"""Explicit immutable native-tool profiles; never resolve arbitrary host tools."""

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

from .model import (
    DeclarationError,
    InfrastructureError,
    UnsupportedCapability,
    mapping,
    regular_bytes,
)

DEFAULT = "python-v1"
GIT = "git-local-v1"
MANIFEST = json.loads(Path(__file__).with_name("git_local_v1.json").read_bytes())
ENVIRONMENT = {
    "GIT_EXEC_PATH": "/opt/telescope/git/bin",
    "GIT_TEMPLATE_DIR": "/opt/telescope/git/templates",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/dev/null",
}
INTEGRITY = "TELESCOPE_GIT_PROFILE_INTEGRITY"
DOWNLOAD = r"""
import hashlib, pathlib, sys, urllib.request
url, target, expected_size, expected_hash = sys.argv[1:]
try:
    with urllib.request.urlopen(url, timeout=25) as stream:
        if not stream.geturl().startswith('https://'):
            raise ValueError('non-HTTPS artifact response')
        data=stream.read(16*1024*1024+1)
except Exception:
    raise SystemExit(71)
if len(data)!=int(expected_size) or hashlib.sha256(data).hexdigest()!=expected_hash:
    raise SystemExit(72)
try:
    with pathlib.Path(target).open('xb') as stream:
        stream.write(data)
except OSError:
    raise SystemExit(73)
"""


def config(root):
    try:
        raw, _ = regular_bytes(Path(root).resolve(), "pyproject.toml", 4 * 1024 * 1024)
    except FileNotFoundError:
        return {}
    try:
        document = tomllib.loads(raw.decode())
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DeclarationError("runtime configuration requires valid UTF-8 TOML") from exc
    tool = mapping(document.get("tool", {}), "tool")
    telescope = mapping(tool.get("pytest_telescope", {}), "pytest_telescope")
    return mapping(telescope.get("runtime", {}), "runtime", {"profile", "requires"})


def normalize(spec, profile=None, required=None):
    spec = mapping(spec, "runtime", {"profile", "requires"})
    selected = spec.get("profile", DEFAULT) if profile is None else profile
    if type(selected) is not str:
        raise DeclarationError("runtime profile must be a string")
    if selected not in {DEFAULT, GIT}:
        raise UnsupportedCapability("runtime:profile", "unknown runtime profile or version")
    capabilities = set()
    for value in (spec.get("requires", []), [] if required is None else required):
        if (
            type(value) is not list
            or len(value) > 16
            or any(type(item) is not str or not item or len(item) > 128 for item in value)
        ):
            raise DeclarationError("runtime requirements need at most 16 nonempty strings")
        capabilities.update(value)
    if len(capabilities) > 16:
        raise DeclarationError("runtime requirements exceed 16 capabilities")
    supported = {"git-local"} if selected == GIT else set()
    if capabilities - supported:
        raise UnsupportedCapability(
            "runtime:capability", "runtime cannot supply required capability"
        )
    return dict(profile=selected, required_capabilities=sorted(capabilities), identity=None)


def provenance(profile):
    implementation = hashlib.sha256()
    for name in ("runtime.py", "runtime_profiles.py"):
        implementation.update(name.encode())
        implementation.update(Path(__file__).with_name(name).read_bytes())
    definition = dict(schema=1, profile=profile, preparation=implementation.hexdigest())
    if profile == GIT:
        definition.update(manifest=MANIFEST, environment=ENVIRONMENT, projection=1)
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_backend(selection, command):
    if selection["profile"] == GIT:
        actual = (
            command(
                ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], timeout=10
            )
            .decode()
            .strip()
        )
        if actual != MANIFEST["platform"]:
            raise UnsupportedCapability(
                "runtime:platform", "Git profile requires a linux/amd64 Docker server"
            )


def validate_environment(selection, names):
    if selection["profile"] == GIT and any(
        name
        in {
            *ENVIRONMENT,
            "PATH",
            "HOME",
            "TMPDIR",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_GLOBAL",
            "XDG_CONFIG_HOME",
        }
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        for name in names
    ):
        raise DeclarationError("environment must not override controlled Git runtime settings")


def fetch_archive(context, capture, *, terminal=()):
    target = context / "git.deb"
    archive = MANIFEST["archive"]
    for attempt in range(2):
        try:
            code, _ = capture(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    DOWNLOAD,
                    archive["url"],
                    str(target),
                    str(archive["size"]),
                    archive["sha256"],
                ],
                30,
            )
        except InfrastructureError as exc:
            if isinstance(exc, terminal) or attempt:
                raise
            target.unlink(missing_ok=True)
            continue
        if code == 0:
            # Independently verify the inert artifact before Docker can extract it.
            raw, _ = regular_bytes(context, "git.deb", 16 * 1024 * 1024)
            if len(raw) != archive["size"] or hashlib.sha256(raw).hexdigest() != archive["sha256"]:
                raise InfrastructureError("Git artifact integrity verification failed")
            return raw
        target.unlink(missing_ok=True)
        if code == 72:
            raise InfrastructureError("Git artifact integrity verification failed")
        if code != 71 or attempt:
            raise InfrastructureError(
                "pinned Git artifact retrieval exhausted its bounded attempts"
            )
    raise AssertionError("unreachable retrieval state")


def dockerfile(context, base):
    if base != MANIFEST["base"]:
        raise InfrastructureError("Git profile base differs from its qualified closure")
    verifier = (
        "import hashlib,pathlib,sys\n"
        f"expected={MANIFEST['native_files_sha256']!r}\n"
        "try:\n"
        " valid=all(hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()==digest for name,digest in expected.items())\n"
        "except OSError:\n"
        " valid=False\n"
        "if not valid:\n"
        f" print('{INTEGRITY}',flush=True);sys.exit(72)\n"
        "for name in expected:\n"
        " if name.startswith('/opt/telescope/git/'):\n"
        "  pathlib.Path(name).chmod(0o555)\n"
    )
    (context / "verify_git.py").write_text(verifier)
    aliases = " && ".join(
        f"ln /opt/telescope/git/bin/git /opt/telescope/git/bin/{name}"
        for name in MANIFEST["aliases"]
    )
    prefix = (
        f"FROM {base} AS git-payload\n"
        "COPY git.deb /tmp/git.deb\n"
        "RUN (dpkg-deb --extract /tmp/git.deb /tmp/package && "
        "mkdir -p /opt/telescope/git/bin /opt/telescope/git/templates && "
        "cp /tmp/package/usr/bin/git /opt/telescope/git/bin/git && "
        "cp /tmp/package/usr/share/doc/git/copyright /opt/telescope/git/copyright && "
        f"{aliases}) || {{ echo {INTEGRITY}; exit 72; }}\n"
        "COPY verify_git.py /tmp/verify_git.py\n"
        "RUN python /tmp/verify_git.py\n"
        f"FROM {base}\n"
        "COPY --from=git-payload /opt/telescope/git /opt/telescope/git\n"
    )
    environment = (
        'ENV PATH="/opt/telescope/git/bin:${PATH}" '
        + " ".join(f"{key}={value}" for key, value in ENVIRONMENT.items())
        + "\n"
    )
    return prefix, environment


def integrity_failed(output):
    """Recognize emitted markers, not a rendered Dockerfile instruction containing one."""
    return any(
        re.fullmatch(rb"(?:#[0-9]+ [0-9.]+ )?" + INTEGRITY.encode(), line.strip())
        for line in output.splitlines()
    )
