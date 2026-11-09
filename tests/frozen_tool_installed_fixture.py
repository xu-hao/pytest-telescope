"""An owned copy of the normally installed pluggy distribution, without installation."""

import base64
import csv
import hashlib
import io
import json
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

from pytest_telescope import tool_capture

from frozen_tool_fixtures import file_digest, put
from frozen_tool_program_fixture import bounded_rows, copy_declared_tree, prerequisite_identity, require_prerequisites
from frozen_tool_worker_fixture import WorkerConfiguration

INSTALLED_BYTES = 16 * 1024 * 1024


class InstalledConfiguration:
    def __init__(self, directory, monkeypatch, *, require_receipt=True):
        self.worker = WorkerConfiguration(directory, monkeypatch)
        self.project = self.worker.project
        distribution = metadata.distribution("pluggy")
        records = distribution.files
        if records is None:
            raise ValueError("normal installed pluggy RECORD is required")
        metadata_rows = [row for row in records if row.name == "METADATA" and row.parent.name.endswith(".dist-info")]
        if len(metadata_rows) != 1:
            raise ValueError("normal installed pluggy metadata must be unique")
        self.original_package = Path(distribution.locate_file("pluggy"))
        self.original_metadata = Path(distribution.locate_file(metadata_rows[0].parent))
        self.version = distribution.version
        self.package = self.project.external / "installed-copy/pluggy"
        self.metadata = self.package.parent / self.original_metadata.name
        package_rows = copy_declared_tree(self.original_package, self.package, INSTALLED_BYTES)
        metadata_rows = copy_declared_tree(self.original_metadata, self.metadata, INSTALLED_BYTES)
        self.bytecode = [row["path"] for row in package_rows if row["path"].endswith(".pyc")]
        if not self.bytecode:
            raise ValueError("this installed-package control requires ordinary installed bytecode")
        # Keep installation provenance before any deliberately injected mutation.
        self.initial_rows = {"package": package_rows, "metadata": metadata_rows}
        self.roots = (("package", self.original_package, self.package),
                      ("metadata", self.original_metadata, self.metadata))
        original_parent = tool_capture._parent

        @contextmanager
        def parent(filename):
            filename = Path(filename)
            for _, original, owned in self.roots:
                if filename.is_relative_to(original):
                    filename = owned / filename.relative_to(original)
                    break
            with original_parent(filename) as result:
                yield result

        monkeypatch.setattr(tool_capture, "_parent", parent)
        # Tree roots replace the same sparse package members; no stale duplicate
        # source records remain when an owned mutation is deliberately repinned.
        self.worker.paths = {path for path in self.worker.paths if not any(path.is_relative_to(original) for _, original, _ in self.roots)}
        self.worker.save()
        self.document = self.worker.document
        self.document["python"] = [{
            "name": "pluggy", "version": self.version,
            "metadata": {"root": "metadata", "path": "."},
            "packages": [{"root": "package", "path": "."}], "dependencies": [],
            "runtime": [{"root": name, "path": "."} for name in self.worker.anchors],
        }]
        self.save()
        self.identity = prerequisite_identity({"worker": self.document})
        if require_receipt:
            require_prerequisites("installed", self.identity)
        put(self.project.external, "installed-provenance.json", json.dumps({
            "distribution": "pluggy", "version": self.version,
            "normal_installation_record_sha256": file_digest(self.metadata / "RECORD"),
            "bytes": sum(row.get("size", 0) for rows in self.initial_rows.values() for row in rows),
            "bytecode": len(self.bytecode),
            "package_tree_sha256": hashlib.sha256(json.dumps(package_rows, sort_keys=True).encode()).hexdigest(),
            "metadata_tree_sha256": hashlib.sha256(json.dumps(metadata_rows, sort_keys=True).encode()).hexdigest(),
        }, sort_keys=True))

    def save(self):
        self.document["roots"] = [root for root in self.document["roots"] if root["id"] not in {"package", "metadata"}]
        for name, original, owned in self.roots:
            self.document["roots"].append({"id": name, "source": str(original),
                                           "coverage": "tree", "preserve_source": True,
                                           "entries": bounded_rows(owned, INSTALLED_BYTES)})
        self.project.save("worker")

    def update_owned_record(self, relative):
        """Update one owned source mutation's RECORD; preserve the normal self row."""
        record = self.metadata / "RECORD"
        rows = list(csv.reader(io.StringIO(record.read_text())))
        matching = [row for row in rows if row[0] == relative]
        if len(matching) != 1:
            raise ValueError("owned mutation requires an existing installed record")
        path = self.package.parent / relative
        content = path.read_bytes()
        matching[0][1:] = ["sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode(), str(len(content))]
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record.write_text(output.getvalue())
