"""Bounded ELF and glibc-cache decoding from already frozen files.

This module never runs a loader or discovers files on the host. Resolution uses the
explicit view supplied by the controller; a plausible SONAME is not a dependency pin.
"""

import os
import struct
from dataclasses import dataclass

from .tool_context import MAX_RELATIONS, unsupported

MAX_SECTION = 16 * 1024 * 1024
DEFAULT_SEARCH = (
    "/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
    "/lib64", "/usr/lib64", "/lib", "/usr/lib",
)


def refuse():
    unsupported("layout", "unsupported or incomplete frozen native runtime")


def cstring(content, offset):
    if not 0 <= offset < len(content):
        refuse()
    end = content.find(b"\0", offset, offset + 4097)
    if end < 0:
        refuse()
    try:
        value = content[offset:end].decode("utf-8", errors="strict")
    except UnicodeError:
        refuse()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        refuse()
    return value


@dataclass(frozen=True)
class Elf:
    interpreter: str | None
    needed: tuple
    search: tuple
    dynamic: bool


def read_elf(stream, size):
    """Decode only bounded tables, including for a several-hundred-MiB executable."""
    def read(offset, length):
        if offset < 0 or length < 0 or length > MAX_SECTION or offset + length > size:
            refuse()
        stream.seek(offset)
        result = stream.read(length)
        if len(result) != length:
            refuse()
        return result

    header = read(0, min(size, 64))
    if not header.startswith(b"\x7fELF"):
        return None
    if len(header) != 64 or header[4:7] != b"\x02\x01\x01" or header[7] not in (0, 3):
        refuse()
    fields = struct.unpack("<16sHHIQQQIHHHHHH", header)
    if fields[1] not in (2, 3) or fields[2] != 62 or fields[3] != 1 or fields[8] != 64 or fields[9] != 56 or not 0 < fields[10] <= 512:
        refuse()
    segments = list(struct.iter_unpack("<IIQQQQQQ", read(fields[5], fields[9] * fields[10])))
    if any(row[2] + row[5] > size or row[5] > row[6] for row in segments):
        refuse()
    interpreters, dynamic = [], []
    for row in segments:
        if row[0] == 3:
            encoded = read(row[2], row[5])
            if not encoded.endswith(b"\0") or encoded.count(b"\0") != 1:
                refuse()
            value = cstring(encoded, 0)
            if not value.startswith("/") or os.path.normpath(value) != value:
                refuse()
            interpreters.append(value)
        if row[0] == 2:
            if dynamic or row[5] % 16:
                refuse()
            ended = False
            for tag, value in struct.iter_unpack("<qQ", read(row[2], row[5])):
                if tag == 0:
                    ended = True
                    break
                dynamic.append((tag, value))
            if not ended:
                refuse()
    if len(interpreters) > 1 or len(dynamic) > MAX_RELATIONS:
        refuse()
    # Audit/filter/auxiliary objects and altered search/namespace policies need a
    # different native profile. In particular NODEFLIB cannot use our default set.
    if any(tag in (0x6ffffefb, 0x6ffffefc, 0x7ffffffd, 0x7fffffff, 0x6ffffdfd) for tag, _ in dynamic):
        refuse()
    if any(tag == 0x6ffffffb and value & ~(0x1 | 0x8 | 0x80 | 0x08000000) for tag, value in dynamic):
        refuse()
    relations = [(tag, value) for tag, value in dynamic if tag in (1, 15, 29)]
    strings = b""
    if relations:
        addresses = [value for tag, value in dynamic if tag == 5]
        sizes = [value for tag, value in dynamic if tag == 10]
        if len(addresses) != 1 or len(sizes) != 1:
            refuse()
        locations = [row[2] + addresses[0] - row[3] for row in segments if row[0] == 1 and row[3] <= addresses[0] and addresses[0] + sizes[0] <= row[3] + row[5]]
        if len(locations) != 1:
            refuse()
        strings = read(locations[0], sizes[0])
    needed = tuple(cstring(strings, value) for tag, value in relations if tag == 1)
    search = tuple(cstring(strings, value) for tag, value in relations if tag in (15, 29))
    if len(needed) != len(set(needed)) or len(search) > 2:
        refuse()
    return Elf(interpreters[0] if interpreters else None, needed, search, bool(dynamic))


def read_cache(content):
    """The native little-endian glibc cache 1.1 layout, including hwcap candidates."""
    if len(content) > MAX_SECTION or len(content) < 48 or content[:20] != b"glibc-ld.so.cache1.1":
        refuse()
    count, strings_size = struct.unpack_from("<II", content, 20)
    # 0 is the historical native-endian layout, 2 explicitly little endian.
    if content[28] not in (0, 2) or count > 100_000:
        refuse()
    start = 48 + 24 * count
    end = start + strings_size
    if end > len(content):
        refuse()
    result = {}
    for index in range(count):
        flags, key, value, _, hwcap = struct.unpack_from("<iIIIQ", content, 48 + 24 * index)
        if not start <= key < end or not start <= value < end:
            refuse()
        name, filename = cstring(content[:end], key), cstring(content[:end], value)
        if "/" in name or not filename.startswith("/") or os.path.normpath(filename) != filename:
            refuse()
        # Only x86-64 libc6 entries can participate in this profile. Incompatible
        # entries remain absent candidates, not evidence for a matching dependency.
        if flags == 0x303:
            result.setdefault(name, []).append((filename, hwcap))
    return result


def search_paths(info, filename):
    result = []
    for encoded in info.search:
        for item in encoded.split(":"):
            value = item.replace("${ORIGIN}", os.path.dirname(filename)).replace("$ORIGIN", os.path.dirname(filename))
            if not value.startswith("/") or "$" in value or "\\" in value:
                refuse()
            result.append(os.path.normpath(value))
    return tuple(result)
