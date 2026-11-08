"""Private metadata-only composition of the existing baseline /etc grant.

Regular-file contents are never read here. Retained no-follow descriptors keep each
mount source attached to its admitted object even if its pathname is later replaced.
"""

import os
import stat
from pathlib import Path

from .model import InfrastructureError
from .tool_context import canonical, digest, unsupported

MAX_ENTRIES = 4096
MAX_DESCRIPTION = 1024 * 1024
CONTROLS = frozenset({"ld.so.cache", "ld.so.preload"})


def signature(info):
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


class BaselineEtc:
    """One invocation's bounded baseline metadata and controller-owned handles."""

    def __init__(self, budget):
        from .tool_capture import _parent

        self.descriptors = {}
        self.directory = None
        self.owner_pid = os.getpid()
        self.closed = False
        try:
            with _parent(Path("/etc")) as (parent, leaf):
                before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    unsupported("layout", "baseline system configuration has unsupported indirection")
                self.directory = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                if signature(before) != signature(os.fstat(self.directory)):
                    raise InfrastructureError("baseline system configuration changed during admission")
            self.root_signature = signature(before)
            self.records = self._snapshot(budget, admit=True)
            self.identity = digest({"root": self.root_signature, "entries": self.records})
            self.verify(budget)
        except BaseException:
            self.close()
            raise

    def _snapshot(self, budget, *, admit):
        budget.tick(visit=True)
        before = signature(os.fstat(self.directory))
        records = []
        encoded_size = len(canonical({"root": before, "entries": []}))
        scan = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.directory)
        try:
            with os.scandir(scan) as entries:
                for entry in entries:
                    budget.tick(visit=True)
                    if len(records) >= MAX_ENTRIES:
                        unsupported("bounds", "baseline system composition exceeds its entry limit")
                    name = entry.name
                    observed = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                    record = {"name": name, "destination": "/etc/" + name, "stat": signature(observed)}
                    if stat.S_ISREG(observed.st_mode):
                        record["kind"] = "file"
                    elif stat.S_ISDIR(observed.st_mode):
                        record["kind"] = "directory"
                    elif stat.S_ISLNK(observed.st_mode):
                        record["kind"] = "symlink"
                        record["target"] = os.readlink(name, dir_fd=self.directory)
                    else:
                        unsupported("layout", "baseline system composition contains an unsupported entry type")
                    if name in CONTROLS:
                        if record["kind"] != "file":
                            unsupported("layout", "native loader control path has unsupported indirection")
                    elif record["kind"] != "symlink":
                        if admit:
                            descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=self.directory)
                            self.descriptors[name] = descriptor
                        else:
                            descriptor = self.descriptors.get(name)
                            if descriptor is None:
                                raise InfrastructureError("baseline system composition membership changed")
                        if signature(os.fstat(descriptor)) != record["stat"]:
                            raise InfrastructureError("held system entry disagrees with admitted metadata")
                    if signature(os.stat(name, dir_fd=self.directory, follow_symlinks=False)) != record["stat"]:
                        raise InfrastructureError("baseline system entry changed during enumeration")
                    if record["kind"] == "symlink" and os.readlink(name, dir_fd=self.directory) != record["target"]:
                        raise InfrastructureError("baseline system link changed during enumeration")
                    encoded_size += len(canonical(record)) + 1
                    if encoded_size > MAX_DESCRIPTION:
                        unsupported("bounds", "baseline system composition exceeds its metadata limit")
                    records.append(record)
        finally:
            os.close(scan)
        if signature(os.fstat(self.directory)) != before:
            raise InfrastructureError("baseline system directory changed during enumeration")
        return sorted(records, key=lambda row: row["name"])

    def verify(self, budget):
        from .tool_capture import _parent
        from .model import DeclarationError

        if self.closed or os.getpid() != self.owner_pid:
            raise InfrastructureError("baseline system handles do not belong to this controller")
        try:
            with _parent(Path("/etc")) as (parent, leaf):
                current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if signature(current) != self.root_signature or signature(os.fstat(self.directory)) != self.root_signature:
                    raise InfrastructureError("baseline system directory changed after admission")
            if self._snapshot(budget, admit=False) != self.records:
                raise InfrastructureError("baseline system composition changed after admission")
        except (OSError, DeclarationError) as exc:
            raise InfrastructureError("cannot revalidate baseline system composition") from exc

    def arguments(self):
        """Descriptors remain in the controller; bwrap gets no inherited descriptor.

        Bwrap resolves sources in its original host root before mount setup. A
        controller /proc/PID/fd path addresses the held object, never its mutable
        /etc pathname. The invoking UID already owns these open handles. Namespace
        or proc-handle access failures refuse; there is no pathname fallback. The
        native startup guard separately checks the new PID/proc view and closes
        setup descriptors before executing the ordinary worker interpreter.
        """
        if self.closed or self.owner_pid != os.getpid():
            raise InfrastructureError("baseline system handles are unavailable")
        # The private root is remounted readonly after every authoritative overlay.
        arguments = ["--perms", format(self.root_signature[2] & 0o777, "o"), "--dir", "/etc"]
        for record in self.records:
            name = record["name"]
            if name in CONTROLS:
                continue
            if record["kind"] == "symlink":
                arguments.extend(("--symlink", record["target"], record["destination"]))
            else:
                descriptor = self.descriptors[name]
                if os.get_inheritable(descriptor):
                    raise InfrastructureError("baseline system handle unexpectedly became inheritable")
                arguments.extend(("--ro-bind", f"/proc/{self.owner_pid}/fd/{descriptor}", record["destination"]))
        return arguments

    def close(self):
        from .sandbox import CleanupError

        if self.closed:
            return
        self.closed = True
        failure = False
        owned = list(self.descriptors.values())
        self.descriptors.clear()
        if self.directory is not None:
            owned.append(self.directory)
            self.directory = None
        for descriptor in owned:
            try:
                os.close(descriptor)
            except OSError:
                failure = True
        if failure:
            raise CleanupError("owned system configuration handle cleanup failed")
