"""Prelaunch closure of the supported invoking CPython filesystem search paths.

This module consumes the existing controller description and frozen records only.
It neither probes an interpreter nor discovers or captures additional source files.
"""

import posixpath
import re
from pathlib import Path

from .tool_context import digest
from .tool_graph import layout


def signature(box):
    return digest([box.executable, box.binds, box.links, box.description, "-I"])


def path(value):
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or len(value.encode("utf-8")) > 4096
        or len(Path(value).parts) > 65
    ):
        layout("native startup requires canonical absolute interpreter paths")
    return Path(value)


class Startup:
    def __init__(self, box, originals, budget, project):
        if box is None:
            layout("native view requires the invoking interpreter description")
        description = box.description
        identity = description["identity"]
        version = identity.get("hexversion")
        if (
            identity.get("implementation") != "cpython"
            or type(version) is not int
            or version >> 24 != 3
            or ((version >> 16) & 255) not in (11, 12, 13)
            or identity.get("abiflags") != ""
            or identity.get("platform") != "linux-x86_64"
        ):
            layout("native startup supports standard Linux x86_64 CPython 3.11 through 3.13")
        fields = description.get("startup")
        required = {
            "prefix",
            "exec_prefix",
            "base_prefix",
            "base_exec_prefix",
            "base_executable",
            "stdlib",
            "platstdlib",
            "purelib",
            "platlib",
            "platlibdir",
            "sites",
        }
        if type(fields) is not dict or set(fields) != required or fields["platlibdir"] != "lib":
            layout("native startup requires the supported interpreter path description")
        prefix, base = path(fields["prefix"]), path(fields["base_prefix"])
        if path(fields["exec_prefix"]) != prefix or path(fields["base_exec_prefix"]) != base:
            layout("split interpreter prefix layouts are unsupported")
        self.roots = tuple(sorted({str(prefix), str(base)}))
        for root in self.roots:
            current = Path(root)
            if root in (
                "/",
                "/etc",
                "/bin",
                "/lib",
                "/lib64",
                "/home",
                "/root",
                "/tmp",
                "/var",
                "/opt",
                str(Path.home()),
            ):
                layout("native startup prefix overlaps a protected root")
            if current.is_relative_to(project) or Path(project).is_relative_to(current):
                layout("native startup prefix overlaps the project")
            if any(
                current.is_relative_to(p) or Path(p).is_relative_to(current)
                for p in ("/proc", "/dev", "/sys", "/run", "/tools", "/telescope", "/metadata")
            ):
                layout("native startup prefix overlaps a protocol or control path")
        minor = (version >> 16) & 255
        library = f"python3.{minor}"
        stdlib = base / "lib" / library
        if (
            path(fields["stdlib"]) != stdlib
            or path(fields["platstdlib"]) != prefix / "lib" / library
        ):
            layout("native startup requires the standard prefix library layout")
        executable = path(box.executable)
        base_executable = path(fields["base_executable"])
        if executable.parent != prefix / "bin" or base_executable.parent != base / "bin":
            layout("native startup requires a prefix bin interpreter")
        if any(
            not re.fullmatch(r"python(?:3(?:\.\d+)?)?", p.name)
            for p in (executable, base_executable)
        ):
            layout("native startup interpreter alias is unsupported")
        self.signature = signature(box)
        self.originals = originals
        self.budget = budget
        for filename in (executable, base_executable):
            graph, key = self.regular(filename)
            resolved = path(graph.resolved_path(str(filename)))
            if resolved.parent != base / "bin":
                layout("native startup interpreter target leaves its declared base prefix")
            if not graph.nodes[key]["mode"] & 0o111:
                layout("native startup interpreter is not executable")
        for name in ("os.py", "site.py", "encodings/__init__.py"):
            self.regular(stdlib / name)
        search = description["path"]
        sites = fields["sites"]
        if type(sites) is not list or len(sites) > 64 or len(search) > 64:
            layout("native startup search paths exceed the supported bound")
        for filename in [*search, *sites, fields["purelib"], fields["platlib"]]:
            current = path(filename)
            if not any(current.is_relative_to(root) for root in self.roots):
                layout("native startup search path leaves the closed interpreter prefixes")
        # The entire prefix, not only the observed sys.path, is projected. This
        # closes both pyvenv candidates, compiled/zip landmarks, site directories,
        # executable/library ._pth candidates and nonexistent search entries.
        # Hooks and zip startup are not part of this bounded supported profile.
        for filename, (graph, key) in originals.items():
            budget.tick()
            current = Path(filename)
            if not any(current.is_relative_to(root) for root in self.roots):
                continue
            if current.suffix in (".pth", ".egg-link", "._pth") or any(
                part == hook or part.startswith(hook + ".")
                for part in current.parts
                for hook in ("sitecustomize", "usercustomize")
            ):
                layout("native startup import hooks are unsupported")
            if current.name == "pybuilddir.txt" or current.parts[-2:] == ("Modules", "Setup.local"):
                layout("native startup build-tree layouts are unsupported")
            if current.name == f"python3{minor}.zip":
                layout("native startup zip libraries are unsupported")
        for root in (prefix, base):
            if str(root / "bin" / "pyvenv.cfg") in originals:
                layout("interpreter-adjacent virtual environment configuration is unsupported")
        config = prefix / "pyvenv.cfg"
        if prefix == base:
            if str(config) in originals:
                layout("base interpreter cannot declare virtual environment configuration")
        else:
            graph, key = self.regular(config)
            try:
                data = graph.bytes(key, maximum=65536).decode("utf-8")
                values = {}
                for line in data.splitlines():
                    if not line.strip():
                        continue
                    name, separator, value = line.partition("=")
                    name, value = name.strip().lower(), value.strip()
                    if not separator or name in values:
                        raise ValueError
                    values[name] = value
                if set(values) - {
                    "home",
                    "include-system-site-packages",
                    "version",
                    "executable",
                    "command",
                    "prompt",
                }:
                    raise ValueError
                if values.get("home") != str(base / "bin") or values.get(
                    "include-system-site-packages"
                ) not in ("true", "false"):
                    raise ValueError
                if "executable" in values:
                    configured = path(values["executable"])
                    graph, key = self.regular(configured)
                    if (
                        configured.parent != base / "bin"
                        or path(graph.resolved_path(str(configured))).parent != base / "bin"
                    ):
                        raise ValueError
            except (UnicodeError, ValueError):
                layout("unsupported frozen virtual environment configuration")
            if str(base / "pyvenv.cfg") in originals:
                layout("nested base virtual environment configuration is unsupported")
        self.identity = digest(["cpython-prefix-closure-v1", self.roots, self.signature])

    def regular(self, filename):
        self.budget.tick()
        pair = self.originals.get(str(filename))
        if pair is None:
            layout("native startup lacks an explicitly selected interpreter input")
        graph, key = pair
        resolved = graph.resolve(str(filename))
        if graph.nodes[resolved]["kind"] != "file":
            layout("native startup input is not a frozen regular file")
        return graph, resolved


def environment(box, closed):
    """Suppress live environment mounts wholly replaced by owned projections.

    Every invoking environment bind must be covered. Unknown or ancestor mounts
    refuse rather than relying on later overlays to make their contents safe.
    """
    for source, destination in box.binds:
        target = path(destination)
        path(source)
        if any(target.is_relative_to(root) for root in closed):
            continue
        layout("native environment mount is outside the closed startup projections")
    for _, location in box.links:
        if not any(path(location).is_relative_to(root) for root in closed):
            layout("native environment link is outside the closed startup projections")
    return ()
