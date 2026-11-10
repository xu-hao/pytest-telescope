"""Owned static view fixtures, distinct from real CPython worker qualification.

The synthetic interpreter description exercises Startup's declared path model only.
Its executable is a real tiny static ELF, not Python. Full worker tests use a separately
prepared actual Sandbox and explicitly selected installed runtime bytes.
"""

import copy
import struct
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from frozen_tool_fixtures import ToolProject, entries, manifest, put

from pytest_telescope import sandbox, tool_capture, tool_context


def static_reader():
    """ELF64 Linux syscall program: copy at most 4096 bytes of argv[1], exit7 on error.

    No assembler, compiler, loader, libc, import or subprocess runs to create it.
    The two signed branches are resolved within this fixed, bounded instruction list.
    """
    code = bytearray.fromhex(
        "488b7c2410"  # mov rdi,[rsp+16] : argv[1]
        "b802000000"  # mov eax,2 : open
        "31f631d20f05"  # xor esi,esi; xor edx,edx; syscall
        "85c07800"  # test eax,eax; js failure
        "89c74881ec001000004889e6ba0010000031c00f05"  # read(fd,rsp,4096)
        "85c07800"  # test eax,eax; js failure
        "89c2b801000000bf010000000f05"  # write(1,rsp,length)
        "b83c00000031ff0f05"  # exit(0)
    )
    branches = [
        index + 1 for index in range(len(code) - 1) if code[index : index + 2] == b"\x78\x00"
    ]
    failure = len(code)
    code.extend(bytes.fromhex("b83c000000bf070000000f05"))
    for offset in branches:
        code[offset] = failure - (offset + 1)
    assert len(branches) == 2 and len(code) < 128
    offset = 64 + 56
    size = offset + len(code)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + b"\0" * 9,
        2,
        62,
        1,
        0x400000 + offset,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, size, size, 4096)
    return header + segment + bytes(code)


def sparse_rows(source, names):
    """Filter a small owned fixture inventory to selected entries and ancestors."""
    selected = {Path(".")}
    for name in names:
        relative = Path(name)
        selected.add(relative)
        selected.update(relative.parents)
    inventory = {row["path"]: row for row in entries(source)}
    return [inventory[path.as_posix()] for path in sorted(selected)]


def route_configuration(monkeypatch, source):
    original = tool_capture._parent

    @contextmanager
    def parent(filename):
        filename = Path(filename)
        target = (
            source / filename.relative_to("/etc") if filename.is_relative_to("/etc") else filename
        )
        with original(target) as result:
            yield result

    monkeypatch.setattr(tool_capture, "_parent", parent)


class StaticConfiguration:
    """Real Capture/Runtime/Projection with a synthetic standard prefix description."""

    def __init__(self, directory, monkeypatch):
        self.project = ToolProject(directory / "project", directory / "installed")
        self.prefix = directory / "static-prefix"
        self.config = directory / "owned-etc"
        self.prefix.mkdir(mode=0o755)
        self.config.mkdir(mode=0o755)
        self.prefix.chmod(0o755)
        self.config.chmod(0o755)
        self.executable = put(self.prefix, "bin/python3.12", static_reader(), 0o755)
        for name in ("os.py", "site.py", "encodings/__init__.py"):
            put(self.prefix, "lib/python3.12/" + name, "# owned path-model landmark\n")
        for path in self.prefix.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        put(self.config, "selected.txt", "declared configuration\n", 0o640)
        put(self.config, "unselected.txt", "must stay absent\n")
        self.selected = {"selected.txt"}
        route_configuration(monkeypatch, self.config)
        library = str(self.prefix / "lib/python3.12")
        sites = [library + "/site-packages"]
        self.box = sandbox.Sandbox(
            bwrap="/usr/bin/bwrap",
            executable=str(self.executable),
            binds=((str(self.prefix), str(self.prefix)),),
            path=str(self.prefix / "bin"),
            identity="0" * 64,
            description={
                "identity": {
                    "implementation": "cpython",
                    "hexversion": 0x030C0000,
                    "abiflags": "",
                    "platform": "linux-x86_64",
                },
                "path": [library, library + "/lib-dynload", *sites],
                "startup": {
                    "prefix": str(self.prefix),
                    "exec_prefix": str(self.prefix),
                    "base_prefix": str(self.prefix),
                    "base_exec_prefix": str(self.prefix),
                    "base_executable": str(self.executable),
                    "stdlib": library,
                    "platstdlib": library,
                    "purelib": sites[0],
                    "platlib": sites[0],
                    "platlibdir": "lib",
                    "sites": sites,
                },
            },
        )
        self.document = manifest()
        self.document["executables"] = [
            {
                "file": {"root": "prefix", "path": "bin/python3.12"},
                "format": "elf",
                "interpreter": None,
                "libraries": [],
                "resources": [],
                "commands": [],
            }
        ]
        self.save()
        self.sequence = 0

    def save(self):
        self.document["roots"] = [
            {
                "id": "prefix",
                "source": str(self.prefix),
                "coverage": "files",
                "preserve_source": True,
                "entries": entries(self.prefix),
            },
            {
                "id": "config",
                "source": "/etc",
                "coverage": "files",
                "preserve_source": True,
                "entries": sparse_rows(self.config, self.selected),
            },
        ]
        self.project.documents["native"] = self.document
        self.project.definitions["native"] = {
            "format": "frozen-tool-v1",
            "manifest": "tool-locks/native.json",
        }
        self.project.save("native")
        self.project.configure()

    def capture(self):
        self.sequence += 1
        directory = self.project.root.parent / ("capture-" + str(self.sequence))
        directory.mkdir()
        tools = tool_context.load_selected(self.project.root, self.project.definitions, ["native"])
        return tool_capture.Capture(
            SimpleNamespace(root=self.project.root, tools=tools),
            directory,
            20,
            None,
            sandbox=self.box,
        )

    def description(self, mutate):
        description = copy.deepcopy(self.box.description)
        mutate(description)
        from dataclasses import replace

        self.box = replace(self.box, description=description)


def direct_static_read(box, view, path):
    """Launch the real static subject through production base arguments and view binds.

    This is a direct-program filesystem oracle, never a pytest-worker compatibility
    result. Native is the actual Runtime-derived ToolView flag, not injected by fixture.
    """
    from dataclasses import replace

    from pytest_telescope.tool_startup import environment

    controlled = replace(box, binds=environment(box, view.closed))
    argv = sandbox._base_arguments(controlled, frozen_prefixes=view.prefixes, native=view.native)
    for source, destination in view.bindings:
        argv += ["--ro-bind", str(source), destination]
    argv += [
        "--remount-ro",
        "/",
        "--chdir",
        "/tmp",
        *sandbox._environment(controlled),
        "--",
        box.executable,
        path,
    ]
    return sandbox.capture(argv, 20, env=sandbox._host_environment(), limit=1024 * 1024)
