"""Small explicit frozen-tool installations owned by each behavioral test."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from pytest_telescope import controller
from pytest_telescope.model import DeclarationError, InfrastructureError


def put(root, name, content, mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


def digest(content):
    return hashlib.sha256(content).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def entries(source):
    """Inventory an exclusively owned small fixture, without following links."""
    result = []
    pending = [source]
    while pending:
        path = pending.pop()
        info = path.lstat()
        name = "." if path == source else path.relative_to(source).as_posix()
        if stat.S_ISLNK(info.st_mode):
            row = {"path": name, "kind": "symlink", "target": os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            row = {"path": name, "kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            row = {
                "path": name, "kind": "file", "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size, "sha256": file_digest(path),
            }
        else:
            raise ValueError("fixture inventory accepts only regular files, directories and links")
        result.append(row)
    return sorted(result, key=lambda row: row["path"])


def manifest(root_records=()):
    return {
        "schema": 1, "roots": list(root_records), "executables": [], "python": [],
        "links": [], "locks": [], "commands": [], "loader": None,
    }


@dataclass
class ToolProject:
    root: Path
    external: Path
    definitions: dict = field(default_factory=dict)
    documents: dict = field(default_factory=dict)

    def __post_init__(self):
        self.root.mkdir(parents=True)
        self.external.mkdir(parents=True)
        self.configure()

    def configure(self, extra=""):
        text = '[tool.pytest_telescope]\ntest_roots = ["tests"]\n'
        for name, definition in sorted(self.definitions.items()):
            text += f"\n[tool.pytest_telescope.tools.{name}]\n"
            text += "".join(f"{key} = {json.dumps(value)}\n" for key, value in definition.items())
        put(self.root, "pyproject.toml", text + "\n" + extra)

    def add_tree(self, name, *, preserve=False, files=None):
        source = self.external / name
        source.mkdir(mode=0o755)
        source.chmod(0o755)
        for filename, content in ({"value.txt": name} if files is None else files).items():
            put(source, filename, content)
        document = manifest([{
            "id": "install", "source": str(source), "coverage": "tree",
            "preserve_source": preserve, "entries": entries(source),
        }])
        self.documents[name] = document
        self.definitions[name] = {"format": "frozen-tool-v1", "manifest": f"tool-locks/{name}.json"}
        self.save(name)
        self.configure()
        return source

    def save(self, name):
        put(self.root, self.definitions[name]["manifest"], json.dumps(self.documents[name]))

    def refresh(self, name):
        for root in self.documents[name]["roots"]:
            root["entries"] = entries(Path(root["source"]))
        self.save(name)

    def bind_link(self, name, path="node_modules"):
        source = self.documents[name]["roots"][0]["source"]
        self.documents[name]["roots"][0]["preserve_source"] = True
        (self.root / path).symlink_to(source, target_is_directory=True)
        self.documents[name]["links"] = [{
            "path": path, "target": source, "root": "install", "subpath": ".",
        }]
        self.save(name)

    def pin_lock(self, name, path="package-lock.json", content="fixture-lock\n"):
        put(self.root, path, content)
        roots = self.documents[name]["roots"]
        trees = sorted([root["id"], digest(canonical(root["entries"]))] for root in roots)
        self.documents[name]["locks"] = [{
            "path": path, "sha256": digest((self.root / path).read_bytes()),
            "roots": sorted(root["id"] for root in roots), "tree_sha256": digest(canonical(trees)),
        }]
        self.save(name)

    def module(self, name, source):
        return put(self.root, "tests/" + name, source)

    def run(self, **kwargs):
        try:
            options = {"explain": True, "timeout": 20, **kwargs}
            return controller.run(self.root, **options)
        except (DeclarationError, InfrastructureError) as error:
            return controller.error_result(error)


def assert_counts(report, *, executed=0, reused=0, failed=0):
    assert report["kind"] == "run", report
    assert report["counts"]["executed"] == executed, report
    assert report["counts"]["reused"] == reused, report
    assert report["counts"]["failed"] == failed, report
    assert report["counts"]["infrastructure_error"] == 0, report
    assert report["status"] == ("failed" if failed else "passed"), report


def cache_bytes(root):
    cache = root / ".pytest-telescope" / "cache"
    return {
        path.relative_to(cache).as_posix(): path.read_bytes()
        for path in cache.rglob("*.json")
    } if cache.exists() else {}
