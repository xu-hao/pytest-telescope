"""Runtime inspection refuses changed/nonregular inputs within explicit I/O bounds."""

import builtins
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

from pytest_telescope import capsule
from pytest_telescope.model import InfrastructureError, UnsupportedCapability


def minimal_elf(interpreter=None):
    identity = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = struct.pack(
        "<HHIQQQIHHHHHH", 2, 62, 1, 0, 64, 0, 0, 64, 56, int(interpreter is not None), 0, 0, 0
    )
    if interpreter is None:
        return identity + header
    value = os.fsencode(interpreter) + b"\0"
    segment = struct.pack("<IIQQQQQQ", 3, 4, 120, 0, 0, len(value), len(value), 1)
    return identity + header + segment + value


class CoordinatedRead:
    """Observe real file I/O while changing one owned source at an I/O boundary."""

    def __init__(self, target, mutation, *, when):
        self.target = target
        self.mutation = mutation
        self.when = when
        value = target.stat()
        self.identity = value.st_dev, value.st_ino
        self.triggered = False
        self.read_bytes = 0
        self.depth = 0

    def owns(self, fd):
        try:
            value = os.fstat(fd)
        except (OSError, ValueError):
            return False
        return (value.st_dev, value.st_ino) == self.identity

    def fire(self):
        if not self.triggered:
            self.triggered = True
            self.mutation()

    def read(self, original, fd, *args):
        outer = self.depth == 0
        self.depth += 1
        try:
            data = original(*args)
        finally:
            self.depth -= 1
        if outer and self.owns(fd):
            self.read_bytes += len(data)
            if self.when == "read" and data:
                self.fire()
        return data

    def install(self, monkeypatch):
        owner = self

        class Stream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, *args):
                return owner.read(self.stream.read, self.stream.fileno(), *args)

        def opening(original, *, descriptor=False):
            def wrapped(path, *args, **kwargs):
                if (
                    self.when == "before-open"
                    and isinstance(path, str | bytes | os.PathLike)
                    and Path(os.fsdecode(path)).name == self.target.name
                ):
                    self.fire()
                result = original(path, *args, **kwargs)
                fd = result if descriptor else result.fileno()
                if self.owns(fd):
                    if self.when == "open":
                        self.fire()
                    return result if descriptor else Stream(result)
                return result

            return wrapped

        original_read = os.read
        monkeypatch.setattr(os, "open", opening(os.open, descriptor=True))
        monkeypatch.setattr(io, "open", opening(io.open))
        monkeypatch.setattr(builtins, "open", opening(builtins.open))
        monkeypatch.setattr(
            os, "read", lambda fd, size: self.read(lambda: original_read(fd, size), fd)
        )


