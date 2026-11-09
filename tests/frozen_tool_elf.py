"""Independent bounded ELF reads for the caller-owned native fixture declarations.

No executable or dependency-discovery helper is run. This reader supports only the
Linux x86_64 fixture profile and refuses unfamiliar input instead of guessing.
"""

import os
import struct
from pathlib import Path

MAX_STRING_TABLE_BYTES = 16 * 1024 * 1024


def elf(path):
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(64)
        if not header.startswith(b"\x7fELF"):
            return None
        if len(header) != 64 or header[4:7] != b"\x02\x01\x01":
            raise ValueError("fixture requires ELF64 little-endian input")
        fields = struct.unpack("<16sHHIQQQIHHHHHH", header)
        if fields[2] != 62 or fields[9] != 56 or not 0 < fields[10] <= 512:
            raise ValueError("fixture ELF machine or program table is unsupported")
        stream.seek(fields[5])
        program_bytes = stream.read(fields[9] * fields[10])
        if len(program_bytes) != fields[9] * fields[10]:
            raise ValueError("fixture ELF program table is incomplete")
        segments = list(struct.iter_unpack("<IIQQQQQQ", program_bytes))
        interpreter = None
        dynamic = []
        for segment in segments:
            kind, _, offset, _, _, size, _, _ = segment
            if kind not in (2, 3):
                continue
            if size > 1024 * 1024:
                raise ValueError("fixture ELF relation section exceeds its bound")
            stream.seek(offset)
            content = stream.read(size)
            if len(content) != size:
                raise ValueError("fixture ELF relation section is incomplete")
            if kind == 3:
                if not content.endswith(b"\0") or b"\0" in content[:-1]:
                    raise ValueError("fixture ELF interpreter is malformed")
                interpreter = content[:-1].decode("utf-8", errors="strict")
            else:
                if size % 16:
                    raise ValueError("fixture ELF dynamic section is malformed")
                for tag, value in struct.iter_unpack("<qQ", content):
                    if tag == 0:
                        break
                    dynamic.append((tag, value))
        address = [value for tag, value in dynamic if tag == 5]
        lengths = [value for tag, value in dynamic if tag == 10]
        names = [value for tag, value in dynamic if tag in (1, 15, 29)]
        strings = b""
        if names:
            # Existing Node dependencies can have several MiB of dynamic strings.
            # Match the producer's finite metadata ceiling, retaining exact mapping,
            # complete-read, offset, termination and per-string checks below.
            if len(address) != 1 or len(lengths) != 1 or lengths[0] > MAX_STRING_TABLE_BYTES:
                raise ValueError("fixture ELF string table is unsupported")
            mappings = [
                segment[2] + address[0] - segment[3]
                for segment in segments
                if segment[0] == 1 and segment[3] <= address[0]
                and address[0] + lengths[0] <= segment[3] + segment[5]
            ]
            if len(mappings) != 1:
                raise ValueError("fixture ELF string table is not uniquely mapped")
            stream.seek(mappings[0])
            strings = stream.read(lengths[0])
            if len(strings) != lengths[0]:
                raise ValueError("fixture ELF string table is incomplete")

        def string(offset):
            if offset >= len(strings):
                raise ValueError("fixture ELF string offset is invalid")
            end = strings.find(b"\0", offset)
            if end < 0 or end - offset > 4096:
                raise ValueError("fixture ELF string is invalid or oversized")
            return strings[offset:end].decode("utf-8", errors="strict")

        return {
            "interpreter": interpreter,
            "needed": [string(value) for tag, value in dynamic if tag == 1],
            "search": [string(value) for tag, value in dynamic if tag in (15, 29)],
        }


SYSTEM_SEARCH = (
    Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64"), Path("/usr/lib"),
    Path("/usr/local/lib"), Path("/usr/local/lib64"),
)


def native_relations(entrypoints, extra_search=(), library_pins=None):
    """Resolve only the fixture's declared native graph in explicit known directories.

    The product separately verifies the actual pinned loader/cache semantics. Multiple
    distinct candidates here refuse fixture construction; this is not a general host
    linker resolver and never certifies an arbitrary application closure.
    """
    pending = [Path(path) for path in entrypoints]
    library_pins = {} if library_pins is None else library_pins
    information = {}
    dependencies = {}
    search_roots = set()
    paths = set()
    while pending:
        requested = pending.pop()
        paths.add(requested)
        actual = requested.resolve(strict=True)
        if actual in information:
            continue
        if len(information) >= 512:
            raise ValueError("fixture native graph exceeds 512 files")
        info = elf(actual)
        if info is None:
            raise ValueError("fixture native entry is not ELF")
        information[actual] = info
        paths.add(actual)
        directories = []
        for encoded in info["search"]:
            for item in encoded.split(":"):
                item = item.replace("${ORIGIN}", str(actual.parent)).replace("$ORIGIN", str(actual.parent))
                if not item or "$" in item or not Path(item).is_absolute():
                    raise ValueError("fixture native search syntax is unsupported")
                directory = Path(item).resolve(strict=False)
                directories.append(directory)
                search_roots.add(directory)
        directories.extend(Path(path) for path in extra_search)
        directories.extend(SYSTEM_SEARCH)
        chosen = []
        for name in info["needed"]:
            if "/" in name:
                candidates = [Path(name)] if Path(name).is_absolute() else []
            else:
                candidates = [directory / name for directory in directories]
            existing = [candidate for candidate in candidates if candidate.is_file()]
            resolved = {candidate.resolve(strict=True) for candidate in existing}
            if name in library_pins:
                explicit = Path(library_pins[name]).resolve(strict=True)
                if explicit not in resolved:
                    raise ValueError("fixture's explicit library is outside declared search")
                resolved = {explicit}
            if len(resolved) != 1:
                raise ValueError("fixture native dependency is missing or ambiguous")
            target = next(iter(resolved))
            # Keep each selected lexical alias so its actual link text is also pinned.
            paths.update(candidate for candidate in existing if candidate.resolve() == target)
            chosen.append(target)
            pending.append(target)
        dependencies[actual] = sorted(set(chosen))
        if info["interpreter"] is not None:
            requested_loader = Path(info["interpreter"])
            if not requested_loader.is_absolute():
                raise ValueError("fixture loader path is not absolute")
            paths.add(requested_loader)
            pending.append(requested_loader)
    return information, dependencies, paths, search_roots


def bounded_tree_size(root, maximum=768 * 1024 * 1024):
    """Measure an explicitly named installed tree before any owned fixture copy."""
    total = 0
    visits = 0
    pending = [Path(root)]
    while pending:
        path = pending.pop()
        visits += 1
        if visits > 100_000:
            raise ValueError("fixture source tree exceeds its entry bound")
        info = path.lstat()
        if path.is_symlink():
            continue
        if path.is_dir():
            with os.scandir(path) as children:
                for entry in children:
                    pending.append(Path(entry.path))
                    if len(pending) + visits > 100_000:
                        raise ValueError("fixture source tree exceeds its traversal bound")
        elif path.is_file():
            total += info.st_size
            if info.st_size > 512 * 1024 * 1024 or total > maximum:
                raise ValueError(f"fixture source bytes exceed reviewed ceiling: {total}")
        else:
            raise ValueError("fixture source contains a special file")
    return total
