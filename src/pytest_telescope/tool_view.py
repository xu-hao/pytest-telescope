"""Owned read-only projections of exact selected entries and command mappings."""

import os
import stat
from pathlib import Path

from .model import InfrastructureError
from .tool_context import digest
from .tool_graph import layout


def inside(filename, parents):
    return any(Path(filename).is_relative_to(parent) for parent in parents)


def minimal(paths):
    return sorted(
        name
        for name in set(paths)
        if not any(name != other and Path(name).is_relative_to(other) for other in paths)
    )


class Projection:
    def __init__(self, capture, names):
        self.capture = capture
        self.budget = capture._budget()
        self.names = names
        self.bindings = {}
        # Sandbox symlinks by path: a tool tree that keeps its original path is mounted
        # once, there, and its /tools path is a link to it.
        self.links = {}
        # Owned logical directories of files-mode roots that keep their original paths:
        # each entry is a link to its original path instead of a second mount.
        self.logical = {}
        self.commands = {}
        self.command_path = None
        self.entries = {}
        self.directories = set()
        self.native = any(capture.runtimes[name].native for name in names)
        self.startup = None
        originals = {}
        trees, closed = [], []
        controls = set()
        for name in names:
            graph, runtime = capture.graphs[name], capture.runtimes[name]
            for root in graph.roots.values():
                source = capture.directory / name / root["id"]
                logical = str(Path("/tools") / name / root["id"])
                if not root["preserve_source"]:
                    self.bindings[logical] = source
                    continue
                if root["coverage"] == "tree":
                    trees.append(root["source"])
                    self.links[logical] = root["source"]
                else:
                    self.logical[logical] = (graph, root)
                for row in root["entries"]:
                    self.budget.tick()
                    key = (root["id"], row["path"])
                    filename = graph.original(key)
                    previous = originals.get(filename)
                    value = (graph, key)
                    if previous is not None and previous[0].token(previous[1]) != graph.token(key):
                        layout("selected original tool mappings conflict")
                    originals[filename] = value
            closed.extend(runtime.directories)
            if runtime.control is not None:
                controls.add(digest(graph.token(runtime.control)))
            for command, key in runtime.commands.items():
                # Logical mapping and the whole executable context are significant;
                # equal executable bytes alone cannot make two commands equivalent.
                signature = digest(
                    [graph.logical(key), graph.tool.document, graph.prefix_identity()]
                )
                previous = self.commands.get(command)
                if previous is not None and previous[0] != signature:
                    layout("selected tool command mappings conflict")
                self.commands[command] = (signature, graph.logical(key))
        if len(self.commands) > 1024:
            layout("selected command mapping exceeds its bound")
        if len(controls) > 1:
            layout("selected native loader caches disagree")
        from .tool_ordering import validate_processes

        worker = None
        if self.native:
            from .tool_startup import Startup

            self.startup = Startup(capture.sandbox, originals, self.budget, capture.manifest.root)
            fields = capture.sandbox.description["startup"]
            worker = (capture.sandbox.executable, (fields["stdlib"], fields["platstdlib"]))
            closed.extend(self.startup.roots)
            closed.append("/etc")
        ordering_identity = validate_processes(
            tuple(capture.runtimes[name] for name in names),
            self.budget,
            originals,
            worker,
        )
        for filename, (graph, key) in originals.items():
            self.budget.tick()
            if graph.nodes[key]["kind"] == "symlink" and not inside(filename, trees):
                parent = str(Path(filename).parent)
                if not (self.native and Path(parent).is_relative_to("/etc")):
                    closed.append(parent)
        self.closed = minimal(closed)
        protected = ("/", "/usr", "/etc", "/tmp", "/dev/shm", str(capture.manifest.root))
        for filename in self.closed:
            native_root = self.native and (filename == "/etc" or filename in self.startup.roots)
            if (
                (filename in protected and not native_root)
                or Path(capture.manifest.root).is_relative_to(filename)
                or Path(filename).is_relative_to(capture.manifest.root)
            ):
                layout("closed tool directory overlaps a protected mount or project")
        if self.native:
            from .tool_startup import environment

            environment(capture.sandbox, self.closed)
        for filename, (graph, key) in originals.items():
            self.budget.tick()
            for parent in Path(filename).parents:
                previous = originals.get(str(parent))
                if previous is not None and previous[0].nodes[previous[1]]["kind"] != "directory":
                    layout("selected tool mapping has a nondirectory ancestor")
        if self.native:
            preload = originals.get("/etc/ld.so.preload")
            if preload is not None:
                row = preload[0].nodes[preload[1]]
                if row["kind"] != "file" or row["size"] != 0 or row["mode"] & 0o555 != 0o444:
                    layout("native preload declaration conflicts with its fixed empty control")
            etc = originals.get("/etc")
            if etc is not None and etc[0].nodes[etc[1]]["kind"] != "directory":
                layout("native configuration root must be a directory")
            cache = originals.get("/etc/ld.so.cache")
            if cache is not None and cache[0].nodes[cache[1]]["kind"] != "file":
                layout("native loader cache control must be a direct regular file")
        payload = [
            [
                [
                    name,
                    capture.manifest.tools[name].document,
                    capture.graphs[name].prefix_identity(),
                ]
                for name in names
            ],
            self.closed,
            sorted(self.commands.items()),
            [self.native, [[name, capture.runtimes[name].dynamic] for name in names]],
            ordering_identity,
            None
            if not self.native
            else ["declared-configuration-v1", "/etc/ld.so.preload", "file", 0o444, 0],
            None if self.startup is None else self.startup.identity,
        ]
        self.identity = digest(payload)
        self.directory = capture.directory / ".views" / self.identity
        capture.reserve(self.planned_paths(originals), self.budget)
        self.directory.mkdir(mode=0o700)
        for index, (logical, (graph, root)) in enumerate(sorted(self.logical.items())):
            self.bindings[logical] = self.link_entries(graph, root, index)
        for index, destination in enumerate(self.closed):
            self.budget.tick()
            source = self.directory / ("closed-" + str(index))
            source.mkdir(mode=0o700)
            self.directories.add(source)
            members = [
                (path, value)
                for path, value in originals.items()
                if Path(path).is_relative_to(destination)
            ]
            for filename, (graph, key) in sorted(
                members, key=lambda item: (len(Path(item[0]).parts), item[0])
            ):
                if self.native and filename == "/etc/ld.so.preload":
                    continue
                self.materialize(graph, key, source / Path(filename).relative_to(destination))
            self.bindings[destination] = source
        for name in names:
            graph = capture.graphs[name]
            for root in graph.roots.values():
                if not root["preserve_source"] or inside(root["source"], self.closed):
                    continue
                source = capture.directory / name / root["id"]
                if root["coverage"] == "tree":
                    self.bindings[root["source"]] = source
                else:
                    for row in root["entries"]:
                        destination = graph.original((root["id"], row["path"]))
                        if row["kind"] == "file" and not inside(
                            destination, [*trees, *self.closed]
                        ):
                            self.bindings[destination] = source / row["path"]
        if self.commands:
            directory = self.directory / "commands"
            directory.mkdir(mode=0o700)
            self.directories.add(directory)
            for command, (_, target) in self.commands.items():
                leaf = directory / command
                leaf.symlink_to(target)
                self.entries[leaf] = ("symlink", target)
            self.command_path = "/tools/.commands/" + self.identity
            self.bindings[self.command_path] = directory
        if self.native:
            empty = self.bindings["/etc"] / "ld.so.preload"
            empty.touch(mode=0o444, exist_ok=False)
            empty.chmod(0o444)
            info = empty.stat(follow_symlinks=False)
            self.entries[empty] = ("file", info.st_dev, info.st_ino, 0o444, 0)
        for directory in sorted(self.directories, key=lambda path: len(path.parts), reverse=True):
            row = self.entries.get(directory)
            self.capture.owned_directories.freeze(
                directory, row[1] & 0o555 if row and row[0] == "directory" else 0o555
            )
        self.directory.chmod(0o555)

    def planned_paths(self, originals):
        """Enumerate all new projection paths, including empty ancestors and controls."""
        yield self.directory
        for index, destination in enumerate(self.closed):
            root = self.directory / ("closed-" + str(index))
            yield root
            for filename in originals:
                self.budget.tick()
                if not Path(filename).is_relative_to(destination):
                    continue
                if self.native and filename == "/etc/ld.so.preload":
                    continue
                path = root / Path(filename).relative_to(destination)
                yield path
                while path != root:
                    path = path.parent
                    yield path
            if self.native and destination == "/etc":
                yield root / "ld.so.preload"
        for index, (_, (graph, root)) in enumerate(sorted(self.logical.items())):
            top = self.directory / ("logical-" + str(index))
            yield top
            for row in root["entries"]:
                self.budget.tick()
                path = top / row["path"]
                yield path
                while path != top:
                    path = path.parent
                    yield path
        if self.commands:
            directory = self.directory / "commands"
            yield directory
            for command in self.commands:
                yield directory / command

    def link_entries(self, graph, root, index):
        """The logical directory of a files-mode root that keeps its original paths.

        Directories and declared links are owned copies; each file is a link to its
        original path, where it is mounted once.
        """
        top = self.directory / ("logical-" + str(index))
        rows = sorted(root["entries"], key=lambda row: (len(Path(row["path"]).parts), row["path"]))
        for row in rows:
            self.budget.tick(visit=True)
            destination = top / row["path"]
            missing = []
            parent = destination.parent
            while parent != self.directory and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o700)
                self.directories.add(parent)
            if row["kind"] == "directory":
                destination.mkdir(mode=0o700, exist_ok=True)
                self.directories.add(destination)
                self.entries[destination] = ("directory", row["mode"])
                continue
            target = (
                row["target"]
                if row["kind"] == "symlink"
                else graph.original((root["id"], row["path"]))
            )
            destination.symlink_to(target)
            self.entries[destination] = ("symlink", target)
        if not top.is_dir():
            top.mkdir(mode=0o700)
            self.directories.add(top)
        return top

    def materialize(self, graph, key, destination):
        self.budget.tick(visit=True)
        row = graph.nodes[key]
        source = graph.directory / key[0] / key[1]
        # Owned empty ancestors confer no host siblings. Every actual member comes
        # from a pinned record; hard links are ONLY between already-owned copies.
        missing = []
        parent = destination.parent
        while parent != self.directory and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            parent.mkdir(mode=0o700)
            self.directories.add(parent)
        if row["kind"] == "directory":
            destination.mkdir(mode=0o700, exist_ok=True)
            self.directories.add(destination)
            self.entries[destination] = ("directory", row["mode"])
        elif row["kind"] == "symlink":
            destination.symlink_to(row["target"])
            self.entries[destination] = ("symlink", row["target"])
        else:
            os.link(source, destination, follow_symlinks=False)
            info = source.stat(follow_symlinks=False)
            self.entries[destination] = (
                "file",
                info.st_dev,
                info.st_ino,
                row["mode"] & 0o555,
                row["size"],
            )

    def verify(self):
        budget = self.capture._budget()
        if (
            self.directory.is_symlink()
            or not self.directory.is_dir()
            or self.directory.stat().st_mode & 0o222
        ):
            raise InfrastructureError("owned tool projection root changed")
        expected = set(self.entries) | self.directories
        actual = {
            self.directory / name
            for name in self.capture.owned_directories.names(self.directory, budget)
            if name != "."
        }
        if actual != expected:
            raise InfrastructureError("owned tool projection membership changed")
        for filename, row in self.entries.items():
            budget.tick()
            info = filename.lstat()
            if row[0] == "symlink":
                valid = stat.S_ISLNK(info.st_mode) and os.readlink(filename) == row[1]
            elif row[0] == "directory":
                valid = stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == row[1] & 0o555
            else:
                valid = (
                    stat.S_ISREG(info.st_mode)
                    and (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_size)
                    == row[1:]
                )
            if not valid:
                raise InfrastructureError("owned tool projection mapping changed")
        for directory in self.directories:
            budget.tick()
            if not directory.is_dir() or directory.is_symlink() or directory.stat().st_mode & 0o222:
                raise InfrastructureError("owned tool projection directory changed")