def probe(root, scenario):
    """Executed only in a deadline-bounded child; no native payload is executed."""
    root = Path(root)
    target = root / "sample"
    target.write_bytes(minimal_elf())
    observed = {"scenario": scenario}
    tracker = None
    writer = None
    monkeypatch = pytest.MonkeyPatch()
    visited = [0]
    try:
        if scenario in ("elf-fifo", "elf-directory"):
            target.unlink()
            if scenario == "elf-fifo":
                os.mkfifo(target)
            else:
                target.mkdir()
            operation = partial(capsule.elf_info, target)
        elif scenario == "elf-growth":
            monkeypatch.setattr(capsule, "MAX_NATIVE", 128)
            writer = os.open(target, os.O_RDWR)
            tracker = CoordinatedRead(target, lambda: os.ftruncate(writer, 512), when="open")
            tracker.install(monkeypatch)
            operation = partial(capsule.elf_info, target)
        elif scenario == "closure-replacement":
            loader = root / "loader"
            loader.write_bytes(minimal_elf())
            replacement = root / "replacement"
            changed = minimal_elf(str(loader))
            replacement.write_bytes(changed)
            observed.update(
                source=str(target),
                loader=str(loader),
                original_sha256=hashlib.sha256(minimal_elf()).hexdigest(),
                replacement_sha256=hashlib.sha256(changed).hexdigest(),
            )
            tracker = CoordinatedRead(target, lambda: os.replace(replacement, target), when="read")
            tracker.install(monkeypatch)
            operation = partial(capsule.elf_closure, [target])
        else:
            payload = capsule.Payload(root / "runtime", max_bytes=128, max_entries=4)
            payload.write("/sample", minimal_elf())
            target = payload.root / "sample"
            if scenario == "verify-growth":
                target.chmod(0o644)
                writer = os.open(target, os.O_RDWR)
                target.chmod(0o444)
                tracker = CoordinatedRead(target, lambda: os.ftruncate(writer, 512), when="open")
                tracker.install(monkeypatch)
            elif scenario == "verify-fifo-replacement":

                def replace_with_fifo():
                    target.unlink()
                    os.mkfifo(target)

                tracker = CoordinatedRead(target, replace_with_fifo, when="before-open")
                tracker.install(monkeypatch)
            elif scenario == "verify-entry-growth":
                for number in range(64):
                    (payload.root / f"extra-{number}").touch()
                original_scandir, original_listdir = os.scandir, os.listdir

                class Entries:
                    def __init__(self, path):
                        self.iterator = original_scandir(path)

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        self.iterator.close()

                    def __iter__(self):
                        return self

                    def __next__(self):
                        entry = next(self.iterator)
                        visited[0] += 1
                        return entry

                    def close(self):
                        self.iterator.close()

                def listdir(path):
                    entries = original_listdir(path)
                    visited[0] += len(entries)
                    return entries

                monkeypatch.setattr(os, "scandir", Entries)
                monkeypatch.setattr(os, "listdir", listdir)
            else:
                raise AssertionError("unknown owned probe scenario")
            operation = payload.verify
        try:
            observed["value"] = operation()
            observed["outcome"] = "accepted"
        except UnsupportedCapability as error:
            observed.update(outcome="unsupported", capability=error.capability)
        except InfrastructureError:
            observed["outcome"] = "infrastructure_error"
        except Exception as error:
            observed.update(outcome="unexpected_exception", exception_type=type(error).__name__)
    finally:
        monkeypatch.undo()
        if writer is not None:
            os.close(writer)
        if tracker is not None:
            observed.update(mutated=tracker.triggered, bytes_read=tracker.read_bytes)
        observed["entries_visited"] = visited[0]
    print(json.dumps(observed), flush=True)


def run_probe(root, scenario):
    entry = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from test_runtime_capsule_integrity import probe;probe(sys.argv[2],sys.argv[3])"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", entry, str(Path(__file__).parent), str(root), scenario],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its exact direct child before raising.
        pytest.fail(f"{scenario} blocked instead of refusing within the owned process deadline")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_elf_nonregular_input_refuses_without_blocking(tmp_path, kind):
    result = run_probe(tmp_path, "elf-" + kind)
    assert result["outcome"] == "unsupported", result
    assert result["capability"] == "runtime:layout"


def test_elf_growth_after_open_is_bounded_and_refused(tmp_path):
    result = run_probe(tmp_path, "elf-growth")
    assert result["mutated"] is True
    assert result["outcome"] in ("unsupported", "infrastructure_error"), result
    assert result["bytes_read"] <= 129, result


def test_elf_closure_parses_and_hashes_one_coherent_snapshot(tmp_path):
    result = run_probe(tmp_path, "closure-replacement")
    assert result["mutated"] is True
    if result["outcome"] == "infrastructure_error":
        return
    assert result["outcome"] == "accepted", result
    closure = result["value"]
    recorded = closure[result["source"]]["sha256"]
    if recorded == result["original_sha256"]:
        assert set(closure) == {result["source"]}, result
    else:
        assert recorded == result["replacement_sha256"], result
        assert set(closure) == {result["source"], result["loader"]}, result
        assert closure[result["loader"]]["sha256"] == result["original_sha256"]


def test_payload_verification_does_not_read_past_storage_bound_after_growth(tmp_path):
    result = run_probe(tmp_path, "verify-growth")
    assert result["mutated"] is True
    assert result["outcome"] == "infrastructure_error", result
    assert result["bytes_read"] <= 129, result


def test_payload_verification_bounds_altered_tree_enumeration(tmp_path):
    result = run_probe(tmp_path, "verify-entry-growth")
    assert result["outcome"] == "infrastructure_error", result
    assert result["entries_visited"] <= 5, result


def test_payload_verification_refuses_file_replaced_by_fifo(tmp_path):
    result = run_probe(tmp_path, "verify-fifo-replacement")
    assert result["mutated"] is True
    assert result["outcome"] == "infrastructure_error", result
