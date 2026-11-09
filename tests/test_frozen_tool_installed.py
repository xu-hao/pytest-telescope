"""Real installed plugin bytes/metadata and ordinary bytecode in isolated children."""

import json
import stat

import pytest

from frozen_tool_fixtures import assert_counts, cache_bytes, file_digest, put
from frozen_tool_installed_fixture import InstalledConfiguration
from frozen_tool_program_fixture import capture_evidence


@pytest.fixture(autouse=True)
def evidence(tmp_path, monkeypatch):
    with capture_evidence(monkeypatch, tmp_path):
        yield


@pytest.fixture
def installed(tmp_path, monkeypatch):
    return InstalledConfiguration(tmp_path, monkeypatch)


def installed_case(installed):
    bytecode = {name: file_digest(installed.package / name) for name in installed.bytecode}
    source = installed.package / "__init__.py"
    child = f"""import csv, hashlib, io, json, stat
from importlib import metadata, resources
from pathlib import Path
import pluggy
spec = pluggy.HookspecMarker('owned')
impl = pluggy.HookimplMarker('owned')
class Specification:
    @spec
    def answer(self, value): pass
class Plugin:
    @impl
    def answer(self, value): return value + 7
manager = pluggy.PluginManager('owned')
manager.add_hookspecs(Specification)
manager.register(Plugin())
assert manager.hook.answer(value=5) == [12]
distribution = metadata.distribution('pluggy')
assert distribution.version == {installed.version!r}
assert str(Path(pluggy.__file__)) == {str(installed.original_package / '__init__.py')!r}
assert resources.files('pluggy').joinpath('py.typed').read_bytes() == {(installed.package / 'py.typed').read_bytes()!r}
rows = list(csv.reader(io.StringIO(distribution.read_text('RECORD'))))
assert next(row for row in rows if row[0] == {installed.original_metadata.name + '/RECORD'!r})[1:] == ['', '']
assert (Path(pluggy.__file__).stat().st_dev, Path(pluggy.__file__).stat().st_ino) != {(source.stat().st_dev, source.stat().st_ino)!r}
assert stat.S_IMODE(Path(pluggy.__file__).stat().st_mode) == {stat.S_IMODE(source.stat().st_mode) & 0o555}
for name, digest in {bytecode!r}.items():
    assert hashlib.sha256((Path(pluggy.__file__).parent / name).read_bytes()).hexdigest() == digest
try:
    metadata.distribution('owned-unselected-plugin')
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('unselected metadata became visible')
print(json.dumps({{'answer': 12, 'version': distribution.version, 'bytecode': len({bytecode!r})}}))
"""
    installed.project.module("test_installed.py", f"""import json
import subprocess
import sys
import pytest
pytestmark = pytest.mark.telescope(tools=['worker'])
def test_installed():
    result = subprocess.run([sys.executable, '-I', '-c', {child!r}], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {{'answer': 12, 'version': {installed.version!r}, 'bytecode': {len(bytecode)}}}
""")


def test_normally_installed_plugin_metadata_resources_and_bytecode_in_child(installed):
    installed_case(installed)
    # An owned, valid-looking sibling is deliberately outside both selected trees.
    put(installed.package.parent, "owned_unselected_plugin-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: owned-unselected-plugin\nVersion: 1.0.0\n")
    assert_counts(installed.project.run(timeout=90), executed=1)
    assert_counts(installed.project.run(timeout=90), reused=1)


@pytest.mark.parametrize("member", ["module", "record", "bytecode"])
def test_changed_installed_bytes_refuse_stale_pins(installed, member):
    installed_case(installed)
    assert_counts(installed.project.run(timeout=90), executed=1)
    previous = cache_bytes(installed.project.root)
    selected = {"module": installed.package / "__init__.py", "record": installed.metadata / "RECORD",
                "bytecode": installed.package / installed.bytecode[0]}[member]
    selected.write_bytes(selected.read_bytes() + b"\n")
    report = installed.project.run(timeout=90)
    assert report["status"] == "unsupported" and report["capability"] == "tool:identity", report
    assert cache_bytes(installed.project.root) == previous
    assert str(installed.package) not in json.dumps(report)


@pytest.mark.parametrize("fault", ["missing_resource", "missing_metadata", "version_conflict", "record_hash_conflict"])
def test_installed_metadata_inconsistency_refuses_before_project_import(installed, fault):
    if fault == "missing_resource":
        (installed.package / "py.typed").unlink()
    elif fault == "missing_metadata":
        (installed.metadata / "METADATA").unlink()
    elif fault == "version_conflict":
        installed.document["python"][0]["version"] = "0.0.0"
    else:
        # Repin the changed bytes, retaining the old actual RECORD hash.
        source = installed.package / "_hooks.py"
        source.write_bytes(source.read_bytes() + b"\n# owned mismatch\n")
    installed.save()
    installed.project.module("test_refusal.py", "import pytest\npytestmark = pytest.mark.telescope(tools=['worker'])\nraise RuntimeError('inconsistent installed package reached import')\ndef test_refused(): pass\n")
    report = installed.project.run(timeout=90)
    assert report["status"] in {"unsupported", "declaration_error"}, report
    assert not cache_bytes(installed.project.root)


def test_updated_installed_resource_invalidates_consumer_only(installed):
    installed.project.module("test_resource.py", """from importlib import resources
import pytest
@pytest.mark.telescope(tools=['worker'])
def test_resource(): assert resources.files('pluggy').joinpath('py.typed').read_bytes() in (b'', b'# owned update\\n')
@pytest.mark.telescope(tools=[])
def test_unrelated(): pass
""")
    assert_counts(installed.project.run(timeout=90), executed=2)
    assert_counts(installed.project.run(timeout=90), reused=2)
    (installed.package / "py.typed").write_bytes(b"# owned update\n")
    installed.update_owned_record("pluggy/py.typed")
    installed.save()
    assert_counts(installed.project.run(timeout=90), executed=1, reused=1)
